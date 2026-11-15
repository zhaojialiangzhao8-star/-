"""涨停天梯 API —— 参考 Tick 的 /api/limit-ladder。

不是只给「几板几只」，而是把每一档的个股全列出来，前端按高度排成天梯。
口径：
  · 涨停按板块阈值判定：主板 9.5% / 创业板·科创板 19.5% / 北交所 29.5% / ST 4.8%
  · 连板数 = 从最新交易日往回，连续满足涨停的天数
  · 只收最新交易日仍在涨停的票（断板的不进天梯）
按代际缓存：算一次约 1-3 秒。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query

from ...runtime import get_store

if TYPE_CHECKING:  # pragma: no cover
    from ...store import GenerationStore

router = APIRouter(tags=["limit-ladder"])

_CACHE: dict[str, dict] = {}


def _limits(symbol: str, name: str) -> tuple[float, float]:
    """该票的（涨停幅度, 跌停幅度），百分比。

    主板普通 10%、主板 ST 5%；创业板/科创板 20%；北交所 30%。
    ST 只在主板减半 —— 先判板块再判 ST，顺序反了会把 20% 板上的 ST 当成 5% 涨停股。
    """
    code = symbol.split(".")[0]
    if code.startswith(("30", "68")):
        return 19.5, 19.5
    if symbol.endswith(".BJ") or code.startswith(("43", "83", "87", "92")):
        return 29.5, 29.5
    is_st = name.startswith(("ST", "*ST", "SST", "S*ST")) or "ST" in name[:4]
    return (4.8, 4.8) if is_st else (9.5, 9.5)


def _at_limit(
    change: float | None, close, high, low, symbol: str, up: bool = True
) -> bool:
    """判定「收盘时是否封在涨/跌停」。

    两道条件同时满足才算：
      1) 涨跌幅落在该票所属档位的带宽里（主板 5%/10%、创业科创 20%、北交 30%）；
      2) 收盘价就是当日最高价（涨停）/ 最低价（跌停）—— 封住的票收盘一定在极值。
    为什么不用「涨停价」精确比：库里的 close 是后复权，反推的涨停价和交易所两位小数
    取整对不上（实测只剩 2 只）；带宽判定 + 极值约束既有足够召回也不虚增。
    """
    if change is None or close is None:
        return False
    cp = change if up else -change
    if cp <= 0:
        return False
    # 封住的票收盘在当日极值
    if up:
        if high is None or close < high - abs(high) * 1e-6:
            return False
    else:
        if low is None or close > low + abs(low) * 1e-6:
            return False

    code = symbol.split(".")[0]
    if code.startswith(("30", "68")):
        limits = (19.5,)
    elif symbol.endswith(".BJ") or code.startswith(("43", "83", "87", "92")):
        limits = (29.5,)
    else:
        limits = (4.8, 9.5)  # 主板：ST 与普通都试
    return any(lim - 0.35 <= cp <= lim + 0.9 for lim in limits)


def compute_limit_ladder(store: GenerationStore, days: int = 30, per_tier: int = 60) -> dict:
    import polars as pl

    from ...api import _frame_recent
    from .dashboard_api import _names

    gen = store.require().generation
    hit = _CACHE.get(gen)
    if hit is not None:
        return hit

    df = _frame_recent(store, days)
    if not {"change_pct", "close", "symbol", "date"}.issubset(set(df.columns)):
        return {"date": None, "tiers": [], "counts": {"up": 0, "down": 0}, "stats": {}}

    names = _names()
    latest = df["date"].max()
    df = df.sort(["symbol", "date"])

    # 逐行判定涨跌停（30 个交易日 × ~4800 只 ≈ 14 万行，纯 Python 循环约 0.3s）
    lu: list[bool] = []
    ld: list[bool] = []
    cols = df.select(["symbol", "change_pct", "close", "high", "low"]).to_dict(as_series=False)
    for sym, cp, cl, hi, lo in zip(
        cols["symbol"], cols["change_pct"], cols["close"], cols["high"], cols["low"]
    ):
        lu.append(_at_limit(cp, cl, hi, lo, sym, up=True))
        ld.append(_at_limit(cp, cl, hi, lo, sym, up=False))
    df = df.with_columns(
        pl.Series("_lu", lu),
        pl.Series("_ld", ld),
    )

    last = df.filter(pl.col("date") == latest)
    ups = last.filter(pl.col("_lu"))
    downs = last.filter(pl.col("_ld"))

    def _streak(flag: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, sub in df.group_by("symbol"):
            sym = key[0] if isinstance(key, (tuple, list)) else key
            flags = sub.sort("date")[flag].to_list()
            if not flags or not flags[-1]:
                continue
            c = 0
            for v in reversed(flags):
                if v:
                    c += 1
                else:
                    break
            out[sym] = c
        return out

    up_streak = _streak("_lu")
    down_streak = _streak("_ld")

    def _pack(row, streak: int) -> dict:
        sym = row["symbol"]
        return {
            "symbol": sym,
            "name": names.get(sym) or sym,
            "close": round(float(row["close"]), 2) if row.get("close") is not None else None,
            "change_pct": round(float(row["change_pct"]), 2)
            if row.get("change_pct") is not None
            else None,
            "turnover": round(float(row["turnover"]) / 100, 2)
            if row.get("turnover") is not None
            else None,
            "amount_yi": round(float(row["amount"]) / 1e8, 2)
            if row.get("amount") is not None
            else None,
            "boards": streak,
        }

    rows_up = sorted(
        (list(ups.iter_rows(named=True))),
        key=lambda r: (-up_streak.get(r["symbol"], 0), -(r.get("change_pct") or 0)),
    )
    by_boards: dict[int, list[dict]] = {}
    for r in rows_up:
        b = int(up_streak.get(r["symbol"], 1))
        by_boards.setdefault(b, []).append(_pack(r, b))

    tiers = [
        {
            "boards": b,
            "label": "首板" if b == 1 else f"{b}板",
            "count": len(by_boards[b]),
            "stocks": by_boards[b][:per_tier],
            "truncated": max(0, len(by_boards[b]) - per_tier),
        }
        for b in sorted(by_boards, reverse=True)
    ]

    rows_dn = sorted(
        list(downs.iter_rows(named=True)),
        key=lambda r: (down_streak.get(r["symbol"], 0), r.get("change_pct") or 0),
    )
    down_list = [_pack(r, int(down_streak.get(r["symbol"], 1))) for r in rows_dn]

    ge2 = sum(t["count"] for t in tiers if t["boards"] >= 2)
    first = next((t["count"] for t in tiers if t["boards"] == 1), 0)
    out = {
        "date": str(latest),
        "max_boards": tiers[0]["boards"] if tiers else 0,
        "counts": {"up": len(rows_up), "down": len(rows_dn)},
        "stats": {
            "first_board": first,
            "ge2": ge2,
            "ladder_tiers": len(tiers),
            "promotion_rate": round(ge2 / max(len(rows_up), 1) * 100, 1),
        },
        "tiers": tiers,
        "downs": down_list[:per_tier],
        "downs_truncated": max(0, len(down_list) - per_tier),
    }
    _CACHE[gen] = out
    return out


@router.get("/limit-ladder")
def limit_ladder(
    store: GenerationStore = Depends(get_store),
    days: int = Query(30, ge=5, le=120),
    per_tier: int = Query(60, ge=5, le=300),
) -> dict:
    """涨停天梯：每个板数档位 + 该档全部个股（点进去直接到个股分析）。"""
    try:
        return compute_limit_ladder(store, days, per_tier)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"涨停天梯计算失败: {exc}") from exc
