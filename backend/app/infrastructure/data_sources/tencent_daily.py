"""腾讯日线（前复权/后复权/不复权）—— 东财被限流时的备份源。

`/appstock/app/fqkline/get` 返回：
    data["sh600519"]["qfqday"] = [[date, open, close, high, low, volume(手)], ...]
注意字段顺序是 开/收/高/低（不是 OHLC），成交额拿到的是「元」的时候才有第 7 列，
没有就置 0 —— 成交额另有 data/real_amount 快照，不依赖这里。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date

from .base import Bar, Capability, DataProvider, ProviderInfo

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://gu.qq.com/"}
URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={code},day,,,{bars},{adj}"
)


def _code(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    mkt = (mkt or "").upper()
    if mkt == "SH" or code.startswith(("6", "9")):
        return f"sh{code}"
    if mkt == "BJ" or code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


class TencentDailyProvider(DataProvider):
    name = "tencent-daily"

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.DAILY},
            max_daily_bars=3000,
            max_minute_bars=0,
            adjust_support={"back", "forward", "none"},
            note="腾讯前复权日线，东财 502 时的备份源（无成交额时置 0）",
        )

    def fetch_daily(self, symbol: str, bars: int = 1500, adjust: str = "back", **_: object) -> list[Bar]:
        # back 必须用 hfq（后复权），否则和库里 6.3M 行（后复权价）混在一起会出现 -90% 的假跌
        adj = {"back": "hfq", "forward": "qfq", "none": ""}.get(adjust, "hfq")
        url = URL.format(code=_code(symbol), bars=bars, adj=adj)
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        node = (payload.get("data") or {}).get(_code(symbol)) or {}
        # hfq → "hfqday"，qfq → "qfqday"，不复权 → "day"
        rows = node.get(f"{adj}day") or node.get("day") or []
        out: list[Bar] = []
        prev_close = None

        def _f(v) -> float | None:
            """腾讯偶尔把某个字段塞成 dict/字符串，统一安全转换。"""
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v)
                except ValueError:
                    return None
            return None

        for r in rows:
            # 停牌/异常日腾讯会塞 dict 而不是 list，跳过即可
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            o, c, h, l, v = (_f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]))
            if None in (o, c, h, l, v) or not c or not prev_close and False:
                continue
            d = r[0]
            amt = _f(r[6]) if len(r) > 6 else 0.0
            chg = ((c / prev_close - 1) * 100) if prev_close else 0.0
            amp = ((h - l) / prev_close * 100) if prev_close else 0.0
            out.append(
                Bar(
                    date=str(d),
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=v,
                    amount=amt,
                    amplitude=round(amp, 2),
                    change_pct=round(chg, 2),
                    turnover=0.0,
                )
            )
            prev_close = c
        return out
