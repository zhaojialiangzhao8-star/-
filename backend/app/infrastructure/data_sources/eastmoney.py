"""东方财富 Provider —— 日线 / 全市场快照 / 指数。

上游就是 push2his.eastmoney.com，和用户终端、InStock 同一家。
关键约束（踩过的坑，别改）：
  · fqt=1（前复权）会把早期价格减成负数 → 默认用 fqt=2（后复权）
  · 成交量字段单位是「手」→ 这里统一 ×100 变成「股」
  · lmt=10000 实际只会返回 6423 根（1999 年至今）
"""
from __future__ import annotations

import json
import time
import urllib.request

from .base import Bar, Capability, DataProvider, ProviderError, ProviderInfo

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

KLINE_URL = (
    "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt={klt}&fqt={fqt}&end=20500101&lmt={bars}"
)
SNAPSHOT_URL = (
    "http://82.push2.eastmoney.com/api/qt/clist/get"
    "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
    "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    "&fields=f12,f14,f2,f3,f5,f6,f7,f8,f13,f20,f21"
)

ADJUST = {"none": 0, "back": 2, "forward": 1}
FREQ = {"daily": 101, 5: 5, 15: 15, 30: 30, 60: 60}


class EastmoneyProvider(DataProvider):
    name = "eastmoney"

    def __init__(self, timeout: int = 25, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.DAILY, Capability.SNAPSHOT, Capability.INDEX},
            max_daily_bars=6423,
            max_minute_bars=256,       # 实测只有 32 个交易日
            adjust_support={"none", "forward", "back"},
            note="日线主力数据源；30 分钟只有 256 根，不够做一年回测",
        )

    # ---------- 内部 ----------
    def _get(self, url: str) -> dict:
        last: Exception | None = None
        for i in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i < self.retries:
                    time.sleep(0.6 * (i + 1))
        raise ProviderError(self.name, f"请求失败: {last}")

    # ---------- 日线 / 分钟 ----------
    def _fetch_kline(self, symbol: str, klt: int, bars: int, adjust: str) -> list[Bar]:
        if adjust not in ADJUST:
            raise ProviderError(self.name, f"不支持的复权口径 {adjust}（可选 {sorted(ADJUST)}）")
        market, code = self.to_secid(symbol)
        url = KLINE_URL.format(
            secid=f"{market}.{code}", klt=klt, fqt=ADJUST[adjust], bars=bars
        )
        payload = self._get(url)
        data = payload.get("data") or {}
        lines = data.get("klines") or []
        if not lines:
            raise ProviderError(self.name, f"{symbol} 无数据（klt={klt} adjust={adjust}）")

        out: list[Bar] = []
        for line in lines:
            p = line.split(",")
            if len(p) < 11:
                continue
            o, c, h, low = float(p[1]), float(p[2]), float(p[3]), float(p[4])
            # 后复权偶发数据错误会把价格算成负数，直接丢弃
            if min(o, c, h, low) <= 0:
                continue
            out.append(
                Bar(
                    date=p[0],
                    open=o, high=h, low=low, close=c,
                    volume=float(p[5]) * 100,   # 手 → 股
                    amount=float(p[6]),
                    change_pct=float(p[8]) if len(p) > 8 else 0.0,
                    amplitude=float(p[7]) if len(p) > 7 else 0.0,
                    turnover=float(p[10]) if len(p) > 10 else 0.0,
                )
            )
        return out

    def fetch_daily(self, symbol: str, bars: int = 5000, adjust: str = "back") -> list[Bar]:
        return self._fetch_kline(symbol, FREQ["daily"], bars, adjust)

    def fetch_minute(self, symbol: str, period: int = 30, bars: int = 256) -> list[Bar]:
        klt = FREQ.get(period)
        if klt is None or klt == 101:
            raise ProviderError(self.name, f"不支持的分钟周期 {period}")
        return self._fetch_kline(symbol, klt, min(bars, 256), "back")

    # ---------- 指数 ----------
    def fetch_index(self, symbol: str, bars: int = 5000) -> list[Bar]:
        code, _, market = symbol.partition(".")
        market = market.upper()
        secid = f"{'1' if market in ('SH', 'SS') else '0'}.{code or symbol}"
        url = KLINE_URL.format(secid=secid, klt=101, fqt=0, bars=bars)
        payload = self._get(url)
        lines = ((payload.get("data") or {}).get("klines")) or []
        if not lines:
            raise ProviderError(self.name, f"指数 {symbol} 无数据")
        out: list[Bar] = []
        for line in lines:
            p = line.split(",")
            if len(p) < 11:
                continue
            out.append(
                Bar(
                    date=p[0], open=float(p[1]), high=float(p[3]),
                    low=float(p[4]), close=float(p[2]),
                    volume=float(p[5]), amount=float(p[6]),
                )
            )
        return out

    # ---------- 快照 ----------
    def fetch_snapshot(self) -> list[dict]:
        out: list[dict] = []
        total = None
        for pn in range(1, 90):
            payload = self._get(SNAPSHOT_URL.format(pn=pn))
            data = payload.get("data") or {}
            if total is None:
                total = data.get("total")
            rows = data.get("diff") or []
            if not rows:
                break
            for r in rows:
                code = r.get("f12")
                mkt = r.get("f13")
                if not code:
                    continue
                suffix = "SH" if mkt == 1 else "SZ"
                out.append(
                    {
                        "symbol": f"{code}.{suffix}",
                        "name": r.get("f14"),
                        "price": r.get("f2"),
                        "change_pct": r.get("f3"),
                        "volume": r.get("f5"),
                        "amount": r.get("f6"),
                        "turnover": r.get("f8"),
                        "market_cap": r.get("f20"),
                    }
                )
            if total and len(out) >= total:
                break
        if not out:
            raise ProviderError(self.name, "快照为空")
        return out


if __name__ == "__main__":  # 自测
    p = EastmoneyProvider()
    print(p.info())
    bars = p.fetch_daily("605258.SH", bars=10)
    print(f"日线 {len(bars)} 根: {bars[0]} ... {bars[-1]}")
    idx = p.fetch_index("000300.SH", bars=5)
    print(f"指数 {len(idx)} 根, 最后一根 {idx[-1]}")
