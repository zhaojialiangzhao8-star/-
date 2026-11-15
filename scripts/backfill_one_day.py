"""补齐某一个交易日（默认库里最新日之后缺的那天），并按「基准对齐」修正复权差异。

为什么之前失败：
  腾讯给的是它自己的后复权基准，和库里的后复权基准不是同一个归一化 →
  直接拼在一起，最后一天的涨跌幅全是错的（中位数 -4.7%）。

现在的做法（逐票比值对齐）：
  1. 取库里这只票「最后一天」的收盘价 C_store
  2. 取腾讯同一交易日的收盘价 C_tx（两者是同一根 K 线，只是基准不同）
  3. ratio = C_store / C_tx，把腾讯的新增日 OHLC 乘以 ratio → 落到我们的基准上
  这样新旧两段严丝合缝，涨跌幅可算

用法：
    python scripts/backfill_one_day.py --dry-run
    python scripts/backfill_one_day.py --workers 10
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
from app.indicators import build_enriched  # noqa: E402
from app.infrastructure.data_sources.base import Capability  # noqa: E402
from app.infrastructure.data_sources.registry import registry  # noqa: E402
from app.store import GenerationStore  # noqa: E402

RAW_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "amplitude", "change_pct", "turnover", "benchmark"]
WINDOW = 61


def tx_bars(symbol: str, bars: int = 15) -> list:
    for name, adj in (("tencent-daily", "back"), ("sina-daily", "forward")):
        try:
            rows = registry.get(name).fetch_daily(symbol, bars=bars, adjust=adj)
            if rows:
                return rows
        except Exception:  # noqa: BLE001
            continue
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=15)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    gen = store.require()

    last = (
        store.scan().select(["symbol", "date", "close"])
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .last()
        .collect()
    )
    market_latest = last["date"].max()
    today_col = last.filter(pl.col("date") == market_latest)
    print(f"代际 {gen.generation[:8]} · 市场最新日 {market_latest} · 已有 {today_col.height} 只")

    # 目标日 = 库里出现过、但覆盖不足的那天（就是最新日），补到 90% 以上
    target = market_latest
    have = set(today_col["symbol"].to_list())
    todo = [s for s in last["symbol"].to_list() if s not in have]
    print(f"需要补 {target} 的：{len(todo)} 只")
    if args.dry_run or not todo:
        return

    last_map = {r["symbol"]: (r["date"], float(r["close"])) for r in last.iter_rows(named=True)}

    t0 = time.time()
    rows: list[dict] = []
    skipped = 0
    done = 0

    def work(sym: str) -> list[dict]:
        nonlocal skipped
        our_date, our_close = last_map[sym]
        bars = tx_bars(sym, args.bars)
        if not bars:
            skipped += 1
            return []
        # 找到重叠日，算基准比值
        overlap = next((b for b in bars if str(b.date)[:10] == str(our_date)), None)
        if overlap is None or not overlap.close:
            skipped += 1
            return []
        ratio = our_close / float(overlap.close)
        out = []
        for b in bars:
            d = str(b.date)[:10]
            if d <= str(our_date):
                continue
            out.append(
                {
                    "symbol": sym,
                    "date": d,
                    "open": b.open * ratio,
                    "high": b.high * ratio,
                    "low": b.low * ratio,
                    "close": b.close * ratio,
                    "volume": b.volume / 100,
                    "amount": (b.volume / 100) * 100 * b.close * ratio,  # 近似成交额（元）
                    "amplitude": b.amplitude,
                    "change_pct": b.change_pct,
                    "turnover": b.turnover,
                }
            )
        return out

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for batch in ex.map(work, todo):
            rows.extend(batch)
            done += 1
            if done % 400 == 0:
                print(f"  {done}/{len(todo)}  行 {len(rows):,}  跳过 {skipped}  {time.time()-t0:.0f}s", flush=True)

    print(f"抓取完成：{len(rows):,} 行 / 跳过 {skipped} 只 / {time.time()-t0:.0f}s")
    if not rows:
        return

    raw = Path(config.DATA_DIR) / "daily" / "_oneday_raw.parquet"
    df = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())
    df.write_parquet(raw, compression="lz4")
    print(f"已落盘 {raw}")

    syms = df["symbol"].unique().to_list()
    hist = (
        store.scan().filter(pl.col("symbol").is_in(syms)).select(RAW_COLS).collect()
        .sort(["symbol", "date"]).group_by("symbol", maintain_order=True).tail(WINDOW)
    )
    bench = hist.select(["date", "benchmark"]).unique(subset=["date"])
    df = df.drop("benchmark", strict=False).join(bench, on="date", how="left")
    merged = pl.concat([hist, df], how="vertical_relaxed").sort(["symbol", "date"])

    print(f"重算 enriched：{merged.height:,} 行（{WINDOW} 根窗口 + 新行）…")
    enriched = build_enriched(merged)
    new_dates = df["date"].unique().to_list()
    part = enriched.filter(pl.col("date").is_in(new_dates))
    print(f"新增日 {new_dates} → {part.height:,} 行 / {part['symbol'].n_unique()} 只")
    if part.height == 0:
        raise SystemExit("算不出 enriched")

    cur = store.require()
    src_parts = store.gens_dir / cur.generation / "data.parquet"
    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_day_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    for f in sorted(src_parts.rglob("*.parquet")):
        rel = f.relative_to(src_parts).as_posix().replace("/", "__")
        shutil.copy2(f, staging / rel)
    part.write_parquet(staging / f"part_day_{int(time.time())}.parquet", compression="lz4")

    actual = int(pl.scan_parquet(src_parts).select(pl.len()).collect()[0, 0])
    staged = int(pl.scan_parquet(staging).select(pl.len()).collect()[0, 0])
    expect = actual + part.height
    print(f"基线 {actual:,} + 新增 {part.height:,} = {expect:,}；staging {staged:,}")
    if staged != expect:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("行数校验不通过")

    out = store.publish_file(staging, rows=expect)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"新代际 {out.generation[:8]} rows={out.rows:,}")


if __name__ == "__main__":
    main()
