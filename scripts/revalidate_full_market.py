"""全市场条件下的策略复验 —— 样本池从 699 只扩到 5,218 只后，旧结论必须重跑。

为什么单独写：
  `oos_reversal.py` / `oos_composite.py` 是 699 只时代写的，直接 collect 全表。
  全市场 1,150 万行会 OOM。这里改用区间裁剪 + 9 列，并且只跑关键参数组。

跑法：
    python scripts/revalidate_full_market.py
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig, FeeConfig  # noqa: E402
from app.store import GenerationStore  # noqa: E402

BAR_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "change_pct"]
IN_S, IN_E = date(2015, 1, 5), date(2022, 12, 30)
OUT_S, OUT_E = date(2023, 1, 3), date(2026, 11, 7)


def reversal_hits(df: pl.DataFrame, th: float) -> pl.DataFrame:
    d = df.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("_r3"),
        (pl.col("volume") / pl.col("volume").shift(1).over("symbol")).alias("_vr"),
    )
    h = d.filter(
        (pl.col("_r3") < th) & (pl.col("close") > pl.col("open"))
        & (pl.col("_vr") > 1.2) & pl.col("_r3").is_not_null()
    )
    return h.select("symbol", "date", (-pl.col("_r3") * 100).alias("score"))


def run(bars: BarStore, hits: pl.DataFrame, stop: float, hold: int, s: date, e: date) -> dict:
    cfg = BacktestConfig(
        init_cash=100_000, risk_amount=3000, max_positions=10, max_position_pct=0.25,
        stop_loss_pct=stop, max_hold_days=hold, fees=FeeConfig(),
    )
    r = BacktestEngine(cfg, bars).run(hits, start=s, end=e)
    return r.metrics


def main() -> None:
    t0 = time.time()
    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    print(f"代际 {gen.generation[:8]}  {gen.rows:,} 行  {gen.updated_text}", flush=True)

    print("加载 2013 年至今（区间裁剪）…", flush=True)
    lf = store.scan().filter(pl.col("date") >= date(2013, 1, 1))
    names = lf.collect_schema().names()
    frame = lf.select([c for c in BAR_COLS if c in names]).collect().sort(["symbol", "date"])
    print(f"  {frame.height:,} 行 · {frame['symbol'].n_unique()} 只 · {time.time()-t0:.0f}s", flush=True)

    bars = BarStore(frame)
    print(f"  BarStore 就绪 {len(bars.calendar)} 个交易日 · {time.time()-t0:.0f}s\n", flush=True)

    # 只跑关键参数组（全量 18 组 × 2 区间 = 36 次 × 60s 太久）
    cases = [
        ("−18% / 止损12% / 5天", -0.18, 0.12, 5),
        ("−18% / 止损8%  / 5天", -0.18, 0.08, 5),
        ("−15% / 止损12% / 5天", -0.15, 0.12, 5),
        ("−12% / 止损12% / 5天", -0.12, 0.12, 5),
        ("−15% / 止损12% / 20天", -0.15, 0.12, 20),
    ]
    cache = {th: reversal_hits(frame, th) for th in {c[1] for c in cases}}
    for th, h in cache.items():
        print(f"  信号池 {th*100:+.0f}%: {h.height:,} 条", flush=True)
    print(flush=True)

    head = f"{'参数':26}{'内收益':>10}{'内夏普':>8}{'外收益':>10}{'外夏普':>8}{'外回撤':>9}{'外交易':>7}"
    print(head)
    print("-" * len(head))
    for name, th, stop, hold in cases:
        mi = run(bars, cache[th], stop, hold, IN_S, IN_E)
        mo = run(bars, cache[th], stop, hold, OUT_S, OUT_E)
        print(f"{name:26}{(mi['total_return'] or 0)*100:>9.1f}%{(mi['sharpe'] or 0):>8.2f}"
              f"{(mo['total_return'] or 0)*100:>9.1f}%{(mo['sharpe'] or 0):>8.2f}"
              f"{(mo['max_drawdown'] or 0)*100:>8.1f}%{mo['trades']:>7}", flush=True)

    print(f"\n总耗时 {time.time()-t0:.0f}s")
    print("\n对比：699 只时代（同参数 −18%/12%/5）样本内夏普 0.90，样本外 0.32")


if __name__ == "__main__":
    main()
