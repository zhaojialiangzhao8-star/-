"""通达信本地数据源（vipdoc 的 .day / .lc5）—— 完全离线，不依赖网络和协议。

格式（公开、多年未变）：
  .day 每条 32 字节，小端：
    offset 0  uint32 日期 YYYYMMDD
    offset 4  uint32 开（分）
    offset 8  uint32 高（分）
    offset 12 uint32 低（分）
    offset 16 uint32 收（分）
    offset 20 float  成交额（元）
    offset 24 uint32 成交量（股）
    offset 28 uint32 保留

  .lc5 每条 32 字节（5 分钟线）：
    offset 0  uint16 日期编码（(year-2004)*2048 + month*100 + day）
    offset 2  uint16 分钟（hour*60+min）
    offset 4  float  开 / 8 高 / 12 低 / 16 收
    offset 20 float  成交额 / 24 uint32 成交量(股) / 28 uint32 保留

用法：
    python -c "from app.infrastructure.data_sources.vipdoc_local import VipdocProvider; print(VipdocProvider().scan_summary())"
"""
from __future__ import annotations

import struct
from datetime import date
from pathlib import Path

from .base import Bar, Capability, DataProvider, ProviderInfo

DAY = struct.Struct("<IIIIIfII")
LC5 = struct.Struct("<HHfffffII")


class VipdocProvider(DataProvider):
    name = "tdx-vipdoc"

    def __init__(self, root: str | Path = r"D:\new_tdx") -> None:
        self.root = Path(root)
        self.vipdoc = self.root / "vipdoc"

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.DAILY, Capability.MINUTE},
            max_daily_bars=20000,
            max_minute_bars=20000,
            adjust_support={"none"},
            note="通达信本地 vipdoc 文件：离线、全历史、不复权（复权需配合 gbbq）",
        )

    # ------------------------------------------------------------------ 路径
    def _day_path(self, symbol: str) -> Path:
        code, _, mkt = symbol.partition(".")
        mkt = (mkt or "").upper()
        pref = "sh" if (mkt == "SH" or code.startswith(("6", "9", "5"))) else (
            "bj" if (mkt == "BJ" or code.startswith(("4", "8", "92"))) else "sz"
        )
        return self.vipdoc / pref / "lday" / f"{pref}{code}.day"

    def _lc5_path(self, symbol: str) -> Path:
        p = self._day_path(symbol)
        return p.parent.parent / "fzline" / p.name.replace(".day", ".lc5")

    # ------------------------------------------------------------------ 读取
    def fetch_daily(self, symbol: str, bars: int = 20000, adjust: str = "none", **_: object) -> list[Bar]:
        p = self._day_path(symbol)
        if not p.exists():
            raise FileNotFoundError(str(p))
        raw = p.read_bytes()
        n = len(raw) // DAY.size
        out: list[Bar] = []
        start = max(0, n - bars)
        prev = None
        for i in range(start, n):
            d, o, h, l, c, amt, vol, _ = DAY.unpack_from(raw, i * DAY.size)
            close = c / 100.0
            chg = ((close / prev - 1) * 100) if prev else 0.0
            amp = ((h - l) / 100.0 / prev * 100) if prev else 0.0
            out.append(
                Bar(
                    date=f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}",
                    open=o / 100.0,
                    high=h / 100.0,
                    low=l / 100.0,
                    close=close,
                    volume=vol / 100.0,   # 股 → 手（与库内口径一致）
                    amount=float(amt),
                    change_pct=round(chg, 3),
                    amplitude=round(amp, 2),
                    turnover=0.0,
                )
            )
            prev = close
        return out

    def fetch_minute(self, symbol: str, period: int = 5, bars: int = 20000) -> list[Bar]:
        if period != 5:
            raise RuntimeError("vipdoc 只有 1 分钟(.lc1) 和 5 分钟(.lc5)；这里只实现了 5 分钟")
        p = self._lc5_path(symbol)
        if not p.exists():
            raise FileNotFoundError(str(p))
        raw = p.read_bytes()
        n = len(raw) // LC5.size
        out: list[Bar] = []
        for i in range(max(0, n - bars), n):
            dt, minutes, o, h, l, c, amt, vol, _ = LC5.unpack_from(raw, i * LC5.size)
            year = dt // 2048 + 2004
            month = dt % 2048 // 100
            day = dt % 2048 % 100
            t = f"{year:04d}-{month:02d}-{day:02d} {minutes // 60:02d}:{minutes % 60:02d}"
            out.append(
                Bar(
                    date=t,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=vol / 100.0,
                    amount=float(amt),
                    change_pct=0.0,
                    amplitude=0.0,
                    turnover=0.0,
                )
            )
        return out

    # ------------------------------------------------------------------ 体检
    def scan_summary(self) -> dict:
        res = {"root": str(self.root), "sh": 0, "sz": 0, "bj": 0, "lc5": 0, "latest": ""}
        latest = ""
        for mkt in ("sh", "sz", "bj"):
            d = self.vipdoc / mkt / "lday"
            if not d.exists():
                continue
            files = list(d.glob("*.day"))
            res[mkt] = len(files)
            if files:
                newest = max(files, key=lambda f: f.stat().st_mtime)
                res["latest"] = newest.name
        for mkt in ("sh", "sz"):
            d = self.vipdoc / mkt / "fzline"
            if d.exists():
                res["lc5"] += len(list(d.glob("*.lc5")))
        return res
