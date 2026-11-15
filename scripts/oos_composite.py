"""合成 IC6 的样本外验证 + 回撤压力测试。

要回答两个问题：
  1. "+563.8%" 是不是样本内挑出来的？（网格调 min_votes/min_score，训练/测试分割）
  2. "−45% 回撤" 实盘扛得住吗？（最长水下期、恢复时间、逐年表现）

用法：
    python scripts/oos_composite.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "app" / "strategies"))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig, FeeConfig  # noqa: E402
import composite_ic6 as M  # noqa: E402

IN_S, IN_E = date(2015, 1, 5), date(2022, 12, 30)
OUT_S, OUT_E = date(2023, 1, 3), date(2026, 11, 7)
ALL_S, ALL_E = IN_S, OUT_E

CFG = dict(init_cash=100_000, risk_pct=0.03, max_positions=10, max_position_pct=0.25,
           stop_loss_pct=0.08, max_hold_days=20, fees=FeeConfig())

GRID = [(mv, ms) for mv in (1, 2) for ms in (0.0, 0.10, 0.15, 0.20, 0.30)]


def run(bars, frame, mv, ms, s, e):
    hits = M.filter_frame(frame, {"min_votes": mv, "min_score": ms})
    r = BacktestEngine(BacktestConfig(**CFG), bars).run(hits, start=s, end=e)
    return r, hits.height


def underwater(snapshots) -> dict:
    """最长水下天数 + 最大回撤恢复天数。"""
    nav = [s.nav for s in snapshots]
    dates = [s.date for s in snapshots]
    peak, peak_i = nav[0], 0
    longest = 0
    longest_span = (dates[0], dates[0])
    recover_days = 0
    max_recover = 0
    cur_start = dates[0]
    for i, v in enumerate(nav):
        if v >= peak:
            if peak_i > 0:
                d = (dates[i] - cur_start).days
                max_recover = max(max_recover, d)
            peak, peak_i, cur_start = v, i, dates[i]
        else:
            d = (dates[i] - cur_start).days
            if d > longest:
                longest = d
                longest_span = (cur_start, dates[i])
    return {
        "longest_underwater_days": longest,
        "longest_span": (str(longest_span[0]), str(longest_span[1])),
        "max_recovery_days": max_recover,
    }


def main() -> None:
    raw = GenerationStore(config.DATA_DIR).collect()
    frame = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )
    bars = BarStore(frame)

    print("=== 样本内（2015-2022）网格 ===")
    print(f"{'min_votes':>10}{'min_score':>10}{'信号':>8}{'收益':>10}{'夏普':>7}{'回撤':>9}{'交易':>6}")
    print("-" * 62)
    rows = []
    for mv, ms in GRID:
        r, n = run(bars, frame, mv, ms, IN_S, IN_E)
        m = r.metrics
        rows.append({"mv": mv, "ms": ms, "n": n, "in": m})
        print(f"{mv:>10}{ms:>10.2f}{n:>8}{m['total_return']*100:>9.1f}%"
              f"{(m['sharpe'] or 0):>7.2f}{m['max_drawdown']*100:>8.1f}%{m['trades']:>6}", flush=True)

    ok = [x for x in rows if x["in"].get("sharpe") is not None]
    best = max(ok, key=lambda x: x["in"]["sharpe"])
    print(f"\n样本内最优: min_votes={best['mv']} min_score={best['ms']} "
          f"夏普 {best['in']['sharpe']:.2f}")

    print(f"\n=== 该参数在样本外（2023-2026）的表现 ===")
    r_out, n_out = run(bars, frame, best["mv"], best["ms"], OUT_S, OUT_E)
    mo = r_out.metrics
    print(f"  信号 {n_out}  收益 {mo['total_return']*100:+.1f}%  年化 {(mo['annual_return'] or 0)*100:+.1f}%  "
          f"回撤 {mo['max_drawdown']*100:.1f}%  夏普 {(mo['sharpe'] or 0):.2f}  交易 {mo['trades']}")
    decay = (mo["sharpe"] or 0) / best["in"]["sharpe"] - 1
    print(f"  夏普衰减 {decay*100:+.0f}%")

    print(f"\n=== 默认参数（min_votes=2, min_score=0）的样本外 ===")
    r_d, n_d = run(bars, frame, 2, 0.0, OUT_S, OUT_E)
    md = r_d.metrics
    print(f"  信号 {n_d}  收益 {md['total_return']*100:+.1f}%  夏普 {(md['sharpe'] or 0):.2f}  "
          f"回撤 {md['max_drawdown']*100:.1f}%")

    print(f"\n=== 最优参数全区间（2015-2026）回撤压力 ===")
    r_all, n_all = run(bars, frame, best["mv"], best["ms"], ALL_S, ALL_E)
    ma = r_all.metrics
    uw = underwater(r_all.snapshots)
    print(f"  收益 {ma['total_return']*100:+.1f}%  年化 {(ma['annual_return'] or 0)*100:+.1f}%  "
          f"回撤 {ma['max_drawdown']*100:.1f}%（持续 {ma['max_drawdown_days']} 天）")
    print(f"  夏普 {ma['sharpe']:.2f}  索提诺 {(ma['sortino'] or 0):.2f}  卡玛 {(ma['calmar'] or 0):.2f}")
    print(f"  最长水下 {uw['longest_underwater_days']} 天 {uw['longest_span']}")
    print(f"  最长回撤恢复 {uw['max_recovery_days']} 天")

    # 逐年
    yearly: dict[str, float] = {}
    for k, v in (ma.get("monthly") or {}).items():
        y = k.split("-")[0]
        yearly[y] = (1 + yearly.get(y, 0)) * (1 + v) - 1
    print("\n  年度收益：")
    for y in sorted(yearly):
        bar = "#" * int(abs(yearly[y]) * 100)
        print(f"    {y}: {yearly[y]*100:+7.1f}%  {bar}")

    out = Path(__file__).resolve().parents[1] / "reports" / "oos_composite.json"
    out.write_text(json.dumps({
        "grid": [{k: v for k, v in x.items() if k != "in"} | {"in_sharpe": x["in"]["sharpe"],
                 "in_return": x["in"]["total_return"]} for x in rows],
        "best": {"mv": best["mv"], "ms": best["ms"], "in": best["in"], "out": mo},
        "all": ma, "underwater": uw, "yearly": yearly,
    }, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON → {out}")


if __name__ == "__main__":
    main()
