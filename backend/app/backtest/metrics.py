"""绩效指标 — 全部基于真实账户权益与真实成交记录，不用任何近似替代。

口径说明：
  · 收益率 = 期末净值 / 期初净值 − 1
  · 年化   = (1+总收益)^(365.25/自然日跨度) − 1（跨度不足 30 天时不年化，返回 None）
  · 夏普   = 日收益均值 / 日收益标准差 × √252（无风险利率按 0）
  · 索提诺 = 日收益均值 / 下行标准差 × √252
  · 卡玛   = 年化收益 / |最大回撤|
  · 胜率/盈亏比/连续亏损：按**卖出成交**统计，每笔卖出即一次完整交易
"""
from __future__ import annotations

import math
from datetime import date

from .models import BacktestConfig, Fill, Side, Snapshot


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def max_drawdown(nav: list[float]) -> tuple[float, int, int]:
    """返回 (最大回撤(负数), 回撤开始索引, 谷底索引)。"""
    peak = nav[0] if nav else 1.0
    peak_idx = 0
    best_dd = 0.0
    best_start = best_end = 0
    for i, v in enumerate(nav):
        if v > peak:
            peak, peak_idx = v, i
        dd = v / peak - 1
        if dd < best_dd:
            best_dd, best_start, best_end = dd, peak_idx, i
    return best_dd, best_start, best_end


def monthly_returns(snapshots: list[Snapshot]) -> dict[str, float]:
    if not snapshots:
        return {}
    out: dict[str, float] = {}
    first_by_month: dict[str, float] = {}
    last_by_month: dict[str, float] = {}
    for s in snapshots:
        key = f"{s.date.year}-{s.date.month:02d}"
        first_by_month.setdefault(key, s.nav)
        last_by_month[key] = s.nav
    prev = 1.0
    for key in sorted(first_by_month):
        out[key] = round(last_by_month[key] / prev - 1, 6)
        prev = last_by_month[key]
    return out


def compute(
    snapshots: list[Snapshot],
    fills: list[Fill],
    config: BacktestConfig,
    benchmark_nav: list[float] | None = None,
) -> dict:
    if len(snapshots) < 2:
        return {"error": "样本不足（少于 2 个交易日）"}

    nav = [s.nav for s in snapshots]
    dates = [s.date for s in snapshots]
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]

    total_ret = nav[-1] - 1.0
    days = (dates[-1] - dates[0]).days
    annual = None
    if days >= 30:
        annual = (1 + total_ret) ** (365.25 / days) - 1

    mdd, dd_start, dd_end = max_drawdown(nav)
    dd_days = (dates[dd_end] - dates[dd_start]).days if dd_end > dd_start else 0

    std = _std(rets)
    sharpe = (_mean(rets) / std * math.sqrt(252)) if std > 0 else None
    downside = [r for r in rets if r < 0]
    dstd = _std(downside)
    sortino = (_mean(rets) / dstd * math.sqrt(252)) if dstd > 0 else None
    calmar = (annual / abs(mdd)) if (annual is not None and mdd < 0) else None
    vol = std * math.sqrt(252)

    sells = [f for f in fills if f.side is Side.SELL]
    wins = [f for f in sells if f.realized_pnl > 0]
    losses = [f for f in sells if f.realized_pnl <= 0]
    avg_win = _mean([f.realized_pnl for f in wins])
    avg_loss = _mean([f.realized_pnl for f in losses])
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else None

    max_win = max((f.realized_pnl for f in sells), default=0.0)
    max_loss = min((f.realized_pnl for f in sells), default=0.0)

    # 最大连续亏损
    streak = worst_streak = 0
    for f in sells:
        if f.realized_pnl <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    # 平均持仓时间（按 signal_dt 到卖出 dt 的自然日）
    hold_days = [
        (f.dt.date() - f.signal_dt.date()).days for f in sells if f.signal_dt is not None
    ]

    excess = None
    if benchmark_nav and len(benchmark_nav) == len(nav):
        excess = total_ret - (benchmark_nav[-1] - 1)

    return {
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "days": days,
        "init_cash": config.init_cash,
        "final_equity": round(snapshots[-1].equity, 2),
        "total_return": round(total_ret, 6),
        "annual_return": round(annual, 6) if annual is not None else None,
        "excess_return": round(excess, 6) if excess is not None else None,
        "max_drawdown": round(mdd, 6),
        "max_drawdown_days": dd_days,
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "sortino": round(sortino, 4) if sortino is not None else None,
        "calmar": round(calmar, 4) if calmar is not None else None,
        "volatility": round(vol, 6),
        "trades": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(sells), 4) if sells else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff": round(payoff, 4) if payoff is not None else None,
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "max_consecutive_losses": worst_streak,
        "avg_hold_days": round(_mean(hold_days), 1) if hold_days else None,
        "total_fees": round(snapshots[-1].total_fees, 2),
        "realized_pnl": round(snapshots[-1].realized_pnl, 2),
        "unrealized_pnl": round(snapshots[-1].unrealized_pnl, 2),
        "monthly": monthly_returns(snapshots),
    }
