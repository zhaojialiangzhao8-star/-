"""交易首页看板数据 —— 一个接口喂满整屏。

设计参照 Tick Stock Panel 的 Dashboard：一屏里同时给出
  ① 指数  ② 涨跌广度  ③ 情绪评分  ④ 趋势强度  ⑤ 梯队  ⑥ 四榜  ⑦ 板块热度

为什么单开一个 router：首页要一次拿到 7 组数据，分成 7 个接口前端要发 7 个请求，
首屏会明显变慢；这里在同一个 polars 快照上算完，只扫一次最新一天。

数据口径全部来自 enriched 表已有的列（ret_1d / ma5 / ma20 / ma60 / high_20 /
low_20 / turnover / change_pct），不在这里重算派生列。
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
from fastapi import APIRouter, Depends, Query

from ... import config
from ...runtime import get_store
from ...services import emotion_prefs
from ...services.emotion_score import compute as compute_emotion
from ...store import GenerationStore

router = APIRouter(tags=["dashboard"])

# 涨跌分布的分桶（%）
BUCKETS = [
    (-999, -5, "<-5%"),
    (-5, -3, "-5~-3%"),
    (-3, -1, "-3~-1%"),
    (-1, 0, "-1~0%"),
    (0, 1, "0~1%"),
    (1, 3, "1~3%"),
    (3, 5, "3~5%"),
    (5, 999, ">5%"),
]


def _names() -> dict[str, str]:
    """代码 → 中文名。缺失时返回空表，前端退回显示代码。"""
    for p in (
        config.DATA_DIR / "symbol_names.json",
        Path(getattr(__import__("sys"), "_MEIPASS", "")) / "data" / "symbol_names.json",
        config.BACKEND_DIR / "data" / "symbol_names.json",
    ):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
    return {}


_DASH_CACHE: dict[str, object] = {}



def _as_date(value: str):
    """\"2026-11-13\" → datetime.date；polars 2.0 起不再支持字符串 cast 成 Date。"""
    from datetime import date as _date

    return _date.fromisoformat(str(value)[:10])


def _common_latest(df: "pl.DataFrame") -> str:
    """取「最多股票有数据的那一天」作为市场快照日。

    回补/增量后各票最新日期会不一致（老票 11-12、新补的票 11-13）。
    用全局 max(date) 会把 90% 的票漏掉；用 mode（覆盖股票数最多的那天）才代表「整市场已收盘」。
    """
    if df.height == 0:
        return ""
    counts = (
        df.group_by("date")
        .agg(pl.len().alias("n"))
        .sort("date", descending=True)
    )
    # 取「最新的、且覆盖 ≥90% 股票」的那一天：
    # 回补后常出现「最新一天只有几百只」的情况，用 max 会少 90% 的票，
    # 用 mode 又会把 11-11（比 11-12 早一天）当成最新。90% 阈值同时避开这两个坑。
    top = int(counts["n"].max())
    for row in counts.iter_rows(named=True):
        if row["n"] >= top * 0.9:
            return str(row["date"])
    return str(counts["date"][0])

_ASOF_CACHE: dict[str, tuple[pl.DataFrame, str]] = {}


def _frame_at(store: GenerationStore, as_of: str, days: int = 45) -> pl.DataFrame:
    """取「截至 as_of 的近期窗口」——首页选历史日期时用。

    为什么不用 _frame_recent：那个只能取「最新 N 天」，历史日期取不到。
    这里按日期区间过滤，只解 45 天的数据（够算连板梯队/均线），并按 (代际, as_of) 缓存。
    """
    from datetime import date as _date, timedelta as _td

    from ...api import _FRAME_CACHE

    gen = store.require().generation
    key = f"{gen}|asof|{as_of}|{days}"
    if key in _FRAME_CACHE:
        return _FRAME_CACHE[key]

    end = _date.fromisoformat(as_of)
    start = end - _td(days=days)
    frame = store.scan().filter((pl.col("date") >= start) & (pl.col("date") <= end)).collect()
    _FRAME_CACHE[key] = frame
    return frame


def _latest_frame(store: GenerationStore, as_of: str | None = None) -> tuple[pl.DataFrame, str]:
    """取「每只票各自最新一行」的快照。

    直接 `scan() + filter(date==latest)` 会扫全表（635 万行，几十秒），
    所以复用 api.py 的最近 N 天缓存：那里按代际缓存，第二次起是纯内存过滤。

    为什么按 symbol 取最后一行而不是 `date == 全局最大日期`：
    回补/增量后各票的最新日期会不一致（老票 11-12、新补的票 11-13），
    用全局最大日期会把 90% 的票漏掉，指标直接算错。
    """
    if as_of:
        recent = _frame_at(store, as_of, days=10)
        if recent.height == 0:
            return recent, ""
        snap = recent.filter(pl.col("date") == _as_date(as_of))
        if snap.height == 0:
            # 该日期没有交易数据（周末/停牌），退回窗口内最近的一天
            latest = _common_latest(recent)
            snap = recent.filter(pl.col("date") == _as_date(latest)) if latest else recent
            return snap, latest
        return snap, as_of

    from ...api import _frame_recent

    recent = _frame_recent(store, 5)
    if recent.height == 0:
        return recent, ""
    latest = _common_latest(recent)
    snap = recent.filter(pl.col("date") == _as_date(latest)) if latest else recent
    return snap, latest


_BOARD_CACHE: dict[str, object] = {}


