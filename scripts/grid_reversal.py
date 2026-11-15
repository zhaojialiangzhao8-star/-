"""急跌反弹 · 参数网格 + 样本外验证。

三件事：
  1. 网格搜索：跌幅阈值 × 止损 × 最大持仓天数 → 样本内(2015-2022) 夏普
  2. 样本外：把样本内最优的组合放到 2023-2026 跑，看是否保持
  3. 参数敏感性：每个维度单独的平均表现，判断是"高原"还是"针尖"

输出 JSON 供后续做热力图和报告，不直接打印大表。
"""
from __future__ import annotations

import itertools
import json
import sys
import time
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

OUT = Path(__file__).resolve().parents[1] / "reports" / "grid_reversal.json"

IN_START, IN_END = date(2015, 1, 5), date(2022, 12, 30)
OUT_START, OUT_END = date(2023, 1, 3), date(2026, 11, 7)

THRESHOLDS = [-0.06, -0.08, -0.10, -0.12, -0.15, -0.18]
STOPS = [0.04, 0.06, 0.08, 0.10, 0.12]
HOLDS = [5, 10, 20, 30]


def clean(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )


def reversal_hits(frame: pl.DataFrame, ret3_th: float) -> pl.DataFrame:
    """参数化的急跌反弹：3 日跌幅超过阈值 + 当日收阳 + 量比 1.2 倍。"""
    d = frame.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("_r3"),
        (pl.col("volume") / pl.col("volume").shift(1).over("symbol")).alias("_vr"),
    )
    hit = d.filter(
        (pl.col("_r3") < ret3_th)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("_vr") > 1.2)
        & pl.col("_r3").is_not_null()
    )
    if hit.height == 0:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64, "score": pl.Float64})
    return hit.select(
        "symbol", "date", "close", (-pl.col("_r3") * 100).alias("score")
    )


def run_one(bars: BarStore, hits: pl.DataFrame, stop: float, hold: int, s: date, e: date) -> dict:
    cfg = BacktestConfig(
        init_cash=100_000, risk_pct=0.01, max_positions=5, max_position_pct=0.2,
        stop_loss_pct=stop, max_hold_days=hold, fees=FeeConfig(),
    )
    try:
        r = BacktestEngine(cfg, bars).run(hits, start=s, end=e)
    except ValueError:
        return {"error": "no data"}
    m = r.metrics
    return {
        "total_return": m.get("total_return"),
        "annual_return": m.get("annual_return"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe": m.get("sharpe"),
        "calmar": m.get("calmar"),
        "trades": m.get("trades"),
        "win_rate": m.get("win_rate"),
        "payoff": m.get("payoff"),
        "final_equity": m.get("final_equity"),
    }


def main() -> None:
    t0 = time.time()
    frame = clean(GenerationStore(config.DATA_DIR).collect())
    bars = BarStore(frame)
    print(f"数据 {frame.height:,} 行 · 日历 {len(bars.calendar)} 天 · BarStore {time.time()-t0:.1f}s", flush=True)

    grid: list[dict] = []
    for th, stop, hold in itertools.product(THRESHOLDS, STOPS, HOLDS):
        hits = reversal_hits(frame, th)
        res_in = run_one(bars, hits, stop, hold, IN_START, IN_END)
        res_out = run_one(bars, hits, stop, hold, OUT_START, OUT_END)
        grid.append(
            {
                "th": th, "stop": stop, "hold": hold,
                "signals": hits.height,
                "in": res_in, "out": res_out,
            }
        )
        if len(grid) % 20 == 0:
            print(f"  {len(grid)}/{len(THRESHOLDS)*len(STOPS)*len(HOLDS)}  {time.time()-t0:.0f}s", flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(grid, ensure_ascii=False), encoding="utf-8")

    ok = [g for g in grid if g["in"].get("sharpe") is not None]
    ok.sort(key=lambda g: -g["in"]["sharpe"])
    print(f"\n完成 {len(grid)} 组，耗时 {time.time()-t0:.0f}s")
    print(f"\n样本内(2015-2022) Top 10（按夏普）：")
    print(f"{'跌幅':>6}{'止损':>6}{'持仓':>5}{'信号':>7}{'内收益':>10}{'内夏普':>8}{'外收益':>10}{'外夏普':>8}")
    for g in ok[:10]:
        i, o = g["in"], g["out"]
        print(f"{g['th']*100:>5.0f}%{g['stop']*100:>5.0f}%{g['hold']:>5}{g['signals']:>7}"
              f"{(i['total_return'] or 0)*100:>9.1f}%{(i['sharpe'] or 0):>8.2f}"
              f"{(o['total_return'] or 0)*100:>9.1f}%{(o['sharpe'] or 0):>8.2f}")
    print(f"\nJSON → {OUT}")


if __name__ == "__main__":
    main()
