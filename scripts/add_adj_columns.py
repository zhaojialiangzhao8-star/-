"""给当前代际加两列：adj_factor（后复权因子）、adj_close（后复权收盘价）。

为什么单独一列而不直接改 close：
  · 盘面口径（涨停/连板/涨跌幅）必须用原始价 —— 这是和同花顺对齐的前提
  · 回测收益必须用后复权价 —— 除权日不能凭空跌 30%
  两者都留着，用哪套由调用方决定，不再互相污染。

用法：
    python scripts/add_adj_columns.py
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

CHUNK = 400


def main() -> None:
    fx_path = Path(config.DATA_DIR) / "adj_factors.parquet"
    if not fx_path.exists():
        raise SystemExit("缺少 adj_factors.parquet，先跑 scripts/build_adj_factors.py")
    fx = pl.read_parquet(fx_path).sort(["symbol", "date"])

    store = GenerationStore(config.DATA_DIR)
    cur = store.require()
    src = store.gens_dir / cur.generation / "data.parquet"
    cols = pl.scan_parquet(src).collect_schema().names()
    if "adj_close" in cols:
        print("已经有 adj_close 列，跳过")
        return

    syms = pl.scan_parquet(src).select("symbol").unique().collect()["symbol"].to_list()
    print(f"代际 {cur.generation[:8]}  {len(syms):,} 只，分批加复权列…")

    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_adj_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    total = 0
    for i in range(0, len(syms), CHUNK):
        batch = syms[i : i + CHUNK]
        df = (
            pl.scan_parquet(src)
            .filter(pl.col("symbol").is_in(batch))
            .collect()
            .sort(["symbol", "date"])
        )
        sub_fx = fx.filter(pl.col("symbol").is_in(batch)).sort(["symbol", "date"])
        df = (
            df.join_asof(sub_fx, on="date", by="symbol", strategy="backward")
            .with_columns(pl.col("factor").fill_null(1.0).alias("adj_factor"))
            .drop("factor")
            .with_columns((pl.col("close") * pl.col("adj_factor")).alias("adj_close"))
        )
        df.write_parquet(staging / f"part_{i // CHUNK:04d}.parquet", compression="lz4")
        total += df.height
        print(f"  {min(i+CHUNK, len(syms)):,}/{len(syms):,}  {total:,} 行  {time.time()-t0:.0f}s", flush=True)

    before = int(pl.scan_parquet(src).select(pl.len()).collect()[0, 0])
    if total != before:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(f"行数不一致：{total:,} vs {before:,}")

    gen = store.publish_file(staging, rows=total)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"新代际 {gen.generation[:8]} rows={gen.rows:,}（含 adj_close / adj_factor）")


if __name__ == "__main__":
    main()
