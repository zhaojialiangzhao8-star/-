"""Tick Stock Panel 兼容层 —— 让搬过来的原版看板直接跑在我们的数据上。

TSP 前端只调用 `GET /api/overview/market`，返回 `OverviewMarket`（22 个字段）。
这里把现有 dashboard_api 的聚合并结果重新映射成那个结构。

单位约定（照 TSP 前端的格式化函数反推）：
  · 股票的 change_pct 是**小数**（0.0382 = +3.82%），因为 fmtStockPct 会 ×100
  · 指数的 change_pct 是**百分数**（3.82），因为 fmtIndexPct 直接 toFixed(2)
  · amount 单位是**元**（fmtBigNum 按 1e8/1e12 换算成亿/万亿）

另外 9 个接口是 TSP 的周边设置/状态，看板只读不写，返回合理默认值即可。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...runtime import get_store
from ...store import GenerationStore

from . import dashboard_api as dash

router = APIRouter(tags=["tick-compat"])


def _pct_small(v: float | None) -> float | None:
    """百分数 → 小数（TSP 股票口径）"""
    return None if v is None else round(v / 100, 6)


@router.get("/overview/market")
def overview_market(
    as_of: str = Query("", description="交易日 YYYY-MM-DD，留空取最新"),
    store: GenerationStore = Depends(get_store),
) -> dict:
    """TSP 看板的唯一数据源。"""
    d = dash.dashboard(store=store, rank_n=10, as_of=as_of or None)
    if d.get("empty"):
        return {
            "as_of": d.get("date"),
            "quote_status": {"enabled": False, "running": False},
            "indices": [],
            "breadth": {"total": 0, "up": 0, "down": 0, "flat": 0, "up_pct": 0, "down_pct": 0},
            "amount": {"total": 0, "avg": 0},
            "boards": [],
            "limit": {"limit_up": 0, "broken": 0, "failed": 0, "limit_down": 0, "max_boards": 0, "tiers": []},
            "distribution": [],
            "trend": {},
            "activity": {},
            "radar": [],
            "emotion": {"score": 0, "label": "—"},
            "top_gainers": [], "top_losers": [], "turnover_leaders": [], "active_leaders": [],
            "concept_rank": {"leading": [], "lagging": []},
            "industry_rank": {"leading": [], "lagging": []},
        }

    b = d["breadth"]
    total = d["total"]

    # ---- 指数（TSP 要百分数）----
    indices = []
    try:
        from .market_api import indices as _idx

        # TSP 只显示 4 个：上证 / 深证 / 创业板 / 科创50
        ORDER = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH"]
        got = {x.get("symbol"): x for x in (_idx().get("items") or []) if x.get("available")}
        for sym in ORDER:
            it = got.get(sym)
            if not it:
                continue
            indices.append({
                "symbol": it.get("symbol"),
                "name": it.get("name"),
                "last_price": it.get("price"),
                "close": it.get("price"),
                "prev_close": it.get("prev_close"),
                "change_pct": it.get("change_pct"),
                "change_amount": it.get("change_amount"),
                "open": it.get("open"),
                "high": it.get("high"),
                "low": it.get("low"),
                "volume": it.get("volume"),
                "amount": it.get("amount"),
                "timestamp": it.get("timestamp"),
            })
    except Exception:  # noqa: BLE001 - 指数拉不到不该影响整屏
        indices = []

    # ---- 涨停梯队：TSP 的 tiers 带 stocks ----
    tiers = []
    for it in d["ladder"]["items"]:
        label = it["label"]
        boards = {"5板+": 5, "4板": 4, "3板": 3, "2板": 2}.get(label, 2)
        tiers.append({
            "boards": boards,
            "count": it["count"],
            "stocks": [{"symbol": "", "name": nm} for nm in (it.get("names") or [])],
        })

    top = d["ladder"].get("top") or {}
    lu = b["limit_up"]
    ld = b["limit_down"]
    broken = (d.get("monitor") or {}).get("broken_board") or 0

    # ---- 板块（TSP 的 boards[] 是「板块聚合」，我们给概念+行业合并的前 N 个）----
    boards = []
    bh = d.get("boards") or {}
    for grp, kind in (("concept", "概念"), ("industry", "行业")):
        g = bh.get(grp) or {}
        for row in (g.get("up") or []) + (g.get("down") or []):
            boards.append({
                "board": row["board"],
                "count": row.get("members") or 0,
                "up": row.get("up") or 0,
                "down": row.get("down") or 0,
                "up_pct": None,
                "amount": 0,
                "kind": kind,
            })

    # ---- 概念/行业热度（TSP 的 leading/lagging 带 leader 个股，我们暂无个股）----
    def _rank_items(rows: list[dict], kind: str) -> list[dict]:
        return [
            {
                "name": r["board"],
                "count": r.get("members") or 0,
                "avg_pct": _pct_small(r["change_pct"]),
                "up_count": r.get("up") or 0,
                "down_count": r.get("down") or 0,
                "amount": 0,
                # 前端靠 source_field 判断这一行可点（点开看成分股）；为空则整行不可点
                "source_field": kind,
                # 概念有 leader（板块内最强个股），行业暂时没有
                "leader": r.get("leader"),
            }
            for r in rows
        ]

    concept = bh.get("concept") or {}
    industry = bh.get("industry") or {}

    # ---- 四榜（TSP 的 change_pct 要小数、amount 要元）----
    def _rows(items: list[dict]) -> list[dict]:
        return [
            {
                "symbol": r["symbol"],
                "name": r.get("name") or "",
                "close": r.get("close"),
                "change_pct": _pct_small(r.get("change_pct")),
                "amount": (r.get("amount_yi") or 0) * 1e8,
                "volume": None,
                "turnover_rate": r.get("turnover"),
                "vol_ratio_5d": None,
            }
            for r in items
        ]

    emotion_score = d["emotion"]["score"]
    # 档位由后端按可配置阈值给出（强势/偏暖/震荡/偏冷/冰点），前端不再自己拍阈值
    emotion_label = d["emotion"].get("label") or "震荡"

    return {
        "as_of": d["date"],
        "quote_status": {"enabled": True, "running": True, "quote_age_ms": None, "is_trading_hours": None},
        "indices": indices,
        "breadth": {
            "total": total,
            "up": b["up"],
            "down": b["down"],
            "flat": b["flat"],
            "up_pct": b["up_pct"],
            "down_pct": round(b["down"] / max(total, 1) * 100, 1),
            "avg_pct": _pct_small(b["avg_pct"]),
            "median_pct": _pct_small(b["median_pct"]),
            "strong_up": b["strong"],
            "strong_down": b["weak"],
        },
        "amount": {
            "total": d["amount"]["total_yi"] * 1e8,
            "avg": d["amount"]["avg_yi"] * 1e8,
        },
        "boards": boards,
        "limit": {
            "limit_up": lu,
            "broken": broken,
            "failed": broken,
            "limit_down": ld,
            "max_boards": (top.get("boards") or 0),
            "seal_rate": round(lu / max(lu + ld, 1) * 100, 1),
            "tiers": tiers,
            "sealed_ready": True,
        },
        "distribution": [
            {"label": x["label"], "count": x["count"], "pct": round(x["count"] / max(total, 1), 6)}
            for x in b["distribution"]
        ],
        "trend": {
            "above_ma5": 0, "above_ma20": 0, "above_ma60": 0,
            "above_ma5_pct": d["trend"].get("above_ma5") or 0,
            "above_ma20_pct": d["trend"].get("above_ma20") or 0,
            "above_ma60_pct": d["trend"].get("above_ma60") or 0,
            "new_high": d["trend"].get("new_high_60") or 0,
            "new_low": d["trend"].get("new_low_60") or 0,
        },
        "activity": {
            "avg_turnover": d["turnover"].get("avg") or 0,
            "high_turnover": d["turnover"].get("high_count") or 0,
            "high_vol_ratio": d["turnover"].get("high_count") or 0,
            "vol_ratio": d["turnover"].get("vol_ratio") or 0,
        },
        "radar": [
            {"key": x["key"], "label": x["label"], "value": x["value"]}
            for x in d["emotion"]["dims"]
        ],
        "emotion": {"score": emotion_score, "label": emotion_label},
        "top_gainers": _rows(d["ranks"]["gain"]),
        "top_losers": _rows(d["ranks"]["loss"]),
        "turnover_leaders": _rows(d["ranks"]["amount"]),
        "active_leaders": _rows(d["ranks"]["turnover"]),
        "concept_rank": {
            "leading": _rank_items(concept.get("up") or [], "concept"),
            "lagging": _rank_items(concept.get("down") or [], "concept"),
        },
        "industry_rank": {
            "leading": _rank_items(industry.get("up") or [], "industry"),
            "lagging": _rank_items(industry.get("down") or [], "industry"),
        },
    }


# ---------------------------------------------------------------- 周边状态接口
# 看板会读这些，但只用于开关/提示，返回合理默认值即可（不写死为假数据，
# 而是「功能未启用」的诚实状态）。


@router.get("/data/status")
def data_status(store: GenerationStore = Depends(get_store)) -> dict:
    import polars as pl

    try:
        lf = store.scan()
        days = lf.select(pl.col("date").n_unique()).collect().item()
        rows = lf.select(pl.len()).collect().item()
    except Exception:  # noqa: BLE001
        days, rows = 0, 0
    return {
        "enriched": {"trading_days": days, "rows": rows},
        "daily": {"trading_days": days, "rows": rows},
        "stats_pending": False,
        "indicators_ready": True,
        "generation": None,
    }


@router.get("/settings")
def settings() -> dict:
    return {"theme": "dark", "locale": "zh-CN", "page_zoom": 1, "values": {}}


@router.get("/settings/preferences")
def preferences() -> dict:
    return {"dashboard_layout": None, "daily_data_provider": "local", "quote_interval": 3}


@router.get("/settings/emotion-score/config")
def emotion_score_config() -> dict:
    """情绪评分口径（维度/子项权重/归一化区间/档位阈值）。

    口径本体在 app/services/emotion_score.py，跟着 TSP 的同一套 6 维模型；
    用户改动存在 data/user_data/emotion_score.json，`config` 为合并后的完整口径。
    """
    from ...services import emotion_prefs
    from ...services.emotion_score import public_config

    raw = emotion_prefs.load_raw()
    payload = public_config(raw)
    payload["customized"] = raw is not None
    return payload


@router.put("/settings/emotion-score/config")
def emotion_score_config_update(body: dict) -> dict:
    """保存口径；传 {"config": null} 或空 body 表示恢复出厂。"""
    from ...services import emotion_prefs
    from ...services.emotion_score import public_config

    raw = body.get("config") if isinstance(body, dict) else None
    emotion_prefs.save_raw(raw if isinstance(raw, dict) else None)
    payload = public_config(raw if isinstance(raw, dict) else None)
    payload["customized"] = isinstance(raw, dict)
    return payload


@router.get("/settings/preferences/quote-interval")
def quote_interval() -> dict:
    return {"interval": 3, "min_interval": 1, "max_interval": 60}


@router.get("/settings/capability-matrix")
def capability_matrix() -> dict:
    return {"capabilities": {}, "limits": {}}


@router.get("/data/integrity")
def data_integrity() -> dict:
    return {"report": None, "blocking_assets": [], "data_revisions": {}}
