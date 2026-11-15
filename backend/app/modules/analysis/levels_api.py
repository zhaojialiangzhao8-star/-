"""支撑 / 压力 / 目标位 —— 给 K 线图划三条线。

用户的要求是「在相对低位划线作为止损位，压力位划线，目标位划线」。

算法说明（都是可解释的，不用黑盒）：

**支撑位（止损参考）**
  1. 找近 `--lookback` 天内的「摆动低点」：某天的最低价同时低于它前后各
     `--swing` 天的最低价。这是经典 fractal / swing low 定义。
  2. 每个摆动低点按「被回踩次数」加权：后续价格跌到它 ±0.5% 以内的次数越多，
     说明这个位置越被市场认可。
  3. 取权重最高的 2 个作止损参考；另外把「近 20 日最低」和「MA60」也算进去，
     它们是更粗但更稳的支撑。

**压力位**
  同样逻辑换成摆动高点（swing high），外加「近 20 / 60 日最高」。

**目标位**
  按盈亏比算，不猜：
    目标1 = 现价 + 2 × (现价 − 止损)
    目标2 = 现价 + 3 × (现价 − 止损)
  同时把上方最近的摆动高点作为「技术目标」，两者取较近的作为第一目标。

为什么不用筹码分布：需要逐笔/成本分布数据，我们只有日线。摆动高低点是
同一份日线能做到的最好近似。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))

from app.runtime import get_store  # noqa: E402
from app.store import GenerationStore, StoreUnavailable  # noqa: E402

router = APIRouter(tags=["levels"])


def _swings(bars: pl.DataFrame, swing: int, want: str) -> list[dict]:
    """找摆动低点/高点。

    want='low' 找 swing low（前后各 swing 天都更高）
    want='high' 找 swing high（前后各 swing 天都更低）
    然后按「后续被触及次数」加权。
    """
    n = bars.height
    if n < swing * 2 + 5:
        return []

    col = "low" if want == "low" else "high"
    prices = bars[col].to_list()
    dates = bars["date"].to_list()

    pivots: list[tuple[int, float]] = []
    for i in range(swing, n - swing):
        window = prices[i - swing : i + swing + 1]
        cur = prices[i]
        if want == "low":
            if cur <= min(window):
                pivots.append((i, cur))
        else:
            if cur >= max(window):
                pivots.append((i, cur))

    out: list[dict] = []
    for idx, price in pivots:
        # 后续有多少次回到这个位置 ±0.6%
        tol = price * 0.006
        hits = 0
        for j in range(idx + swing, n):
            if want == "low":
                if prices[j] <= price + tol:
                    hits += 1
            else:
                if prices[j] >= price - tol:
                    hits += 1
        out.append(
            {
                "price": round(price, 2),
                "date": str(dates[idx]),
                "touches": hits,
                "strength": hits,
            }
        )

    # 同一个价位可能被多次识别，按 0.8% 聚类取最强
    merged: list[dict] = []
    for s in sorted(out, key=lambda x: -x["touches"]):
        if any(abs(s["price"] - m["price"]) / m["price"] < 0.008 for m in merged):
            continue
        merged.append(s)
    return merged


@router.get("/levels/{symbol}")
def levels(
    symbol: str,
    lookback: int = Query(250, ge=60, le=800, description="回看多少个交易日"),
    swing: int = Query(5, ge=2, le=20, description="摆动点前后各几天"),
    store: GenerationStore = Depends(get_store),
) -> dict:
    """支撑 / 压力 / 目标位。三条线都能直接画到 K 线上。"""
    try:
        store.require()
    except StoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    df = (
        store.scan()
        .filter(pl.col("symbol") == symbol)
        .select("date", "open", "high", "low", "close", "volume")
        .sort("date")
        .tail(lookback)
        .collect()
    )
    if df.height < 30:
        raise HTTPException(status_code=404, detail=f"{symbol} 数据不足（{df.height} 根）")

    last_close = float(df["close"][-1])

    # 复权因子：数据是后复权，行情软件看的是真实价。
    # 用腾讯的实时价做分母算一个比例，所有价位都能换算回真实价。
    real_close = last_close
    adjust_factor = 1.0
    try:
        from app.modules.analysis.stock_api import _fetch_quote

        q = _fetch_quote(symbol)
        if q.get("prev_close") and q.get("price"):
            # 用昨收对齐（收盘后 price 可能等于昨收，两者任取其一都稳定）
            real_close = float(q["prev_close"]) or float(q["price"])
        elif q.get("price"):
            real_close = float(q["price"])
        if real_close > 0:
            adjust_factor = last_close / real_close
    except Exception:  # noqa: BLE001  拿不到实时价就保持 1.0
        pass
    last_date = str(df["date"][-1])
    ma20 = float(df["close"].tail(20).mean())
    ma60 = float(df["close"].tail(60).mean()) if df.height >= 60 else ma20

    lows20 = float(df["low"].tail(20).min())
    highs20 = float(df["high"].tail(20).max())
    lows60 = float(df["low"].tail(60).min()) if df.height >= 60 else lows20
    highs60 = float(df["high"].tail(60).max()) if df.height >= 60 else highs20

    swing_lows = [s for s in _swings(df, swing, "low") if s["price"] < last_close]
    swing_highs = [s for s in _swings(df, swing, "high") if s["price"] > last_close]

    # ---- 支撑：摆动低点 + 均线 + 近端低点 ----
    supports: list[dict] = []
    for s in sorted(swing_lows, key=lambda x: -x["touches"])[:2]:
        supports.append({"price": s["price"], "label": f"摆动低点（回踩{s['touches']}次）", "kind": "swing"})
    supports.append({"price": round(lows20, 2), "label": "近20日最低", "kind": "range"})
    if ma60 < last_close:
        supports.append({"price": round(ma60, 2), "label": "MA60", "kind": "ma"})
    supports.append({"price": round(lows60, 2), "label": "近60日最低", "kind": "range"})
    # 去重 + 按价格降序（离现价近的在前）
    supports = _dedupe(supports)

    # ---- 压力：摆动高点 + 近端高点 ----
    resistances: list[dict] = []
    for s in sorted(swing_highs, key=lambda x: x["touches"])[:2]:
        resistances.append({"price": s["price"], "label": f"摆动高点（触及{s['touches']}次）", "kind": "swing"})
    resistances.append({"price": round(highs20, 2), "label": "近20日最高", "kind": "range"})
    resistances.append({"price": round(highs60, 2), "label": "近60日最高", "kind": "range"})
    if ma60 > last_close:
        resistances.append({"price": round(ma60, 2), "label": "MA60", "kind": "ma"})
    resistances = _dedupe(resistances, ascending=True)

    # ---- 止损：直接用「近期低点」 ----
    # 用近 10 日最低：20 日太容易碰到一波上涨前的起点（涨了 70% 的票，
    # 20 日最低还在半山腰），10 日是「这一波的低点」，更贴近实际止损。
    # 再往下让 0.5% 作缓冲（刚好压在最低价上容易被插针扫掉）。
    recent_n = 10 if lookback >= 10 else lookback
    recent_low = float(df["low"].tail(recent_n).min())
    stop_price = round(recent_low * 0.995, 2)

    # 兜底：太近（<1.5%，容易插针扫掉）或太远（>15%，止损没意义）
    dist = stop_price / last_close - 1
    if dist > -0.015:
        stop_price = round(last_close * 0.985, 2)
        stop_label = f"近{recent_n}日最低太近，改用 −1.5%"
    elif dist < -0.15:
        stop_price = round(last_close * 0.92, 2)
        stop_label = f"近{recent_n}日最低太远，改用 −8%"
    else:
        stop_label = f"近{recent_n}日最低 {recent_low:.2f}（下移0.5%）"

    stop = {
        "price": stop_price,
        "label": stop_label,
        "kind": "recent_low",
        "anchor": round(recent_low, 2),
        "window": recent_n,
    }
    stop["distance_pct"] = round((stop["price"] / last_close - 1) * 100, 2)

    # ---- 目标：盈亏比 + 技术阻力 ----
    risk = last_close - stop["price"]
    targets: list[dict] = []
    if risk > 0:
        targets.append(
            {
                "price": round(last_close + risk * 2, 2),
                "label": "盈亏比 2:1",
                "rr": 2.0,
                "kind": "rr",
                "distance_pct": round((risk * 2 / last_close) * 100, 2),
            }
        )
        targets.append(
            {
                "price": round(last_close + risk * 3, 2),
                "label": "盈亏比 3:1",
                "rr": 3.0,
                "kind": "rr",
                "distance_pct": round((risk * 3 / last_close) * 100, 2),
            }
        )
    # 上方最近的摆动高点当技术目标
    if resistances:
        r0 = resistances[0]
        targets.insert(
            0,
            {
                "price": r0["price"],
                "label": f"技术目标 · {r0['label']}",
                "rr": round((r0["price"] - last_close) / risk, 2) if risk > 0 else None,
                "kind": "technical",
                "distance_pct": round((r0["price"] / last_close - 1) * 100, 2),
            },
        )
    targets = [t for t in _dedupe(targets, ascending=True) if t["price"] > last_close * 1.01]

    # 给所有价位补上真实价（= 后复权价 / 复权因子），前端要能在图上直接读真实价
    af = adjust_factor if adjust_factor > 0 else 1.0
    for _grp in (supports, resistances, targets, [stop]):
        for _it in _grp:
            _it["real_price"] = round(_it["price"] / af, 2)

    return {
        "symbol": symbol,
        "date": last_date,
        "close": round(last_close, 2),
        "real_close": round(real_close, 2),
        "adjust_factor": round(af, 4),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "lookback": df.height,
        "swing": swing,
        "supports": supports,
        "resistances": resistances,
        "stop": stop,
        "targets": targets,
        "note": (
            "支撑/压力来自日线摆动高低点（前后各 "
            f"{swing} 天的极值）+ 近端高低点 + 均线；"
            "目标是「止损距离 × 盈亏比」和上方最近摆动高点。"
            "没有筹码分布数据，做不到真正的成交密集区。"
        ),
    }


def _dedupe(items: list[dict], ascending: bool = False) -> list[dict]:
    """按 0.8% 合并同价位，保留先出现的（即权重高的）。"""
    out: list[dict] = []
    for it in items:
        p = it["price"]
        if p <= 0:
            continue
        if any(abs(p - o["price"]) / o["price"] < 0.008 for o in out):
            continue
        out.append(it)
    return sorted(out, key=lambda x: x["price"], reverse=not ascending)
