"""个股档案 —— 名称、五档盘口、关键数据、所属板块。

数据来源分工（都是实测过能用的）：
  · 名称 / 现价 / 五档 / PE / PB / 市值 / 涨跌停 / 量比 → 腾讯 qt.gtimg.cn
    字段位置见 tencent.py 的注释；这里取前 30 个（含五档）和 30-50 段。
  · 所属板块 → 东财 datacenter RPT_F10_CORETHEME_BOARDTYPE
    东财 push2 被限流（RemoteDisconnected），但 datacenter 一直可用。

腾讯五档字段（q=sh600519 的 88 个 ~ 分隔字段）：
  [9] 买一价  [10] 买一量(手)   [11] 买二价 [12] 买二量 ...
  [19] 卖一价 [20] 卖一量       [21] 卖二价 [22] 卖二量 ...
  注意：腾讯是「先买后卖」，和东财的「先卖后买」顺序相反。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))

router = APIRouter(tags=["stock"])

UA_TX = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://gu.qq.com/"}
UA_EM = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://data.eastmoney.com/"}

TX_URL = "https://qt.gtimg.cn/q={code}"
BOARD_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
    "?reportName=RPT_F10_CORETHEME_BOARDTYPE"
    "&columns=SECUCODE,SECURITY_NAME_ABBR,BOARD_NAME,BOARD_CODE,IS_PRECISE"
    '&filter=(SECUCODE%3D%22{sym}%22)'
    "&pageSize=50&pageNumber=1&source=WEB&client=WEB"
)


def _tx_code(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    m = mkt.upper()
    prefix = "sh" if m in ("SH", "SS") else "bj" if m == "BJ" else "sz"
    if not mkt:
        prefix = "sh" if code.startswith(("6", "9", "5")) else "sz"
    return f"{prefix}{code}"


def _num(f: list[str], i: int, scale: float = 1.0) -> float:
    try:
        return round(float(f[i]) * scale, 4)
    except (IndexError, ValueError):
        return 0.0


def _fetch_quote(symbol: str) -> dict:
    req = urllib.request.Request(TX_URL.format(code=_tx_code(symbol)), headers=UA_TX)
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("gbk", errors="replace")
    if '="' not in raw:
        raise HTTPException(status_code=502, detail="腾讯行情返回格式异常")
    body = raw.split('="', 1)[1].rstrip('";\n').strip()
    f = body.split("~")
    if len(f) < 50:
        raise HTTPException(status_code=404, detail=f"{symbol} 无行情（可能已退市或代码有误）")

    # 五档：腾讯 [9..18] 是买1价/量…买5价/量，[19..28] 是卖1价/量…卖5价/量
    bids = []
    asks = []
    for k in range(5):
        bp = _num(f, 9 + k * 2)
        bv = _num(f, 10 + k * 2)          # 手
        ap = _num(f, 19 + k * 2)
        av = _num(f, 20 + k * 2)
        if bp > 0:
            bids.append({"level": k + 1, "price": bp, "volume": bv, "amount": round(bp * bv * 100, 0)})
        if ap > 0:
            asks.append({"level": k + 1, "price": ap, "volume": av, "amount": round(ap * av * 100, 0)})

    return {
        "symbol": symbol,
        "name": f[1].strip(),
        "price": _num(f, 3),
        "prev_close": _num(f, 4),
        "open": _num(f, 5),
        "high": _num(f, 33),
        "low": _num(f, 34),
        "change": _num(f, 31),
        "change_pct": _num(f, 32),
        "volume": _num(f, 6),            # 手
        "amount": _num(f, 37),           # 万元
        "turnover": _num(f, 38),
        "pe_ttm": _num(f, 39),
        "amplitude": _num(f, 43),
        "float_cap": _num(f, 44),        # 亿元
        "total_cap": _num(f, 45),        # 亿元
        "pb": _num(f, 46),
        "limit_up": _num(f, 47),
        "limit_down": _num(f, 48),
        "vol_ratio": _num(f, 49),
        "bids": bids,
        "asks": asks,
        # 外盘/内盘：腾讯没有直接给的字段，用买卖量近似（不精确，标注一下）
        "note": "五档来自腾讯 3 秒快照；不是逐笔订单流，看不到排队和撤单",
    }


def _fetch_boards(symbol: str) -> dict:
    """所属板块（东财 datacenter 概念+行业）。"""
    try:
        req = urllib.request.Request(BOARD_URL.format(sym=symbol), headers=UA_EM)
        with urllib.request.urlopen(req, timeout=12) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001  板块挂了不影响主信息
        return {"industry": [], "concept": [], "error": "板块数据获取失败"}

    rows = ((payload.get("result") or {}).get("data")) or []
    industry: list[dict] = []
    concept: list[dict] = []
    seen: set[str] = set()
    for d in rows:
        name = str(d.get("BOARD_NAME") or "").strip()
        code = str(d.get("BOARD_CODE") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        item = {"name": name, "code": code}
        # IS_PRECISE: 1 = 行业/主营，0 = 概念
        if str(d.get("IS_PRECISE")) == "1":
            industry.append(item)
        else:
            concept.append(item)
    return {"industry": industry[:6], "concept": concept[:10]}


@router.get("/stock/{symbol}/profile")
def profile(symbol: str, with_boards: bool = Query(True)) -> dict:
    """个股档案：名称 + 五档 + 关键数据 + 所属板块。"""
    quote = _fetch_quote(symbol)
    boards = _fetch_boards(symbol) if with_boards else {"industry": [], "concept": []}
    return {"quote": quote, **boards}


@router.get("/stock/{symbol}/boards")
def stock_boards(symbol: str) -> dict:
    """只要板块归属（前端单独刷新用）。"""
    return _fetch_boards(symbol)


@router.get("/sparklines")
def sparklines(
    symbols: str = Query(..., description="逗号分隔，最多 30 只"),
    days: int = Query(20, ge=5, le=60),
) -> dict:
    """批量返回近 N 日收盘价，给左侧股票列表画迷你走势图用。

    一次查一屏（≤30 只），不要每只单独请求 —— 那会打 30 次接口。
    """
    from app.runtime import STORE

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:30]
    if not syms:
        return {"items": {}}

    try:
        store = STORE
        store.require()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    import polars as pl

    df = (
        store.scan()
        .filter(pl.col("symbol").is_in(syms))
        .select("symbol", "date", "close")
        .sort("date")
        .collect()
    )

    out: dict[str, dict] = {}
    for sym in syms:
        sub = df.filter(pl.col("symbol") == sym).tail(days)
        if sub.height < 2:
            continue
        closes = [round(float(x), 4) for x in sub["close"].to_list()]
        out[sym] = {
            "closes": closes,
            "change_pct": round((closes[-1] / closes[0] - 1) * 100, 2) if closes[0] else 0.0,
            "last": closes[-1],
        }
    return {"days": days, "count": len(out), "items": out}
