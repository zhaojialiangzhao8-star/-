"""把策略信号接进 Tick 的监控中心（`/api/alerts`）。

Tick 的 MonitorWidget 读 `GET /api/alerts?days=7&limit=10`，
返回 `{ alerts: AlertEvent[], total }`，每条形如：

    { ts, source: 'strategy', type: 'enter', symbol, name,
      price, change_pct, message（策略名）, severity }

我们的信号源是 `/api/v1/screener/all`（按策略分组的今日信号），
这里摊平成 Tick 要的时间流。ts 用信号日的收盘时间（15:00），
因为库里只记了信号日期，没有逐条触发时刻——不编造时分秒。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from . import dashboard_api as dash
from ...runtime import get_store
from ...store import GenerationStore
from ..screener import batch as screener_batch

router = APIRouter(tags=["tick-compat"])


def _ts_of(date_str: str | None) -> int:
    """把信号日期转成 Unix 秒（收盘 15:00 Asia/Shanghai）。"""
    if not date_str:
        return int(datetime.now(timezone.utc).timestamp())
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except ValueError:
        return int(datetime.now(timezone.utc).timestamp())
    # 15:00 CST = 07:00 UTC
    return int(d.replace(hour=7, minute=0, second=0, tzinfo=timezone.utc).timestamp())


@router.get("/alerts")
def alerts(
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(10, ge=1, le=100),
    source: str = Query("", description="留空=全部；strategy=只要策略信号"),
    type: str = Query(""),
    store: GenerationStore = Depends(get_store),
) -> dict:
    """策略信号时间流（Tick 监控中心的数据源）。"""
    try:
        data = screener_batch.screener_all(
            limit_per_strategy=max(limit, 10),
            scope="all",
        )
    except Exception:  # noqa: BLE001 - 信号算不出来时返回空列表，不让整块看板崩
        return {"alerts": [], "total": 0}

    # 只保留**已启用**的策略（策略库是唯一真相源，停用的不该出现在监控中心）
    from ...runtime import ENGINE

    enabled = set(ENGINE.registry.keys())

    # 当日快照：symbol → (涨跌幅%, 名称)，用来补信号行缺的 change_pct
    pct_map: dict[str, float] = {}
    try:
        snap, _ = dash._latest_frame(store)
        if snap.height and "change_pct" in snap.columns:
            for r in snap.select(["symbol", "change_pct"]).iter_rows(named=True):
                if r["change_pct"] is not None:
                    pct_map[r["symbol"]] = float(r["change_pct"])
    except Exception:  # noqa: BLE001
        pct_map = {}

    out: list[dict] = []
    for item in data.get("items") or []:
        sid = item.get("id") or ""
        if enabled and sid not in enabled:
            continue
        sname = item.get("name") or sid
        for row in item.get("rows") or []:
            sym = row.get("symbol")
            if not sym:
                continue
            close = row.get("close")
            sym_u = str(sym).upper()
            # 涨跌幅从当日快照补（信号行本身没带）
            pct = row.get("change_pct")
            if pct is None:
                pct = pct_map.get(sym_u)
            out.append({
                "ts": _ts_of(row.get("date") or item.get("signal_date")),
                "source": "strategy",
                "type": "pool_entry",
                "symbol": sym_u,
                "name": row.get("name") or "",
                "price": float(close) if close is not None else None,
                # Tick 的 change_pct 是小数
                "change_pct": (float(pct) / 100 if pct is not None else None),
                "message": f"策略「{sname}」",
                "strategy_id": sid,
                "severity": "info",
                "signals": [sname],
            })

    if source:
        out = [x for x in out if x["source"] == source]
    if type:
        out = [x for x in out if x["type"] == type]

    # 时间倒序（最新的在前），截断到 limit
    out.sort(key=lambda x: x["ts"], reverse=True)
    total = len(out)
    return {"alerts": out[:limit], "total": total}
