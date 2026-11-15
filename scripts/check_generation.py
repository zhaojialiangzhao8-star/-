"""代际体检：发布前后都该跑一遍，任何一条不过就别发。

为什么要有它：11-13 那次出事，表面是"复权基准不一致"，实际是我
发布前后都没有校验「同一 (股票, 日期) 有没有两行」「当天覆盖多少只」，
错了 4,773 行都没人拦。这里把这几条做成硬门禁。

用法：
    python scripts/check_generation.py            # 检查当前代际，不通过 exit 1
    python scripts/check_generation.py --dir <path>   # 检查任意分片目录
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

MIN_COVERAGE = 0.90      # 每天至少 90% 的股票有数据
MAX_MEDIAN_MOVE = 12.0   # 全市场中位涨跌幅超过 ±12% 基本可以确定是坏的
MAX_AMOUNT_YI = 60_000   # 全市场单日成交额上限（60 万亿，真实约 1-3 万亿）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="")
    ap.add_argument("--days", type=int, default=5, help="只看最近 N 个交易日")
    args = ap.parse_args()

    if args.dir:
        src = Path(args.dir)
    else:
        store = GenerationStore(config.DATA_DIR)
        cur = store.require()
        src = store.gens_dir / cur.generation / "data.parquet"
        print(f"代际 {cur.generation[:8]}  rows={cur.rows:,}")

    lf = pl.scan_parquet(str(src) if src.is_file() else src / "*.parquet")
    total = int(lf.select(pl.len()).collect()[0, 0])
    dates = lf.select(pl.col("date").max()).collect()[0, 0]
    recent = lf.filter(pl.col("date") >= pl.lit(str(dates)).str.to_date() - pl.duration(days=args.days * 3))
    by_day = recent.group_by("date").agg(
        [pl.len().alias("n"), pl.col("change_pct").median().alias("med"), pl.col("amount").sum().alias("amt")]
    ).sort("date").collect()

    fails: list[str] = []

    # 1) 全表 (symbol, date) 必须唯一
    dup = lf.select(["symbol", "date"]).unique().select(pl.len()).collect()[0, 0]
    if dup != total:
        fails.append(f"(symbol,date) 有重复：{total - dup:,} 行重复")

    # 2) 每天覆盖率 ≥ 90%
    top = int(by_day["n"].max())
    thin = by_day.filter(pl.col("n") < top * MIN_COVERAGE)
    if thin.height:
        for r in thin.iter_rows(named=True):
            fails.append(f"{r['date']} 覆盖只有 {r['n']:,} / {top:,} 只")

    # 3) 中位涨跌幅不能离谱
    wild = by_day.filter(pl.col("med").abs() > MAX_MEDIAN_MOVE)
    if wild.height:
        for r in wild.iter_rows(named=True):
            fails.append(f"{r['date']} 中位涨跌幅 {r['med']:.2f}% 异常")

    # 4) 成交额量级：各日之间不应该差 5 倍以上（单位错误时就是这个特征）
    med_amt = float(by_day["amt"].median())
    if med_amt > 0:
        bad = by_day.filter(
            (pl.col("amt") > med_amt * 5) | (pl.col("amt") < med_amt / 5)
        )
        for r in bad.iter_rows(named=True):
            fails.append(
                f"{r['date']} 成交额 {r['amt'] / 1e8:,.0f} 亿，与窗口中位数 {med_amt / 1e8:,.0f} 亿差超过 5 倍"
            )

    print(f"\n最近 {by_day.height} 个交易日：")
    for r in by_day.iter_rows(named=True):
        print(f"  {r['date']}  {r['n']:>5,} 只  中位 {r['med']:>6.2f}%  成交额 {r['amt'] / 1e8:>9,.0f} 亿")

    if fails:
        print("\n[FAIL] 检查未通过：")
        for f in fails:
            print("   -", f)
        return 1
    print("\n[OK] 全部通过（唯一性 / 覆盖率 / 涨跌幅 / 成交额）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
