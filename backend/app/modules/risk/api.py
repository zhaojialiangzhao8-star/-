"""风险库 API。

  GET  /api/v1/risk/overview   规则清单 + 命中统计 + 数据缺口
  GET  /api/v1/risk/list       命中明细（可按档位/规则过滤）
  GET  /api/v1/risk/blacklist  最终黑名单（人工 ∪ 命中）
  POST /api/v1/risk/blacklist  人工拉黑 {symbol, reason}
  DEL  /api/v1/risk/blacklist/{symbol}
  POST /api/v1/risk/refresh    立即重算
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import evaluator, rules as R, store as S

router = APIRouter(tags=["risk"])


@router.get("/risk/overview")
def overview() -> dict:
    state = S.load_state()
    hits = state.get("rule_hits") or {}
    unknown = set(state.get("unknown_rules") or [])
    rule_rows = []
    for rule in R.ALL_RULES:
        rule_rows.append(
            {
                "id": rule.id,
                "level": rule.level,
                "level_label": R.LEVEL_LABEL[rule.level],
                "title": rule.title,
                "desc": rule.desc,
                "needs": list(rule.needs),
                "auto": rule.auto,
                "hits": int(hits.get(rule.id, 0)),
                "status": "no-data" if rule.id in unknown else ("ok" if rule.auto or rule.id in hits else "idle"),
            }
        )
    return {
        "rules": rule_rows,
        "stats": {
            **S.stats(),
            "scanned": state.get("scanned", 0),
            "flagged": state.get("flagged", 0),
            "unknown_rules": sorted(unknown),
        },
        "levels": R.LEVEL_LABEL,
    }


@router.get("/risk/list")
def risk_list(
    level: str = Query("", description="star / st / latent；空=全部"),
    rule: str = Query("", description="按规则 id 过滤"),
    limit: int = Query(300, ge=1, le=2000),
) -> dict:
    flags = S.load_flags()
    manual = S.load_manual()
    out = []
    for sym, item in flags.items():
        lv = item.get("level") or "latent"
        rules = item.get("rules") or []
        if level and lv != level:
            continue
        if rule and not any(r.get("id") == rule for r in rules):
            continue
        out.append(
            {
                "symbol": sym,
                "name": item.get("name") or "",
                "level": lv,
                "level_label": R.LEVEL_LABEL.get(lv, lv),
                "rules": rules,
                "watching": item.get("watching") or [],
                "manual": sym in manual,
                "at": item.get("at"),
                "in_blacklist": True,
            }
        )
    # 人工拉黑但没命中规则的也列出来
    for sym, item in manual.items():
        if any(o["symbol"] == sym for o in out):
            continue
        if level and level != "st":
            continue
        out.append(
            {
                "symbol": sym,
                "name": "",
                "level": "st",
                "level_label": "人工拉黑",
                "rules": [],
                "watching": [],
                "manual": True,
                "at": item.get("at"),
                "in_blacklist": True,
            }
        )
    order = {"star": 0, "st": 1, "latent": 2}
    out.sort(key=lambda x: (order.get(x["level"], 9), len(x["rules"]) * -1, x["symbol"]))
    return {"count": len(out), "items": out[:limit]}


@router.get("/risk/blacklist")
def blacklist() -> dict:
    data = S.blacklist()
    return {"count": len(data), "items": data}


class ManualRequest(BaseModel):
    symbol: str = Field(..., description="如 600519.SH")
    reason: str = ""


@router.post("/risk/blacklist")
def add_blacklist(req: ManualRequest) -> dict:
    sym = req.symbol.strip().upper()
    if not sym:
        raise HTTPException(400, "symbol 不能为空")
    S.add_manual(sym, req.reason)
    return {"ok": True, "symbol": sym}


@router.delete("/risk/blacklist/{symbol}")
def del_blacklist(symbol: str) -> dict:
    S.remove_manual(symbol.upper())
    return {"ok": True, "symbol": symbol.upper()}


@router.post("/risk/refresh")
def refresh() -> dict:
    try:
        state = evaluator.evaluate(force=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"风险库计算失败: {exc}") from exc
    return {"ok": True, **state}
