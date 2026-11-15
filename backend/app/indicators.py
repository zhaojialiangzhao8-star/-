"""指标流水线 — 原始日线 -> enriched（策略/回测/监控唯一读取口径）。

一条硬规则：策略里禁止自己算均线、自己拼 join。
所有派生列在这里一次算完落盘，任何消费方都只读这张表，
否则「策略看到的 20 日均线」和「回测看到的 20 日均线」迟早对不上。
"""
from __future__ import annotations

import polars as pl

WARMUP_BARS = 60

# 复权序列可信度阈值：一只票 5 年内「最高收盘 / 最低收盘」超过这个倍数，
# 基本可以断定后复权拼接错乱（实测 600601.SH 达到 304 倍，
# 2026-11-12 那天 close 从 11 万掉到 575，跌幅 -99.5%）。
# A 股 5 年真涨 30 倍的极少数存在，但那种票的日线不会出现这种断崖。
MAX_PRICE_RATIO = 30.0


def _drop_corrupted(df: pl.DataFrame, max_ratio: float = MAX_PRICE_RATIO) -> pl.DataFrame:
    """剔除复权序列被污染的标的。

    腾讯 hfq（以及东财 fqt=2）在某些股票上返回的是分段拼接的后复权，
    不同段用的复权基准不一致，导致序列里混进完全不同的价格量级。
    这种数据拿来算均线、画 K 线、判涨停全是错的，只能整只丢掉。

    实测 4800 只里有 14 只（0.3%）命中，丢掉不影响整体。
    """
    stat = (
        df.group_by("symbol")
        .agg(
            pl.col("close").max().alias("_mx"),
            pl.col("close").min().alias("_mn"),
        )
        .with_columns((pl.col("_mx") / pl.col("_mn")).alias("_ratio"))
    )
    bad = stat.filter(pl.col("_ratio") > max_ratio)["symbol"].to_list()
    if not bad:
        return df
    import logging

    logging.getLogger("facet").warning(
        "剔除 %d 只复权序列异常的标的（收盘极差 > %.0f 倍）：%s",
        len(bad),
        max_ratio,
        ", ".join(bad[:10]) + (" …" if len(bad) > 10 else ""),
    )
    return df.filter(~pl.col("symbol").is_in(bad))


def build_enriched(daily: pl.DataFrame, max_price_ratio: float = MAX_PRICE_RATIO) -> pl.DataFrame:
    """算派生列。max_price_ratio 用于剔除被污染的序列：
    后复权口径下 30 倍够用；**原始价 + 十几年历史**时正常股票也能涨几十倍，应传更大的值。
    """
    df = _drop_corrupted(daily.sort(["symbol", "date"]), max_price_ratio)
    close = pl.col("close")
    return df.with_columns(
        (close / close.shift(1).over("symbol") - 1).alias("ret_1d"),
        close.rolling_mean(5).over("symbol").alias("ma5"),
        close.rolling_mean(10).over("symbol").alias("ma10"),
        close.rolling_mean(20).over("symbol").alias("ma20"),
        close.rolling_mean(60).over("symbol").alias("ma60"),
        pl.col("volume").rolling_mean(5).over("symbol").alias("vol_ma5"),
        pl.col("volume").rolling_mean(20).over("symbol").alias("vol_ma20"),
        pl.col("high").rolling_max(20).over("symbol").alias("high_20"),
        pl.col("low").rolling_min(20).over("symbol").alias("low_20"),
        pl.col("amount").rolling_mean(5).over("symbol").alias("amount_ma5"),
        (close / close.shift(20).over("symbol") - 1).alias("ret_20d"),
        # 换手率代理：成交额 / 20 日均额（真实项目里用流通市值）
        (pl.col("amount") / pl.col("amount").rolling_mean(20).over("symbol")).alias(
            "turnover_proxy"
        ),
        # ---- 下面三列是后加的（ENRICHED_SCHEMA 只允许追加）----
        # change_pct：涨跌幅百分数。策略和回测都按「≥9.8 = 涨停」判断，
        # 用 ret_1d（小数）容易和 0.098 混淆，所以单独落一列。
        #
        # 注意异常值：后复权数据在除权日会跳变（腾讯/东财都是分段拼接），
        # 会产生 +183%、-61% 这种不可能的日涨幅。A 股单日最大 20%（科创/创业），
        # 所以 |chg| > 21% 一律判为复权跳变，置 null —— 让策略自己跳过，
        # 不要让它把假涨停当成信号。
        pl.when((close / close.shift(1).over("symbol") - 1).abs() <= 0.21)
        .then((close / close.shift(1).over("symbol") - 1).mul(100))
        .otherwise(None)
        .alias("change_pct"),
        # amplitude：振幅 =（最高-最低）/ 昨收 × 100
        ((pl.col("high") - pl.col("low")) / close.shift(1).over("symbol")).mul(100).alias("amplitude"),
        # turnover：换手率代理（成交额 / 20 日均额 × 100）。
        # 没有流通股本，这个数不是真实换手率，只能当「相对活跃度」看。
        (pl.col("amount") / pl.col("amount").rolling_mean(20).over("symbol")).mul(100).alias("turnover"),
    ).drop_nulls(["ma20", "vol_ma20"])


def _real_amount_yi() -> float | None:
    """读最近一次抓的「真实成交额」快照（新浪行情，未复权）。

    库里的 amount 是后复权口径（复权价 × 成交量），比真实成交额大几十倍，
    所以页面要展示的是这份快照的合计。抓取脚本：scripts/fetch_real_amount.py。

    候选路径：开发环境 DATA_DIR 指向 backend/data，但快照实际落在
    %LOCALAPPDATA%/Facet/data（打包后的运行目录），所以要按顺序找。
    """
    import json
    import os
    from pathlib import Path

    from . import config

    cands = [Path(config.DATA_DIR) / "real_amount"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Facet" / "data" / "real_amount")
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "data" / "real_amount")

    d = next((c for c in cands if c.exists()), None)
    if d is None:
        return None
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    try:
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        total = sum(float(v) for v in data.values())
    except Exception:  # noqa: BLE001
        return None
    return round(total / 1e8, 2) if total > 0 else None


def market_overview(enriched: pl.DataFrame) -> dict:
    latest_date = enriched["date"].max()
    snap = enriched.filter(pl.col("date") == latest_date)
    advancers = snap.filter(pl.col("ret_1d") > 0).height
    decliners = snap.filter(pl.col("ret_1d") < 0).height
    # 优先用真实成交额快照；没有就退回复权口径（并标注，避免被当成真实金额）
    real_yi = _real_amount_yi()
    amount_yi = real_yi if real_yi is not None else round(float(snap["amount"].sum()) / 1e8, 2)
    return {
        "date": str(latest_date),
        "symbols": snap.height,
        "advancers": advancers,
        "decliners": decliners,
        "limit_ups": snap.filter(pl.col("ret_1d") >= 0.095).height,
        "limit_downs": snap.filter(pl.col("ret_1d") <= -0.095).height,
        "amount_yi": amount_yi,
        "amount_real": real_yi is not None,
        "median_ret_pct": round(float(snap["ret_1d"].median()) * 100, 2),
        "breadth_pct": round(advancers / max(snap.height, 1) * 100, 1),
    }
