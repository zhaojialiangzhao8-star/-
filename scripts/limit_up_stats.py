"""打板晋级率统计 — 1进2 / 2进3 / 3进4 的真实胜率。

用 30 年日线算：
  · 每个连板高度（1板、2板、3板、4板+）的次日涨停率（晋级率）
  · 次日开盘买、次日收盘卖的平均收益（打板的可执行口径）
  · 按板块拆分（主板 10% vs 创业板/科创 20%）

涨停判定用留余量的阈值：主板 ≥9.5%，创业板/科创 ≥19.5%，ST ≥4.5%。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def board_of(symbol: str) -> str:
    code = symbol.split(".")[0]
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


def limit_threshold(board: str) -> float:
    return 19.5 if board in ("创业板", "科创板") else 30.0 if board == "北交所" else 9.5


def main() -> None:
    raw = GenerationStore(config.DATA_DIR).collect()
    df = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    ).sort(["symbol", "date"])

    # 板块 + 涨停判定
    boards = pl.DataFrame(
        {"symbol": df["symbol"].unique().to_list()}
    ).with_columns(
        pl.col("symbol").map_elements(board_of, return_dtype=pl.Utf8).alias("board")
    )
    df = df.join(boards, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("board").is_in(["创业板", "科创板"]))
        .then(19.5)
        .when(pl.col("board") == "北交所")
        .then(30.0)
        .otherwise(9.5)
        .alias("_thr")
    )
    df = df.with_columns(
        (pl.col("change_pct") >= pl.col("_thr")).alias("is_lu"),
        pl.int_range(0, pl.len()).over("symbol").alias("_idx"),
    )

    # 连板计数：streak = idx - 最近一次"非涨停"的 idx
    df = df.with_columns(
        pl.when(~pl.col("is_lu")).then(pl.col("_idx")).otherwise(None).alias("_last_zero")
    )
    df = df.with_columns(
        pl.col("_last_zero").forward_fill().over("symbol").alias("_lz")
    )
    df = df.with_columns(
        (pl.col("_idx") - pl.col("_lz").fill_null(-1)).alias("_streak_raw")
    )
    # 只有涨停日 _streak_raw 才有意义（非涨停日是 0）
    df = df.with_columns(
        pl.when(pl.col("is_lu")).then(pl.col("_streak_raw")).otherwise(0).alias("boards")
    )

    # 次日数据
    df = df.with_columns(
        pl.col("is_lu").shift(-1).over("symbol").alias("next_lu"),
        pl.col("open").shift(-1).over("symbol").alias("next_open"),
        pl.col("close").shift(-1).over("symbol").alias("next_close"),
        pl.col("open").shift(-1).over("symbol").alias("next_open_raw"),
        pl.col("close").shift(-1).over("symbol").alias("next_close_raw"),
        pl.col("change_pct").shift(-1).over("symbol").alias("next_chg"),
    )
    df = df.with_columns(
        (pl.col("next_close") / pl.col("next_open") - 1).alias("oc_ret"),
        (pl.col("next_close") / pl.col("close") - 1).alias("cc_ret"),
    )

    # 只统计有次日数据的涨停日
    lu = df.filter(pl.col("is_lu") & pl.col("next_lu").is_not_null())

    print(f"数据：{df.height:,} 行 · {df['symbol'].n_unique()} 只 · "
          f"{df['date'].min()} ~ {df['date'].max()}")
    print(f"涨停日总数：{lu.height:,}\n")

    print("=== 连板晋级率（全样本）===")
    print(f"{'连板高度':>10}{'样本数':>9}{'次日涨停率':>12}{'次日均涨幅':>12}"
          f"{'开盘买收盘卖':>14}{'胜率':>8}{'中位收益':>10}")
    print("-" * 78)

    buckets = [(1, 1, "1 板 → 2 板"), (2, 2, "2 板 → 3 板"), (3, 3, "3 板 → 4 板"),
               (4, 5, "4~5 板"), (6, 99, "6 板+")]
    for lo, hi, label in buckets:
        sub = lu.filter((pl.col("boards") >= lo) & (pl.col("boards") <= hi))
        if sub.height == 0:
            continue
        n = sub.height
        promo = sub["next_lu"].mean() * 100
        avg_chg = sub["next_chg"].mean()
        oc = sub["oc_ret"].mean()
        win = (sub["oc_ret"] > 0).mean() * 100
        med = sub["oc_ret"].median()
        print(f"{label:>10}{n:>9}{promo:>11.1f}%{avg_chg:>11.2f}%{oc*100:>13.2f}%{win:>7.1f}%{med*100:>9.2f}%")

    print("\n=== 按板块拆分（1 板 → 2 板）===")
    print(f"{'板块':>8}{'样本数':>9}{'次日涨停率':>12}{'开盘买收盘卖':>14}{'胜率':>8}")
    print("-" * 52)
    for b in ["主板", "创业板", "科创板"]:
        sub = lu.filter((pl.col("boards") == 1) & (pl.col("board") == b))
        if sub.height < 50:
            continue
        print(f"{b:>8}{sub.height:>9}{sub['next_lu'].mean()*100:>11.1f}%"
              f"{sub['oc_ret'].mean()*100:>13.2f}%{(sub['oc_ret']>0).mean()*100:>7.1f}%")

    print("\n=== 按年份看 1 板 → 2 板 晋级率（市场情绪周期）===")
    yearly = (
        lu.filter(pl.col("boards") == 1)
        .with_columns(pl.col("date").dt.year().alias("y"))
        .group_by("y")
        .agg(pl.len().alias("n"), pl.col("next_lu").mean().alias("promo"),
             pl.col("oc_ret").mean().alias("oc"))
        .sort("y")
    )
    for row in yearly.iter_rows(named=True):
        bar = "#" * int(row["promo"] * 60)
        print(f"  {row['y']}: {row['promo']*100:>5.1f}%  (n={row['n']:>5})  开盘买收盘卖 {row['oc']*100:>+6.2f}%  {bar}")

    # 最近 60 个交易日的 1进2 晋级率（实盘参考）
    recent_dates = sorted(df["date"].unique().to_list())[-60:]
    recent = lu.filter(pl.col("date").is_in(recent_dates) & (pl.col("boards") == 1))
    if recent.height:
        print(f"\n最近 60 个交易日：1 板 {recent.height} 个，"
              f"晋级率 {recent['next_lu'].mean()*100:.1f}%，"
              f"开盘买收盘卖 {recent['oc_ret'].mean()*100:+.2f}%")


if __name__ == "__main__":
    main()
