"""按「涨跌幅锚定」重算某一天的 OHLC，修掉跨源拼接的偏差。

问题：之前用比值 ratio = 我的收盘 / 腾讯同收盘 缩放新一天，
      除权除息那天两家的复权因子不同步，导致 4% 的票涨跌幅偏 0.3~0.4pp。

做法：以「涨跌幅」为锚（它和复权基准无关）：
      close = 我的前收 × (1 + 腾讯当天涨跌幅)
      再按 close/腾讯收盘 的比例缩放 开/高/低，保证 K 线形状不变
这样既内部自洽（close/pre_close 等于涨跌幅），又和源一致。

用法：
    python scripts/repair_day_anchored.py --date 2026-11-13 --workers 12
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.infrastructure.data_sources.registry import registry  # noqa: E402
from app.store import GenerationStore  # noqa: E402

RAW_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "amplitude", "change_pct", "turnover", "benchmark"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    day = args.date

    store = GenerationStore(config.DATA_DIR)
    cur = store.require()

    # 每只票在 day 之前最后一根
    hist = (
        store.scan()
        .filter(pl.col("date") < pl.lit(day).str.to_date())
        .select(["symbol", "date", "close"])
        .collect()
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .last()
    )
    prev = {r["symbol"]: float(r["close"]) for r in hist.iter_rows(named=True)}
    print(f"前收基准 {hist.height:,} 只（截至 {hist['date'].max()}）")

    provider = registry.get("tencent-daily")
    syms = hist["symbol"].to_list()

    t0 = time.time()
    rows: list[dict] = []
    done = 0
    miss = 0

    def work(sym: str) -> list[dict]:
        nonlocal miss
        try:
            bars = provider.fetch_daily(sym, bars=6, adjust="back")
        except Exception:  # noqa: BLE001
            miss += 1
            return []
        bar = next((b for b in bars if str(b.date)[:10] == day), None)
        if bar is None or not bar.close or bar.change_pct is None:
            miss += 1
            return []
        base = prev[sym]
        close = base * (1 + float(bar.change_pct) / 100.0)
        factor = close / float(bar.close)
        return [
            {
                "symbol": sym,
                "date": day,
                "open": float(bar.open) * factor,
                "high": float(bar.high) * factor,
                "low": float(bar.low) * factor,
                "close": close,
                "volume": float(bar.volume),          # 腾讯给的是手，不再除 100
                "amount": float(bar.volume) * 100 * close,
                "amplitude": float(bar.amplitude or 0),
                "change_pct": float(bar.change_pct),
                "turnover": float(bar.turnover or 0),
            }
        ]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for batch in ex.map(work, syms):
            rows.extend(batch)
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(syms)} 行 {len(rows):,} 缺 {miss} {time.time()-t0:.0f}s", flush=True)

    print(f"重算 {len(rows):,} 行 / 缺 {miss} / {time.time()-t0:.0f}s")
    if not rows:
        raise SystemExit("一行都没算出来")

    new = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())
    bench = (
        store.scan().select(["date", "benchmark"]).unique(subset=["date"]).collect()
    )
    new = new.join(bench, on="date", how="left")

    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_anchor_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    out = staging / "part_anchored.parquet"

    keep = store.scan().filter(pl.col("date") != pl.lit(day).str.to_date()).select(RAW_COLS)
    lf = pl.concat([keep, new.select(RAW_COLS)], how="vertical_relaxed").sort(["symbol", "date"])
    lf.sink_parquet(out, compression="lz4")

    before = int(pl.scan_parquet(store.gens_dir / cur.generation / "data.parquet").select(pl.len()).collect()[0, 0])
    after = int(pl.scan_parquet(out).select(pl.len()).collect()[0, 0])
    print(f"替换 {day}：{before:,} → {after:,} 行（旧该日 {before - after + new.height:,} 行被替换）")
    if after <= 0:
        raise SystemExit("行数异常")

    gen = store.publish_file(staging, rows=after)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"新代际 {gen.generation[:8]} rows={gen.rows:,}")


if __name__ == "__main__":
    main()