def _board_rank(snap: pl.DataFrame | None, n: int = 5) -> dict | None:
    """按 TSP 的方式算板块排行：全部从同一份快照推导。

    概念用 symbol_industry_em.json（4 万条），行业用 industry_members.json（1.7 万条）。
    返回 None 表示映射不全，调用方退回 board_quotes.json。
    """
    if snap is None or snap.height == 0 or "change_pct" not in snap.columns:
        return None

    # symbol → (涨跌幅%, 名称)
    daily: dict[str, tuple[float, str]] = {}
    nm = _names()
    for r in snap.select(["symbol", "change_pct"]).iter_rows(named=True):
        v = r["change_pct"]
        if v is not None:
            daily[r["symbol"]] = (float(v), nm.get(r["symbol"]) or r["symbol"])
    if not daily:
        return None

    def groups() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        concept: dict[str, list[str]] = {}
        if _CONCEPT_MAP:
            for item in _CONCEPT_MAP:
                b, sym = item.get("industry"), item.get("symbol")
                if b and sym:
                    concept.setdefault(b, []).append(sym)
        industry = {k: list(v) for k, v in _industry_map().items()}
        return concept, industry

    # 先确保 _CONCEPT_MAP 已加载（复用 _concept_leaders 的加载逻辑）
    if _CONCEPT_MAP is None:
        _concept_leaders(snap, reverse=False)
    concept_g, industry_g = groups()

    def rank_one(by_board: dict[str, list[str]]) -> list[dict]:
        out = []
        for board, syms in by_board.items():
            vals = []
            best = None
            best_pct = None
            for sym in syms:
                hit = daily.get(sym)
                if hit is None:
                    continue
                p, name = hit
                vals.append(p)
                # TSP 的 _leader_sort_key：缺行情排最后；0.00% 是有效值
                if best_pct is None or p > best_pct:
                    best_pct = p
                    best = (sym, name)
            if not vals or best is None:
                continue
            out.append({
                "board": board,
                "change_pct": round(sum(vals) / len(vals), 2),
                "members": len(vals),
                "up": sum(1 for v in vals if v > 0),
                "down": sum(1 for v in vals if v < 0),
                "leader": {
                    "symbol": best[0],
                    "name": best[1],
                    "change_pct": round(best_pct / 100, 6),
                },
            })
        out.sort(key=lambda x: x["change_pct"], reverse=True)
        return out

    con_rows = rank_one(concept_g)
    ind_rows = rank_one(industry_g)
    if not con_rows and not ind_rows:
        return None

    return {
        "date": None,          # 由调用方填快照日
        "source": "snapshot",  # 标注：数字来自快照日，与看板其他模块一致
        "industry": {"count": len(ind_rows), "up": ind_rows[:n], "down": ind_rows[-n:][::-1]},
        "concept": {"count": len(con_rows), "up": con_rows[:n], "down": con_rows[-n:][::-1]},
    }


def _board_hot(n: int = 5, snap: pl.DataFrame | None = None) -> dict:
    """板块热度：**行业**和**概念**分开返回。

    数据来自 strategy_lib/fetch_board_quotes.py 抓的 board_quotes.json：
    东财 clist 的行业(m:90+t:2, 496 个) 和概念(m:90+t:3, 554 个) 分开，
    而且直接带当日涨跌幅 f3，比等权合成的 board_index 准。

    老数据 board_index.parquet 只有概念且要自己算涨跌，保留作兜底。
    """
    import json as _json
    import os as _os

    # 优先：按 TSP 的方式从同一份快照算（板块涨跌幅与领涨股才自洽）
    ranked = _board_rank(snap, n)
    if ranked is not None:
        return ranked

    cands = [
        Path(config.DATA_DIR) / "board_quotes.json",
        Path(_os.environ.get("LOCALAPPDATA", "")) / "Facet" / "data" / "board_quotes.json",
    ]
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "data" / "board_quotes.json")
    p = next((c for c in cands if c.exists()), None)

    if p is not None:
        try:
            blob = _json.loads(p.read_text(encoding="utf-8"))

            def top(items, reverse: bool):
                rows = [x for x in items if x.get("change_pct") is not None]
                rows.sort(key=lambda x: x["change_pct"], reverse=reverse)
                return [
                    {
                        "board": x["name"],
                        "change_pct": x["change_pct"],
                        "members": (x.get("up") or 0) + (x.get("down") or 0),
                        "up": x.get("up"),
                        "down": x.get("down"),
                    }
                    for x in rows[:n]
                ]

            ind = blob.get("industry") or []
            con = blob.get("concept") or []
            # Tick 的 leader 语义：板块内**最强**的那只（涨幅排名第一）。
            # 领涨板块里它涨得更多，领跌板块里它跌得更少/逆势翻红，都是同一个规则。
            leaders = _concept_leaders(snap)
            # 注意：top() 每次都返回新列表，必须先存下来再注入 leader，
            # 否则返回时重新调 top() 会把刚加的 leader 丢掉。
            con_up = top(con, True)
            con_down = top(con, False)
            for row in con_up + con_down:
                row["leader"] = leaders.get(row["board"])
            ind_up = top(ind, True)
            ind_down = top(ind, False)
            for row in ind_up + ind_down:
                row["leader"] = leaders.get(row["board"])
            return {
                "date": blob.get("date"),
                "updated": blob.get("updated"),
                "industry": {"count": len(ind), "up": ind_up, "down": ind_down},
                "concept": {"count": len(con), "up": con_up, "down": con_down},
            }
        except Exception:  # noqa: BLE001 - 读坏了就退回旧口径
            pass

    # ---- 兜底：老 board_index.parquet（只有概念，涨跌要自己算）----
    cands2 = [
        Path(config.DATA_DIR) / "board_index.parquet",
        Path(_os.environ.get("LOCALAPPDATA", "")) / "Facet" / "data" / "board_index.parquet",
    ]
    if meipass:
        cands2.append(Path(meipass) / "data" / "board_index.parquet")
    p2 = next((c for c in cands2 if c.exists()), None)
    if p2 is None:
        return {"date": None, "industry": {"count": 0, "up": [], "down": []},
                "concept": {"count": 0, "up": [], "down": []}}

    key = "board"
    if key in _BOARD_CACHE:
        df = _BOARD_CACHE[key]
    else:
        df = pl.read_parquet(p2).sort(["board", "date"])
        _BOARD_CACHE[key] = df

    latest = df["date"].max()
    last2 = df.filter(pl.col("date") >= (df.filter(pl.col("date") < latest)["date"].max()))
    out = []
    for b in last2["board"].unique().to_list():
        sub = last2.filter(pl.col("board") == b).sort("date")
        if sub.height < 2:
            continue
        prev = sub["close"][0]
        cur = sub["close"][-1]
        if not prev:
            continue
        out.append({
            "board": b,
            "change_pct": round((cur / prev - 1) * 100, 2),
            "members": int(sub["n"][-1] or 0),
        })
    out.sort(key=lambda x: x["change_pct"], reverse=True)
    return {
        "date": str(latest),
        "industry": {"count": 0, "up": [], "down": []},
        "concept": {"count": len(out), "up": out[:n], "down": out[-n:][::-1]},
    }


