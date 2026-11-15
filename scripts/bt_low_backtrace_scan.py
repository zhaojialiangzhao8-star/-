"""低回撤增长：参数扫描（止损 × 持有期）

上一轮固定 8% 止损 + 10 天持有，结果是 -44.7% / -37.2%，巨亏。

但之前审计（旧代际、不同口径）说 20 天持有 +25.3%。

必须先排除「参数不匹配」这个可能：
    止损：5% / 8% / 15% / 25% / 无
    持有：5 / 10 / 20 / 40 天

如果有某个组合能翻正，说明是参数问题；如果全部大亏，说明策略本身有问题。
"""
from __future__ import annotations

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
}

STRATEGY = "instock_low_backtrace_increase"
STOPS = [0.05, 0.08, 0.15, 0.25, None]
HOLDS = [5, 10, 20, 40]


def main() -> None:
    store = GenerationStore(config.DATA_DIR)
    store.require()
    eng = StrategyEngine(config.STRATEGY_DIR)

    for split, (d0, d1) in SPLITS.items():
        print("=" * 92)
        print(f"Split {split}  {d0} ~ {d1}")
        print("=" * 92)

        seg = (
            store.scan()
            .filter(
                (pl.col("date") >= pl.lit(d0 - timedelta(days=400)))
                & (pl.col("date") <= pl.lit(d1))
            )
            .collect()
        )
        sig = eng.run(STRATEGY, {}, seg).filter(
            (pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1))
        )
        del seg

        syms = sig["symbol"].unique().to_list()
        bars = (
            store.scan()
            .filter(
                pl.col("symbol").is_in(syms)
                & (pl.col("date") >= pl.lit(d0))
                & (pl.col("date") <= pl.lit(d1 + timedelta(days=50)))
            )
            .select(
                "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "change_pct", "turnover",
            )
            .collect()
        )
        print(f"  信号 {sig.height:,}  股票 {len(syms)}  行情 {bars.height:,} 行")
        print()
        print("{:<10}{:>10}{:>10}{:>8}{:>8}{:>8}".format("止损", "持有", "收益", "回撤", "胜率", "交易"))
        print("-" * 62)

        for stop in STOPS:
            for hold in HOLDS:
                cfg = BacktestConfig(
                    init_cash=100_000,
                    risk_pct=0.01,
                    max_positions=5,
                    max_position_pct=0.20,
                    stop_loss_pct=stop,
                    max_hold_days=hold,
                )
                try:
                    res = BacktestEngine(cfg, BarStore(bars)).run(sig, d0, d1)
                    m = res.metrics
                    print(
                        "{:<10}{:>10}{:>9.1f}%{:>7.1f}%{:>7.1f}%{:>8}".format(
                            f"{stop*100:.0f}%" if stop else "无",
                            f"{hold}天",
                            (m.get("total_return") or 0) * 100,
                            (m.get("max_drawdown") or 0) * 100,
                            (m.get("win_rate") or 0) * 100,
                            m.get("trades") or 0,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"{stop} {hold} 失败 {type(exc).__name__}")
        print()


if __name__ == "__main__":
    main()
