"""全市场历史数据拉取 —— 分批落盘，避免把 1600 万个 dict 堆在内存里。

为什么要单独写一个：
  `fetch_history.py` 是抽样版（默认 600 只），把全部行累积在 list 里再建 DataFrame。
  全市场 5,563 只 × 3,000 根 ≈ 1,600 万行，list of dict 会吃 8 GB 直接 OOM。

本脚本的做法：
  1. 每 30 万行 flush 成一个 parquet 分片（内存恒定 ~200 MB）
  2. 用 polars lazy 流式合并成 daily.parquet（sink_parquet，不 collect）
  3. enriched 按 400 只一批算（滚动窗口在每只股票内部完整，不会切断）
     —— 这是关键：按月/年分批会切断窗口，按股票分批不会

跑法：
    python scripts/fetch_all_market.py                 # 全市场，3000 根（约 12 年）
    python scripts/fetch_all_market.py --bars 5000     # 全历史
    python scripts/fetch_all_market.py --max 1000      # 只拉 1000 只（调试）
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config  # noqa: E402
from app.indicators import build_enriched  # noqa: E402
from app.infrastructure.data_sources.base import Capability  # noqa: E402
from app.infrastructure.data_sources.registry import registry  # noqa: E402
from app.store import GenerationStore  # noqa: E402

FLUSH_ROWS = 300_000      # 每 30 万行落一个分片
ENRICH_CHUNK = 400        # 每次给 build_enriched 喂 400 只


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_one(provider, symbol: str, bars: int, adjust: str) -> list[dict]:
    try:
        rows = provider.fetch_daily(symbol, bars=bars, adjust=adjust)
    except Exception:
        return []
    out = []
    for b in rows:
        out.append(
            {
                "symbol": symbol,
                "date": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume / 100,   # 落盘用「手」，与既有 schema 一致
                "amount": b.amount,
                "amplitude": b.amplitude,
                "change_pct": b.change_pct,
                "turnover": b.turnover,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=3000, help="每只股票拉多少根（3000≈12年）")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--max", type=int, default=0, help="最多拉多少只（0=全市场）")
    ap.add_argument("--adjust", default="back", choices=["none", "forward", "back"])
    ap.add_argument("--min-price", type=float, default=1.0)
    ap.add_argument("--min-amount", type=float, default=0.0, help="日均成交额下限（元）")
    ap.add_argument("--keep-parts", action="store_true")
    args = ap.parse_args()

    config.ensure_dirs()
    provider = registry.best(Capability.DAILY)
    print(f"数据源: {provider.name}（{provider.info().note}）")

    print("拉全市场快照 …")
    snap = provider.fetch_snapshot()
    pool = [
        s for s in snap
        if _num(s["price"]) >= args.min_price and _num(s["amount"]) >= args.min_amount
    ]
    if args.max:
        pool = pool[: args.max]
    print(f"快照 {len(snap):,} 只 → 过滤后 {len(pool):,} 只，每只 {args.bars} 根，{args.workers} 并发")

    parts = config.DATA_DIR / "daily" / "_parts"
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    buf: list[dict] = []
    part_no = 0
    done = 0
    failed: list[str] = []
    total_rows = 0

    def flush() -> None:
        nonlocal buf, part_no
        if not buf:
            return
        pl.DataFrame(buf).write_parquet(parts / f"part_{part_no:04d}.parquet", compression="lz4")
        part_no += 1
        buf = []

    symbols = [s["symbol"] for s in pool if s.get("symbol")]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for batch in ex.map(lambda s: fetch_one(provider, s, args.bars, args.adjust), symbols):
            if not batch:
                failed.append(symbols[0])  # 只计数，不追具体是哪只
                done += 1
                continue
            buf.extend(batch)
            total_rows += len(batch)
            done += 1
            if len(buf) >= FLUSH_ROWS:
                flush()
                print(f"  {done:,}/{len(symbols):,} 只  {total_rows:,} 行  "
                      f"分片 {part_no}  {time.time()-t0:.0f}s  内存 {_rss()} MB", flush=True)
    flush()

    print(f"\n拉取完成: {done:,} 只 / {total_rows:,} 行 / {part_no} 个分片 / {time.time()-t0:.0f}s")
    if failed:
        print(f"  失败 {len(failed)} 只（多为退市或代码变更）")

    # 流式合并 + 排序，不 collect 到内存
    daily_path = config.DATA_DIR / "daily" / "daily.parquet"
    print("合并分片 → daily.parquet（流式）…")
    t1 = time.time()
    bench_path = config.DATA_DIR / "benchmark" / "000300.parquet"
    lf = pl.scan_parquet(str(parts / "*.parquet")).with_columns(
        pl.col("date").str.to_date()
    )
    if bench_path.exists():
        bench = pl.scan_parquet(bench_path).select("date", "change_pct").rename({"change_pct": "benchmark"})
        lf = lf.join(bench, on="date", how="left")
    lf = lf.sort(["symbol", "date"])
    lf.sink_parquet(daily_path, compression="lz4")
    print(f"  完成 {time.time()-t1:.0f}s  {daily_path.stat().st_size/1024/1024:.0f} MB")

    # 读回统计（只取几列，不整表加载）
    stats = pl.scan_parquet(daily_path).select(
        pl.len().alias("rows"),
        pl.col("symbol").n_unique().alias("symbols"),
        pl.col("date").min().alias("d0"),
        pl.col("date").max().alias("d1"),
    ).collect()
    print(f"  {stats['rows'][0]:,} 行 · {stats['symbols'][0]:,} 只 · {stats['d0'][0]} ~ {stats['d1'][0]}")

    if not args.keep_parts:
        shutil.rmtree(parts, ignore_errors=True)

    # 分批构建 enriched（按股票分批，滚动窗口不断裂）
    print("\n构建派生列（按股票分批，每批 {} 只）…".format(ENRICH_CHUNK))
    t2 = time.time()
    out_dir = config.DATA_DIR / "enriched" / "_parts"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_syms = pl.scan_parquet(daily_path).select("symbol").unique().collect()["symbol"].to_list()
    n_parts = 0
    for i in range(0, len(all_syms), ENRICH_CHUNK):
        chunk = all_syms[i : i + ENRICH_CHUNK]
        sub = pl.scan_parquet(daily_path).filter(pl.col("symbol").is_in(chunk)).collect()
        enriched = build_enriched(sub)
        enriched.write_parquet(out_dir / f"e_{n_parts:04d}.parquet", compression="lz4")
        n_parts += 1
        print(f"  {min(i+ENRICH_CHUNK, len(all_syms)):,}/{len(all_syms):,} 只  "
              f"{enriched.height:,} 行  {time.time()-t2:.0f}s  内存 {_rss()} MB", flush=True)
        del sub, enriched

    print("\n合并 enriched 分片并发布代际 …")
    tmp_path = config.DATA_DIR / "enriched" / "_merged.parquet"
    pl.scan_parquet(str(out_dir / "*.parquet")).sink_parquet(tmp_path, compression="lz4")

    store = GenerationStore(config.DATA_DIR)
    gen = store.publish_file(tmp_path)
    shutil.rmtree(out_dir, ignore_errors=True)
    tmp_path.unlink(missing_ok=True)
    print(f"代际 {gen.generation[:8]} 已发布  rows={gen.rows:,}")
    print(f"总耗时 {time.time()-t0:.0f}s")


def _rss() -> int:
    """当前进程内存占用（MB）。Windows 用 psutil 太重，这里读 PerformanceCounter 的近似值。"""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
        )
        return pmc.WorkingSetSize // 1024 // 1024
    except Exception:
        return 0


if __name__ == "__main__":
    main()
