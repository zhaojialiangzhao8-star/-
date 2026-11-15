"""实时行情 —— 只轮询自选股，盘中才跑。

为什么不用全市场：
  东财批量接口 50 只/次，全市场 4,800 只要 96 次请求，一轮 8 秒还没轮到
  下一轮。而实盘真正需要实时的是**你手里和自选里的票**，不是全市场。

数据流：
  东财 ulist.np（50 只/次，0.08s）→ 内存快照 → REST 查询 / SSE 推送

盘中判断：
  9:30-11:30 / 13:00-15:00 才轮询，其余时间停（省资源，也避免拿到陈旧数据）。
  非交易日直接跳过。

能力边界（写清楚，别让人误以为能做逐笔）：
  · 3 秒级快照，不是逐笔成交
  · 没有委托队列，做不了「封板瞬间」「撤单墙」这类分析
  · 免费源，无 SLA
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
ULIST = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?secids={secids}&fields=f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f10,f8,f20"
)

BATCH = 50          # 单次最多查多少只
INTERVAL = 3.0      # 轮询间隔（秒）
MORNING = (dtime(9, 15), dtime(11, 35))
AFTERNOON = (dtime(12, 55), dtime(15, 5))


def to_secid(symbol: str) -> str | None:
    """`600519.SH` → `1.600519`；`000001.SZ` → `0.000001`"""
    code, _, market = symbol.partition(".")
    market = market.upper()
    if not code:
        return None
    if market in ("SH", "SS"):
        return f"1.{code}"
    if market == "SZ":
        return f"0.{code}"
    if market == "BJ":
        return f"0.{code}"
    return f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"


def is_trading_time(now: datetime | None = None) -> bool:
    """盘中（且当天确实是交易日）才返回 True。

    交易日用指数日线反推的真实日历判断：节假日/调休也当休市，
    否则春节、国庆照样每 3 秒轮询一次，白拉接口还显示"交易中"。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:      # 周末
        return False
    try:
        from app.market_calendar import is_trading_day

        if not is_trading_day(now.date()):
            return False
    except Exception:  # noqa: BLE001 — 日历读不到就退回只看 weekday
        pass
    t = now.time()
    return (MORNING[0] <= t <= MORNING[1]) or (AFTERNOON[0] <= t <= AFTERNOON[1])


@dataclass
class Quote:
    symbol: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0       # 手
    amount: float = 0.0       # 元
    turnover: float = 0.0     # %
    vol_ratio: float = 0.0    # 量比
    market_cap: float = 0.0
    pe_ttm: float = 0.0       # 市盈率 TTM（腾讯提供）
    pb: float = 0.0           # 市净率（腾讯提供）
    limit_up: float = 0.0     # 涨停价（腾讯提供）
    limit_down: float = 0.0   # 跌停价（腾讯提供）
    ts: float = 0.0           # 抓取时间戳

    @property
    def age(self) -> float:
        return time.time() - self.ts if self.ts else 9e9


