"""第一阶段审计：数据质量与系统能力盘点。

输出一份结构化的事实清单，用于给决策者看「现在到底有什么、缺什么」。
不修改任何数据，只读。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def section(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def main() -> None:
    store = GenerationStore(config.DATA_DIR)
    store.require()
    cur = store.current()
    df = store.scan().collect()

    section("1. 数据代际")
    print(f"  代际 ID     {cur.generation}")
    print(f"  行数        {df.height:,}")
    print(f"  标的数      {df['symbol'].n_unique():,}")
    print(f"  字段数      {len(df.columns)}")
    print(f"  日期范围    {df['date'].min()} ~ {df['date'].max()}")

    # 交易日历
    dates = df["date"].unique().sort()
    print(f"  交易日      {len(dates)} 个")

    section("2. 字段清单")
    for c in df.columns:
        nn = df[c].null_count()
        pct = (1 - nn / df.height) * 100
        print(f"  {c:<22} {str(df[c].dtype):<12} 非空 {pct:>6.1f}%")

    section("3. 板块分布")
    syms = df["symbol"].unique().to_list()
    sh = [s for s in syms if s.endswith(".SH")]
    sz = [s for s in syms if s.endswith(".SZ")]
    bj = [s for s in syms if s.endswith(".BJ")]
    star = [s for s in sz if s[:3] in ("300", "301")]
    kechuang = [s for s in sh if s[:3] in ("688", "689")]
    main_sh = [s for s in sh if s not in kechuang]
    main_sz = [s for s in sz if s not in star]
    print(f"  沪市主板    {len(main_sh):>5}")
    print(f"  深市主板    {len(main_sz):>5}")
    print(f"  创业板      {len(star):>5}")
    print(f"  科创板      {len(kechuang):>5}")
    print(f"  北交所      {len(bj):>5}")
    print(f"  合计        {len(syms):>5}")

    section("4. 数据覆盖度")
    per = df.group_by("symbol").agg(
        pl.len().alias("bars"),
        pl.col("date").min().alias("d0"),
        pl.col("date").max().alias("d1"),
    )
    print(f"  每只 K 线数：最少 {per['bars'].min()}  中位 {int(per['bars'].median())}  最多 {per['bars'].max()}")
    print(f"  起始日早于 2021-06 的标的：{per.filter(pl.col('d0') <= pl.date(2021, 6, 1)).height}")
    print(f"  起始日晚于 2022-01 的标的：{per.filter(pl.col('d0') > pl.date(2022, 1, 1)).height}")
    print(f"  最新日 != 全局最新的标的：{per.filter(pl.col('d1') < df['date'].max()).height}")

    section("5. 每日横截面宽度")
    bydate = df.group_by("date").agg(pl.len().alias("n")).sort("date")
    print(f"  最后 5 天：")
    for r in bydate.tail(5).iter_rows(named=True):
        print(f"    {r['date']}  {r['n']:,} 只")
    print(f"  最早 5 天：")
    for r in bydate.head(5).iter_rows(named=True):
        print(f"    {r['date']}  {r['n']:,} 只")
    thin = bydate.filter(pl.col("n") < 1000)
    print(f"  少于 1000 只的交易日：{thin.height} 个（数据不完整期）")

    section("6. 数据异常")
    x = df.sort(["symbol", "date"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_chg")
    )
    jumps = x.filter(pl.col("_chg").abs() > 0.21)
    print(f"  复权跳变（|涨跌|>21%）：{jumps.height:,} 行")
    if jumps.height:
        byd = jumps.group_by("date").agg(pl.len().alias("n")).sort("n", descending=True).head(5)
        for r in byd.iter_rows(named=True):
            print(f"    {r['date']}  {r['n']} 只")

    bad = df.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("open") > pl.col("high") * 1.001)
        | (pl.col("close") > pl.col("high") * 1.001)
        | (pl.col("close") < pl.col("low") * 0.999)
    )
    print(f"  OHLC 内部矛盾：{bad.height:,} 行")

    zero_vol = df.filter(pl.col("volume") <= 0)
    print(f"  成交量为 0：{zero_vol.height:,} 行（停牌？）")

    section("7. 数据缺口（V3 需要但当前没有）")
    need = {
        "基本面（PE/PB/PS/ROE/ROA/毛利率/负债率）": "完全没有，只有腾讯实时 PE/PB，无历史序列",
        "财务报告（营收/净利同比）": "没有",
        "行业分类（申万/中信）": "只有同花顺板块快照，无历史归属",
        "指数成分与权重": "只有沪深300收盘价，无成分股",
        "30 分钟 / 60 分钟 K 线": "没有（东财 502，通达信未下载）",
        "逐笔成交 / Level-2": "没有",
        "五档盘口历史": "只有实时快照 3 秒，无历史",
        "融资融券余额": "没有",
        "北向资金": "没有",
        "分红送转明细": "没有（只有复权后的价格）",
        "停复牌记录": "没有显式字段，只能靠 volume=0 推断",
        "ST 标记历史": "没有（board_limit_pct 有 is_st 参数但没数据源）",
    }
    for k, v in need.items():
        print(f"  ✗ {k}")
        print(f"      {v}")

    section("8. 已有报告清单")
    for f in sorted((ROOT / "reports").glob("*")):
        if f.is_file():
            print(f"  {f.name:<42} {f.stat().st_size:>8,} B")


if __name__ == "__main__":
    main()
