"""parquet 读写契约 — schema 只增不改，扫描端容忍列差异。

抄的是 TSP 的思路：
    missing_columns="insert"  -> 旧文件缺新列时补 null，不报错
    extra_columns="ignore"    -> 新文件多出的列直接忽略
这样「加字段」「换数据源」「升级版本」都不会让历史 parquet 变成读不了的垃圾。
"""
from __future__ import annotations

from typing import Any

import polars as pl

# 落盘的原始日线：只有事实，没有派生
DAILY_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}

# 加工后的 enriched：策略/回测/监控唯一读取口径
ENRICHED_SCHEMA: dict[str, pl.DataType] = {
    **DAILY_SCHEMA,
    "ret_1d": pl.Float64,
    "ma5": pl.Float64,
    "ma10": pl.Float64,
    "ma20": pl.Float64,
    "ma60": pl.Float64,
    "vol_ma5": pl.Float64,
    "vol_ma20": pl.Float64,
    "high_20": pl.Float64,
    "low_20": pl.Float64,
    "amount_ma5": pl.Float64,
    "ret_20d": pl.Float64,
    "turnover_proxy": pl.Float64,
    # —— 以下为 2026-11-09 追加：真实行情字段（东财原始口径）——
    "change_pct": pl.Float64,  # 当日涨跌幅 %（东财 f59）
    "amplitude": pl.Float64,  # 当日振幅 %（东财 f58）
    "turnover": pl.Float64,  # 当日换手率 %（东财 f61）
    "benchmark": pl.Float64,  # 基准指数：**两种口径都可能**——fetch_5y/history 落的是点位(3000+)，
    #                          fetch_all_market 落的是当日涨跌幅(%)。用它之前先判断量级（见 terminal_picks 的 _bm_ret）。
    # ---- 2026-11-15 追加：vipdoc 口径（原始价 + 后复权价并存）----
    "adj_factor": pl.Float64,  # 后复权因子（gbbq 算出来的）
    "adj_close": pl.Float64,   # 后复权收盘价 = close * adj_factor（回测用）
}


def scan_compat(source: Any, schema: dict[str, pl.DataType], **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("missing_columns", "insert")
    kwargs.setdefault("extra_columns", "ignore")
    kwargs.setdefault("schema", schema)
    return pl.scan_parquet(source, **kwargs)


def scan_daily(source: Any) -> pl.LazyFrame:
    return scan_compat(source, DAILY_SCHEMA)


def scan_enriched(source: Any) -> pl.LazyFrame:
    return scan_compat(source, ENRICHED_SCHEMA)
