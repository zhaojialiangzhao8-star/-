"""在现有全市场数据上加「可交易性过滤」，发布新代际。

为什么要做：全市场 5,215 只跑出来的回撤是 −62%，因为池子里塞满了
小盘股、ST、即将退市的票。实盘的硬约束不是「有没有这个代码」，
而是「这个价位能不能买到、卖出时会不会砸盘」。

过滤条件（可调）：
  · 日均成交额 ≥ min_amount（默认 5000 万）—— 10 万本金买一只，冲击成本 < 0.1%
  · 上市交易日 ≥ min_days（默认 250）—— 剔除次新股，避免无历史窗口
  · 最新价 ≥ min_price（默认 3 元）—— 剔除仙股

实现要点：全程 lazy + sink_parquet，不把 1150 万行 collect 到内存。

跑法：
    python scripts/build_tradable_universe.py
    python scripts/build_tradable_universe.py --min-amount 1e8 --min-days 500
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amount", type=float, default=5e7, help="日均成交额下限（元）")
    ap.add_argument("--min-days", type=int, default=250, help="最少上市交易日")
    ap.add_argument("--min-price", type=float, default=3.0, help="最新价下限")
    args = ap.parse_args()

    t0 = time.time()
    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    print(f"当前代际 {gen.generation[:8]}  {gen.rows:,} 行", flush=True)

    lf = store.scan()
    print("统计每只股票的日均成交额 / 交易日数 …", flush=True)
    stats = (
        lf.group_by("symbol")
        .agg(
            pl.col("amount").mean().alias("avg_amount"),
            pl.col("date").count().alias("days"),
            pl.col("close").last().alias("last_close"),
        )
        .collect()
    )
    print(f"  {stats.height:,} 只", flush=True)

    keep = stats.filter(
        (pl.col("avg_amount") >= args.min_amount)
        & (pl.col("days") >= args.min_days)
        & (pl.col("last_close") >= args.min_price)
    )
    dropped = stats.height - keep.height
    print(
        f"过滤条件：日均成交额 ≥ {args.min_amount/1e8:.2f} 亿 · "
        f"上市 ≥ {args.min_days} 日 · 最新价 ≥ {args.min_price} 元",
        flush=True,
    )
    print(f"  保留 {keep.height:,} 只，剔除 {dropped:,} 只", flush=True)

    if keep.height == 0:
        raise SystemExit("过滤后没有股票，检查参数")

    symbols = keep["symbol"].to_list()
    tmp = config.DATA_DIR / "enriched" / "_filtered.parquet"
    print("流式写出过滤后的数据 …", flush=True)
    lf.filter(pl.col("symbol").is_in(symbols)).sink_parquet(tmp, compression="lz4")
    rows = int(pl.scan_parquet(tmp).select(pl.len()).collect()[0, 0])
    print(f"  {rows:,} 行  {tmp.stat().st_size/1024/1024:.0f} MB  {time.time()-t0:.0f}s", flush=True)

    new_gen = store.publish_file(tmp)
    print(f"\n新代际 {new_gen.generation[:8]} 已发布  {new_gen.rows:,} 行  {len(symbols):,} 只")
    print(f"总耗时 {time.time()-t0:.0f}s")

    # 保存过滤后的股票池，便于对照
    out = Path(__file__).resolve().parents[1] / "reports" / "tradable_universe.csv"
    keep.select("symbol", "avg_amount", "days", "last_close").sort("avg_amount", descending=True).write_csv(out)
    print(f"股票池清单 → {out}")


if __name__ == "__main__":
    main()
