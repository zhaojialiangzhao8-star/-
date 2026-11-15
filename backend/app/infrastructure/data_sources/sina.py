"""新浪 Provider —— 30 分钟线（免费源里最长：1,023 根 ≈ 6 个月）。

为什么单独做一家：东财 30 分钟只有 256 根（32 个交易日），腾讯 320 根，
新浪 1,023 根是免费源里能拿到最长的。做 30 分钟回测时优先用它。

接口：`https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData`
参数：symbol（sh600519 / sz000001）、scale（分钟周期）、datalen（根数，上限 1023）
返回：JSON 数组，每项含 day / open / high / low / close / volume
注意：新浪的 volume 单位是「股」，不需要 ×100。
"""
from __future__ import annotations

import json
import time
import urllib.request

from .base import Bar, Capability, DataProvider, ProviderError, ProviderInfo

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
    "?symbol={symbol}&scale={scale}&ma=no&datalen={datalen}"
)

MAX_BARS = 1023


def sina_symbol(symbol: str) -> str:
    """`600519.SH` → `sh600519`"""
    code, _, market = symbol.partition(".")
    m = market.upper()
    prefix = "sh" if m in ("SH", "SS") else "bj" if m == "BJ" else "sz"
    if not market:
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return f"{prefix}{code}"


class SinaProvider(DataProvider):
    name = "sina"

    def __init__(self, timeout: int = 20, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.MINUTE},
            max_daily_bars=0,
            max_minute_bars=MAX_BARS,
            adjust_support={"none"},  # 新浪返回不复权
            note="30 分钟最长 1,023 根（约 6 个月），免费源里最长；不复权",
        )

    def _get(self, url: str) -> list[dict]:
        last: Exception | None = None
        for i in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace").strip()
                if not text or text in ("null", "[]"):
                    raise ProviderError(self.name, "返回为空")
                return json.loads(text)
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i < self.retries:
                    time.sleep(0.6 * (i + 1))
        raise ProviderError(self.name, f"请求失败: {last}")

    def fetch_minute(self, symbol: str, period: int = 30, bars: int = MAX_BARS) -> list[Bar]:
        if period not in (5, 15, 30, 60):
            raise ProviderError(self.name, f"不支持 {period} 分钟（可选 5/15/30/60）")
        rows = self._get(URL.format(symbol=sina_symbol(symbol), scale=period, datalen=min(bars, MAX_BARS)))
        out: list[Bar] = []
        for r in rows:
            try:
                o, h, low, c = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            except (KeyError, TypeError, ValueError):
                continue
            if min(o, h, low, c) <= 0:
                continue
            vol = float(r.get("volume") or 0)   # 新浪是股，不用乘 100
            out.append(
                Bar(
                    date=str(r.get("day") or ""),   # 新浪给 "2026-11-09 14:30:00"
                    open=o, high=h, low=low, close=c,
                    volume=vol,
                    amount=vol * c,
                )
            )
        if not out:
            raise ProviderError(self.name, f"{symbol} 分钟线为空")
        return out


if __name__ == "__main__":  # 自测
    p = SinaProvider()
    print(p.info())
    try:
        bars = p.fetch_minute("605258.SH", 30, 20)
        print(f"30 分钟 {len(bars)} 根: {bars[0].date} → {bars[-1].date}")
    except ProviderError as exc:
        print("取数失败:", exc)