class RealtimeCache:
    """内存快照。所有读取都带时间戳，调用方自己判断新鲜度。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: dict[str, Quote] = {}
        self._last_poll: float = 0.0
        self._last_error: str = ""
        self._polls: int = 0

    def update(self, quotes: list[Quote]) -> None:
        with self._lock:
            now = time.time()
            for q in quotes:
                q.ts = now
                self._quotes[q.symbol] = q
            self._last_poll = now
            self._polls += 1
            self._last_error = ""

    def fail(self, err: str) -> None:
        with self._lock:
            self._last_error = err[:200]

    def get(self, symbols: list[str]) -> list[dict]:
        with self._lock:
            now = time.time()
            out = []
            for s in symbols:
                q = self._quotes.get(s)
                if q is None:
                    out.append({"symbol": s, "available": False})
                    continue
                d = asdict(q)
                d["available"] = True
                d["age_ms"] = int((now - q.ts) * 1000)
                # age_ms 只是「我们多久没拉了」，不是行情自身时间：
                # 非交易时段拉到的就是收盘价，标 closed 让前端别假装"3 秒前"。
                d["session"] = "trading" if is_trading_time() else "closed"
                out.append(d)
            return out

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "count": len(self._quotes),
                "polls": self._polls,
                "last_poll": self._last_poll,
                "last_poll_text": (
                    datetime.fromtimestamp(self._last_poll).strftime("%H:%M:%S") if self._last_poll else "—"
                ),
                "age_sec": round(now - self._last_poll, 1) if self._last_poll else None,
                "trading": is_trading_time(),
                "error": self._last_error,
                "quotes": [
                    {**asdict(q), "age_ms": int((now - q.ts) * 1000)}
                    for q in sorted(self._quotes.values(), key=lambda x: x.symbol)
                ],
            }


CACHE = RealtimeCache()


def fetch_quotes(symbols: list[str]) -> list[Quote]:
    """批量拉取实时报价。

    优先走腾讯（不封 IP，字段含 PE/PB/市值/涨跌停价/量比），
    腾讯失败才回落到东财（有风控，少用）。
    """
    out: list[Quote] = []
    uniq = list(dict.fromkeys(s for s in symbols if s))
    if not uniq:
        return out

    # ---- 首选：腾讯 ----
    try:
        from app.infrastructure.data_sources.registry import registry

        tx = registry.get("tencent")
        raw = tx.fetch_quotes(uniq)
        for d in raw:
            out.append(
                Quote(
                    symbol=d["symbol"],
                    name=d.get("name", ""),
                    price=d.get("price") or 0.0,
                    prev_close=d.get("prev_close") or 0.0,
                    open=d.get("open") or 0.0,
                    high=d.get("high") or 0.0,
                    low=d.get("low") or 0.0,
                    change=d.get("change") or 0.0,
                    change_pct=d.get("change_pct") or 0.0,
                    volume=d.get("volume") or 0.0,
                    amount=(d.get("amount") or 0.0) * 1e4,   # 腾讯给的是万元
                    turnover=d.get("turnover") or 0.0,
                    vol_ratio=d.get("vol_ratio") or 0.0,
                    market_cap=(d.get("total_cap") or 0.0) * 1e8,  # 亿元 → 元
                    pe_ttm=d.get("pe_ttm") or 0.0,
                    pb=d.get("pb") or 0.0,
                    limit_up=d.get("limit_up") or 0.0,
                    limit_down=d.get("limit_down") or 0.0,
                )
            )
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        CACHE.fail(f"tencent: {exc}")

    # ---- 回落：东财 ----
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i : i + BATCH]
        secids = ",".join(x for x in (to_secid(s) for s in chunk) if x)
        if not secids:
            continue
        try:
            req = urllib.request.Request(ULIST.format(secids=secids), headers=UA)
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            CACHE.fail(f"{chunk[0]}... {exc}")
            continue
        for d in (payload.get("data") or {}).get("diff") or []:
            code = str(d.get("f12") or "")
            mkt = "SH" if str(d.get("f13", 1)) == "1" else "SZ"
            symbol = next((s for s in chunk if s.startswith(code)), f"{code}.{mkt}")

            def num(v, scale=100.0):
                try:
                    v = float(v)
                    return v / scale if scale else v
                except (TypeError, ValueError):
                    return 0.0

            out.append(
                Quote(
                    symbol=symbol,
                    name=str(d.get("f14") or ""),
                    price=num(d.get("f2")),
                    change_pct=num(d.get("f3")),
                    change=num(d.get("f4")),
                    volume=num(d.get("f5"), 0),
                    amount=num(d.get("f6"), 0),
                    turnover=num(d.get("f8")),
                    vol_ratio=num(d.get("f10")),
                    high=num(d.get("f15")),
                    low=num(d.get("f16")),
                    open=num(d.get("f17")),
                    prev_close=num(d.get("f18")),
                    market_cap=num(d.get("f20"), 0),
                )
            )
    return out


class Poller:
    """后台轮询线程。只在盘中跑，symbols 由外部（自选股表）提供。"""

    def __init__(self, symbols_fn, interval: float = INTERVAL) -> None:
        self.symbols_fn = symbols_fn
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rt-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not is_trading_time():
                    continue
                symbols = self.symbols_fn()
                if not symbols:
                    continue
                quotes = fetch_quotes(symbols)
                if quotes:
                    CACHE.update(quotes)
            except Exception as exc:  # noqa: BLE001
                CACHE.fail(str(exc))