_LADDER_CACHE: dict[str, object] = {}

_CONCEPT_MAP: list[dict] | None = None
_INDUSTRY_MAP: dict[str, list[str]] | None = None


def _concept_groups() -> dict[str, list[str]]:
    """概念 → 成分股（data/symbol_industry_em.json，382 个概念 / 约 4 万条）。

    与 _concept_leaders 同一份数据；单独取一次是为了在没有当日快照的场合
    （板块成分股弹窗）也能按名字查成员。
    """
    global _CONCEPT_MAP
    if _CONCEPT_MAP is None:
        import json as _json
        import os as _os

        cands = [
            Path(config.DATA_DIR) / "symbol_industry_em.json",
            Path(_os.environ.get("LOCALAPPDATA", "")) / "Facet" / "data" / "symbol_industry_em.json",
        ]
        meipass = getattr(__import__("sys"), "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "data" / "symbol_industry_em.json")
        p = next((c for c in cands if c.exists()), None)
        try:
            _CONCEPT_MAP = _json.loads(p.read_text(encoding="utf-8")) if p else []
        except Exception:  # noqa: BLE001
            _CONCEPT_MAP = []
    out: dict[str, list[str]] = {}
    for item in _CONCEPT_MAP or []:
        b, sym = item.get("industry"), item.get("symbol")
        if b and sym:
            out.setdefault(b, []).append(sym)
    return out


@router.get("/market/metric-members")
def metric_members(
    metric: str = Query(..., description="above_ma5 / above_ma20 / above_ma60 / new_high / new_low / high_turnover / expand_volume"),
    limit: int = Query(200, ge=1, le=500),
    store: GenerationStore = Depends(get_store),
) -> dict:
    """某个「趋势强度 / 实用监控」指标命中的个股清单（看板点指标 → 看是谁）。

    口径和 dashboard 里那几张卡完全一致：
      · 站上 MA：close > ma5/20/60
      · 新高/新低：close ≥ high_20×0.999 / ≤ low_20×1.001
      · 高换手：turnover/100 ≥ 5%
      · 放量：volume > vol_ma20 × 1.5
    """
    snap, latest = _latest_frame(store, as_of)
    names = _names()
    if snap.height == 0:
        return {"date": latest, "metric": metric, "count": 0, "items": []}

    col = {
        "above_ma5": "ma5",
        "above_ma20": "ma20",
        "above_ma60": "ma60",
    }.get(metric)

    if col is not None:
        if col not in snap.columns:
            raise HTTPException(404, f"没有 {col} 列")
        mask = (snap["close"] > snap[col]) & snap[col].is_not_null() & snap["close"].is_not_null()
    elif metric == "new_high":
        if "high_20" not in snap.columns:
            raise HTTPException(404, "没有 high_20 列")
        mask = snap["close"] >= snap["high_20"] * 0.999
    elif metric == "new_low":
        if "low_20" not in snap.columns:
            raise HTTPException(404, "没有 low_20 列")
        mask = snap["close"] <= snap["low_20"] * 1.001
    elif metric == "high_turnover":
        if "turnover" not in snap.columns:
            raise HTTPException(404, "没有 turnover 列")
        mask = snap["turnover"] / 100 >= 5
    elif metric == "expand_volume":
        if "vol_ma20" not in snap.columns:
            raise HTTPException(404, "没有 vol_ma20 列")
        mask = (snap["volume"] > snap["vol_ma20"] * 1.5) & (snap["vol_ma20"] > 0)
    else:
        raise HTTPException(400, f"未知指标 {metric}")

    sub = snap.filter(mask.fill_null(False))
    total = sub.height
    # 涨得多的排前面，方便直接看「谁最强」
    if "change_pct" in sub.columns:
        sub = sub.sort("change_pct", descending=True, nulls_last=True)
    rows = []
    for r in sub.head(limit).iter_rows(named=True):
        sym = r["symbol"]
        rows.append(
            {
                "symbol": sym,
                "name": names.get(sym, ""),
                "close": round(float(r["close"]), 2) if r.get("close") is not None else None,
                "change_pct": round(float(r["change_pct"]), 2) if r.get("change_pct") is not None else None,
                "turnover": round(float(r["turnover"]) / 100, 2) if r.get("turnover") is not None else None,
                "amount_yi": round(float(r["amount"]) / 1e8, 2) if r.get("amount") is not None else None,
            }
        )
    labels = {
        "above_ma5": "站上 MA5",
        "above_ma20": "站上 MA20",
        "above_ma60": "站上 MA60",
        "new_high": "20 日新高",
        "new_low": "20 日新低",
        "high_turnover": "换手 ≥ 5%",
        "expand_volume": "放量（量 > 20 日均量 1.5 倍）",
    }
    return {"date": latest, "metric": metric, "label": labels.get(metric, metric), "count": total, "items": rows}


@router.get("/dashboard/board-members")
def board_members(
    kind: str = Query("concept", pattern="^(concept|industry)$"),
    board: str = Query(..., min_length=1),
    limit: int = Query(200, ge=1, le=2000),
    store: GenerationStore = Depends(get_store),
) -> dict:
    """某个概念/行业板块的成分股（点看板热度卡里的板块行 → 这个接口）。

    数据同源：概念来自 symbol_industry_em.json，行业来自 industry_members.json；
    涨跌幅/现价取最新一代快照。取不到映射就返回空列表，不编造。
    """
    snap, latest = _latest_frame(store)
    groups = _concept_groups() if kind == "concept" else _industry_map()
    members = groups.get(board) or []
    if not members or snap.height == 0:
        return {"kind": kind, "board": board, "date": latest, "items": [], "count": 0}

    nm = _names()
    sub = snap.filter(pl.col("symbol").is_in(members))
    sort_col = "change_pct" if "change_pct" in sub.columns else "ret_1d"
    sub = sub.sort(sort_col, descending=True, nulls_last=True).head(limit)
    items = []
    for r in sub.iter_rows(named=True):
        pct = r.get("change_pct")
        if pct is None and r.get("ret_1d") is not None:
            pct = float(r["ret_1d"]) * 100
        items.append({
            "symbol": r["symbol"],
            "name": nm.get(r["symbol"]) or r["symbol"],
            "close": round(float(r["close"]), 2) if r.get("close") is not None else None,
            "change_pct": round(float(pct), 2) if pct is not None else None,
            "amount_yi": round(float(r["amount"]) / 1e8, 2) if r.get("amount") is not None else None,
        })
    return {"kind": kind, "board": board, "date": latest, "items": items, "count": len(items)}


def _industry_map() -> dict[str, list[str]]:
    """行业 → 成分股（strategy_lib/fetch_industry_members.py 抓的）。

    TSP 用扩展数据源的 dimension 字段分行业/概念；我们没那套扩展框架，
    所以概念用 symbol_industry_em.json、行业用 industry_members.json。
    """
    global _INDUSTRY_MAP
    if _INDUSTRY_MAP is not None:
        return _INDUSTRY_MAP
    import json as _json
    import os as _os

    cands = [
        Path(config.DATA_DIR) / "industry_members.json",
        Path(_os.environ.get("LOCALAPPDATA", "")) / "Facet" / "data" / "industry_members.json",
    ]
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "data" / "industry_members.json")
    p = next((c for c in cands if c.exists()), None)
    try:
        _INDUSTRY_MAP = _json.loads(p.read_text(encoding="utf-8")) if p else {}
    except Exception:  # noqa: BLE001
        _INDUSTRY_MAP = {}
    return _INDUSTRY_MAP


