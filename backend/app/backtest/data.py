"""行情读取 — 把 parquet 代际展开成按日期索引的 Bar 表。

内存取舍（V2 重写，全市场实测倒逼）：
  旧实现把每一行都转成 Python `Bar` 对象存进 `dict[date][symbol]`。
  200 万行时约 600 MB，还能用；全市场 1150 万行时峰值超过 3 GB，进程直接 OOM。

  新实现改为：
    · 底层只保留一份 polars DataFrame（全市场 9 列约 400 MB）
    · 用 `partition_by("date")` 一次性切成按日分片（polars 内部零拷贝）
    · `day(d)` 时才把当天那 5,000 行转成 Bar —— 用完即弃，不缓存
      （回测每天访问一次，日均 5ms，730 天也只要 4 秒）

  对外接口与原实现完全一致：calendar / symbols / day / bar / closes / next_date / slice。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from .models import Bar

BAR_COLS = ["open", "high", "low", "close", "volume", "amount", "change_pct", "turnover"]


class BarStore:
    def __init__(self, frame: pl.DataFrame) -> None:
        self._build(frame)

    def _build(self, frame: pl.DataFrame) -> None:
        have = [c for c in BAR_COLS if c in frame.columns]
        cols = ["symbol", "date", *have]
        has_pre = "pre_close" in frame.columns
        if has_pre:
            cols.append("pre_close")
        df = frame.select(cols).sort(["date", "symbol"])

        # pre_close 用前一日收盘（按 symbol 前移一行）；原表自带就用原表
        df = df.with_columns(
            (pl.col("pre_close") if has_pre else pl.col("close").shift(1).over("symbol")).alias("_pre")
        )

        # 一次性切分成「日期 → 当日所有标的」
        parts = df.partition_by("date", as_dict=True, maintain_order=True)
        self._parts: dict[date, pl.DataFrame] = {
            (k[0] if isinstance(k, tuple) else k): v for k, v in parts.items()
        }
        self._calendar: list[date] = sorted(self._parts)
        self._index: dict[date, int] = {d: i for i, d in enumerate(self._calendar)}
        self._symbols: list[str] = sorted(df["symbol"].unique().to_list())
        self._columns = list(df.columns)

    # ---------- 元信息 ----------
    @property
    def calendar(self) -> list[date]:
        return self._calendar

    @property
    def symbols(self) -> list[str]:
        return self._symbols

    # ---------- 按日取 ----------
    def day(self, d: date) -> dict[str, Bar]:
        """当日的 {symbol: Bar}。每次现造（不缓存），内存恒定。"""
        part = self._parts.get(d)
        if part is None:
            return {}
        base = datetime.combine(d, datetime.min.time())
        out: dict[str, Bar] = {}
        for row in part.iter_rows(named=True):
            out[row["symbol"]] = Bar(
                symbol=row["symbol"],
                dt=base,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0),
                amount=float(row.get("amount") or 0),
                pre_close=float(row.get("_pre") or row["close"]),
                change_pct=float(row.get("change_pct") or 0),
                turnover=float(row.get("turnover") or 0),
            )
        return out

    def bar(self, d: date, symbol: str) -> Bar | None:
        return self.day(d).get(symbol)

    def closes(self, d: date) -> dict[str, float]:
        part = self._parts.get(d)
        if part is None:
            return {}
        return dict(zip(part["symbol"].to_list(), part["close"].to_list()))

    def next_date(self, d: date) -> date | None:
        i = self._index.get(d)
        if i is None or i + 1 >= len(self._calendar):
            return None
        return self._calendar[i + 1]

    def slice(self, start: date | None = None, end: date | None = None) -> "BarStore":
        keep = [d for d in self._calendar if (not start or d >= start) and (not end or d <= end)]
        df = pl.concat([self._parts[d] for d in keep]) if keep else self._parts[self._calendar[0]].clear()
        return BarStore(df.select([c for c in self._columns if c != "_pre"]))
