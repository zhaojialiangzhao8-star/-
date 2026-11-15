"""新浪日线 —— 腾讯不覆盖科创板（688xxx 返回 501）时的兜底源。

`CN_MarketData.getKLineData` 返回 JSON 数组：
    [{"day":"2026-11-09","open":"820.0","high":"879.9","low":"819.32","close":"851.21","volume":"12255979"}, ...]
注意：新浪给的是**前复权**序列，和库里的后复权口径不同。只用于「整只票都缺」的回补
（一只票的整段历史来自同一源，内部收益连续，不影响回测）；不要拿它去补已有票的局部。
"""
from __future__ import annotations

import json
import urllib.request

from .base import Bar, Capability, DataProvider, ProviderInfo

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://finance.sina.com.cn"}
URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={bars}"
)


def _code(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    mkt = (mkt or "").upper()
    if mkt == "SH" or code.startswith(("6", "9")):
        return f"sh{code}"
    if mkt == "BJ" or code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


class SinaDailyProvider(DataProvider):
    name = "sina-daily"

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.DAILY},
            max_daily_bars=1500,
            max_minute_bars=0,
            adjust_support={"forward"},
            note="新浪前复权日线，仅用于整只票回补（腾讯不覆盖 688）",
        )

    def fetch_daily(self, symbol: str, bars: int = 1500, adjust: str = "forward", **_: object) -> list[Bar]:
        url = URL.format(code=_code(symbol), bars=min(bars, 1500))
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            return []
        out: list[Bar] = []
        prev = None
        for r in rows:
            try:
                c = float(r["close"])
                h = float(r["high"])
                l = float(r["low"])
                o = float(r["open"])
                v = float(r["volume"]) / 100  # 股 → 手
            except (KeyError, TypeError, ValueError):
                continue
            chg = (c / prev - 1) * 100 if prev else 0.0
            amp = (h - l) / prev * 100 if prev else 0.0
            out.append(
                Bar(
                    date=str(r.get("day", ""))[:10],
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=v,
                    amount=0.0,
                    amplitude=round(amp, 2),
                    change_pct=round(chg, 2),
                    turnover=0.0,
                )
            )
            prev = c
        return out
