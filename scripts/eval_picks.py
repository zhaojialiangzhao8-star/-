"""用真实历史验证「终端 picks 评分」的区分度。

两个口径同时给：
  A. 当日收盘买入（理想化，等于假设你收盘那一刻就能成交）
  B. 次日开盘买入（可执行，评分是盘后算出来的，实盘只能次日开盘买）

三块分析：
  1) 分档：80+/65-80/50-65/<50 的未来 N 日收益与胜率
  2) 十分位：分数从高到低切 10 段，看单调性（判断评分是"排序器"还是"筛子"）
  3) IC + TopN cohort（非重叠）
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

STRATEGY = BACKEND / "app" / "strategies" / "terminal_picks.py"
REPORT = Path(__file__).resolve().parents[1] / "reports" / "picks_eval.md"
HORIZONS = (1, 2, 3)
TOP_N = 20
GRADE_ORDER = ["强信号", "关注", "观察", "不参与"]


def load_strategy():
    spec = importlib.util.spec_from_file_location("terminal_picks_eval", STRATEGY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pct(x: float | None, digits: int = 2) -> str:
    if x is None or x != x:
        return "—"
    return f"{x * 100:+.{digits}f}%"


def main() -> None:
    store = GenerationStore(config.DATA_DIR)
    frame = store.collect()
    mod = load_strategy()
    scored = mod.filter_frame(frame, {"min_score": 0.0})

    # 未来收益：A=当日收盘买，B=次日开盘买
    px = frame.select("symbol", "date", "open", "close").sort(["symbol", "date"])
    px = px.with_columns(
        pl.col("open").shift(-1).over("symbol").alias("entry_open"),
        pl.col("close").shift(-1).over("symbol").alias("next_close"),
    )
    for n in HORIZONS:
        px = px.with_columns(
            (pl.col("close").shift(-n).over("symbol") / pl.col("close") - 1).alias(f"A{n}"),
            (pl.col("close").shift(-n).over("symbol") / pl.col("entry_open") - 1).alias(f"B{n}"),
        )

    merged = scored.join(
        px.select("symbol", "date", *[f"A{n}" for n in HORIZONS], *[f"B{n}" for n in HORIZONS]),
        on=["symbol", "date"],
        how="left",
    ).drop_nulls(["A1", "B1"])

    n_sym, n_day = frame["symbol"].n_unique(), frame["date"].n_unique()
    lines = [
        "# 终端 picks 评分 · 真实历史验证",
        "",
        f"- 样本：**{n_sym} 只** × **{n_day} 个交易日**（{frame['date'].min()} ~ {frame['date'].max()}），"
        f"评分样本 {merged.height} 行",
        "- 数据源：东财前复权日线（与你终端 8721 同源）",
        "- 权重：rs .22 / vol .20 / trend .18 / **seal .15（日线拿不到，已剔除并重归一化）** / range .13 / flow .12",
        "",
        "**两个口径**：",
        "- `A` = 当日收盘价买入（理想化：假设评分在收盘前就已知并能成交）",
        "- `B` = **次日开盘价买入**（可执行：评分是盘后算的，实盘只能次日开盘买）",
        "",
        "未计滑点与手续费；未剔除涨跌停不可成交；样本取自今日仍满足「价格≥3 / 成交额≥1亿 / 流通市值≥10亿」",
        "的股票（**存在幸存者偏差**，真实全样本会比这更差）。",
        "",
    ]

    # ---- 1) 分档 ----
    lines += ["## 1. 分档表现", "", "| 档位 | 样本 | A1 | A1胜率 | B1 | B1胜率 | B2 | B3 |", "|---|---|---|---|---|---|---|---|"]
    agg = (
        merged.group_by("grade")
        .agg(
            pl.len().alias("n"),
            pl.col("A1").mean().alias("a1"),
            (pl.col("A1") > 0).mean().alias("a1w"),
            pl.col("B1").mean().alias("b1"),
            (pl.col("B1") > 0).mean().alias("b1w"),
            pl.col("B2").mean().alias("b2"),
            pl.col("B3").mean().alias("b3"),
        )
        .with_columns(pl.col("grade").cast(pl.Utf8))
    )
    order = {g: i for i, g in enumerate(GRADE_ORDER)}
    for row in sorted(agg.to_dicts(), key=lambda r: order.get(r["grade"], 99)):
        lines.append(
            f"| {row['grade']} | {row['n']} | {pct(row['a1'])} | {row['a1w']*100:.1f}% | "
            f"**{pct(row['b1'])}** | {row['b1w']*100:.1f}% | {pct(row['b2'])} | {pct(row['b3'])} |"
        )
    base = merged.select(
        pl.col("A1").mean().alias("a1"), pl.col("B1").mean().alias("b1"),
        pl.col("B3").mean().alias("b3"),
    ).to_dicts()[0]
    lines.append(
        f"\n**全样本基准**（无脑等权买入所有票）：A1 {pct(base['a1'])} / B1 {pct(base['b1'])} / B3 {pct(base['b3'])}"
    )

    # ---- 2) 十分位 ----
    lines += ["", "## 2. 分数十分位（判断评分是「排序器」还是「筛子」）", "",
              "| 分数段 | 样本 | B1 均值 | B1 胜率 | B3 均值 |", "|---|---|---|---|---|"]
    dec = (
        merged.with_columns(
            ((pl.col("score").rank(method="ordinal", descending=True).over("date") - 1) * 10
             // pl.len().over("date")).cast(pl.Int32).alias("decile")
        )
        .group_by("decile")
        .agg(
            pl.len().alias("n"),
            pl.col("score").min().alias("smin"),
            pl.col("score").max().alias("smax"),
            pl.col("B1").mean().alias("b1"),
            (pl.col("B1") > 0).mean().alias("b1w"),
            pl.col("B3").mean().alias("b3"),
        )
        .sort("decile")
    )
    for row in dec.to_dicts():
        lines.append(
            f"| D{row['decile']+1} ({row['smin']:.0f}–{row['smax']:.0f}) | {row['n']} | "
            f"{pct(row['b1'])} | {row['b1w']*100:.1f}% | {pct(row['b3'])} |"
        )

    # ---- 3) IC ----
    lines += ["", "## 3. 截面 IC（score vs 未来收益，Spearman 秩相关）", "",
              "| 持有期 | IC 均值 | IC 中位数 | IC>0 天数占比 |", "|---|---|---|---|"]
    for n in HORIZONS:
        ranked = (
            merged.select("date", "score", f"B{n}")
            .drop_nulls()
            .with_columns(
                pl.col("score").rank().over("date").alias("rs"),
                pl.col(f"B{n}").rank().over("date").alias("rf"),
            )
        )
        ic = (
            ranked.group_by("date")
            .agg(pl.corr("rs", "rf").alias("ic"))
            .drop_nulls()
            .drop_nans()
        )
        lines.append(
            f"| {n} 日 | {ic['ic'].mean():+.4f} | {ic['ic'].median():+.4f} | "
            f"{(ic['ic'] > 0).mean()*100:.1f}% |"
        )

    # ---- 4) cohort ----
    lines += ["", f"## 4. Top{TOP_N} 组合（每日取分数最高 {TOP_N} 只，次日开盘买入，非重叠采样）", "",
              "| 持有期 | 期数 | 平均每期 | 胜率 | 最差一期 | 最好一期 |", "|---|---|---|---|---|---|"]
    for n in HORIZONS:
        daily = (
            merged.sort("score", descending=True)
            .group_by("date", maintain_order=True)
            .head(TOP_N)
            .group_by("date")
            .agg(pl.col(f"B{n}").mean().alias("ret"))
            .sort("date")
            .drop_nulls()
        )
        cohort = daily.gather_every(n)
        lines.append(
            f"| {n} 日 | {cohort.height} | {pct(cohort['ret'].mean())} | "
            f"{(cohort['ret'] > 0).mean()*100:.1f}% | {pct(cohort['ret'].min())} | {pct(cohort['ret'].max())} |"
        )

    lines += [
        "",
        "## 口径提醒（别忽略）",
        "",
        "1. **seal 缺失**：封单额 / 炸板次数 / 连板数都是盘中数据，日线无法复现。",
        "   这部分占原权重 15%，被剔除后其余 5 项重归一化 —— 所以本文只评价「可日线验证的 85%」。",
        "2. **幸存者偏差**：样本只包含今天还活着的 696 只（按今日市值/流动性筛的），",
        "   真实的「当时全市场」会更差。",
        "3. **不可成交未建模**：一字板、涨停封死、停牌都当成能买能卖。",
        "4. 未计佣金/印花税/滑点；短线策略换手高，成本能把上面的正收益吃掉相当一部分。",
    ]

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告写入 {REPORT}")


if __name__ == "__main__":
    main()
