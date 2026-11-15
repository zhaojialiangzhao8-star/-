"""A 股交易日历 —— 直接从指数日线反推，不硬编码节假日。

为什么这么做：2020-2026 的节假日表要手抄几十个日期，抄错一个就变成「休市日按盘中轮询」。
指数日线（data/index_daily.parquet）本身就是交易所日历：某天有 K 线就是交易日。
数据范围之外（未来）退回「周一至周五」判断，等数据更新后自动变准。
"""
from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path


def _index_daily_path() -> Path:
    return Path(
        os.environ.get("FACET_INDEX_DAILY")
        or (Path(os.environ.get("LOCALAPPDATA", ".")) / "Facet" / "data" / "index_daily.parquet")
    )


@lru_cache(maxsize=1)
def _trading_days() -> tuple[frozenset[date], date, date]:
    """(交易日集合, 最早, 最晚)；读不到就返回空集合。"""
    path = _index_daily_path()
    if not path.exists():
        return frozenset(), date.min, date.min
    try:
        import polars as pl

        df = pl.read_parquet(path, columns=["symbol", "date"])
        # 上证指数覆盖最全，没有就退回全部指数的并集
        sub = df.filter(pl.col("symbol") == "000001.SH")
        if sub.height == 0:
            sub = df
        days = {d if isinstance(d, date) else date.fromisoformat(str(d)[:10]) for d in sub["date"].to_list()}
        if not days:
            return frozenset(), date.min, date.min
        return frozenset(days), min(days), max(days)
    except Exception:  # noqa: BLE001
        return frozenset(), date.min, date.min


def is_trading_day(d: date) -> bool:
    days, lo, hi = _trading_days()
    if days:
        if d in days:
            return True
        if lo <= d <= hi:
            return False  # 在数据范围内却没有 K 线 = 休市（节假日/调休）
    return d.weekday() < 5


def is_holiday(d: date) -> bool:
    """工作日但不是交易日 —— 春节/国庆这类。"""
    return d.weekday() < 5 and not is_trading_day(d)


def calendar_info() -> dict:
    days, lo, hi = _trading_days()
    return {
        "source": "index_daily" if days else "weekday-fallback",
        "days": len(days),
        "from": lo.isoformat() if days else None,
        "to": hi.isoformat() if days else None,
    }
