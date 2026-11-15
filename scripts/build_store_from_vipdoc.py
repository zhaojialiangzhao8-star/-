"""用通达信 vipdoc 重建整个数据仓库（原始价口径 + 分批 enrich，内存可控）。

为什么要重建：
  现在的库是「东财后复权价 + 后复权涨跌幅」，和盘面涨跌幅不是一回事，
  除权日会分叉（实测 11-12 有 16% 的票对不上同花顺/通达信）。
  vipdoc 给的是**原始价**，正好用来做盘面口径（涨停/连板/涨跌幅），
  历史还从 2007 年起、含北交所。

分批的原因：
  2,170 万行 × 24 列 ≈ 5-8 GB，一次性 enrich 会 OOM；
  按 400 只一批 enrich 再落分片，内存恒定在几百 MB。

用法：
    python scripts/build_store_from_vipdoc.py --dry-run
    python scripts/build_store_from_vipdoc.py
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
from app.indicators import build_enriched  # noqa: E402
from app.store import GenerationStore  # noqa: E402

# 股票代码前缀（排除基金/债券/指数）
def is_stock(sym: str) -> bool:
    """只留个股，剔掉指数/基金/债券。

    必须按「市场 + 代码」双条件判断：
      只看代码前缀会把上证指数 000001.SH 当成平安银行那样的 00 开头个股。
    规则：
      SH: 60xxxx（主板） 68xxxx（科创）—— 000xxx 是指数、5xxxxx 是基金、9xxxxx 是 B 股
      SZ: 00xxxx（主板/中小） 30xxxx（创业）—— 399xxx 是指数、15/16xxxx 是基金
      BJ: 43/83/87/92 开头
    """
    code, _, mkt = sym.partition(".")
    if mkt == "SH":
        return code.startswith(("60", "68"))
    if mkt == "SZ":
        return code.startswith(("00", "30"))
    if mkt == "BJ":
        return code.startswith(("43", "83", "87", "92"))
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="")
    ap.add_argument("--chunk", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.src) if args.src else Path(config.DATA_DIR) / "vipdoc_daily.parquet"
    if not src.exists():
        raise SystemExit(f"没有 {src}，先跑 scripts/import_vipdoc.py")

    # 全部用 lazy + 分块：2,170 万行一次性 collect 会 OOM
    lf = pl.scan_parquet(src)

    # 只取 symbol 列做清单（单列 + unique，内存可控）
    syms_all = lf.select("symbol").unique().collect()["symbol"].to_list()
    syms = sorted([x for x in syms_all if is_stock(x)])
    print(f"总代码 {len(syms_all):,} → 股票 {len(syms):,} 只")

    # benchmark：指数 000001 的日涨跌幅
    idx = (
        pl.scan_parquet(src)
        .filter(pl.col("symbol") == "000001.SH")
        .select(["date", "close"])
        .collect()
        .sort("date")
        .with_columns((pl.col("close") / pl.col("close").shift(1) - 1).mul(100).round(4).alias("benchmark"))
        .select(["date", "benchmark"])
    )
    print(f"benchmark：指数 {idx.height:,} 天")

    if args.dry_run:
        d = (
            pl.scan_parquet(src)
            .filter(pl.col("symbol").is_in(syms[:500]))
            .select("date").collect()
            .group_by("date").agg(pl.len().alias("n")).sort("date").tail(3)
        )
        print("抽样最近三天覆盖:", d.to_dicts())
        return

    store = GenerationStore(config.DATA_DIR)
    staging = Path(config.DATA_DIR) / "enriched" / f"_staging_vipdoc_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    total = 0
    parts = 0
    for i in range(0, len(syms), args.chunk):
        batch_syms = syms[i : i + args.chunk]
        chunk = (
            pl.scan_parquet(src)
            .filter(pl.col("symbol").is_in(batch_syms))
            .collect()
            .sort(["symbol", "date"])
            .with_columns(
                (pl.col("high") - pl.col("low"))
                .truediv(pl.col("close").shift(1).over("symbol"))
                .mul(100).round(2).alias("amplitude"),
                pl.lit(0.0).alias("turnover"),
            )
            .join(idx, on="date", how="left")
        )
        try:
            # 原始价 + 十几年历史：正常股票也能涨几十倍，
            # 关掉「收盘极差 >30 倍」过滤（那是给后复权短窗口用的）
            enriched = build_enriched(chunk, max_price_ratio=1e9)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 批 {i//args.chunk} enrich 失败: {exc}")
            continue
        enriched.write_parquet(staging / f"part_{i // args.chunk:04d}.parquet", compression="lz4")
        total += enriched.height
        parts += 1
        print(f"  {min(i+args.chunk, len(syms)):,}/{len(syms):,}  行 {total:,}  {time.time()-t0:.0f}s", flush=True)

    if total == 0:
        raise SystemExit("一行都没生成")

    print(f"enrich 完成：{total:,} 行 / {parts} 个分片，发布新代际…")
    gen = store.publish_file(staging, rows=total)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"新代际 {gen.generation[:8]} rows={gen.rows:,}")


if __name__ == "__main__":
    main()
