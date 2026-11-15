"""Step 0 验收：按 R2 补充条款的「逐股完整度」标准检查数据

R2 规定的三条验收标准：
    1. 完整度中位数 ≥ 0.95
    2. 完整度 < 0.80 的股票数 ≤ 100
    3. 数据截止前 250 日，日均横截面 ≥ 4,000 只

完整度 = 实际交易日数 / 应有交易日数
    应有 = 日历中 [max(回测起点, 上市日), 截止日] 的交易日数
    上市日：用该股首根 K 线日期近似（标注为近似）

用法：
    python scripts/verify_5y.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402

DATA = config.DATA_DIR / "daily" / "daily_5y.parquet"
BACKTEST_START = date(2021, 1, 4)     # 回测起点（训练集开始）


def main() -> None:
    if not DATA.exists():
        raise SystemExit(f"缺 {DATA}")

    df = pl.read_parquet(DATA).sort(["symbol", "date"])
    print(f"文件 {DATA.name}  {df.height:,} 行  {df['symbol'].n_unique()} 只")
    print(f"区间 {df['date'].min()} ~ {df['date'].max()}")

    cal = df["date"].unique().sort()
    cutoff = cal[-1]
    print(f"交易日历 {len(cal)} 个，截止 {cutoff}")

    # 每只股票的首根 K 线 = 近似上市日
    per = df.group_by("symbol").agg(
        pl.len().alias("bars"),
        pl.col("date").min().alias("first_date"),
        pl.col("date").max().alias("last_date"),
    )

    # 应有交易日数：从 max(2021-01-04, 首根) 到 cutoff
    cal_list = cal.to_list()
    cal_after_start = [d for d in cal_list if d >= BACKTEST_START]
    total_after_start = len(cal_after_start)

    rows = []
    for r in per.iter_rows(named=True):
        first = r["first_date"]
        start = max(first, BACKTEST_START)
        expected = sum(1 for d in cal_after_start if d >= start)
        actual = r["bars"]
        comp = actual / expected if expected > 0 else 1.0
        rows.append({"symbol": r["symbol"], "expected": expected, "actual": actual, "comp": comp})

    comp_df = pl.DataFrame(rows)
    med = comp_df["comp"].median()
    bad = comp_df.filter(pl.col("comp") < 0.80)

    print()
    print("=" * 70)
    print("R2 验收标准")
    print("=" * 70)
    print(f"  1. 完整度中位数        {med:.4f}   {'✓' if med >= 0.95 else '✗'}  （要求 ≥ 0.95）")
    print(f"  2. 完整度 <0.80 的股票  {bad.height} 只   {'✓' if bad.height <= 100 else '✗'}  （要求 ≤ 100）")

    # 近期横截面宽度
    recent = df.filter(pl.col("date") >= pl.lit(cal_list[-250]))
    byd = recent.group_by("date").agg(pl.len().alias("n"))
    avg_width = byd["n"].mean()
    print(f"  3. 近 250 日平均横截面  {avg_width:,.0f} 只  {'✓' if avg_width >= 4000 else '✗'}  （要求 ≥ 4,000）")

    all_ok = med >= 0.95 and bad.height <= 100 and avg_width >= 4000
    print()
    print(f"  总体 {'✓ 通过' if all_ok else '✗ 未通过'}")

    if bad.height:
        print()
        print(f"  完整度最差的 15 只（<0.80 共 {bad.height} 只）：")
        for r in bad.sort("comp").head(15).iter_rows(named=True):
            print(f"    {r['symbol']}  应有 {r['expected']:>4}  实际 {r['actual']:>4}  完整度 {r['comp']:.2f}")

    print()
    print("=" * 70)
    print("分布")
    print("=" * 70)
    bars = per["bars"]
    print(f"  每只 K 线数：最少 {bars.min()}  中位 {int(bars.median())}  最多 {bars.max()}")
    print(f"  起始日早于 2021-01 的：{per.filter(pl.col('first_date') < pl.lit(BACKTEST_START)).height} 只")
    thin = byd.filter(pl.col("n") < 1000)
    print(f"  近 250 日里不足 1000 只的交易日：{thin.height} 个")

    # 复权跳变
    x = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_chg")
    )
    jumps = x.filter(pl.col("_chg").abs() > 0.21)
    print(f"  复权跳变（|涨跌|>21%）：{jumps.height:,} 行")
    if jumps.height:
        byd2 = jumps.group_by("date").agg(pl.len().alias("n")).sort("n", descending=True).head(5)
        for r in byd2.iter_rows(named=True):
            print(f"    {r['date']}  {r['n']} 只")

    print()
    print(f"结论：{'可以进入 Step 3-4（发布代际 + 跑实验）' if all_ok else '需要先修复数据问题'}")


if __name__ == "__main__":
    main()
