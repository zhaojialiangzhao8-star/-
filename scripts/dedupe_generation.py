"""去重代际：同一 (symbol, date) 只保留一行。

为什么需要：补交易日时如果基准日取错，会把已有的那天又插一遍，
结果同一天两行（价格一个原始、一个折算），指标全歪。
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


def main() -> None:
    store = GenerationStore(config.DATA_DIR)
    cur = store.require()
    src = store.gens_dir / cur.generation / "data.parquet"
    before = int(pl.scan_parquet(src).select(pl.len()).collect()[0, 0])
    print(f"代际 {cur.generation[:8]} rows={before:,}，开始去重…")

    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_dedup_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    lf = (
        store.scan()
        .sort(["symbol", "date"])
        .unique(subset=["symbol", "date"], keep="last")
    )
    out = staging / "part_dedup.parquet"
    lf.sink_parquet(out, compression="lz4")

    after = int(pl.scan_parquet(out).select(pl.len()).collect()[0, 0])
    print(f"去重后 rows={after:,}（去掉 {before - after:,} 行）")
    if after >= before:
        print("没有重复行，删除 staging 不发布")
        shutil.rmtree(staging, ignore_errors=True)
        return

    gen = store.publish_file(staging, rows=after)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"新代际 {gen.generation[:8]} rows={gen.rows:,}")


if __name__ == "__main__":
    main()
