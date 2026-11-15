"""腾讯财经 Provider —— 实时行情 + 估值 + 涨跌停价。

为什么加这一家（来自 a-stock-data 的实测结论）：
  · 腾讯 **不封 IP**，可以高频轮询；东财有风控（>5/秒、≥10 并发会封）
  · 字段比东财丰富：PE(TTM) / PB / 流通市值 / 总市值 / 涨停价 / 跌停价 / 量比

接口：`https://qt.gtimg.cn/q=sh600519,sz000001`（GBK，~ 分隔，88 个字段）
实测字段位置（2026-11-11 校准，网上很多教程是错的）：
  [1] 名称  [2] 代码  [3] 现价  [4] 昨收  [5] 今开  [6] 成交量(手)
  [31] 涨跌额  [32] 涨跌幅%  [33] 最高  [34] 最低
  [37] 成交额(万)  [38] 换手率%  [39] 市盈率TTM  [43] 振幅%
  [44] 流通市值(亿)  [45] 总市值(亿)  [46] PB  [47] 涨停价  [48] 跌停价  [49] 量比
"""
from __future__ import annotations

import urllib.request
from dataclasses import asdict, dataclass

from .base import Capability, DataProvider, ProviderError, ProviderInfo

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://gu.qq.com/"}
URL = "https://qt.gtimg.cn/q={codes}"
BATCH = 60


def tx_code(symbol: str) -> str:
    """`600519.SH` → `sh600519`"""
    code, _, market = symbol.partition(".")
    m = market.upper()
    prefix = "sh" if m in ("SH", "SS") else "bj" if m == "BJ" else "sz"
    if not market:
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return f"{prefix}{code}"


@dataclass
class TxQuote:
    symbol: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0        # 手
    amount: float = 0.0        # 万元
    turnover: float = 0.0      # %
    amplitude: float = 0.0     # %
    pe_ttm: float = 0.0
    pb: float = 0.0
    float_cap: float = 0.0     # 亿元
    total_cap: float = 0.0     # 亿元
    limit_up: float = 0.0
    limit_down: float = 0.0
    vol_ratio: float = 0.0
    ts: str = ""


class TencentProvider(DataProvider):
    name = "tencent"

    def __init__(self, timeout: int = 8) -> None:
        self.timeout = timeout

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.SNAPSHOT},
            max_daily_bars=0,
            max_minute_bars=0,
            adjust_support=set(),
            note="实时报价 + PE/PB/市值/涨跌停价/量比；不封 IP，适合高频轮询",
        )

    # ---------- 单批 ----------
    def _fetch(self, symbols: list[str]) -> list[TxQuote]:
        out: list[TxQuote] = []
        for i in range(0, len(symbols), BATCH):
            chunk = symbols[i : i + BATCH]
            codes = ",".join(tx_code(s) for s in chunk)
            try:
                req = urllib.request.Request(URL.format(codes=codes), headers=UA)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("gbk", errors="replace")
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(self.name, f"请求失败: {exc}") from exc

            for line in raw.splitlines():
                if '="' not in line:
                    continue
                key = line.split('="', 1)[0].replace("v_", "").strip()
                body = line.split('="', 1)[1].rstrip('";').strip()
                if not body:
                    continue
                f = body.split("~")
                if len(f) < 50:
                    continue

                def num(idx: int) -> float:
                    try:
                        return float(f[idx])
                    except (IndexError, TypeError, ValueError):
                        return 0.0

                code = f[2]
                market = "SH" if key.startswith("sh") else "BJ" if key.startswith("bj") else "SZ"
                out.append(
                    TxQuote(
                        symbol=f"{code}.{market}",
                        name=f[1],
                        price=num(3), prev_close=num(4), open=num(5),
                        volume=num(6),
                        change=num(31), change_pct=num(32),
                        high=num(33), low=num(34),
                        amount=num(37), turnover=num(38),
                        pe_ttm=num(39), amplitude=num(43),
                        float_cap=num(44), total_cap=num(45), pb=num(46),
                        limit_up=num(47), limit_down=num(48),
                        vol_ratio=num(49), ts=f[30] if len(f) > 30 else "",
                    )
                )
        return out

    def fetch_snapshot(self) -> list[dict]:
        raise ProviderError(self.name, "不支持全市场快照，请用 eastmoney")

    def fetch_quotes(self, symbols: list[str]) -> list[dict]:
        """批量实时报价（含估值）。腾讯不封 IP，可以按秒级轮询。"""
        if not symbols:
            return []
        return [asdict(q) for q in self._fetch(symbols)]


if __name__ == "__main__":  # 自测
    p = TencentProvider()
    print(p.info())
    for q in p.fetch_quotes(["600519.SH", "000001.SZ", "605258.SH"]):
        print(
            f"  {q['symbol']:<12} {q['name']:<8} 现价 {q['price']:>8.2f}  {q['change_pct']:>+6.2f}%  "
            f"PE {q['pe_ttm']:>7.2f}  PB {q['pb']:>6.2f}  总市值 {q['total_cap']:>8.1f}亿  "
            f"涨停 {q['limit_up']:>7.2f} 跌停 {q['limit_down']:>7.2f}  量比 {q['vol_ratio']:>4.2f}"
        )
