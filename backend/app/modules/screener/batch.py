"""批量选股 + 策略排名缓存。

问题：现在点一个策略跑一次（全市场 15~20 秒），换策略再跑一遍。
      58 个策略就是 58 次全表扫描。

做法：
  · 一次加载 frame（复用 api._frame 的代际缓存）
  · 循环跑所有策略，每次只算 hits（策略本身），不跑账户回测
  · 结果按「数据代际 + 参数签名」缓存，同一个代际只算一次
  · 胜率/收益排名来自最近一次账户回测结果（reports/account_compare_*.md 里已算好）

接口：
  GET  /api/v1/screener/all          批量选股（所有策略的当日信号）
  GET  /api/v1/strategy-ranking      策略排名（含胜率）
  POST /api/v1/screener/all/refresh  强制重算
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Query

from app import config

router = APIRouter(tags=["screener"])

# 代际 → {strategy_id: {"hit_total": n, "today": [...], "signal_date": "..."}}
_BATCH_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}

# 策略中文名映射（前端展示用；META 里有 name，但有些是英文缩写）
NAME_OVERRIDE = {
    "classic_reversal": "急跌反弹",
    "classic_week52_high": "52周新高",
    "classic_rsi_oversold": "RSI超卖",
    "classic_mean_revert": "均值回归",
    "classic_breakout_retest": "突破回踩",
    "classic_boll_squeeze": "布林收口",
    "classic_donchian": "唐奇安通道",
    "classic_kdj_cross": "KDJ金叉",
    "classic_boll_breakout": "布林突破",
    "classic_boll_lower": "布林下轨",
    "classic_gap_fill": "缺口回补",
    "classic_high_stall": "高位滞涨",
    "classic_ma_converge": "均线粘合",
    "classic_ma_cross": "均线金叉",
    "classic_macd_cross": "MACD金叉",
    "classic_momentum": "动量突破",
    "classic_shrink_pullback": "缩量回踩",
    "classic_shrink_yin_yang": "缩量阴阳",
    "classic_three_soldiers": "红三兵",
    "classic_vcp": "VCP形态",
    "classic_vol_divergence": "量价背离",
    "classic_atr_channel": "ATR通道",
    "classic_dual_thrust": "双推力",
    "instock_backtrace_ma250": "跌破年线",
    "instock_breakthrough_platform": "突破平台",
    "instock_climax_limitdown": "极限跌停",
    "instock_enter": "放量进场",
    "instock_high_tight_flag": "高窄旗形",
    "instock_keep_increasing": "连续放量",
    "instock_low_atr": "低波动",
    "instock_low_backtrace_increase": "低回撤增仓",
    "instock_parking_apron": "停机坪",
    "instock_turtle": "海龟交易",
    "composite_ic6": "合成IC6",
    "terminal_picks": "终端精选",
    "ma_trend": "均线趋势",
    "breakout_20d": "20日突破",
}


def cn_name(sid: str, meta_name: str = "") -> str:
    """策略中文名：优先用手工映射，其次看 META.name 里有没有中文。"""
    if sid in NAME_OVERRIDE:
        return NAME_OVERRIDE[sid]
    if meta_name and re.search(r"[\u4e00-\u9fa5]", meta_name):
        return meta_name
    # Alpha101 之类的保留英文，去掉前缀
    return meta_name or sid


def _reports_dir() -> Path | None:
    """找 reports/ 目录。

    源码布局：facet/backend/app/modules/screener/batch.py → parents[4] = facet/
    打包布局：_internal/app/modules/screener/batch.py   → parents[3] = _internal/
    两者差一层，所以不能写死一个，按候选列表逐个试。
    """
    here = Path(__file__).resolve()
    for base in (here.parents[4], here.parents[3], here.parents[2]):
        cand = base / "reports"
        if cand.is_dir():
            return cand
    return None


def _parse_ranking() -> dict[str, dict]:
    """从最近一次账户回测报告里解析排名（收益/回撤/夏普/胜率）。

    报告是 scripts/account_compare.py 生成的 markdown 表格，这里直接读。
    """
    out: dict[str, dict] = {}
    reports = _reports_dir()
    if reports is None:
        return out
    for name in ("account_compare_fullmarket.md", "account_compare.md"):
        p = reports / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        # | 1 | `classic_reversal` | 9,242 | 34.4% | 8.0% | -61.7% | 0.41 | 32.7% | 486 |
        for m in re.finditer(
            r"\|\s*(\d+)\s*\|\s*`([\w.]+)`\s*\|\s*([\d,]+)\s*\|\s*([-\d.]+)%\s*\|\s*([-\d.]+)%\s*\|"
            r"\s*([-\d.]+)%\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)%\s*\|\s*(\d+)\s*\|",
            text,
        ):
            rank, sid, sig, ret, ann, mdd, sharpe, win, trades = m.groups()
            out[sid] = {
                "rank": int(rank),
                "signals": int(sig.replace(",", "")),
                "return": float(ret) / 100,
                "annual": float(ann) / 100,
                "mdd": float(mdd) / 100,
                "sharpe": float(sharpe),
                "win_rate": float(win) / 100,
                "trades": int(trades),
            }
        if out:
            break

    # 报告里的 # 是生成时的编号，后来追加的策略编号会乱
    # （比如收益第 2 的策略编号是 17）。这里按收益重新编号，
    # 让页面上的 # 和实际排名一致。
    if out:
        ordered = sorted(out.items(), key=lambda kv: -(kv[1].get("return") or -99))
        for i, (_sid, v) in enumerate(ordered, start=1):
            v["rank"] = i
    return out


@router.get("/strategy-ranking")
def strategy_ranking(store=None) -> dict:
    """所有策略的排名（来自最近一次账户回测）。

    没跑过账户回测的策略，rank 为 None —— 不编造数字。
    """
    from app.runtime import ENGINE

    ranking = _parse_ranking()
    rows = []
    for sid in sorted(ENGINE.registry):
        spec = ENGINE.registry[sid]
        r = ranking.get(sid)
        rows.append(
            {
                "id": sid,
                "name": cn_name(sid, getattr(spec, "name", "") or ""),
                "tags": list(getattr(spec, "tags", []) or []),
                "rank": r["rank"] if r else None,
                "return": r["return"] if r else None,
                "mdd": r["mdd"] if r else None,
                "sharpe": r["sharpe"] if r else None,
                "win_rate": r["win_rate"] if r else None,
                "trades": r["trades"] if r else None,
            }
        )
    ranked = [x for x in rows if x["rank"] is not None]
    ranked.sort(key=lambda x: x["rank"])
    unranked = [x for x in rows if x["rank"] is None]
    return {
        "count": len(rows),
        "ranked": len(ranked),
        "items": ranked + unranked,
        "note": "排名来自最近一次账户回测（scripts/account_compare.py）；未回测的策略无排名",
    }


def _run_all(store, engine, force: bool = False, scope: str = "ranked") -> dict:
    """跑指定范围的策略，返回 {sid: {...}}。按 (代际, scope) 缓存。

    scope=ranked：只跑有账户回测排名的策略（12 个左右，1 分钟）；
    scope=all：跑全部 58 个（5 分钟+，内存重，慎用）。
    """
    gen = store.require().generation
    cache_key = f"{gen}|{scope}"
    with _LOCK:
        if not force and cache_key in _BATCH_CACHE:
            return _BATCH_CACHE[cache_key]
        _STATE[gen] = {"status": "running", "done": 0, "total": len(engine.registry), "started": time.time()}

    from app.api import _frame

    import gc

    ranking = _parse_ranking()
    all_ids = sorted(engine.registry)
    if scope == "ranked":
        # 有回测排名的排前面，其余启用的策略照样跑（只是没有排名数字）。
        # 只跑「有排名的」会漏掉 pattern_* 这类新加的、还没回测过的策略，
        # 页面上就只剩一半策略，看起来像缺了东西。
        ranked = [sid for sid in all_ids if sid in ranking]
        rest = [sid for sid in all_ids if sid not in ranking]
        ids = ranked + rest
    else:
        ids = all_ids

    frame = _frame(store)
    result: dict[str, dict] = {}
    latest = frame["date"].max()

    for i, sid in enumerate(ids):
        hits = None
        try:
            hits = engine.run(sid, {}, frame)
        except Exception as exc:  # noqa: BLE001
            result[sid] = {"error": f"{type(exc).__name__}: {exc}"[:120], "hit_total": 0, "today": []}
        else:
            if hits.height == 0:
                result[sid] = {"hit_total": 0, "today": [], "signal_date": None}
            else:
                hit_dates = hits["date"].unique().sort().to_list()
                target = hit_dates[-1] if hit_dates else latest
                today = hits.filter(hits["date"] == target)
                cols = [c for c in ("symbol", "date", "close", "score", "note") if c in hits.columns]
                result[sid] = {
                    "hit_total": int(hits.height),
                    "signal_date": str(target),
                    "today": today.select(cols).head(30).to_dicts(),
                }
        # 关键：立即释放 hits（alpha 类单个 140 万行，58 个累积会把内存打爆）
        del hits
        if i % 8 == 7:
            gc.collect()
        with _LOCK:
            _STATE[gen]["done"] = i + 1

    with _LOCK:
        _BATCH_CACHE.clear()
        _BATCH_CACHE[cache_key] = result
        _STATE[gen] = {
            "status": "done", "done": len(ids), "total": len(ids),
            "elapsed": round(time.time() - _STATE[gen]["started"], 1),
        }
    # 释放全表 frame（2.5 GB），否则批量选股后服务内存被吃光
    del frame
    try:
        from app import api as _api

        _api._FRAME_CACHE.clear()
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    return result


def _cache_paths() -> tuple[Path, Path]:
    from app import config

    d = Path(config.DATA_DIR)
    return d / "screener_cache.json", d / "screener_cache.meta.json"


def _read_cache(limit_per_strategy: int) -> dict | None:
    """读独立进程算好的结果。没有就返回 None。"""
    p, mp = _cache_paths()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    # 截断到调用方要的条数
    for it in d.get("items", []):
        rows = it.get("rows") or []
        if len(rows) > limit_per_strategy:
            it["rows"] = rows[:limit_per_strategy]
    d["cached"] = True
    d["state"] = {"status": "done", "source": "cache"}
    try:
        d["meta"] = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    except Exception:  # noqa: BLE001
        d["meta"] = {}
    return d


_INLINE_LOCK = threading.Lock()


def _start_inline_build(days: int = 300) -> bool:
    """在本进程里用后台线程重算缓存（打包环境唯一可行的路径）。"""
    if not _INLINE_LOCK.acquire(blocking=False):
        return False  # 已经在算了

    def _work() -> None:
        try:
            from .inline_build import build_cache

            build_cache(days=days)
        except Exception:  # noqa: BLE001 - 后台线程失败不影响服务
            pass
        finally:
            _INLINE_LOCK.release()

    threading.Thread(target=_work, daemon=True, name="screener-inline-build").start()
    return True


def _cache_stale(meta: dict | None, generation: str) -> bool:
    """缓存是否需要重算。

    · 数据代际变了 → 过期
    · 缓存不是今天生成的 → 过期（信号日期每天都会变）
    """
    if not meta:
        return True
    if str(meta.get("generation", "")) != generation[:8]:
        return True
    built = str(meta.get("built_at", ""))
    return built[:10] != date.today().isoformat()


def _spawn_build(days: int = 300) -> bool:
    """重算选股缓存。

    源码环境优先起独立进程（内存干净）；打包成 exe 后没有 python 解释器，
    scripts/ 也不在 _internal 里，所以退回「本进程后台线程」。
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[4]      # facet/
    script = root / "scripts" / "build_screener_cache.py"
    if getattr(sys, "frozen", False) or not script.exists():
        return _start_inline_build(days)
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(script), "--days", str(days)],
            cwd=str(root),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001
        return _start_inline_build(days)