def _concept_leaders(snap: pl.DataFrame | None, reverse: bool = False) -> dict[str, dict]:
    """每个概念里当日涨幅最高的那只股票（Tick 的 leader 字段）。

    映射来自 data/symbol_industry_em.json（382 个概念 → 成分股，4 万条）。
    行业板块没有这份映射（东财 b:BKxxxx 接口被限流），所以只对概念生效——
    行业行前端会显示 "--"，不编造。
    """
    global _CONCEPT_MAP
    if snap is None or snap.height == 0 or "change_pct" not in snap.columns:
        return {}

    if _CONCEPT_MAP is None:
        import json as _json
        import os as _os

        cands = [
            Path(config.DATA_DIR) / "symbol_industry_em.json",
            Path(_os.environ.get("LOCALAPPDATA", "")) / "Facet" / "data" / "symbol_industry_em.json",
        ]
        meipass = getattr(__import__("sys"), "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "data" / "symbol_industry_em.json")
        p = next((c for c in cands if c.exists()), None)
        try:
            _CONCEPT_MAP = _json.loads(p.read_text(encoding="utf-8")) if p else []
        except Exception:  # noqa: BLE001
            _CONCEPT_MAP = []

    if not _CONCEPT_MAP:
        return {}

    # 当日快照：symbol → 涨跌幅(%)
    daily: dict[str, float] = {}
    for r in snap.select(["symbol", "change_pct"]).iter_rows(named=True):
        v = r["change_pct"]
        if v is not None:
            daily[r["symbol"]] = float(v)

    by_board: dict[str, list[str]] = {}
    for item in _CONCEPT_MAP:
        b = item.get("industry")
        sym = item.get("symbol")
        if b and sym:
            by_board.setdefault(b, []).append(sym)
    # 行业映射是「行业 → [股票列表]」结构，直接并进来（同名不会被覆盖，概念优先）
    for b, syms in _industry_map().items():
        by_board.setdefault(b, list(syms))

    nm = _names()
    out: dict[str, dict] = {}
    for board, syms in by_board.items():
        best_sym = None
        best_pct = None
        for sym in syms:
            p = daily.get(sym)
            if p is None:
                continue
            # reverse=True 时取跌幅最大的（领跌榜的 leader）
            if best_pct is None or (p < best_pct if reverse else p > best_pct):
                best_pct = p
                best_sym = sym
        if best_sym is not None and best_pct is not None:
            out[board] = {
                "symbol": best_sym,
                "name": nm.get(best_sym) or best_sym,
                # TSP 的 change_pct 统一是小数
                "change_pct": round(best_pct / 100, 6),
            }
    return out


_HL60_CACHE: dict[str, object] = {}


def _highlow60(store: GenerationStore) -> dict:
    """60 日新高/新低只数。

    enriched 里只有 high_20/low_20，60 日要自己滚：取最近 95 个自然日
    （约 65 个交易日）按股票 rolling_max(60)/rolling_min(60)。
    展示层临时算，不落盘；按代际缓存。
    """
    gen = store.require().generation
    if gen in _HL60_CACHE:
        return _HL60_CACHE[gen]

    from ...api import _frame_recent

    raw = _frame_recent(store, 95)
    if raw.height == 0 or "high" not in raw.columns:
        return {"new_high_60": None, "new_low_60": None}

    # 逐股遍历：rolling_max().over("symbol") 在 95 天全表上会吃掉几十 GB 内存
    df = raw.select(["symbol", "date", "high", "low", "close"]).sort(["symbol", "date"])
    latest = df["date"].max()
    nh = 0
    nl = 0
    for key, sub in df.group_by("symbol"):
        if sub.height < 61:
            continue
        if sub["date"][-1] != latest:
            continue
        highs = sub["high"].to_list()
        lows = sub["low"].to_list()
        close = sub["close"][-1]
        if close is None:
            continue
        # 前 60 个交易日（不含当天）
        h60 = max(h for h in highs[-61:-1] if h is not None) if any(highs[-61:-1]) else None
        l60 = min(lo for lo in lows[-61:-1] if lo is not None) if any(lows[-61:-1]) else None
        if h60 is not None and close >= h60 * 0.999:
            nh += 1
        if l60 is not None and close <= l60 * 1.001:
            nl += 1

    out = {"new_high_60": nh, "new_low_60": nl}
    _HL60_CACHE[gen] = out
    return out


def _ladder(store: GenerationStore, days: int = 12, as_of: str | None = None) -> dict:
    """当日涨停梯队：2板/3板/4板/5板+ 各几只。

    「连续涨停」要从历史上数，所以要取最近 12 个交易日（够数到 5 板+）。
    按代际缓存——算一次约 3 秒，不能每次请求都算。
    """
    gen = store.require().generation
    cache_key = f"{gen}|{as_of or 'latest'}"
    if cache_key in _LADDER_CACHE:
        return _LADDER_CACHE[cache_key]

    from ...api import _frame_recent

    df = _frame_at(store, as_of, days=days * 3) if as_of else _frame_recent(store, days)
    if "change_pct" not in df.columns:
        return {"items": [], "total": 0}

    latest = _common_latest(df)
    # 涨停口径要分板块：主板 10%、创业板/科创板 20%、北交所 30%、ST 5%
    # （以前一律用 9.5%，会把创业板 +10% 的票错记成涨停，梯队数量虚高）
    df = df.sort(["symbol", "date"]).with_columns(
        pl.when(pl.col("symbol").str.contains(r"^(30|68)")).then(0.20)
        .when(pl.col("symbol").str.contains(r"^(4|8|920)")).then(0.30)
        .otherwise(0.10)
        .alias("_band")
    )
    names_now = _names()
    is_st = pl.col("symbol").replace_strict(
        {k: ("ST" in (v or "")) for k, v in names_now.items()}, default=False, return_dtype=pl.Boolean
    )
    df = df.with_columns(
        (
            pl.col("change_pct")
            >= (
                pl.when(is_st).then(0.05)
                .otherwise(pl.col("_band") * 100 - 0.8)  # 后复权价按 0.01 取整，涨停日常只有 9.4~9.7%，留 0.8 容差
            )
        ).fill_null(False).alias("lu")
    )
    # 只统计最新日仍在涨停的
    # 注意：polars 的 group_by 返回的 key 是 tuple（单列也是 ("600519.SZ",)），要解包
    counts: dict[str, int] = {}
    # 连板梯队默认剔除 ST：同花顺「连板天梯」也是这个口径（涨停池仍含 ST）
    st_syms = {k for k, v in names_now.items() if "ST" in (v or "")}
    for key, sub in df.group_by("symbol"):
        sym = key[0] if isinstance(key, (tuple, list)) else key
        if sym in st_syms:
            continue
        lus = sub.sort("date")["lu"].to_list()
        if not lus or not lus[-1]:
            continue
        c = 0
        for v in reversed(lus):
            if v:
                c += 1
            else:
                break
        if c >= 2:
            counts[sym] = c

    buckets: dict[str, int] = {}
    bucket_names: dict[str, list[str]] = {}
    nm_map = _names()
    for sym, c in counts.items():
        key = f"{c}板" if c < 5 else "5板+"
        buckets[key] = buckets.get(key, 0) + 1
        bucket_names.setdefault(key, []).append(nm_map.get(sym) or sym)

    # 从高到低排（Tick 的顺序：5板 → 4板 → 3板 → 2板）
    order = ["5板+", "4板", "3板", "2板"]
    items = [
        {"label": k, "count": buckets.get(k, 0), "names": bucket_names.get(k, [])[:8]}
        for k in order
    ]

    # 最高连板那只（名称要给前端显示）
    top = None
    if counts:
        top_sym, top_n = max(counts.items(), key=lambda kv: kv[1])
        nm = _names().get(top_sym, "")
        top = {"symbol": top_sym, "name": nm, "boards": top_n}

    out = {"items": items, "total": len(counts), "date": str(latest), "top": top}
    _LADDER_CACHE[gen] = out
    return out


def _rank(snap: pl.DataFrame, by: str, desc: bool, n: int, names: dict[str, str], amount_scale: float = 1.0) -> list[dict]:
    """按某列取前 n 名。"""
    # 风险库黑名单：命中风险规则的票不进榜单（复盘桌面不展示）
    try:
        from ..risk.store import blacklist_set

        blocked = blacklist_set()
        if blocked:
            snap = snap.filter(~pl.col("symbol").is_in(list(blocked)))
    except Exception:  # noqa: BLE001
        pass
    sub = (
        snap.filter(pl.col(by).is_not_null())
        .sort(by, descending=desc, nulls_last=True)
        .head(n)
    )
    out = []
    for r in sub.iter_rows(named=True):
        out.append({
            "symbol": r["symbol"],
            "name": names.get(r["symbol"], ""),
            "close": round(float(r["close"]), 2) if r["close"] is not None else None,
            "change_pct": round(float(r["change_pct"]), 2) if r.get("change_pct") is not None
                          else (round(float(r["ret_1d"]) * 100, 2) if r.get("ret_1d") is not None else None),
            "amount_yi": round(float(r["amount"]) / 1e8 * amount_scale, 2) if r.get("amount") is not None else None,
            "turnover": round(float(r["turnover"]) / 100, 2) if r.get("turnover") is not None else None,
        })
    return out


def _index_avg_pct(latest: str | None) -> float | None:
    """本地 index_daily 的当日指数平均涨跌（%）。

    离线可算（数据由 strategy_lib/fetch_indices.py 落盘）；没文件就返回 None，
    指数维度自然按中性 50 处理，不影响其它维度。
    """
    try:
        from .market_api import _load_index_daily

        df = _load_index_daily()
        if df is None or getattr(df, "height", 0) == 0 or "close" not in df.columns:
            return None
        d = (
            df.sort(["symbol", "date"])
            .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_r"))
            .drop_nulls("_r")
        )
        if latest:
            d = d.filter(pl.col("date") <= pl.lit(str(latest)).str.to_date())
        if d.height == 0:
            return None
        rows = d.filter(pl.col("date") == d["date"].max())["_r"]
        if rows.len() == 0:
            return None
        return round(float(rows.mean()) * 100, 3)
    except Exception:  # noqa: BLE001 — 指数是可选输入, 坏掉不能拖挂看板
        return None


# ---------------------------------------------------------------- 盘中实时梯队
_LIVE_LADDER: dict[str, tuple[float, dict]] = {}
_LIVE_TTL = 60.0


def _live_ladder(store: GenerationStore, force: bool = False) -> dict | None:
    """用腾讯批量实时报价重算「今日」涨停 + 连板，60 秒缓存。

    为什么需要：库是收盘后的快照，盘中刷新首页看到的永远是昨天。
    东财全市场快照现在 502，腾讯支持批量（60 只/次），5200 只 ≈ 87 次请求 ≈ 10 秒，
    所以做 60 秒缓存 —— 刷新首页时数据最多旧 1 分钟，而不是旧一天。
    """
    import time as _time

    from ...api import _frame_recent
    from ...infrastructure.data_sources.registry import registry

    gen = store.require().generation
    hit = _LIVE_LADDER.get(gen)
    if hit and not force and _time.time() - hit[0] < _LIVE_TTL:
        return hit[1]

    recent = _frame_recent(store, 30)
    if recent.height == 0:
        return None
    last_day = _common_latest(recent)
    snap = recent.filter(pl.col("date") == _as_date(last_day))
    syms = snap["symbol"].to_list()
    names = _names()

    try:
        quotes = registry.get("tencent").fetch_quotes(syms)
    except Exception:  # noqa: BLE001
        return None
    if not quotes:
        return None

    q = {r["symbol"]: r for r in quotes if r.get("symbol")}
    prev = {r["symbol"]: r.get("prev_close") for r in quotes if r.get("symbol")}

    def band(sym: str) -> float:
        code = sym.split(".")[0]
        nm = names.get(sym, "")
        if "ST" in nm:
            return 5.0
        if code.startswith(("30", "68")):
            return 20.0
        if code.startswith(("43", "83", "87", "92")):
            return 30.0
        return 10.0

    today_lu = {}
    for sym in syms:
        r = q.get(sym)
        if not r or not r.get("price") or not prev.get(sym):
            continue
        chg = (float(r["price"]) / float(prev[sym]) - 1) * 100
        if chg >= band(sym) - 0.8:
            today_lu[sym] = chg

    # 历史连板（截至 last_day 之前一天）用库里已经算好的 lu
    hist = recent.filter(pl.col("date") < _as_date(last_day)).sort(["symbol", "date"])
    hist = hist.with_columns(
        (pl.col("change_pct") >= 9.2).fill_null(False).alias("_lu")
    )
    streak: dict[str, int] = {}
    for key, sub in hist.group_by("symbol"):
        sym = key[0] if isinstance(key, tuple) else key
        flags = sub["_lu"].to_list()
        c = 0
        for v in reversed(flags):
            if not v:
                break
            c += 1
        if c:
            streak[sym] = c

    tiers: dict[int, list[dict]] = {}
    max_boards = 0
    for sym, _chg in today_lu.items():
        boards = streak.get(sym, 0) + 1
        max_boards = max(max_boards, boards)
        if boards >= 2:
            tiers.setdefault(boards, []).append({"symbol": sym, "name": names.get(sym, "")})

    out_tiers = []
    for b in (5, 4, 3, 2):
        stocks = tiers.get(b if b < 5 else max(5, b), [])
        out_tiers.append(
            {
                "boards": b,
                "count": len(stocks),
                "names": [x["name"] for x in stocks[:8]],
                "stocks": stocks[:8],
            }
        )

    result = {
        "items": out_tiers,
        "total": len(today_lu),
        "max_boards": max_boards,
        "live": True,
        "as_of": _time.strftime("%H:%M:%S"),
    }
    _LIVE_LADDER[gen] = (_time.time(), result)
    return result


@router.get("/dashboard")
def dashboard(
    store: GenerationStore = Depends(get_store),
    rank_n: int = Query(8, ge=1, le=30),
    as_of: str | None = Query(None, description="历史日期 YYYY-MM-DD，留空取最新"),
) -> dict:
    """首页看板：一次返回整屏需要的全部数据。"""
    snap, latest = _latest_frame(store, as_of)
    names = _names()
    total = snap.height
    if total == 0:
        return {"date": latest, "empty": True}

    # 涨跌幅优先用真实 change_pct(%)，没有就从 ret_1d 换算
    if "change_pct" in snap.columns and snap["change_pct"].null_count() < total:
        pct = snap["change_pct"]
        use_real = True
    else:
        pct = snap["ret_1d"] * 100
        use_real = False
    flat_mask = pct.abs() <= 1e-9

    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    flat = int(flat_mask.sum())

    # 分布
    dist = []
    for lo, hi, label in BUCKETS:
        cnt = int(((pct > lo) & (pct <= hi)).sum())
        dist.append({"label": label, "count": cnt})

    # 强势/弱势（±3%）
    strong = int((pct >= 3).sum())
    weak = int((pct <= -3).sum())

    # 涨跌停：和「涨停天梯」同一套判定（档位带宽 + 收盘在当日极值），
    # 否则首页说 23 家、天梯说 25 家，两个页面自相矛盾。
    # 单个 symbol 可能有多行（多日），这里只算最新交易日那一行。
    limit_up = 0
    limit_down = 0
    try:
        from .limit_ladder_api import _at_limit

        last_rows = snap.filter(pl.col("date") == snap["date"].max())
        cols = last_rows.select(
            ["symbol", "change_pct", "close", "high", "low"]
        ).to_dict(as_series=False)
        for sym, cp, cl, hi, lo in zip(
            cols["symbol"], cols["change_pct"], cols["close"], cols["high"], cols["low"]
        ):
            if _at_limit(cp, cl, hi, lo, sym, up=True):
                limit_up += 1
            elif _at_limit(cp, cl, hi, lo, sym, up=False):
                limit_down += 1
    except Exception:  # noqa: BLE001
        limit_up = int((pct >= 9.5).sum())
        limit_down = int((pct <= -9.5).sum())

    # 成交额：库里的 amount 是后复权口径（复权价×成交量），比真实成交额大几十倍。
    # 真实合计从 data/real_amount 快照读；读不到才退回后复权口径并标注。
    amt = snap["amount"].drop_nulls()
    from ...indicators import _real_amount_yi

    real_total = _real_amount_yi()
    raw_total = float(amt.sum()) / 1e8 if amt.len() else 0.0
    try:
        from ...api import _frame_recent as _fr

        latest_day = _common_latest(_fr(store, 5))
    except Exception:  # noqa: BLE001
        latest_day = str(latest) if latest else ""
    is_latest_day = bool(as_of) and str(as_of) == latest_day
    if as_of and real_total is not None and is_latest_day:
        # 选的就是最新交易日：直接用真实成交额快照
        amount_yi = real_total
    elif as_of and real_total is not None:
        # 历史日期的 amount 是后复权口径（量级和真实成交额差几十倍）。
        # 用「最近几天里后复权合计最大的那天」当参照，算一个市场级折算系数：
        # 不用最新一天，因为最新一天可能是刚补的、成交额还是估算值（会算出天文数字）。
        try:
            from ...api import _frame_recent

            recent = _frame_recent(store, 8)
            ref = (
                recent.group_by("date")
                .agg(pl.col("amount").sum().alias("s"))
                .sort("s", descending=True)
            )
            raw_ref = float(ref["s"][0]) / 1e8 if ref.height else 0.0
            factor = (real_total / raw_ref) if raw_ref > 0 else 1.0
        except Exception:  # noqa: BLE001
            factor = 1.0
        amount_yi = round(raw_total * factor, 2)
    else:
        amount_yi = real_total if real_total is not None else round(raw_total, 2)
    avg_amount_yi = round(amount_yi / total, 4) if total else 0.0
    # 榜单里单只股票的金额：用「真实总额 / 后复权总额」当校正系数，让数量级对得上
    raw_total = float(amt.sum()) / 1e8 if amt.len() else 0.0
    amount_scale = (amount_yi / raw_total) if raw_total > 0 and real_total is not None else 1.0

    # 换手：turnover 列是 ×100 的口径（102.55 表示 1.03%），要除以 100
    to = (snap["turnover"] / 100).drop_nulls() if "turnover" in snap.columns else None
    turnover_avg = round(float(to.mean()), 2) if to is not None and to.len() else None
    high_turnover = int((to >= 5).sum()) if to is not None and to.len() else None

    # 量比：volume / vol_ma5（中位数，反应当日整体活跃度）
    vol_ratio = None
    if "volume" in snap.columns and "vol_ma5" in snap.columns:
        vr = (snap["volume"] / snap["vol_ma5"]).drop_nulls()
        if vr.len():
            vol_ratio = round(float(vr.median()), 2)

    # 放量占比：volume 超过 20 日均量 1.5 倍的股票占比
    expand_ratio = None
    if "volume" in snap.columns and "vol_ma20" in snap.columns:
        vm = snap["vol_ma20"]
        ex = (snap["volume"] > vm * 1.5) & vm.is_not_null() & (vm > 0)
        valid = vm.is_not_null() & (vm > 0)
        if int(valid.sum()) > 0:
            expand_ratio = round(float(ex.sum()) / int(valid.sum()) * 100, 1)

    # 炸板：最高价摸过涨停(相对收盘 +9.5%以上) 但收盘没涨停
    broken_board = None
    if "high" in snap.columns:
        hi_ret = (snap["high"] / snap["close"] - 1) * 100
        is_broken = (hi_ret >= 9.5) & (pct < 9.5)
        broken_board = int(is_broken.fill_null(False).sum())

    # 趋势强度：站上均线的比例
    def above(col: str) -> float | None:
        if col not in snap.columns:
            return None
        c = snap[col]
        m = snap["close"]
        valid = (c.is_not_null() & m.is_not_null())
        if int(valid.sum()) == 0:
            return None
        return round(float(((m > c) & valid).sum()) / int(valid.sum()) * 100, 1)

    # 20 日新高/新低
    h20 = snap["high_20"] if "high_20" in snap.columns else None
    l20 = snap["low_20"] if "low_20" in snap.columns else None
    new_high = int((snap["close"] >= h20 * 0.999).sum()) if h20 is not None else None
    new_low = int((snap["close"] <= l20 * 1.001).sum()) if l20 is not None else None

    # ---- 七维情绪雷达（口径对齐 Tick：指数/主线/赚钱/亏钱/量能/投机/抗跌）----
    # 已改为走 services/emotion_score 的可配置口径：6 维（指数/赚钱/量能/投机/抗跌/主线）、
    # 子项区间线性归一化 + 维度加权。指标缺项的维度按中性 50 处理，不再各自的硬编码公式。
    bh_top = {}
    try:
        bh_top = ((_board_hot(1).get("concept") or {}).get("up") or [{}])[0]
    except Exception:  # noqa: BLE001
        bh_top = {}
    lad = {}
    try:
        lad = _ladder(store, as_of=as_of)
        # 盘中（且没选历史日期）用实时报价重算今日梯队，否则看到的永远是收盘快照
        if not as_of:
            try:
                from ...infrastructure.realtime.quotes import is_trading_time

                if is_trading_time():
                    live = _live_ladder(store)
                    if live and live.get("total"):
                        lad = {**lad, **live}
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        lad = {}
    max_boards = int(((lad.get("top") or {}).get("boards")) or 0) or None
    tier2 = None
    if lad.get("items"):
        tier2 = sum(int(x.get("count") or 0) for x in lad["items"]) or None

    emotion_metrics = {
        # 指数维度：本地 index_daily 的当日均涨跌（离线可算；取不到则整个维度中性）
        "avg_index_pct": _index_avg_pct(latest),
        "up_pct": up / max(up + flat + down, 1) * 100,
        "avg_pct": float(pct.mean()) / 100,
        "median_pct": float(pct.median()) / 100,
        "strong_diff_pct": (strong - weak) / max(total, 1) * 100,
        "avg_vol_ratio": vol_ratio,
        "high_vol_pct": expand_ratio,
        "limit_up": limit_up,
        "seal_rate": round(limit_up / max(limit_up + limit_down, 1) * 100, 1),
        "max_boards": max_boards,
        "tier2_count": tier2,
        "down_pct": down / max(up + flat + down, 1) * 100,
        "strong_down_pct": weak / max(total, 1) * 100,
        "mainline_avg": (float(bh_top.get("change_pct") or 0)) / 100 if bh_top else None,
    }
    emotion_metrics = {k: v for k, v in emotion_metrics.items() if v is not None}
    emotion_cfg = emotion_prefs.load_raw()
    emo = compute_emotion(emotion_metrics, emotion_cfg)
    emotion = emo["score"]

    # 高低比：新高 / (新高+新低)
    hl_ratio = None
    _h60 = _highlow60(store)
    _nh, _nl = _h60.get("new_high_60"), _h60.get("new_low_60")
    if _nh is not None and _nl is not None and (_nh + _nl) > 0:
        hl_ratio = round(_nh / (_nh + _nl) * 100, 1)

    return {
        "date": latest,
        "empty": False,
        "total": total,
        "breadth": {
            "up": up, "flat": flat, "down": down,
            "up_pct": round(up / total * 100, 1),
            "strong": strong, "weak": weak,
            "limit_up": limit_up, "limit_down": limit_down,
            "median_pct": round(float(pct.median()), 2),
            "avg_pct": round(float(pct.mean()), 2),
            "distribution": dist,
        },
        "amount": {
            "total_yi": amount_yi,
            "avg_yi": avg_amount_yi,
            "real": use_real,
        },
        "turnover": {
            "avg": turnover_avg,
            "high_count": high_turnover,
            "vol_ratio": vol_ratio,
            "expand_ratio": expand_ratio,
        },
        "monitor": {
            "broken_board": broken_board,
            "limit_down": limit_down,
            "above_ma60": above("ma60"),
            "new_high_60": _nh,
            "new_low_60": _nl,
            "high_turnover": high_turnover,
            "expand_ratio": expand_ratio,
        },
        "trend": {
            "above_ma5": above("ma5"),
            "above_ma20": above("ma20"),
            "above_ma60": above("ma60"),
            "new_high_20": new_high,
            "new_low_20": new_low,
            "hl_ratio": hl_ratio,
            **_h60,
        },
        "emotion": {
            "score": emotion,
            "label": emo["label"],
            "thresholds": emo["thresholds"],
            "dims": emo["radar"],
            "dimensions": emo["dimensions"],
            "customized": emotion_cfg is not None,
        },
        "boards": _board_hot(5, snap),
        "ladder": lad if lad else _ladder(store, as_of=as_of),
        "ranks": {
            "gain": _rank(snap, "change_pct" if use_real else "ret_1d", True, rank_n, names, amount_scale),
            "loss": _rank(snap, "change_pct" if use_real else "ret_1d", False, rank_n, names, amount_scale),
            "amount": _rank(snap.filter(pl.col("amount") < 2e12), "amount", True, rank_n, names, amount_scale),
            "turnover": _rank(snap, "turnover", True, rank_n, names, amount_scale) if "turnover" in snap.columns else [],
        },
    }
