"""把通达信 vipdoc 的本地日线导入成一份 parquet，用来做离线校验/替换。

只读客户端目录，不写它任何东西。
产出：<DATA_DIR>/vipdoc_daily.parquet（symbol/date/OHLC/amount/volume/change_pct）

用法：
    python scripts/import_vipdoc.py --scan          # 只看有多少文件、最新到哪天
    python scripts/import_vipdoc.py                 # 全量解析并落盘
    python scripts/import_vipdoc.py --limit 500     # 调试：只导前 500 只
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.infrastructure.data_sources.vipdoc_local import VipdocProvider  # noqa: E402

MARKETS = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:\new_tdx")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    provider = VipdocProvider(args.root)
    summary = provider.scan_summary()
    print(f"vipdoc 扫描：{summary}")

    files: list[Path] = []
    for mkt in MARKETS:
        d = provider.vipdoc / mkt / "lday"
        if d.exists():
            files += sorted(d.glob("*.day"))
    print(f".day 文件 {len(files)} 个")
    if args.scan or not files:
        if not files:
            print("还没有数据：先在通达信里做一次「盘后数据下载」（系统 → 盘后数据下载）")
        return
    if args.limit:
        files = files[: args.limit]

    t0 = time.time()
    parts = Path(config.DATA_DIR) / "vipdoc_parts"
    if parts.exists():
        import shutil
        shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True, exist_ok=True)

    CHUNK = 400
    total_rows = 0
    written = 0

    def work(f: Path) -> list[dict]:
        code = f.stem[2:]
        mkt = f.stem[:2].upper()
        sym = f"{code}.{mkt}"
        try:
            bars = provider.fetch_daily(sym, bars=20000, adjust="none")
        except Exception:  # noqa: BLE001
            return []
        return [
            {
                "symbol": sym,
                "date": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "amount": b.amount,
                "change_pct": b.change_pct,
            }
            for b in bars
        ]

    # 流式：每 400 个文件落一个分片，避免把上千万行 dict 堆在内存里
    for i in range(0, len(files), CHUNK):
        batch_files = files[i : i + CHUNK]
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for batch in ex.map(work, batch_files):
                rows.extend(batch)
        if rows:
            df = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())
            df.write_parquet(parts / f"part_{i // CHUNK:04d}.parquet", compression="lz4")
            total_rows += df.height
            written += 1
        del rows
        print(f"  {min(i + CHUNK, len(files))}/{len(files)}  累计 {total_rows:,} 行  {time.time()-t0:.0f}s", flush=True)

    if total_rows == 0:
        raise SystemExit("一条都没解析出来")

    out = Path(config.DATA_DIR) / "vipdoc_daily.parquet"
    pl.scan_parquet(str(parts / "*.parquet")).sort(["symbol", "date"]).sink_parquet(out, compression="lz4")
    df = pl.read_parquet(out, columns=["symbol", "date"])

    print(f"\n写出 {out}")
    print(f"  股票 {df['symbol'].n_unique():,} 只 / {df.height:,} 行 / {df['date'].min()} ~ {df['date'].max()}")
    by_day = df.group_by("date").agg(pl.len().alias("n")).sort("date").tail(3)
    print("  最近三天覆盖:", by_day.to_dicts())


if __name__ == "__main__":
    main()
