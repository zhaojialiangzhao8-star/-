"""大盘状态（强市 / 震荡 / 弱市）—— 给每次回测打上"当时市场什么样"的标签。

三因子打分（每个交易日一个标签，不靠拍脑袋）：
  指数位置：收盘 > MA20 → +1；MA20 > MA60 → +1
  市场宽度：上涨家数占比 > 60% → +1；< 40% → -1
  合计 ≥ 2   → 强市
  合计 ≤ -1  → 弱市
  其余       → 震荡

缓存：data/regime_daily.parquet（date / index_close / ma20 / ma60 / up_pct / score / label）
数据来源：
  指数 000001.SH ← data/vipdoc_daily.parquet（本地 vipdoc 有指数日线）
  上涨家数     ← 代际仓（每只票当天 change_pct > 0 记一次）
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl

from . import config

CACHE = Path(config.DATA_DIR) / "regime_daily.parquet"
INDEX = "000001.SH"
LABELS = {2: "强市", 1: "震荡", 0: "震荡", -1: "弱市", -2: "弱市"}


def _index_frame() -> pl.DataFrame | None:
    """指数日线。优先 vipdoc（2007 起），没有就退回 index_daily.parquet（2018 起）。

    为什么要有退路：打包版的数据目录里不一定有 vipdoc_daily.parquet
    （它是导入脚本产出的），但 index_daily.parquet 是随程序内置的。
    """
    srcs = [
        Path(config.DATA_DIR) / "vipdoc_daily.parquet",
        Path(config.DATA_DIR) / "index_daily.parquet",
    ]
    for src in srcs:
        if not src.exists():
            continue
        try:
            df = (
                pl.scan_parquet(src)
                .filter(pl.col("symbol") == INDEX)
                .select(["date", "close"])
                .collect()
                .sort("date")
            )
        except Exception:  # noqa: BLE001
            continue
        if df.height == 0:
            continue
        return df.with_columns(
            pl.col("close").rolling_mean(20).alias("ma20"),
            pl.col("close").rolling_mean(60).alias("ma60"),
        )
    return None


def _breadth_frame(store) -> pl.DataFrame:
    """每天上涨家数占比（用代际仓里 5,000+ 只股票算，不依赖外部数据）。"""
    df = (
        store.scan()
        .select(["date", "change_pct"])
        .collect()
    )
    return (
        df.with_columns((pl.col("change_pct") > 0).alias("up"))
        .group_by("date")
        .agg([pl.len().alias("total"), pl.col("up").sum().alias("up_n")])
        .with_columns((pl.col("up_n") / pl.col("total") * 100).round(2).alias("up_pct"))
        .select(["date", "up_pct"])
    )


def build(store, force: bool = False) -> pl.DataFrame:
    """生成/刷新状态缓存，返回 DataFrame。"""
    idx = _index_frame()
    if idx is None:
        return pl.DataFrame(schema={"date": pl.Date, "label": pl.Utf8, "score": pl.Int32})

    if CACHE.exists() and not force:
        cached = pl.read_parquet(CACHE)
        if cached.height and cached["date"].max() >= idx["date"].max():
            return cached

    breadth = _breadth_frame(store)
    df = idx.join(breadth, on="date", how="left")
    df = df.with_columns(
        (
            (pl.col("close") > pl.col("ma20")).cast(pl.Int32).fill_null(0)
            + (pl.col("ma20") > pl.col("ma60")).cast(pl.Int32).fill_null(0)
            + pl.when(pl.col("up_pct") > 60).then(1)
            .when(pl.col("up_pct") < 40).then(-1)
            .otherwise(0)
            .alias("_b")
        ).alias("score")
    )
    df = df.with_columns(
        pl.when(pl.col("score") >= 2).then(pl.lit("强市"))
        .when(pl.col("score") <= -1).then(pl.lit("弱市"))
        .otherwise(pl.lit("震荡"))
        .alias("label")
    )
    out = df.select(["date", "close", "ma20", "ma60", "up_pct", "score", "label"]).sort("date")
    out.write_parquet(CACHE, compression="lz4")
    return out


def labels() -> dict[date, str]:
    if not CACHE.exists():
        return {}
    df = pl.read_parquet(CACHE, columns=["date", "label"])
    return {r["date"]: r["label"] for r in df.iter_rows(named=True)}


def summarize(start: str | None, end: str | None) -> dict:
    """回测区间的大盘状态摘要：主导状态、各状态占比、指数区间涨跌。"""
    if not CACHE.exists():
        return {}
    df = pl.read_parquet(CACHE).sort("date")
    lo = date.fromisoformat(start) if start else df["date"].min()
    hi = date.fromisoformat(end) if end else df["date"].max()
    win = df.filter((pl.col("date") >= lo) & (pl.col("date") <= hi))
    if win.height == 0:
        return {}
    counts = win.group_by("label").agg(pl.len().alias("n")).sort("n", descending=True)
    total = win.height
    dist = {r["label"]: round(r["n"] / total * 100, 1) for r in counts.iter_rows(named=True)}
    first = win["close"][0]
    last = win["close"][-1]
    return {
        "dominant": counts["label"][0],
        "dist": dist,
        "days": total,
        "index": INDEX,
        "index_return": round((last / first - 1) * 100, 2) if first else None,
        "index_from": str(win["date"][0]),
        "index_to": str(win["date"][-1]),
    }


def by_date(start: str | None, end: str | None) -> dict[date, str]:
    m = labels()
    if not m:
        return {}
    lo = date.fromisoformat(start) if start else min(m)
    hi = date.fromisoformat(end) if end else max(m)
    return {d: v for d, v in m.items() if lo <= d <= hi}
