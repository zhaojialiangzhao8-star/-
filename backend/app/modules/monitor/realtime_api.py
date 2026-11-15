"""实时行情 API —— REST 查询 + 自选股管理 + 轮询器启停。

端点：
  GET  /api/v1/realtime?symbols=600519.SH,000001.SZ   查快照
  GET  /api/v1/realtime/status                        轮询器与缓存状态
  GET  /api/v1/watchlist                              自选股
  POST /api/v1/watchlist                              加自选
  DELETE /api/v1/watchlist/{symbol}                   删自选

轮询器在 lifespan 里启动，只轮询自选股；自选股为空时什么都不做。
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(tags=["realtime"])


def _watch_symbols() -> list[str]:
    from app.infrastructure.storage.db import get_db

    try:
        return [w["symbol"] for w in get_db().list_watch()]
    except Exception:
        return []


@router.get("/realtime")
def realtime(
    symbols: str = Query("", description="逗号分隔；留空则返回自选股"),
) -> dict:
    """查实时快照。`symbols` 为空时用自选股列表。"""
    from app.infrastructure.realtime.quotes import CACHE, fetch_quotes, is_trading_time

    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()] or _watch_symbols()
    if not wanted:
        return {"count": 0, "quotes": [], "hint": "没有自选股，也没有指定 symbols"}

    # 缓存里没有或太旧（> 10 秒）就现拉一次
    cached = CACHE.get(wanted)
    fresh = [c for c in cached if c.get("available") and c.get("age_ms", 9e9) < 10_000]
    if len(fresh) < len(wanted):
        quotes = fetch_quotes(wanted)
        if quotes:
            CACHE.update(quotes)
        cached = CACHE.get(wanted)

    return {
        "count": len(cached),
        "trading": is_trading_time(),
        "quotes": cached,
    }


@router.get("/realtime/status")
def realtime_status() -> dict:
    from app.infrastructure.realtime.quotes import CACHE

    snap = CACHE.snapshot()
    snap["watchlist"] = _watch_symbols()
    return snap


class WatchRequest(BaseModel):
    symbol: str = Field(..., description="如 600519.SH")
    name: str = ""
    grp: str = "default"
    note: str = ""


@router.get("/watchlist")
def watchlist() -> dict:
    from app.infrastructure.storage.db import get_db

    items = get_db().list_watch()
    return {"count": len(items), "items": items}


@router.post("/watchlist")
def watch_add(req: WatchRequest) -> dict:
    from app.infrastructure.storage.db import get_db

    get_db().add_watch(req.symbol.upper(), req.name, req.grp, req.note)
    return {"ok": True, "symbol": req.symbol.upper(), "watchlist": _watch_symbols()}


class BatchWatchRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, description="要加的代码列表")
    grp: str = "default"
    note: str = ""


@router.post("/watchlist/batch")
def watch_add_batch(req: BatchWatchRequest) -> dict:
    """批量加自选（智能选股「一键加自选」用）。已有同名的不重复，直接更新。"""
    from app.infrastructure.storage.db import get_db

    syms = [s.strip().upper() for s in req.symbols if s.strip()]
    if not syms:
        return {"ok": True, "added": 0, "watchlist": _watch_symbols()}
    db = get_db()
    # 名称从库里已有的 symbol_names 补（避免前端逐个传）
    names: dict[str, str] = {}
    try:
        import json as _json
        from pathlib import Path as _Path

        p = _Path(__file__).resolve().parents[3] / "data" / "symbol_names.json"
        if p.exists():
            names = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        names = {}
    for s in syms:
        db.add_watch(s, names.get(s, ""), req.grp, req.note)
    return {"ok": True, "added": len(syms), "watchlist": _watch_symbols()}


class BatchDeleteRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)


@router.post("/watchlist/batch-delete")
def watch_delete_batch(req: BatchDeleteRequest) -> dict:
    """批量删自选。"""
    from app.infrastructure.storage.db import get_db

    syms = [s.strip().upper() for s in req.symbols if s.strip()]
    db = get_db()
    for s in syms:
        db.remove_watch(s)
    return {"ok": True, "removed": len(syms), "watchlist": _watch_symbols()}


class PlanRequest(BaseModel):
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    note: str = ""


@router.put("/watchlist/{symbol}/plan")
def watch_set_plan(symbol: str, req: PlanRequest) -> dict:
    """给自选股挂一个交易计划（入场/止损/目标/备注），存在 note 字段的 JSON 里。"""
    import json as _json

    from fastapi import HTTPException

    from app.infrastructure.storage.db import get_db

    db = get_db()
    sym = symbol.upper()
    items = [w for w in db.list_watch() if str(w.get("symbol", "")).upper() == sym]
    if not items:
        raise HTTPException(404, f"自选股里没有 {sym}")
    plan = {"entry": req.entry, "stop": req.stop, "target": req.target, "note": req.note}
    db.add_watch(sym, items[0].get("name") or "", items[0].get("grp") or "default",
                 _json.dumps(plan, ensure_ascii=False))
    return {"ok": True, "symbol": sym, "plan": plan}


@router.delete("/watchlist/{symbol}")
def watch_remove(symbol: str) -> dict:
    from app.infrastructure.storage.db import get_db

    get_db().remove_watch(symbol.upper())
    return {"ok": True, "watchlist": _watch_symbols()}

