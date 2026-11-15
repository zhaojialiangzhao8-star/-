"""通达信本地数据 Provider —— 读 `D:\\new_tdx\\vipdoc` 里的 .day / .lc5 文件。

为什么加这条路（来自 a-stock-data 的实测结论）：通达信协议（mootdx / pytdx）
不封 IP，而东财有风控（>5 次/秒、≥10 并发会封）。但实测本机的 mootdx 在线模式
连不上任何行情主站（8 个服务器只有 2 个 TCP 通，握手后全部返回 0 条），
所以走**离线读本地文件**这条路：只要通达信下载过盘后数据，就能秒级读全市场。

前置条件（用户操作，一次性）：
    通达信 → 系统 → 盘后数据下载 → 勾选「日线」和「分钟线」→ 下载
    下载完 `D:\\new_tdx\\vipdoc\\sh\\lday\\*.day` 会有文件

数据特性（必须知道）：
    · **不复权**。通达信本地存的是原始价，除权日会有跳空。
      要复权得自己按分红送转表算，或者跟 eastmoney 的后复权数据对齐。
    · 日线文件 .day 是 32 字节定长记录：date(u32) open(u32) high(u32) low(u32)
      close(u32) amount(f32) volume(u32) reserved(u32)，价格 ×100。
    · 分钟线 .lc5 是 32 字节：date(u16) time(u16) open(f32) high(f32)
      low(f32) close(f32) amount(f32) volume(u32) reserved(u32)。
    · 只交易日有记录，停牌日没有 —— 下游要自己按交易日历补齐。

用法：
    from app.infrastructure.data_sources.tdx_local import TdxLocalProvider
    p = TdxLocalProvider(r"D:\\new_tdx")
    p.info()                      # 看覆盖情况（多少个 .day 文件）
    p.fetch_daily("600519.SH")    # 日线
    p.fetch_bars("600519.SH", "5m", limit=200)   # 5 分钟
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .base import Capability, DataProvider, ProviderError, ProviderInfo

DAY_FMT = "<IIIIIfII"      # 32 字节
LC_FMT = "<HHfffffII"      # 32 字节


@dataclass
class TdxBar:
    symbol: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float      # 股（通达信给手，×100）
    amount: float


def _market_dir(symbol: str) -> str:
    """`600519.SH` → `sh`"""
    code, _, mkt = symbol.partition(".")
    m = mkt.upper()
    if m in ("SH", "SS"):
        return "sh"
    if m == "BJ":
        return "bj"
    if m == "SZ":
        return "sz"
    # 没写市场就按代码前缀猜
    return "sh" if code.startswith(("6", "9", "5")) else "bj" if code.startswith(("4", "8")) else "sz"


class TdxLocalProvider(DataProvider):
    name = "tdx"

    def __init__(self, tdxdir: str | Path | None = None) -> None:
        # 安装路径可用环境变量覆盖：set TDX_DIR=E:\other_tdx
        self.tdxdir = Path(tdxdir or os.getenv("TDX_DIR") or r"D:\new_tdx")
        self.vipdoc = self.tdxdir / "vipdoc"

    def info(self) -> ProviderInfo:
        counts = {}
        for mkt in ("sh", "sz", "bj"):
            d = self.vipdoc / mkt / "lday"
            counts[mkt] = len(list(d.glob("*.day"))) if d.is_dir() else 0
        total = sum(counts.values())
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.DAILY, Capability.MINUTE} if total else set(),
            max_daily_bars=0,        # 本地文件，不限
            max_minute_bars=0,
            adjust_support={"none"},  # 本地数据不复权
            note=(
                f"通达信本地 vipdoc · 日线 {counts['sh']}/{counts['sz']}/{counts['bj']} 个文件。"
                if total
                else "通达信本地数据未下载：通达信 → 系统 → 盘后数据下载"
            ),
        )

    # ---------- 日线 ----------
    def _day_path(self, symbol: str) -> Path:
        mkt = _market_dir(symbol)
        code = symbol.split(".")[0]
        return self.vipdoc / mkt / "lday" / f"{mkt}{code}.day"

    def fetch_daily(self, symbol: str) -> list[TdxBar]:
        p = self._day_path(symbol)
        if not p.exists():
            raise ProviderError(self.name, f"没有 {p.name}（通达信里下载过盘后数据吗？）")
        raw = p.read_bytes()
        n = len(raw) // 32
        out: list[TdxBar] = []
        for i in range(n):
            d, o, h, l, c, amt, vol, _ = struct.unpack_from(DAY_FMT, raw, i * 32)
            y, mo, dy = d // 10000, (d // 100) % 100, d % 100
            if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= dy <= 31):
                continue
            out.append(
                TdxBar(
                    symbol=symbol,
                    dt=datetime(y, mo, dy),
                    open=o / 100, high=h / 100, low=l / 100, close=c / 100,
                    volume=float(vol) * 100,     # 手 → 股
                    amount=float(amt),
                )
            )
        return out

    # ---------- 分钟线 ----------
    def fetch_bars(self, symbol: str, freq: str = "5m", limit: int | None = None) -> list[TdxBar]:
        """freq: 5m / 15m / 30m / 60m / 1m —— 对应 .lc5 / .lc1 文件。"""
        mkt = _market_dir(symbol)
        code = symbol.split(".")[0]
        fname = {"1m": "lc1"}.get(freq, "lc5")
        p = self.vipdoc / mkt / "fzline" / f"{mkt}{code}.{fname}"
        if not p.exists():
            raise ProviderError(self.name, f"没有 {p.name}（分钟线需单独下载）")
        raw = p.read_bytes()
        n = len(raw) // 32
        out: list[TdxBar] = []
        for i in range(n):
            d, t, o, h, l, c, amt, vol, _ = struct.unpack_from(LC_FMT, raw, i * 32)
            y, mo, dy = d // 2048 + 2004, (d % 2048) // 100, (d % 2048) % 100
            hh, mm = t // 60, t % 60
            try:
                dt = datetime(y, mo, dy, hh, mm)
            except ValueError:
                continue
            out.append(
                TdxBar(symbol=symbol, dt=dt, open=o, high=h, low=l, close=c,
                       volume=float(vol) * 100, amount=float(amt))
            )
        return out[-limit:] if limit else out

    # ---------- 覆盖情况 ----------
    def coverage(self) -> dict:
        """各市场有多少只股票有日线文件，以及最后一根 K 线的日期。"""
        rows = []
        for mkt in ("sh", "sz", "bj"):
            d = self.vipdoc / mkt / "lday"
            if not d.is_dir():
                continue
            files = list(d.glob("*.day"))
            latest = 0
            for f in files[:50]:      # 抽 50 个看最新日期
                try:
                    raw = f.read_bytes()[-32:]
                    dd = struct.unpack_from(DAY_FMT, raw, 0)[0]
                    latest = max(latest, dd)
                except Exception:  # noqa: BLE001
                    continue
            rows.append({
                "market": mkt,
                "symbols": len(files),
                "latest": f"{latest // 10000}-{latest // 100 % 100:02d}-{latest % 100:02d}" if latest else None,
            })
        return {"tdxdir": str(self.tdxdir), "markets": rows}


if __name__ == "__main__":  # 自测
    p = TdxLocalProvider()
    print(p.info())
    print(p.coverage())
    for sym in ("600519.SH", "000001.SZ"):
        try:
            bars = p.fetch_daily(sym)
            print(f"  {sym}: {len(bars)} 根，最新 {bars[-1].dt.date()} 收 {bars[-1].close}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: {exc}")
