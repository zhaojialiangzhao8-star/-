"""把所有股票补到「同一最新交易日」，并只重算新增那几天的 enriched。

背景：
  · 回补只补了缺失的票，老票还停在 11-12，全市场指标混日期 → 涨停梯队/广度都会错
  · 只把「新行」丢给 build_enriched 是算不出来的（单行没有 ma60 窗口），会被全丢

做法：
  1. 抓新行（每票按自己最后一天往后补），**先落盘到 daily/_recent_new.parquet**
     —— 抓一轮要十几分钟，崩一次不能白跑
  2. 每票取最近 61 根历史 + 新行一起算 enriched，只保留新增日期的行
  3. 合并旧分片发布新代际（带行数校验）

用法：
    python scripts/append_recent_days.py --fetch-only     # 只抓、落盘
    python scripts/append_recent_days.py --reuse          # 用已落盘的新行，直接算+发布
    python scripts/append_recent_days.py                  # 抓 + 算 + 发布
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
CACHE = Path(config.DATA_DIR) / "daily" / "_recent_new.parquet"


def fetch_recent(symbol: str, bars: int) -> list[dict]:
    provider = registry.best(Capability.DAILY)
    rows = []
    if provider.name == "eastmoney":
        try:
            rows = provider.fetch_daily(symbol, bars=bars, adjust="back")
        except Exception:  # noqa: BLE001
            rows = []
    if not rows:
        for name, adj in (("tencent-daily", "back"), ("sina-daily", "forward")):
            try:
                rows = registry.get(name).fetch_daily(symbol, bars=bars, adjust=adj)
                if rows:
                    break
            except Exception:  # noqa: BLE001
                continue
    return [
        {
            "symbol": symbol, "date": str(b.date)[:10], "open": b.open, "high": b.high,
            "low": b.low, "close": b.close, "volume": b.volume / 100, "amount": b.amount,
            "amplitude": b.amplitude, "change_pct": b.change_pct, "turnover": b.turnover,
        }
        for b in rows
    ]


def do_fetch(store: GenerationStore, args) -> None:
    last = (
        store.scan().select(["symbol", "date"]).group_by("symbol")
        .agg(pl.col("date").max().alias("last")).collect()
    )
    target = last["last"].max()
    last_map = {r["symbol"]: r["last"] for r in last.iter_rows(named=True)}
    todo = [s for s, d in last_map.items() if d < target]
    print(f"市场最新日 {target}；需要补的票 {len(todo)} 只（共 {len(last_map)} 只）")
    if not todo:
        return

    t0 = time.time()
    rows: list[dict] = []
    done = 0

    def work(sym: str) -> list[dict]:
        since = str(last_map[sym])
        return [r for r in fetch_recent(sym, args.bars) if r["date"] > since]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for batch in ex.map(work, todo):
            rows.extend(batch)
            done += 1
            if done % 400 == 0:
                print(f"  {done}/{len(todo)}  新行 {len(rows):,}  {time.time()-t0:.0f}s", flush=True)

    print(f"抓取完成 {len(rows):,} 行 / {time.time()-t0:.0f}s")
    if not rows:
        return
    df = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(CACHE, compression="lz4")
    print(f"已落盘 {CACHE}（{df.height:,} 行）")


def do_publish(store: GenerationStore) -> None:
    if not CACHE.exists():
        raise SystemExit(f"没有落盘的新行 {CACHE}，先跑 --fetch-only")
    new_df = pl.read_parquet(CACHE)
    print(f"读取新行 {new_df.height:,} 行，{new_df['symbol'].n_unique()} 只")

    syms = new_df["symbol"].unique().to_list()
    hist = (
        store.scan().filter(pl.col("symbol").is_in(syms)).select(RAW_COLS).collect()
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True).tail(WINDOW)
    )
    # benchmark：优先用历史里的；新日期没有就留空（rs 因子那列允许空）
    bench = hist.select(["date", "benchmark"]).unique(subset=["date"])
    new_df = new_df.drop("benchmark", strict=False).join(bench, on="date", how="left")

    merged = pl.concat([hist, new_df], how="vertical_relaxed").sort(["symbol", "date"])
    print(f"参与重算 {merged.height:,} 行（{WINDOW} 根窗口 + 新行）…")
    enriched = build_enriched(merged)
    part = enriched.filter(pl.col("date").is_in(new_df["date"].unique().to_list()))
    print(f"新增日 {new_df['date'].unique().sort().to_list()} → enriched {part.height:,} 行 / {part['symbol'].n_unique()} 只")
    if part.height == 0:
        raise SystemExit("新一天算不出 enriched，检查窗口/数据")

    cur = store.require()
    src_parts = store.gens_dir / cur.generation / "data.parquet"
    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_recent_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    for f in sorted(src_parts.rglob("*.parquet")):
        rel = f.relative_to(src_parts).as_posix().replace("/", "__")
        shutil.copy2(f, staging / rel)
    part.write_parquet(staging / f"part_recent_{int(time.time())}.parquet", compression="lz4")

    actual = int(pl.scan_parquet(src_parts).select(pl.len()).collect()[0, 0])
    staged = int(pl.scan_parquet(staging).select(pl.len()).collect()[0, 0])
    expect = actual + part.height
    print(f"基线 {actual:,} + 新增 {part.height:,} = {expect:,}；staging {staged:,}")
    if staged != expect:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("行数校验不通过，已中止")

    out = store.publish_file(staging, rows=expect)
    shutil.rmtree(staging, ignore_errors=True)
    CACHE.unlink(missing_ok=True)
    print(f"新代际 {out.generation[:8]} rows={out.rows:,}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=15)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="跳过抓取，用 _recent_new.parquet")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()
    if args.dry_run:
        last = (
            store.scan().select(["symbol", "date"]).group_by("symbol")
            .agg(pl.col("date").max().alias("last")).collect()
        )
        target = last["last"].max()
        n = int((last["last"] < target).sum())
        print(f"市场最新日 {target}，需要补 {n} 只 / 共 {last.height} 只")
        return

    if not args.reuse:
        do_fetch(store, args)
        if args.fetch_only:
            return
    do_publish(store)


if __name__ == "__main__":
    main()
