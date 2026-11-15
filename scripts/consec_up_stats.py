"""连续上涨 N 天 —— 把各种定义都测一遍，看哪个真有 alpha。

之前的结论：「连续 3 天小涨（0.3%~3%）」是负 alpha（45.7% < 48.2% 基准）。
但那是限制在「小涨」区间。用户现在说的是「连续涨 3 天，不设上限」。

这两个不是一回事：
    连续小涨  = 每天 0.3%~3%，缓涨
    连续涨    = 每天 > 0，包含大涨、涨停

大涨的加入会完全改变分布 —— 连涨里可能混着 2 个涨停板，
那实际上是打板信号，不是「蓄势」。

所以这个脚本按涨幅区间、连涨天数、累计涨幅分档，逐一看次日表现。

用法：
    python scripts/consec_up_stats.py
    python scripts/consec_up_stats.py --days 3 --min 0
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


def load() -> pl.DataFrame:
    store = GenerationStore(config.DATA_DIR)
    store.require()
    df = (
        store.scan()
        .select("symbol", "date", "open", "high", "low", "close", "change_pct", "volume")
        .sort(["symbol", "date"])
        .collect()
    )
    return df.filter(pl.col("change_pct").is_not_null())


def add_forward(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.col("change_pct").shift(-1).over("symbol").alias("n1_chg"),
        pl.col("open").shift(-1).over("symbol").alias("n1_open"),
        pl.col("close").shift(-1).over("symbol").alias("n1_close"),
        pl.col("high").shift(-1).over("symbol").alias("n1_high"),
        pl.col("low").shift(-1).over("symbol").alias("n1_low"),
        pl.col("change_pct").shift(1).over("symbol").alias("p1"),
        pl.col("change_pct").shift(2).over("symbol").alias("p2"),
        pl.col("change_pct").shift(3).over("symbol").alias("p3"),
        pl.col("change_pct").shift(4).over("symbol").alias("p4"),
        pl.col("change_pct").shift(5).over("symbol").alias("p5"),
    )


def report(sub: pl.DataFrame, label: str, base_up: float, base_avg: float) -> None:
    sub = sub.drop_nulls("n1_chg")
    n = sub.height
    if n < 50:
        return
    up = sub.filter(pl.col("n1_chg") > 0).height
    big = sub.filter(pl.col("n1_chg") >= 5).height
    lim = sub.filter(pl.col("n1_chg") >= 9.8).height
    avg = sub["n1_chg"].mean()
    med = sub["n1_chg"].median()
    edge = (up / n * 100) - base_up
    print(
        f"  {label:<26} {n:>7,} 次  "
        f"上涨 {up / n * 100:>5.1f}% ({edge:+.1f}pp)  "
        f"涨5%+ {big / n * 100:>4.1f}%  涨停 {lim / n * 100:>4.1f}%  "
        f"均值 {avg:>+6.2f}%  中位 {med:>+6.2f}%"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    df = add_forward(load())
    base = df.drop_nulls("n1_chg")
    base_up = base.filter(pl.col("n1_chg") > 0).height / base.height * 100
    base_avg = base["n1_chg"].mean()

    print(f"数据 {df.height:,} 行  {df['symbol'].n_unique()} 只  {df['date'].min()} ~ {df['date'].max()}")
    print(f"全市场基准：次日上涨 {base_up:.1f}%   次日均值 {base_avg:+.2f}%")
    print()

    D = args.days
    cols = [pl.col("change_pct")] + [pl.col(f"p{i}") for i in range(1, D)]

    # ---- 1. 纯连涨：前 D 天每天都 > 0 ----
    pure = df
    for c in cols:
        pure = pure.with_columns(c.alias("_tmp"))
    cond = pl.lit(True)
    for c in cols:
        cond = cond & (c > 0)
    print(f"=== 连续上涨 {D} 天（每天都 > 0，不限幅度）===")
    report(df.filter(cond), f"连涨{D}天", base_up, base_avg)

    # ---- 2. 按每日涨幅区间分层 ----
    print()
    print(f"=== 连续 {D} 天，按每天的涨幅区间分层 ===")
    for lo, hi, name in [
        (0, 1, "每天 0~1%"),
        (1, 3, "每天 1~3%"),
        (3, 5, "每天 3~5%"),
        (5, 9.8, "每天 5~9.8%"),
        (9.8, 21, "含涨停"),
        (0, 3, "每天 0~3%（小涨）"),
        (0, 5, "每天 0~5%"),
    ]:
        c2 = pl.lit(True)
        for c in cols:
            c2 = c2 & c.is_between(lo, hi)
        report(df.filter(c2), name, base_up, base_avg)

    # ---- 3. 按累计涨幅分层 ----
    print()
    print(f"=== 连续 {D} 天，按累计涨幅分层 ===")
    cum = pl.col("change_pct")
    for c in cols[1:]:
        cum = cum + c
    df2 = df.with_columns(cum.alias("_cum"))
    for lo, hi, name in [
        (0, 3, "累计 0~3%"),
        (3, 6, "累计 3~6%"),
        (6, 10, "累计 6~10%"),
        (10, 15, "累计 10~15%"),
        (15, 25, "累计 15~25%"),
        (25, 100, "累计 >25%"),
    ]:
        c2 = pl.lit(True)
        for c in cols:
            c2 = c2 & (c > 0)
        report(df2.filter(c2 & pl.col("_cum").is_between(lo, hi)), name, base_up, base_avg)

    # ---- 4. 加量能条件 ----
    print()
    print(f"=== 连涨 {D} 天 + 量能条件 ===")
    vma = pl.col("volume").rolling_mean(20).over("symbol")
    df3 = df.with_columns(vma.alias("_vma20"))
    for vmult, name in [(1.0, "最后一天量>均量"), (1.5, "最后一天量>1.5倍"), (2.0, "最后一天量>2倍")]:
        c2 = pl.lit(True)
        for c in cols:
            c2 = c2 & (c > 0)
        report(
            df3.filter(c2 & (pl.col("volume") > pl.col("_vma20") * vmult)),
            name,
            base_up,
            base_avg,
        )

    # ---- 5. 缩量连涨（每天量都在减少）----
    print()
    print(f"=== 连涨 {D} 天 + 缩量/放量 ===")
    vol_inc = pl.col("volume") > pl.col("volume").shift(1).over("symbol")
    c2 = pl.lit(True)
    for c in cols:
        c2 = c2 & (c > 0)
    report(df.filter(c2 & vol_inc), "最后一天放量", base_up, base_avg)
    report(df.filter(c2 & ~vol_inc), "最后一天缩量", base_up, base_avg)

    # ---- 6. 位置：连涨时离 60 日高点多远 ----
    print()
    print(f"=== 连涨 {D} 天，按位置分层 ===")
    hi60 = pl.col("high").rolling_max(60).over("symbol")
    df4 = df.with_columns((pl.col("close") / hi60).alias("_pos"))
    c2 = pl.lit(True)
    for c in cols:
        c2 = c2 & (c > 0)
    for lo, hi, name in [
        (0.99, 2, "创 60 日新高"),
        (0.95, 0.99, "距高点 1~5%"),
        (0.85, 0.95, "距高点 5~15%"),
        (0, 0.85, "距高点 >15%"),
    ]:
        report(df4.filter(c2 & pl.col("_pos").is_between(lo, hi)), name, base_up, base_avg)


if __name__ == "__main__":
    main()
