"""回测统计 — 把「命中表 + 价格矩阵」算成一组成交员看得懂的指标。

两个口径：
  A = 信号当日收盘买入（理想化）
  B = 次日开盘买入（可执行：信号是盘后算出来的）
"""
from __future__ import annotations

import polars as pl

HORIZONS = (1, 2, 3)


def price_matrix(frame: pl.DataFrame) -> pl.DataFrame:
    """预计算每只股票每个日期的未来 1/2/3 日收益（A/B 两个口径）。"""
    px = frame.select("symbol", "date", "open", "close").sort(["symbol", "date"])
    px = px.with_columns(
        pl.col("open").shift(-1).over("symbol").alias("_entry_open"),
    )
    exprs = []
    for n in HORIZONS:
        exprs.append((pl.col("close").shift(-n).over("symbol") / pl.col("close") - 1).alias(f"A{n}"))
        exprs.append(
            (pl.col("close").shift(-n).over("symbol") / pl.col("_entry_open") - 1).alias(f"B{n}")
        )
    return px.with_columns(exprs).select(
        "symbol", "date", *[f"A{n}" for n in HORIZONS], *[f"B{n}" for n in HORIZONS]
    )


def _safe(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def evaluate(
    strategy_id: str,
    name: str,
    hits: pl.DataFrame,
    px: pl.DataFrame,
    top_n: int = 10,
    tag: str = "",
) -> dict:
    out: dict = {
        "id": strategy_id,
        "name": name,
        "tag": tag,
        "hits": hits.height,
        "signal_days": hits["date"].n_unique() if hits.height else 0,
        "samples": 0,
    }
    if hits.height == 0:
        out["note"] = "全样本无命中"
        return out

    merged = hits.join(px, on=["symbol", "date"], how="left").drop_nulls(["A1", "B1"])
    if merged.height == 0:
        out["note"] = "命中都落在最近 N 天，没有未来收益可算"
        return out

    out["samples"] = merged.height
    out["avg_per_day"] = round(merged.height / max(merged["date"].n_unique(), 1), 2)
    out["dates"] = merged["date"].n_unique()

    for n in HORIZONS:
        a, b = merged[f"A{n}"], merged[f"B{n}"]
        out[f"A{n}"] = _safe(a.mean())
        out[f"B{n}"] = _safe(b.mean())
        out[f"win{n}"] = _safe((b > 0).mean())

    win = merged.filter(pl.col("B1") > 0)["B1"]
    lose = merged.filter(pl.col("B1") <= 0)["B1"]
    out["payoff"] = _safe(win.mean() / abs(lose.mean())) if win.len() and lose.len() else None
    out["big_win"] = _safe((merged["B1"] > 0.05).mean())

    # 截面 IC
    ranked = merged.select("date", "score", "B1").drop_nulls().with_columns(
        pl.col("score").rank().over("date").alias("rs"),
        pl.col("B1").rank().over("date").alias("rf"),
    )
    ic = ranked.group_by("date").agg(pl.corr("rs", "rf").alias("ic")).drop_nulls().drop_nans()
    if ic.height:
        out["ic1"] = _safe(ic["ic"].mean())
        out["ic_pos"] = _safe((ic["ic"] > 0).mean())

    # TopN cohort（非重叠）
    daily = (
        merged.sort("score", descending=True)
        .group_by("date", maintain_order=True)
        .head(top_n)
        .group_by("date")
        .agg(pl.col("B1").mean().alias("ret"))
        .sort("date")
        .drop_nulls()
    )
    cohort = daily.gather_every(2)
    if cohort.height:
        out["topn_mean"] = _safe(cohort["ret"].mean())
        out["topn_win"] = _safe((cohort["ret"] > 0).mean())
        out["topn_n"] = cohort.height

    return out