_SNAP: dict[str, dict[str, dict]] = {}
_INDUSTRY: dict[str, dict[str, str]] = {}
_BOARDS: dict[str, dict[str, str]] = {}


def _limit_pct(sym: str) -> float:
    """涨停判定阈值（按板块，留 0.5 个点容差兜住复权噪声）。"""
    code = sym.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 19.5
    if code.startswith(("8", "4", "92")):
        return 29.5
    return 9.5


def _board_labels(store) -> dict[str, dict]:
    """symbol → {label: 「N天M板」, streak: 连续涨停天数}。按代际缓存。

    口径：从最近 12 个交易日里找涨停日，从最后一天往前连成一轮；相邻两次涨停
    间隔 ≤3 个交易日算同一轮（中途断板但没走远），超过就断开。
    N = 这一轮覆盖的交易日数，M = 涨停次数；streak = 末尾连续涨停的天数。
    """
    gen = store.require().generation
    if gen in _BOARDS:
        return _BOARDS[gen]

    import polars as pl

    lf = store.scan()
    dates = lf.select(pl.col("date").unique().sort().tail(12)).collect()["date"].to_list()
    if not dates:
        _BOARDS[gen] = {}
        return {}
    df = (
        lf.filter(pl.col("date").is_in(dates))
        .select(["symbol", "date", "change_pct"])
        .collect()
        .sort(["symbol", "date"])
    )

    out: dict[str, dict] = {}
    for (sym,), g in df.group_by(["symbol"], maintain_order=True):
        limit = _limit_pct(sym)
        flags = [
            (r["change_pct"] is not None and float(r["change_pct"]) >= limit)
            for r in g.iter_rows(named=True)
        ]
        idx = [i for i, f in enumerate(flags) if f]
        if not idx:
            continue
        run = [idx[-1]]
        for i in reversed(idx[:-1]):
            if run[0] - i <= 3:
                run.insert(0, i)
            else:
                break
        streak = 0
        for f in reversed(flags):
            if f:
                streak += 1
            else:
                break
        out[sym] = {"label": f"{run[-1] - run[0] + 1}天{len(run)}板", "streak": streak}
    _BOARDS[gen] = out
    return out


