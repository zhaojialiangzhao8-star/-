"""定向回补个股日线 —— 只抓缺失的代码，合并进现有 daily.parquet，再发布新代际。

为什么不用 fetch_all_market.py：它是全市场重建（5,564 只 × 1500 根），
东财对高频并发会直接 502。这里做三件事：
  1. 只抓「库里没有」或「K 线明显偏少」的代码（默认从 symbol_names.json 对比 store）
  2. 低并发 + 每只之间 sleep，失败指数退避重试
  3. 只把新代码的行并进旧 daily.parquet，不重拉已有的 4,780 只

用法：
    python scripts/backfill_symbols.py --dry-run                  # 只看要补多少只
    python scripts/backfill_symbols.py --max 20                   # 先补 20 只试水
    python scripts/backfill_symbols.py --symbols 300308.SZ,300502.SZ
    python scripts/backfill_symbols.py                            # 补全部缺失（慢，建议后台跑）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.indicators import build_enriched  # noqa: E402
from app.infrastructure.data_sources.base import Capability  # noqa: E402
from app.infrastructure.data_sources.registry import registry  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def store_symbols() -> set[str]:
    """当前代际里的代码集合。代际实际在 `<DATA_DIR>/enriched/gens/`，不是 `gen/`。"""
    store = GenerationStore(config.DATA_DIR)
    try:
        store.require()
    except Exception:  # noqa: BLE001
        return set()
    # 只投影 symbol 列 + unique，polars 会做列裁剪，不会把 24 列全读进来
    return set(store.scan().select("symbol").unique().collect()["symbol"].to_list())


def names() -> dict[str, str]:
    p = Path(config.DATA_DIR) / "symbol_names.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def fetch_one(symbol: str, bars: int, adjust: str = "back") -> list[dict]:
    """东财 → 腾讯 → 新浪，三级回退（东财 502、腾讯不覆盖 688）。"""
    provider = registry.best(Capability.DAILY)
    if provider.name == "eastmoney":
        try:
            rows = provider.fetch_daily(symbol, bars=bars, adjust=adjust)
            if rows:
                return _to_rows(symbol, rows)
        except Exception:  # noqa: BLE001
            pass
        try:
            provider = registry.get("tencent-daily")
            rows = provider.fetch_daily(symbol, bars=bars, adjust=adjust)
            if rows:
                return _to_rows(symbol, rows)
        except Exception:  # noqa: BLE001
            pass
        # 腾讯不覆盖 688，最后退到新浪（前复权，整只票回补可接受）
        provider = registry.get("sina-daily")
    return _to_rows(symbol, provider.fetch_daily(symbol, bars=bars, adjust="forward"))


def _to_rows(symbol: str, rows) -> list[dict]:
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
                "volume": b.volume / 100,
                "amount": b.amount,
                "amplitude": b.amplitude,
                "change_pct": b.change_pct,
                "turnover": b.turnover,
            }
        )
    return out


def fetch_with_retry(symbol: str, bars: int, tries: int = 2) -> list[dict]:
    delay = 1.0
    for i in range(tries):
        try:
            return fetch_one(symbol, bars)
        except Exception as exc:  # noqa: BLE001
            if i == tries - 1:
                print(f"  ! {symbol} 放弃: {exc}")
                return []
            time.sleep(delay)
            delay *= 2
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=1500)
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--symbols", default="", help="逗号分隔，指定就只补这些")
    ap.add_argument("--sleep", type=float, default=0.25, help="每只之间的间隔（防 502）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="--symbols 里已存在的代码也重抓（默认跳过，保证可重复执行）")
    args = ap.parse_args()

    have = store_symbols()
    nm = names()
    if args.symbols:
        todo = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        todo = sorted(set(nm) - have)

    # 北交所：腾讯/新浪这两个日线接口都不支持（返回 None），默认跳过，否则每只重试会拖死整批
    skipped_bj = [s for s in todo if s.endswith(".BJ")]
    if skipped_bj:
        todo = [s for s in todo if not s.endswith(".BJ")]
        print(f"跳过北交所 {len(skipped_bj)} 只（两个日线源都不支持 920xxx/8xxxxx.BJ）")

    if args.max:
        todo = todo[: args.max]

    print(f"库里 {len(have)} 只 / 名称表 {len(nm)} 只 / 待补 {len(todo)} 只，每只 {args.bars} 根")
    if args.dry_run:
        print("前 20 只:", ", ".join(f"{nm.get(s, '?')}({s})" for s in todo[:20]))
        return
    if not todo:
        print("没有需要补的代码")
        return

    daily_path = Path(config.DATA_DIR) / "daily" / "daily.parquet"
    base = pl.read_parquet(daily_path) if daily_path.exists() else None

    rows: list[dict] = []
    t0 = time.time()
    ok = 0
    for i, sym in enumerate(todo, 1):
        got = fetch_with_retry(sym, args.bars)
        if got:
            rows.extend(got)
            ok += 1
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}  成功 {ok}  行 {len(rows):,}  {time.time()-t0:.0f}s", flush=True)
        time.sleep(args.sleep)

    if not rows:
        print("一只都没拉到（多半是数据源限流），稍后重试")
        return

    add = pl.DataFrame(rows)
    # 腾讯源给的是字符串日期，统一转成 Date（代际里的 date 是 date 类型）
    if add.schema["date"] == pl.Utf8:
        add = add.with_columns(pl.col("date").str.to_date())
    add = add.sort(["symbol", "date"])
    new_syms = set(add["symbol"].unique().to_list())

    # 只给「新代码」做 enriched：全量重算 650 万行没必要，指标窗口本来就按股票内部滚动
    bench_map = {}
    if base is not None and "benchmark" in base.columns:
        bench_map = dict(
            base.select(["date", "benchmark"]).unique(subset=["date"]).iter_rows()
        )
    if bench_map:
        add = add.with_columns(
            pl.col("date").replace_strict(bench_map, default=None).alias("benchmark")
            if False
            else pl.lit(None).alias("benchmark")
        )
        # replace_strict 对几万个 date 不划算，直接按 map 建小表 join
        bdf = pl.DataFrame({"date": list(bench_map.keys()), "benchmark": list(bench_map.values())})
        add = add.drop("benchmark").join(bdf, on="date", how="left")

    print(f"新增 {len(new_syms)} 只 / {add.height:,} 行，开始算 enriched（只算这部分）…")
    enriched_new = build_enriched(add)

    # 发布新代际 = 当前代际的全部 parquet + 新 part。
    # 两条硬约束（踩过坑）：
    #   1. 代际里的 data.parquet 是「多分片目录」，必须整目录 copy，不能只 glob 顶层
    #   2. 发布前先核对行数 = 旧行数 + 新行数，对不上就中止（否则会静默丢上一批回补）
    store = GenerationStore(config.DATA_DIR)
    cur = store.require()
    src_parts = store.gens_dir / cur.generation / "data.parquet"
    src_files = sorted(src_parts.rglob("*.parquet"))
    if not src_files:
        raise SystemExit(f"当前代际 {cur.generation[:8]} 的 data.parquet 里没有分片，中止")

    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    for f in src_files:
        # 一律摊平到顶层，带路径的分片加前缀 —— 否则会和本次新增分片重名，互相覆盖
        rel = f.relative_to(src_parts).as_posix().replace("/", "__")
        shutil.copy2(f, staging / rel)
    new_part = staging / f"part_backfill_{int(time.time())}.parquet"
    enriched_new.write_parquet(new_part, compression="lz4")

    # 以「实际 parquet 行数」为基线，不用 manifest 里的 rows：
    # 之前几次回补发现 manifest 会和实际分片对不上（记的是旧值），拿它当基准会把行数差算错。
    actual_cur = int(pl.scan_parquet(src_parts).select(pl.len()).collect()[0, 0])
    base_rows = int(pl.scan_parquet(staging).select(pl.len()).collect()[0, 0])
    expect = actual_cur + enriched_new.height
    print(
        f"基线代际 {cur.generation[:8]} 实际 rows={actual_cur:,}（manifest {cur.rows:,}）；"
        f"staging={base_rows:,}，期望 {expect:,}"
    )
    if actual_cur != cur.rows:
        print(f"  ! manifest 与实际不一致（差 {cur.rows - actual_cur:,} 行），本次以实际为准")
    if base_rows != expect:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(
            f"行数校验不通过：staging {base_rows:,} != 实际旧代际 {actual_cur:,} + 新增 {enriched_new.height:,}；"
            "已中止，未发布（避免覆盖上一批回补）"
        )

    gen = store.publish_file(staging, rows=expect)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"新代际 {gen.generation[:8]}  rows={gen.rows:,}")

    # daily.parquet 也补上（后续增量脚本读它）
    if base is not None:
        for c in base.columns:
            if c not in add.columns:
                add = add.with_columns(pl.lit(None).alias(c))
        daily = pl.concat([base, add.select(base.columns)], how="vertical_relaxed")
        daily = daily.unique(subset=["symbol", "date"], keep="last").sort(["symbol", "date"])
        daily.write_parquet(daily_path, compression="lz4")
        print(f"daily -> {daily_path}  rows={daily.height:,}  symbols={daily['symbol'].n_unique():,}")


if __name__ == "__main__":
    main()
