"""通达信协议源（TCP 7709，pytdx）—— 不依赖客户端、不走 HTTP，所以不受网站限流。

为什么值得接：单位规范（价格=元、成交量=手、成交额=元）、一次能拉全市场，
且和 HTTP 源完全独立 —— 天然适合做「双源比对」的第二个源。

已知限制（写清楚，别踩）：
  · 通达信日线是**不复权**的；后复权要自己用 get_xdxr_info 的除权数据算（尚未实现）
  · 服务器列表会变：优先用本机客户端 D:\\new_tdx\\connect.cfg，其次 pytdx 内置表
  · 如果本机走了全局代理/TUN，7709 端口可能被吞（连上但没数据），需要在代理里放行直连
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from .base import Bar, Capability, DataProvider, ProviderInfo

_clients: dict[str, object] = {}
_lock = threading.Lock()
_good_server: tuple[str, int] | None = None

TDX_DIR = Path("D:/new_tdx")


def _servers() -> list[tuple[str, int]]:
    """候选服务器：先读本机客户端的 connect.cfg（最可能可用），再补 pytdx 内置表。"""
    out: list[tuple[str, int]] = []
    cfg = TDX_DIR / "connect.cfg"
    if cfg.exists():
        try:
            text = cfg.read_text(encoding="gbk", errors="ignore")
            for ip, port in re.findall(r"IPAddress\d+=([\d.]+)\s*\nPort\d+=(\d+)", text):
                out.append((ip, int(port)))
        except OSError:
            pass
    try:
        from pytdx.config.hosts import hq_hosts

        out += [(ip, int(port)) for _, ip, port in hq_hosts]
    except Exception:  # noqa: BLE001
        pass
    # 去重保序
    seen = set()
    uniq = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _connect():
    """找一个真能返回数据的服务器，成功后缓存（下次直接用）。"""
    from pytdx.hq import TdxHq_API

    global _good_server
    with _lock:
        if _good_server is not None:
            api = TdxHq_API()
            try:
                if api.connect(*_good_server, time_out=4):
                    return api
            except Exception:  # noqa: BLE001
                _good_server = None

        for ip, port in _servers():
            api = TdxHq_API()
            try:
                if not api.connect(ip, port, time_out=3):
                    continue
                probe = api.get_security_bars(9, 1, "600519", 0, 2)
                if probe:
                    _good_server = (ip, port)
                    return api
            except Exception:  # noqa: BLE001
                pass
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass
    raise RuntimeError("没有可用的通达信服务器（7709 全被挡？检查代理是否放行直连）")


def _market(symbol: str) -> int:
    code, _, mkt = symbol.partition(".")
    if mkt.upper() == "SH" or code.startswith(("6", "9", "5")):
        return 1
    return 0


class TdxProtocolProvider(DataProvider):
    name = "tdx"

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            capabilities={Capability.DAILY, Capability.MINUTE, Capability.SNAPSHOT},
            max_daily_bars=800,
            max_minute_bars=800,
            adjust_support={"none"},
            note="通达信 TCP 协议（pytdx）：不复权、不依赖客户端、不走 HTTP",
        )

    def fetch_daily(self, symbol: str, bars: int = 800, adjust: str = "none", **_: object) -> list[Bar]:
        if adjust not in ("none", ""):
            raise RuntimeError("通达信协议暂只支持不复权；后复权需要用除权数据自己算")
        api = _connect()
        try:
            code = symbol.split(".")[0]
            rows = api.get_security_bars(9, _market(symbol), code, 0, min(bars, 800)) or []
        finally:
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass
        out: list[Bar] = []
        prev = None
        for r in rows:
            close = float(r["close"])
            chg = ((close / prev - 1) * 100) if prev else 0.0
            out.append(
                Bar(
                    date=str(r["datetime"])[:10],
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=close,
                    volume=float(r["vol"]),          # 手
                    amount=float(r["amount"]),       # 元
                    change_pct=round(chg, 3),
                    amplitude=0.0,
                    turnover=0.0,
                )
            )
            prev = close
        return out

    def fetch_snapshot(self) -> list[dict]:
        """全市场快照：按市场遍历证券列表再批量取报价（较慢，按需调用）。"""
        api = _connect()
        rows: list[dict] = []
        try:
            for market in (0, 1):
                count = api.get_security_count(market) or 0
                codes: list[str] = []
                for start in range(0, min(count, 8000), 1000):
                    batch = api.get_security_list(market, start) or []
                    codes += [b["code"] for b in batch if b.get("code")]
                for i in range(0, len(codes), 80):
                    chunk = [(market, c) for c in codes[i : i + 80]]
                    for q in api.get_security_quotes(chunk) or []:
                        rows.append(
                            {
                                "symbol": f"{q['code']}.{'SH' if market == 1 else 'SZ'}",
                                "price": float(q.get("price") or 0),
                                "prev_close": float(q.get("last_close") or 0),
                                "change_pct": ((float(q["price"]) / float(q["last_close"]) - 1) * 100)
                                if q.get("last_close") and q.get("price")
                                else 0.0,
                                "volume": float(q.get("vol") or 0),
                                "amount": float(q.get("amount") or 0),
                            }
                        )
        finally:
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return rows
