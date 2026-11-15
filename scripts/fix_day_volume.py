"""修正某一天的成交量/成交额单位（手 vs 股），并重发代际。

背景：腾讯日线返回的 volume 单位是「手」，补数据时我又除了一次 100，
导致 11-13 的成交额只有 3,629 亿（其它交易日 70-80 万亿的后复权口径）。
这里把该日的 volume ×100、amount 按 volume(手)×100×close 重算。
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
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    d = args.date

    store = GenerationStore(config.DATA_DIR)
    cur = store.require()
    src = store.gens_dir / cur.generation / "data.parquet"
    before = int(pl.scan_parquet(src).select(pl.len()).collect()[0, 0])
    print(f"代际 {cur.generation[:8]} rows={before:,}，修正 {d} 的 volume/amount…")

    lf = store.scan().with_columns(
        pl.when(pl.col("date") == pl.lit(d).str.to_date())
        .then(pl.col("volume") * 100)
        .otherwise(pl.col("volume"))
        .alias("volume")
    ).with_columns(
        pl.when(pl.col("date") == pl.lit(d).str.to_date())
        .then(pl.col("volume") * 100 * pl.col("close"))
        .otherwise(pl.col("amount"))
        .alias("amount")
    )

    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_fixvol_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    out = staging / "part_fixed.parquet"
    lf.sink_parquet(out, compression="lz4")

    after = int(pl.scan_parquet(out).select(pl.len()).collect()[0, 0])
    amt = (
        pl.scan_parquet(out)
        .filter(pl.col("date") == pl.lit(d).str.to_date())
        .select(pl.col("amount").sum())
        .collect()[0, 0]
    )
    print(f"修正后 rows={after:,}，{d} 成交额合计={amt / 1e8:,.0f} 亿（后复权口径）")
    if after != before:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("行数变了，中止")

    gen = store.publish_file(staging, rows=after)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"新代际 {gen.generation[:8]} rows={gen.rows:,}")


if __name__ == "__main__":
    main()