def _real_amount_map(latest: str) -> dict[str, float]:
    """真实成交额快照（未复权）：{symbol: 元}。

    库里 amount 是后复权口径（复权价 × 成交量），跨个股不可比，不能直接当成交额用。
    scripts/fetch_real_amount.py 抓的新浪行情快照才有真实值；取「不晚于 latest 的
    最新一份」，没有就返回空。
    """
    import json

    cands = [Path(config.DATA_DIR) / "real_amount"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Facet" / "data" / "real_amount")
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "data" / "real_amount")

    folder = next((c for c in cands if c.is_dir()), None)
    if folder is None:
        return {}
    files = sorted(folder.glob("*.json"))
    if not files:
        return {}
    eligible = [f for f in files if f.stem <= str(latest)] or files[-1:]
    try:
        raw = json.loads(eligible[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _market_snapshot(store) -> dict[str, dict]:
    """最新交易日的每股行情：涨跌幅 / 现价 / 成交额(亿) / 量比。按代际缓存。

    · 涨跌幅、量比来自 enriched（涨跌幅是比例，复权与否不影响）
    · 成交额取真实成交额快照（未复权）；拿不到就留空，不用后复权口径凑数
    · 现价：enriched 是后复权价，这里只作为兜底（前端优先用实时行情覆盖）
    """
    gen = store.require().generation
    if gen in _SNAP:
        return _SNAP[gen]

    import polars as pl

    lf = store.scan()
    names = lf.collect_schema().names()
    latest = lf.select(pl.col("date").max()).collect().item()
    cols = [c for c in ("symbol", "close", "change_pct", "volume", "vol_ma5", "amplitude") if c in names]
    snap = lf.filter(pl.col("date") == latest).select(cols).collect()
    amounts = _real_amount_map(str(latest))
    boards = _board_labels(store)

    out: dict[str, dict] = {}
    for r in snap.iter_rows(named=True):
        vr = None
        if r.get("volume") and r.get("vol_ma5"):
            vr = round(float(r["volume"]) / float(r["vol_ma5"]), 2)
        amt = amounts.get(r["symbol"])
        out[r["symbol"]] = {
            "change_pct": round(float(r["change_pct"]), 2) if r.get("change_pct") is not None else None,
            "close": round(float(r["close"]), 2) if r.get("close") is not None else None,
            "amount_yi": round(amt / 1e8, 2) if amt else None,
            "vol_ratio": vr,
            "boards_label": (boards.get(r["symbol"]) or {}).get("label"),
            "boards_streak": (boards.get(r["symbol"]) or {}).get("streak", 0),
            "amplitude": round(float(r["amplitude"]), 2) if r.get("amplitude") is not None else None,
        }
    _SNAP[gen] = out
    return out


def _industry_of(store) -> dict[str, str]:
    """symbol → 行业名（industry_members.json 反查）。按代际缓存。"""
    gen = store.require().generation
    if gen in _INDUSTRY:
        return _INDUSTRY[gen]
    mapping: dict[str, str] = {}
    try:
        from ..monitor.dashboard_api import _industry_map

        for ind, syms in (_industry_map() or {}).items():
            for s in syms:
                mapping.setdefault(s, ind)
    except Exception:  # noqa: BLE001
        mapping = {}
    _INDUSTRY[gen] = mapping
    return mapping


def _enrich(data: dict, store) -> dict:
    """给选股结果的每一行补上当日行情 + 所属行业。

    在响应阶段做，不动磁盘缓存格式：缓存一份、行情一份，行情换代后自然新鲜。
    """
    items = data.get("items") or []
    if not items:
        return data
    try:
        snap = _market_snapshot(store)
        ind = _industry_of(store)
    except Exception:  # noqa: BLE001 — 补不上行情不能把选股页拖挂
        return data

    # 风险库黑名单：命中风险规则的票不出现在选股结果里（回测/复盘桌面同一口径）
    try:
        from ..risk.store import blacklist_set

        blocked = blacklist_set()
    except Exception:  # noqa: BLE001
        blocked = set()

    if blocked:
        for it in items:
            rows = it.get("rows") or []
            it["rows"] = [r for r in rows if str(r.get("symbol", "")).upper() not in blocked]
            it["count"] = len(it["rows"])

    for it in items:
        for row in it.get("rows") or []:
            m = snap.get(row.get("symbol")) or {}
            row.update(m)
            row["industry"] = ind.get(row.get("symbol"))
    return data


@router.get("/screener/quote-extras")
def quote_extras(symbols: str = Query("", description="逗号分隔代码")) -> dict:
    """自选股用的补充字段：所属行业 / 几天几板 / 连续涨停天数。

    行情（价格、涨跌幅、成交额、市值、市盈率）走 /api/v1/realtime，
    这里只补那份数据里没有的、需要历史才能算出来的东西。
    """
    from app.runtime import STORE

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        return {"items": {}}
    try:
        ind = _industry_of(STORE)
        boards = _board_labels(STORE)
    except Exception:  # noqa: BLE001 — 补不上就当没有, 不影响页面
        return {"items": {}}
    return {
        "items": {
            s: {
                "industry": ind.get(s),
                "boards_label": (boards.get(s) or {}).get("label"),
                "boards_streak": (boards.get(s) or {}).get("streak", 0),
            }
            for s in syms
        }
    }


@router.get("/screener/all")
def screener_all(
    limit_per_strategy: int = Query(10, ge=1, le=50),
    refresh: bool = False,
    scope: str = Query("ranked", description="ranked=已启用策略 / all=全部"),
) -> dict:
    """批量选股。

    优先读独立进程算好的磁盘缓存（`data/screener_cache.json`）—— 页面秒开，
    服务常驻内存不受影响。缓存不存在时返回空结果并提示「正在计算」，
    同时可以触发一次后台构建。

    为什么不在请求里现算：15 个策略 × 全市场，峰值 3~4 GB，
    在 16 GB 机器上会把服务撑爆（实测 exe 涨到 4.5 GB 后 500）。
    """
    from app.runtime import ENGINE, STORE

    gen = STORE.require().generation

    if not refresh:
        cached = _read_cache(limit_per_strategy)
        if cached is not None:
            if _cache_stale(cached.get("meta"), gen):
                # 过期就顺手在后台重算，先把旧数据给页面（前端可据 stale 提示）
                started = _spawn_build(300)
                cached = dict(cached)
                cached["stale"] = True
                cached["refreshing"] = started
            return _enrich(cached, STORE)

    if refresh:
        spawned = _spawn_build(300)
        msg = "已启动后台重算，约 1~2 分钟后刷新页面" if spawned else "正在重算中，请稍候"
    else:
        msg = "还没有选股缓存，点「刷新」触发生成（约 3~5 分钟）"

    return {
        "generation": "",
        "cached": False,
        "state": {"status": "pending", "message": msg},
        "strategy_count": len(ENGINE.registry),
        "total_signals_today": 0,
        "items": [],
        "updated": "",
        "meta": {},
    }


@router.get("/screener/all/state")
def screener_state() -> dict:
    from app.runtime import STORE

    gen = STORE.require().generation
    with _LOCK:
        return {
            "generation": gen[:8],
            "state": dict(_STATE.get(gen) or {}),
            "cached": gen in _BATCH_CACHE,
            # 本进程内的重算线程是否还在跑（前端据此轮询，算完再刷新数据）
            "building": _INLINE_LOCK.locked(),
        }
