"""平台突破 P1 的账户级回测（进程内直调，不走 HTTP）

为什么要这一步：
    scripts/breakout_engine.py 算的是「信号后 5 天价格变化」，没有仓位、止损、费用。
    那不是回测，是事件研究。

    这个脚本用 app/backtest/engine.py 真正跑一遍：
        · T+1 开盘撮合（涨停不买、一字板不成交）
        · 单笔风险 1% 定股数（100 股取整）
        · 止损（固定百分比）
        · 最多 5 仓、单票 20%、持有上限
        · 真实费用（佣金 0.025% / 印花 0.05% / 过户费 / 滑点 5bp）

用法：
    python scripts/bt_breakout_p1.py --split train
    python scripts/bt_breakout_p1.py --split all
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig  # noqa: E402
from app.engine import StrategyEngine  # noqa: E402
from app.store import GenerationStore  # noqa: E402

SPLITS = {
    "train": (date(2021, 1, 4), date(2023, 12, 29)),
    "valid": (date(2024, 1, 2), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 2), date(2026, 11, 12)),
}

PARAMS = {"N": 60, "R": 10.0, "filters": ""}


def fmt(x, pct=True):
    if x is None:
        return "  n/a "
    return f"{x * 100:+.1f}%" if pct else f"{x:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=["train", "valid", "holdout", "all"])
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--risk", type=float, default=0.01)
    args = ap.parse_args()

    splits = ["train", "valid", "holdout"] if args.split == "all" else [args.split]
    results = []

    for split in splits:
        d0, d1 = SPLITS[split]
        print("=" * 80)
        print(f"Split {split}  {d0} ~ {d1}  止损 {args.stop*100:.0f}%  持有上限 {args.hold} 天")
        print("=" * 80)

        store = GenerationStore(config.DATA_DIR)
        store.require()

        # 1. 信号：用整个代际算（保证指标有足够预热）
        full = store.scan().collect()
        eng = StrategyEngine(config.STRATEGY_DIR)
        sig = eng.run("breakout_p1", PARAMS, full)
        sig = sig.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1)))
        print(f"  区间内信号 {sig.height:,} 条  {sig['symbol'].n_unique()} 只")
        del full

        if sig.height == 0:
            print("  无信号")
            continue

        # 2. 行情：只要信号覆盖的股票 + 区间（省内存）
        syms = sig["symbol"].unique().to_list()
        bars = (
            store.scan()
            .filter(
                pl.col("symbol").is_in(syms)
                & (pl.col("date") >= pl.lit(d0 - timedelta(days=5)))
                & (pl.col("date") <= pl.lit(d1 + timedelta(days=args.hold + 5)))
            )
            .select("symbol", "date", "open", "high", "low", "close", "volume", "amount",
                    "change_pct", "turnover")
            .collect()
        )
        print(f"  行情 {bars.height:,} 行  {bars['symbol'].n_unique()} 只")
        bar_store = BarStore(bars)

        # BacktestConfig 没有 start/end —— 区间由 run(hits, start, end) 传
        cfg = BacktestConfig(
            init_cash=100_000,
            risk_pct=args.risk,
            max_positions=5,
            max_position_pct=0.20,
            stop_loss_pct=args.stop,
            max_hold_days=args.hold,
        )

        try:
            engine = BacktestEngine(cfg, bar_store)
            res = engine.run(sig, d0, d1)
            m = res.metrics
            print(
                "  收益 {}  回撤 {}  夏普 {}  胜率 {}  交易 {} 笔".format(
                    fmt(m.get("total_return")),
                    fmt(m.get("max_drawdown")),
                    fmt(m.get("sharpe"), pct=False),
                    f"{(m.get('win_rate') or 0) * 100:.1f}%",
                    m.get("trades", 0),
                )
            )
            print(f"  盈亏比 {m.get('payoff')}   平均盈 {m.get('avg_win')}   平均亏 {m.get('avg_loss')}")
            print(f"  被拒 {res.rejected}")
            results.append(
                {
                    "split": split,
                    "signals": int(sig.height),
                    "total_return": m.get("total_return"),
                    "max_drawdown": m.get("max_drawdown"),
                    "sharpe": m.get("sharpe"),
                    "win_rate": m.get("win_rate"),
                    "payoff": m.get("payoff"),
                    "trades": m.get("trades"),
                    "rejected": res.rejected,
                }
            )
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  失败 {type(exc).__name__}: {str(exc)[:150]}")
            traceback.print_exc()
        print()

    out = ROOT / "reports" / "breakout_account_backtest.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
