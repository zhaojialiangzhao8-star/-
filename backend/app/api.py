"""HTTP 层 — 薄，只做参数校验与序列化，业务都在 store / engine / indicators。"""
from __future__ import annotations

import gc
import json
import os
from pathlib import Path
from typing import Any

import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import config, indicators
from .backtest_stats import evaluate, price_matrix
from .engine import StrategyEngine, StrategyError
from .runtime import get_engine, get_store
from .store import GenerationStore, StoreUnavailable

router = APIRouter()   # prefix 在 main.py 里挂：/api（兼容旧）+ /api/v1（新版）

# 价格矩阵按代际缓存：命中表 + 这份矩阵 join 就能出指标，避免每次都重算
_PX_CACHE: dict[str, pl.DataFrame] = {}
_FRAME_CACHE: dict[str, pl.DataFrame] = {}
_BATCH_CACHE: dict[str, dict] = {}
_BARS_CACHE: dict[str, Any] = {}


class BtRequest(BaseModel):
    """账户级回测请求。"""

    strategy: str
    init_cash: float = 100_000.0
    risk_pct: float = Field(default=0.01, ge=0.001, le=0.2)
    risk_amount: float | None = Field(default=None, gt=0, description="单笔固定止损金额（元），优先级高于 risk_pct")
    max_positions: int = Field(default=5, ge=1, le=50)
    max_position_pct: float = Field(default=0.2, ge=0.01, le=1.0)
    stop_loss_pct: float | None = 0.08
    take_profit_pct: float | None = None
    max_hold_days: int | None = 20
    commission_rate: float = 0.00025
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 5.0
    start: str | None = None
    end: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


BAR_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "change_pct"]


def _frame_recent(store: GenerationStore, days: int, columns: list[str] | None = None) -> pl.DataFrame:
    """只加载最近 N 个自然日（可选限定列）。

    轻量接口专用：全市场 1150 万行整表加载要 2.5 GB，
    而 `kline` / `daily_signal` / `overview` 实际上只需要最近几十天。
    """
    from datetime import timedelta

    gen = store.require().generation
    key = f"{gen}|recent|{days}|{','.join(columns) if columns else '*'}"
    if key in _FRAME_CACHE:
        return _FRAME_CACHE[key]

    lf = store.scan()
    names = lf.collect_schema().names()
    max_d = lf.select(pl.col("date").max()).collect()[0, 0]
    lf = lf.filter(pl.col("date") >= (max_d - timedelta(days=days)))
    if columns:
        # 去重：调用方可能把已在列表里的列再传一次
        wanted = list(dict.fromkeys(c for c in columns if c in names))
        lf = lf.select(wanted)
    frame = lf.collect()
    _FRAME_CACHE[key] = frame
    return frame


def _norm_symbol(raw: str) -> str:
    """把 `002463` 补成 `002463.SZ` —— 前端手打经常不带后缀。

    6 开头 → .SH；688/689 → .SH；4/8/92 开头 → .BJ；其余 → .SZ。
    已经带 . 的原样返回。
    """
    s = (raw or "").strip().upper()
    if not s or "." in s:
        return s
    code = s
    for p in ("SH", "SZ", "BJ"):
        if code.startswith(p):
            code = code[2:]
            break
    if not (len(code) == 6 and code.isdigit()):
        return s
    if code.startswith("6") or code.startswith(("688", "689")):
        return f"{code}.SH"
    if code.startswith(("4", "8")) or code.startswith("92"):
        return f"{code}.BJ"
    return f"{code}.SZ"



def _frame_symbol(store: GenerationStore, symbol: str, limit: int = 500) -> pl.DataFrame:
    """只加载单只股票 —— `kline` 接口专用，别为了一个代码去读全表。"""
    lf = store.scan()
    names = lf.collect_schema().names()
    cols = [c for c in names if c not in ("benchmark",)]
    return (
        lf.filter(pl.col("symbol") == symbol.upper())
        .select(cols)
        .sort("date")
        .tail(limit)
        .collect()
    )



def _bars(store: GenerationStore, start=None, end=None):
    """按 (代际, 起始日, 结束日) 缓存 BarStore。

    全市场 1150 万行原样灌进 BarStore 要 2.5 GB，实测直接把进程撑爆。
    这里两条硬约束：
      · 只取 BarStore 真正用的 9 列（不搬 24 列派生指标）
      · 按回测区间裁剪，前后各留 400 天缓冲（覆盖最长持仓与均线窗口）
    """
    from datetime import date as _date
    from datetime import timedelta

    from .backtest.data import BarStore

    def _as_date(v):
        """接受 str 或 date，统一成 date。"""
        if v is None:
            return None
        return _date.fromisoformat(v) if isinstance(v, str) else v

    gen = store.require().generation
    s, e = _as_date(start), _as_date(end)
    lo = (s - timedelta(days=400)) if s else None
    hi = (e + timedelta(days=5)) if e else None
    key = f"{gen}|{lo}|{hi}"
    if key not in _BARS_CACHE:
        _BARS_CACHE.clear()
        _FRAME_CACHE.clear()          # 同一时刻内存里只留一份大表
        lf = store.scan()
        if lo:
            lf = lf.filter(pl.col("date") >= lo)
        if hi:
            lf = lf.filter(pl.col("date") <= hi)
        # 风险库黑名单：拉黑的票不参与回测（和选股/复盘桌面同一口径）。
        # 放进缓存键之前过滤，cache 里就不会含这些票，任何策略都拿不到。
        try:
            from .modules.risk.store import blacklist_set

            blocked = blacklist_set()
            if blocked:
                lf = lf.filter(~pl.col("symbol").is_in(list(blocked)))
        except Exception:  # noqa: BLE001
            pass
        names = lf.collect_schema().names()
        have = [c for c in BAR_COLUMNS if c in names]
        _BARS_CACHE[key] = BarStore(lf.select(have).collect())
    return _BARS_CACHE[key]


class BatchRequest(BaseModel):
    strategies: list[str] | None = None
    top_n: int = Field(default=10, ge=1, le=50)
    force: bool = False


class OneRequest(BaseModel):
    strategy: str
    top_n: int = Field(default=10, ge=1, le=50)


class BacktestRequest(BaseModel):
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    hold_days: int = Field(default=5, ge=1, le=60)
    top_n: int = Field(default=10, ge=1, le=50)


# 服务端只加载最近这么多年的数据。磁盘上是全量（2008 起 1,095 万行 1.1 GB），
# 但常驻内存只需要近 10 年：1,095 万行 × 24 列 ≈ 1.97 GB，缩到 10 年 ≈ 1.2 GB。
# 要跑更长的历史回测，用 scripts/batch_backtest_all.py —— 独立进程，不占服务内存。
FRAME_YEARS = int(os.getenv("FACET_FRAME_YEARS", "5"))


def _shrink(df: pl.DataFrame) -> pl.DataFrame:
    """只把 symbol 转成 Categorical —— 省 226 MB，且**不影响任何计算结果**。

    试过把 Float64 全转 Float32（再省 900 MB），但实测会改变策略结果：
      classic_rsi_oversold   24,660 → 24,652 条
      pattern_kline_forms    48,122 → 48,344 条
    原因是浮点精度丢失让 `RSI < 30`、`close >= ma5` 这类边界条件翻转。
    回测最忌讳结果不可复现，所以价格和指标列一律保持 Float64。

    要再省内存，只能从「少加载数据」入手（缩表 / 按列加载 / 走独立进程），
    不能靠降精度。
    """
    if "symbol" in df.columns and df.schema["symbol"] != pl.Categorical:
        return df.with_columns(pl.col("symbol").cast(pl.Categorical))
    return df


def _frame(store: GenerationStore) -> pl.DataFrame:
    """按代际缓存 enriched 表（只近 FRAME_YEARS 年）。

    全市场全量 1,095 万行 × 24 列 ≈ 2.0 GB，每次请求 collect 一次会直接把内存
    打爆（实测：连续两个 screener 请求就让服务 OOM 退出）。所以：
      1. 按代际缓存，且和 _bars 互斥 —— 同一时刻只留一份大表
      2. 只加载近 10 年 —— 1.97 GB → 1.2 GB，精度零损失
      3. symbol 转 Categorical —— 再省一点，同样零损失

    历史更长的回测走 CLI 脚本，那边是独立进程，可以吃满内存。
    """
    try:
        gen = store.require().generation
    except StoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if gen not in _FRAME_CACHE:
        from datetime import date as _dt_date
        from datetime import timedelta

        _FRAME_CACHE.clear()
        _BARS_CACHE.clear()          # 释放 BarStore，把内存让给 DataFrame
        lf = store.scan()
        if FRAME_YEARS > 0:
            since = _dt_date.today() - timedelta(days=365 * FRAME_YEARS + 30)
            lf = lf.filter(pl.col("date") >= since)
        _FRAME_CACHE[gen] = _shrink(lf.collect())
    return _FRAME_CACHE[gen]


_DATA_META: dict[str, dict] = {}


def _data_meta(store: GenerationStore) -> dict:
    """代际的标的数 / 交易日数 / 日期范围。按代际缓存，只扫一次聚合列。"""
    gen = store.require().generation
    if gen in _DATA_META:
        return _DATA_META[gen]
    try:
        agg = (
            store.scan()
            .select(
                pl.col("symbol").n_unique().alias("symbols"),
                pl.col("date").n_unique().alias("days"),
                pl.col("date").min().alias("date_start"),
                pl.col("date").max().alias("date_end"),
            )
            .collect()
            .row(0, named=True)
        )
        meta = {
            "symbols": int(agg["symbols"]),
            "days": int(agg["days"]),
            "date_start": str(agg["date_start"]),
            "date_end": str(agg["date_end"]),
        }
    except Exception:  # noqa: BLE001
        meta = {}
    _DATA_META[gen] = meta
    return meta


@router.get("/health")
def health(
    store: GenerationStore = Depends(get_store),
    engine: StrategyEngine = Depends(get_engine),
) -> dict:
    gen = store.current()
    meta = _data_meta(store) if gen else {}
    # app 标识用来区分「打包后的桌面程序」和「开发时起的 uvicorn」：
    # desktop.py 只复用前者。不加这个区分，开着开发服务时双击 exe 会被误判为
    # 已有实例，exe 直接退出、打开开发服务页面。
    import sys as _sys

    app_tag = "wenyi-desktop" if getattr(_sys, "frozen", False) else "wenyi-dev"

    # 数据代际的「过期天数」：所有从 parquet 派生的数字（K 线尾巴、涨跌分布、情绪、
    # 连板梯队、回测、板块指数）都停在这天，所以要让前端能一眼看出来，而不是静默变旧。
    date_end = meta.get("date_end")
    stale_days = 0
    if date_end:
        try:
            from datetime import date as _date

            stale_days = max(0, (_date.today() - _date.fromisoformat(str(date_end)[:10])).days)
        except Exception:  # noqa: BLE001
            stale_days = 0

    return {
        "app": app_tag,
        "status": "ok" if gen else "no-data",
        "version": config.VERSION,
        "generation": gen.generation if gen else None,
        "rows": gen.rows if gen else 0,
        "symbols": meta.get("symbols", 0),
        "days": meta.get("days", 0),
        "date_start": meta.get("date_start"),
        "date_end": date_end,
        "stale_days": stale_days,
        # 周末不算过期：周五的数据周一还新鲜
        "stale": stale_days >= 3,
        "strategies": len(engine.registry),
        "strategy_errors": engine.errors,
    }


@router.get("/overview")
def overview(store: GenerationStore = Depends(get_store)) -> dict:
    # 市场概览只看最新一天，没必要加载全表
    return indicators.market_overview(_frame_recent(store, 3))


@router.get("/strategies")
def strategies(engine: StrategyEngine = Depends(get_engine)) -> dict:
    return {"items": engine.list_strategies(), "errors": engine.errors}


@router.post("/strategies/reload")
def reload_strategies(engine: StrategyEngine = Depends(get_engine)) -> dict:
    engine.reload()
    return {"count": len(engine.registry), "errors": engine.errors}


# ---------------------------------------------------------------- 代码 → 中文名
_NAMES_CACHE: dict[str, str] | None = None


def _symbol_names() -> dict[str, str]:
    """代码 → 中文名（成交明细/个股弹窗展示用）。

    候选顺序：运行数据目录 → 打包内置 _MEIPASS/data → 仓库 backend/data。
    文件缺失时返回空表，前端自动退回只显示代码。
    """
    global _NAMES_CACHE
    if _NAMES_CACHE is not None:
        return _NAMES_CACHE

    import sys
    from pathlib import Path as _Path

    candidates = [config.DATA_DIR / "symbol_names.json"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(_Path(meipass) / "data" / "symbol_names.json")
    candidates.append(config.BACKEND_DIR / "data" / "symbol_names.json")

    for path in candidates:
        try:
            _NAMES_CACHE = json.loads(path.read_text(encoding="utf-8"))
            return _NAMES_CACHE
        except Exception:  # noqa: BLE001 - 候选路径逐个降级
            continue
    _NAMES_CACHE = {}
    return _NAMES_CACHE


@router.get("/data/audit")
def data_audit(store: GenerationStore = Depends(get_store)) -> dict:
    """数据完整性体检：个股库 / 名称表 / 板块成分 的覆盖情况。

    为什么需要：库里只有 4780 只，名称表 4800 只，板块成分表 5656 个 ——
    三份名单对不齐时，用户搜得到名字却看不到K线、或者板块指数只由 1 只股票合成。
    """
    import polars as pl

    from .modules.monitor.dashboard_api import _industry_map

    frame = store.scan().select(["symbol", "date"]).group_by("symbol").agg(
        pl.len().alias("n"), pl.col("date").max().alias("last")
    ).collect()
    store_syms = set(frame["symbol"].to_list())
    names = _symbol_names()

    missing_in_store = sorted(set(names) - store_syms)
    extra_in_store = sorted(store_syms - set(names))

    boards_total = 0
    board_members = 0
    board_missing: set[str] = set()
    thin: list[dict] = []
    try:
        mapping = _industry_map() or {}
        boards_total = len(mapping)
        for ind, syms in mapping.items():
            in_store = [s for s in syms if s in store_syms]
            board_members += len(in_store)
            board_missing.update(s for s in syms if s not in store_syms)
            if len(in_store) <= 2:
                thin.append({"board": ind, "in_store": len(in_store), "total": len(syms)})
    except Exception:  # noqa: BLE001
        mapping = {}

    thin.sort(key=lambda x: (x["in_store"], x["board"]))

    return {
        "generation": store.current().generation if store.current() else None,
        "stocks": {
            "in_store": len(store_syms),
            "in_names": len(names),
            "missing_in_store": len(missing_in_store),
            "missing_preview": [{"symbol": s, "name": names.get(s, "")} for s in missing_in_store[:30]],
            "extra_in_store": len(extra_in_store),
            "bars_min": int(frame["n"].min()) if frame.height else 0,
            "bars_median": int(frame["n"].median()) if frame.height else 0,
            "latest_date": str(frame["last"].max()) if frame.height else None,
            "stale_today": int(frame.filter(pl.col("last") < frame["last"].max()).height),
        },
        "boards": {
            "total": boards_total,
            "members_in_store": board_members,
            "members_missing": len(board_missing),
            "thin_preview": thin[:10],
            "thin_count": len(thin),
        },
    }


@router.get("/symbols/search")
def symbols_search(
    q: str = Query(..., min_length=1, description="代码或名称，支持模糊"),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    """按代码 / 名称模糊搜股票（个股分析的搜索框用）。

    匹配优先级：代码完全相等 → 代码前缀 → 名称完全相等 → 名称前缀 → 名称包含。
    还认「600519」「600519.SH」「sh600519」「贵州茅台」这几种写法。
    """
    import re as _re

    raw = q.strip()
    if not raw:
        return {"count": 0, "items": []}
    names = _symbol_names()
    kw = raw.upper().replace(" ", "")

    # 名称/代码都用大写比较；中文不受影响
    scored: list[tuple[int, str, str]] = []
    for sym, name in names.items():
        s = sym.upper()
        code = s.split(".")[0]
        nm = (name or "").upper()
        score = None
        if kw == s or kw == code:
            score = 0
        elif code.startswith(kw) or s.startswith(kw):
            score = 1
        elif kw in (nm,):
            score = 2
        elif nm.startswith(kw):
            score = 3
        elif kw in nm:
            score = 4
        elif _re.sub(r"^(SH|SZ|BJ)", "", kw) == code:
            score = 0
        if score is not None:
            scored.append((score, s, name or ""))
    scored.sort(key=lambda x: (x[0], x[1]))
    items = [{"symbol": s, "name": n} for _, s, n in scored[:limit]]
    return {"count": len(items), "items": items}


@router.get("/symbol-names")
def symbol_names() -> dict:
    items = _symbol_names()
    return {"count": len(items), "items": items}


@router.get("/screener")
def screener(
    strategy: str = Query(...),
    limit: int = Query(30, ge=1, le=500),
    params: str = Query("{}", description="JSON 字符串形式的策略参数覆盖"),
    as_of: str | None = Query(None, description="指定信号日 YYYY-MM-DD，默认最新交易日"),
    fallback_days: int = Query(
        0, ge=0, le=60, description="若当日无信号，向前回溯最多 N 个交易日取最近有信号的一天"
    ),
    store: GenerationStore = Depends(get_store),
    engine: StrategyEngine = Depends(get_engine),
) -> dict:
    """选股：跑策略拿信号。支持指定日期与「向前回溯到最近有信号的一天」。"""
    from datetime import date as _date

    try:
        overrides = json.loads(params or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"params 不是合法 JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise HTTPException(status_code=400, detail="params 必须是 JSON 对象")

    frame = _frame(store)
    try:
        hits = engine.run(strategy, overrides, frame)
    except StrategyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    latest = _date.fromisoformat(as_of) if as_of else frame["date"].max()
    if hits.height == 0:
        return {"date": str(latest), "hit_total": 0, "hit_today": 0, "rows": [], "columns": []}

    hit_dates = [d for d in hits["date"].unique().sort().to_list() if d <= latest]
    target = hit_dates[-1] if hit_dates else latest
    if fallback_days > 0 and target != latest:
        # 只回溯 N 个自然日范围内
        if (latest - target).days > fallback_days * 2:
            return {
                "date": str(latest),
                "signal_date": str(target),
                "hit_total": hits.height,
                "hit_today": 0,
                "rows": [],
                "columns": list(hits.columns),
            }
    today = hits.filter(pl.col("date") == target).head(limit)
    return {
        "date": str(latest),
        "signal_date": str(target),
        "hit_total": hits.height,
        "hit_today": today.height,
        "rows": today.to_dicts(),
        "columns": list(today.columns),
    }


_TODAY_BAR_CACHE: dict[str, tuple[float, dict | None]] = {}


def _today_bar(symbol: str, last_close: float, last_date: str) -> dict | None:
    """库里最新一天之后，用实时行情补一根「今天」的 K 线（复权口径对齐）。

    为什么需要：行情库靠手动跑 fetch_all_market.py 重建代际，没人天天跑，
    所以 K 线图总是停在上一个代际。这里只给**看图**补最后一根，不动库、
    不影响回测与策略（那些仍然用入库的日线）。

    复权对齐：实时价是未复权的，库是后复权的 → 用「库内最后收盘 / 实时昨收」做因子。
    """
    import time as _t
    import urllib.request
    from datetime import date as _d

    today = _d.today().isoformat()
    if today <= last_date:
        return None
    cached = _TODAY_BAR_CACHE.get(symbol)
    if cached and _t.time() - cached[0] < 30:
        return cached[1]

    code, _, mkt = symbol.partition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt.upper(), "")
    bar: dict | None = None
    if prefix:
        url = f"http://hq.sinajs.cn/list={prefix}{code}"
        req = urllib.request.Request(
            url,
            headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                text = resp.read().decode("gbk", errors="ignore")
            fields = text.split('"')[1].split(",")
            if len(fields) >= 10:
                o, pre, cur, hi, lo = (float(fields[i]) for i in (1, 2, 3, 4, 5))
                vol = float(fields[8])          # 股
                if pre > 0 and cur > 0:
                    f = last_close / pre        # 复权因子
                    bar = {
                        "date": today,
                        "open": round(o * f, 3),
                        "high": round(hi * f, 3),
                        "low": round(lo * f, 3),
                        "close": round(cur * f, 3),
                        "volume": round(vol / 100, 0),   # 库里 volume 单位是手
                        "realtime": True,
                    }
        except Exception:  # noqa: BLE001 - 实时补丁失败就当没有
            bar = None
    _TODAY_BAR_CACHE[symbol] = (_t.time(), bar)
    return bar


@router.get("/kline/{symbol}")
def kline(
    symbol: str,
    limit: int = Query(120, ge=20, le=1000),
    start: str | None = Query(
        None,
        description="起始日 YYYY-MM-DD：给了就返回该日至今的全部日线（回测买卖点弹窗用，上限 3000 根）",
    ),
    period: str = Query("day", description="day / week / month / quarter / year"),
    store: GenerationStore = Depends(get_store),
) -> dict:
    # 只取这一只股票，别为了一个代码去读 1150 万行全表
    symbol = _norm_symbol(symbol)
    tail = 3000 if start else limit
    frame = _frame_symbol(store, symbol, tail)
    if start:
        # 单只股票 5 年日线约 1200 行，按 start 截断后全给前端，买卖点才不会被切掉
        from datetime import date as _d

        try:
            since = _d.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"start 不是合法日期: {start}") from exc
        frame = frame.filter(pl.col("date") >= since)

    # 周/月/季/年：从日线聚合（零额外数据成本，瞬间切换）
    period = (period or "day").lower()
    DUR = {"week": "1w", "month": "1mo", "quarter": "1q", "year": "1y"}
    if period in DUR:
        frame = (
            frame.with_columns(pl.col("date").dt.truncate(DUR[period]).alias("date"))
            .group_by("date")
            .agg(
                pl.col("open").first(),
                pl.col("high").max(),
                pl.col("low").min(),
                pl.col("close").last(),
                pl.col("volume").sum(),
            )
            .sort("date")
        )
        if frame.height == 0:
            raise HTTPException(status_code=404, detail=f"没有 {symbol} 的{period}数据")
        rows = frame.tail(tail).to_dicts()
        for row in rows:
            row["date"] = str(row["date"])
        return {"symbol": symbol.upper(), "period": period, "rows": rows}

    rows = (
        frame.sort("date")
        .tail(tail)
        .select([c for c in ["date", "open", "high", "low", "close", "volume", "ma5", "ma20"] if c in frame.columns])
        .to_dicts()
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"没有 {symbol} 的数据")
    for row in rows:
        row["date"] = str(row["date"])

    # 库里的最后一天之后，补一根今天的实时 bar（仅用于看图）
    tail_bar = _today_bar(symbol, float(rows[-1]["close"]), str(rows[-1]["date"]))
    if tail_bar:
        rows = rows + [tail_bar]
    return {"symbol": symbol.upper(), "period": "day", "rows": rows}


# ---------------------------------------------------------------- 分钟级 K 线
# 60 分钟数据现抓东财（klt=60），2h/4h 从 60 分钟聚合。
# 为什么不在库里存：4780 只 × 多年 × 4 根/天 = 4600 万行，抓取要十几个小时，
# 而分钟线只在「临时看看这只票」时有用，现抓 1~2 秒可接受。
_MIN_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _em_secid(symbol: str) -> str:
    """600519.SH → 1.600519；000001.SZ → 0.000001。"""
    code, _, mkt = symbol.partition(".")
    return f"{'1' if mkt.upper() == 'SH' else '0'}.{code}"


def _fetch_min_bars(symbol: str, klt: int = 60, limit: int = 1200) -> list[dict]:
    """从东财拉分钟 K 线；返回 [{date, open, high, low, close, volume}]，date 带时分。"""
    import json as _json
    import shutil
    import subprocess

    curl = shutil.which("curl") or r"C:\Windows\System32\curl.exe"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={_em_secid(symbol)}"
        "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
        f"&klt={klt}&fqt=1&end=20500101&lmt={limit}"
    )
    args = [curl, "-sS", "--max-time", "15", "-A", "Mozilla/5.0",
            "-e", "https://quote.eastmoney.com/", url]
    flags = 0x08000000 if __import__("os").name == "nt" else 0
    p = subprocess.run(args, capture_output=True, timeout=20, creationflags=flags)
    if p.returncode != 0:
        raise HTTPException(502, f"抓分钟数据失败: {p.stderr.decode('utf-8', 'replace')[:120]}")
    d = _json.loads(p.stdout.decode("utf-8", "replace"))
    klines = ((d.get("data") or {}).get("klines")) or []
    rows = []
    for k in klines:
        f = k.split(",")
        if len(f) < 6:
            continue
        rows.append({
            "date": f[0],           # "2026-11-13 14:00"
            "open": float(f[1]),
            "close": float(f[2]),
            "high": float(f[3]),
            "low": float(f[4]),
            "volume": float(f[5]),
        })
    return rows


@router.get("/kline-min/{symbol}")
def kline_min(
    symbol: str,
    period: int = Query(60, description="60 / 120 / 240（分钟）"),
    limit: int = Query(600, ge=50, le=2000),
) -> dict:
    """分钟级 K 线：60 分钟现抓，120/240 从 60 分钟聚合（2 根 / 4 根一根）。"""
    import time as _t

    sym = _norm_symbol(symbol)
    if period not in (60, 120, 240):
        raise HTTPException(400, "period 只支持 60 / 120 / 240")

    key = f"{sym}:60"
    hit = _MIN_CACHE.get(key)
    if hit and _t.time() - hit[0] < 120:
        base = hit[1]
    else:
        base = _fetch_min_bars(sym, 60, 1500)
        _MIN_CACHE[key] = (_t.time(), base)
    if not base:
        raise HTTPException(404, f"没有 {sym} 的分钟数据")

    if period == 60:
        rows = base[-limit:]
    else:
        step = period // 60
        merged = []
        for i in range(0, len(base) - step + 1, step):
            chunk = base[i:i + step]
            merged.append({
                "date": chunk[-1]["date"],
                "open": chunk[0]["open"],
                "high": max(c["high"] for c in chunk),
                "low": min(c["low"] for c in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(c["volume"] for c in chunk),
            })
        rows = merged[-limit:]

    return {"symbol": sym.upper(), "period": f"{period}m", "count": len(rows), "rows": rows}


@router.get("/kline-trend/{symbol}")
def kline_trend(symbol: str, days: int = Query(1, ge=1, le=5)) -> dict:
    """分时：当日（或多日）1 分钟走势，走东财 trends2。

    返回的是「点」不是 K 线：每个点只有价格和量，前端用折线/面积图画。
    """
    import json as _json
    import shutil
    import subprocess

    sym = _norm_symbol(symbol)
    curl = shutil.which("curl") or r"C:\Windows\System32\curl.exe"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        f"?secid={_em_secid(sym)}&ndays={days}"
        "&fields1=f1,f2,f3,f4,f5,f6,f7,f8"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
    )
    args = [curl, "-sS", "--max-time", "15", "-A", "Mozilla/5.0",
            "-e", "https://quote.eastmoney.com/", url]
    flags = 0x08000000 if __import__("os").name == "nt" else 0
    p = subprocess.run(args, capture_output=True, timeout=20, creationflags=flags)
    if p.returncode != 0:
        raise HTTPException(502, f"抓分时失败: {p.stderr.decode('utf-8', 'replace')[:120]}")

    d = _json.loads(p.stdout.decode("utf-8", "replace"))
    data = d.get("data") or {}
    trends = data.get("trends") or []
    rows = []
    for t in trends:
        f = t.split(",")
        if len(f) < 6:
            continue
        # time, open, close, high, low, volume, amount, avg
        rows.append({
            "date": f[0],                    # "2026-11-14 09:30"
            "open": float(f[1]),
            "close": float(f[2]),
            "high": float(f[3]),
            "low": float(f[4]),
            "volume": float(f[5]),
            "avg": float(f[7]) if len(f) > 7 and f[7] else None,
        })
    if not rows:
        raise HTTPException(404, f"没有 {sym} 的分时数据（可能不在交易时段）")
    return {
        "symbol": sym.upper(),
        "name": data.get("name") or "",
        "pre_close": float(data.get("preClose") or 0),
        "count": len(rows),
        "rows": rows,
    }


# ---------------------------------------------------------------- 回测历史（落盘）
RUNS_DIR = Path(config.DATA_DIR) / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)


def _save_run(kind: str, params: dict, result: dict, title: str = "") -> str:
    """把一次回测的参数 + 结果写进 data/runs/<id>.json，重启后还能翻出来。"""
    import json as _json
    import time as _time
    import uuid as _uuid

    run_id = f"{_time.strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:6]}"
    payload = {
        "id": run_id,
        "kind": kind,
        "title": title,
        "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": params,
        "metrics": result.get("metrics") or {},
        "result": result,
    }
    try:
        (RUNS_DIR / f"{run_id}.json").write_text(
            _json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except OSError:
        return ""
    return run_id


@router.get("/runs")
def list_runs(limit: int = Query(30, ge=1, le=200)) -> dict:
    """回测历史列表（只回元信息，不带 trades）。"""
    import json as _json

    items = []
    for f in sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            d = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        m = d.get("metrics") or {}
        items.append(
            {
                "id": d.get("id"),
                "kind": d.get("kind"),
                "title": d.get("title") or "",
                "created_at": d.get("created_at"),
                "params": d.get("params") or {},
                "metrics": {
                    k: m.get(k)
                    for k in ("total_return", "annual_return", "max_drawdown", "sharpe", "win_rate", "trades")
                    if k in m
                },
                "trade_count": len((d.get("result") or {}).get("trades") or []),
            }
        )
    return {"items": items, "count": len(items)}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    import json as _json

    f = RUNS_DIR / f"{run_id}.json"
    if not f.exists():
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return _json.loads(f.read_text(encoding="utf-8"))


@router.delete("/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    f = RUNS_DIR / f"{run_id}.json"
    if not f.exists():
        raise HTTPException(status_code=404, detail="回测记录不存在")
    f.unlink()
    return {"deleted": run_id}


@router.post("/backtest")
def backtest(
    req: BacktestRequest,
    store: GenerationStore = Depends(get_store),
    engine: StrategyEngine = Depends(get_engine),
) -> dict:
    frame = _frame(store)
    try:
        out = engine.backtest(
            req.strategy, req.params, frame, hold_days=req.hold_days, top_n=req.top_n
        )
    except StrategyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rid = _save_run(
        "simple",
        {"strategy": req.strategy, "params": req.params, "hold_days": req.hold_days, "top_n": req.top_n},
        out,
        title=req.strategy,
    )
    if rid:
        out["run_id"] = rid
    return out


@router.get("/generations")
def generations(store: GenerationStore = Depends(get_store)) -> dict:
    return {"items": store.history()}


# ---------------------------------------------------------------- 账户级回测
@router.post("/bt/run")
def bt_run(
    req: BtRequest,
    store: GenerationStore = Depends(get_store),
    engine: StrategyEngine = Depends(get_engine),
) -> dict:
    """真实账户回测：10 万本金、T+1、次日开盘成交、涨跌停/停牌拒绝、100 股整数倍。"""
    from datetime import date as _date

    from .backtest.engine import BacktestEngine
    from .backtest.models import BacktestConfig, FeeConfig

    # 算信号**不要**用 _frame 那份常驻全表：它解码后要 2.5 GB，加上 BarStore
    # 和引擎中间量，在 16 GB 机器上直接被系统硬杀（没有 traceback、没有日志，
    # 用户看到的是窗口一闪消失）。
    #
    # 回测只往回看几天到几百天（最长 MA250 / 52 周），所以这里单独按需加载
    # 「回测起点前 300 天至今」的切片，几百兆就够，跟常驻大表完全解耦。
    import gc as _gc

    from datetime import timedelta as _td

    # 区间先解析：算信号、内存守卫、建 BarStore 都要用
    start = _date.fromisoformat(req.start) if req.start else None
    end = _date.fromisoformat(req.end) if req.end else None

    sig_start = (start or _date(2021, 1, 1)) - _td(days=300)
    sig_frame = store.scan().filter(pl.col("date") >= sig_start).collect()
    try:
        hits = engine.run(req.strategy, req.params or {}, sig_frame)
    except StrategyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        del sig_frame
        _gc.collect()

    cfg = BacktestConfig(
        init_cash=req.init_cash,
        risk_pct=req.risk_pct,
        risk_amount=req.risk_amount,
        max_positions=req.max_positions,
        max_position_pct=req.max_position_pct,
        stop_loss_pct=req.stop_loss_pct,
        take_profit_pct=req.take_profit_pct,
        max_hold_days=req.max_hold_days,
        fees=FeeConfig(
            commission_rate=req.commission_rate,
            stamp_tax_rate=req.stamp_tax_rate,
            slippage_bps=req.slippage_bps,
        ),
    )

    # 内存守卫：BarStore 是 slots dataclass（约 120 字节/行）。
    # 不设硬性天数上限 —— 用实测可用内存判断，够就让它跑。
    span_days = ((end or _date.today()) - (start or _date(2024, 1, 1))).days
    _need_mem(max(1, span_days) * 4000, "账户回测")
    bars = _bars(store, start, end)
    try:
        res = BacktestEngine(cfg, bars).run(hits, start=start, end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    trades = [
        {
            "symbol": f.symbol,
            "side": f.side.value,
            "qty": f.qty,
            "price": f.price,
            "dt": f.dt.date().isoformat(),
            "signal_dt": f.signal_dt.date().isoformat(),
            "hold_days": (f.dt.date() - f.signal_dt.date()).days,
            "reason": f.reason,
            "fees": f.fees.as_dict(),
            "pnl": f.realized_pnl,
            "pnl_pct": f.pnl_pct,
            "entry_price": f.entry_price,
            "source": f.source,
        }
        for f in res.fills
    ]
    result = {
        "metrics": res.metrics,
        "nav": res.nav_series,
        "trades": trades,
        "rejected": res.rejected,
        "warnings": res.warnings,
        "config": {
            "init_cash": cfg.init_cash,
            "risk_pct": cfg.risk_pct,
            "max_positions": cfg.max_positions,
            "max_position_pct": cfg.max_position_pct,
            "stop_loss_pct": cfg.stop_loss_pct,
            "max_hold_days": cfg.max_hold_days,
            "period": cfg.period,
        },
    }

    # 显式回收：hits 是几百万行，BarStore 是几十万个对象。不主动释放的话，
    # polars / Python 的内存池不会立刻还给系统，下一次回测就被内存守卫挡住。
    del hits
    _BARS_CACHE.clear()
    try:
        import gc

        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    run_id = _save_run("account", req.model_dump(), result, title=req.strategy)
    if run_id:
        result["run_id"] = run_id
    return result


# ---------------------------------------------------------------- 每日信号
REVERSAL_TH = -0.15
REVERSAL_VR = 1.2
REVERSAL_STOP = 0.12
BOARD_MAP = {"创业板": "创业板", "科创板": "科创板", "北交所": "北交所"}


def _board(symbol: str) -> str:
    code = symbol.split(".")[0]
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


@router.get("/daily-signal")
def daily_signal(
    risk: float = Query(3000, ge=500, le=100_000, description="每笔固定止损金额（元）"),
    max_pos: int = Query(5, ge=1, le=20),
    as_of: str | None = Query(None, description="YYYY-MM-DD，默认最新"),
    store: GenerationStore = Depends(get_store),
) -> dict:
    """急跌反弹的次日买入清单。仓位由固定止损金额反推。"""
    from datetime import date as _date

    # 只需要最近 60 天（3 日跌幅 + 量比最多回看几天），不加载全表
    frame = _frame_recent(store, 60, BAR_COLUMNS)
    df = frame.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    ).sort(["symbol", "date"])
    target = _date.fromisoformat(as_of) if as_of else df["date"].max()
    latest = df.filter(pl.col("date") <= target)
    last_day = latest["date"].max()

    d = latest.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("_r3"),
        (pl.col("volume") / pl.col("volume").shift(1).over("symbol")).alias("_vr"),
    )
    sig = (
        d.filter(pl.col("date") == last_day)
        .filter(
            (pl.col("_r3") < REVERSAL_TH)
            & (pl.col("close") > pl.col("open"))
            & (pl.col("_vr") > REVERSAL_VR)
            & pl.col("_r3").is_not_null()
        )
        .sort("_r3")
        .head(max_pos)
    )

    rows = []
    for r in sig.iter_rows(named=True):
        px = float(r["close"])
        stop = round(px * (1 - REVERSAL_STOP), 2)
        per_share = px - stop
        qty = int((risk / per_share) // 100 * 100) if per_share > 0 else 0
        rows.append(
            {
                "symbol": r["symbol"],
                "board": _board(r["symbol"]),
                "drop3": round(float(r["_r3"]) * 100, 2),
                "vol_ratio": round(float(r["_vr"]), 2),
                "close": px,
                "buy_low": round(px * 0.99, 2),
                "buy_high": round(px * 1.01, 2),
                "stop": stop,
                "qty": qty,
                "amount": round(qty * px, 2),
                "risk_amount": round(qty * per_share, 2),
                "change_pct": round(float(r.get("change_pct") or 0), 2),
            }
        )

    total = sum(x["amount"] for x in rows)
    return {
        "date": str(last_day),
        "risk_per_trade": risk,
        "count": len(rows),
        "rows": rows,
        "total_amount": round(total, 2),
        "total_risk": round(sum(x["risk_amount"] for x in rows), 2),
        "rule": f"3日跌幅<{REVERSAL_TH*100:.0f}% + 收阳 + 量比>{REVERSAL_VR} | 止损 {REVERSAL_STOP*100:.0f}% | 持有 5 日",
    }


# ---------------------------------------------------------------- 连板统计
_LIMITUP_CACHE: dict[str, dict] = {}


@router.get("/limit-up-stats")
def limit_up_stats(store: GenerationStore = Depends(get_store)) -> dict:
    """1进2 / 2进3 / 3进4 的晋级率与可执行收益（次日开盘买、收盘卖）。

    结果只取决于数据代际，所以按代际缓存 —— 全市场 1150 万行算一次就够，
    重复请求直接返回缓存（否则每次都把内存打满）。
    """
    gen = store.require().generation
    if gen in _LIMITUP_CACHE:
        return _LIMITUP_CACHE[gen]

    # 只取 7 列（+ 板块推导），不把 24 列全搬进内存
    lf = store.scan().select(["symbol", "date", "open", "high", "low", "close", "change_pct"])
    df = lf.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    ).collect().sort(["symbol", "date"])
    boards = pl.DataFrame({"symbol": df["symbol"].unique().to_list()}).with_columns(
        pl.col("symbol").map_elements(_board, return_dtype=pl.Utf8).alias("board")
    )
    df = df.join(boards, on="symbol", how="left").with_columns(
        pl.when(pl.col("board").is_in(["创业板", "科创板"]))
        .then(19.5)
        .when(pl.col("board") == "北交所")
        .then(30.0)
        .otherwise(9.5)
        .alias("_thr")
    )
    df = df.with_columns(
        (pl.col("change_pct") >= pl.col("_thr")).alias("is_lu"),
        pl.int_range(0, pl.len()).over("symbol").alias("_idx"),
    )
    df = df.with_columns(
        pl.when(~pl.col("is_lu")).then(pl.col("_idx")).otherwise(None).alias("_lz")
    ).with_columns(pl.col("_lz").forward_fill().over("symbol").alias("_lzf"))
    df = df.with_columns(
        pl.when(pl.col("is_lu"))
        .then(pl.col("_idx") - pl.col("_lzf").fill_null(-1))
        .otherwise(0)
        .alias("boards")
    )
    df = df.with_columns(
        pl.col("is_lu").shift(-1).over("symbol").alias("next_lu"),
        pl.col("open").shift(-1).over("symbol").alias("n_open"),
        pl.col("close").shift(-1).over("symbol").alias("n_close"),
        pl.col("change_pct").shift(-1).over("symbol").alias("n_chg"),
    ).with_columns((pl.col("n_close") / pl.col("n_open") - 1).alias("oc"))
    lu = df.filter(pl.col("is_lu") & pl.col("next_lu").is_not_null())

    buckets = [(1, 1, "1板→2板"), (2, 2, "2板→3板"), (3, 3, "3板→4板"),
               (4, 5, "4~5板"), (6, 99, "6板+")]
    out = []
    for lo, hi, label in buckets:
        sub = lu.filter((pl.col("boards") >= lo) & (pl.col("boards") <= hi))
        if sub.height == 0:
            continue
        out.append({
            "label": label,
            "n": sub.height,
            "promote": round(float(sub["next_lu"].mean()) * 100, 1),
            "next_chg": round(float(sub["n_chg"].mean()), 2),
            "oc_return": round(float(sub["oc"].mean()) * 100, 2),
            "win_rate": round(float((sub["oc"] > 0).mean()) * 100, 1),
        })
    total_lu = int(lu.height)
    result = {"buckets": out, "total_limit_up_days": total_lu,
              "sample": f"{df['symbol'].n_unique()} 只 · {df['date'].min()} ~ {df['date'].max()}"}
    _LIMITUP_CACHE[gen] = result
    return result


# ---------------------------------------------------------------- 数据源
@router.get("/data-sources")
def data_sources() -> dict:
    """能力矩阵与路由：哪个源支持什么、首选是哪个。"""
    from .infrastructure.data_sources.base import Capability
    from .infrastructure.data_sources.registry import registry

    caps = registry.capabilities()
    routing = {}
    for cap in Capability:
        found = [p.name for p in registry.find(cap)]
        routing[cap.value] = {"providers": found, "preferred": found[0] if found else None}
    return {"providers": caps, "routing": routing}


def _free_mem_gb() -> float:
    """当前可用物理内存（GB）。Windows 用 GlobalMemoryStatusEx，其他平台返回很大值。"""
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / 1024**3
    except Exception:  # noqa: BLE001
        return 99.0


def _need_mem(bars_rows: int, what: str = "回测") -> None:
    """内存守卫：不够就直接报 503，别让进程被系统静默杀掉。

    BarStore 用的是 `slots=True` 的 dataclass（实测 120 字节/行，不是带
    __dict__ 的 250），再给策略计算留 1.5 GB。
    """
    # 余量 2.5 GB 而不是 1.5：exe 里除了 frame 和 BarStore，还有 pywebview /
    # WebView2 子进程常驻几百兆。留 1.5 GB 时估算说"够"，实际却被系统硬杀
    # （没有 traceback、没有日志、窗口一闪消失）。
    need = bars_rows * 120 / 1024**3 + 0.8   # 余量 0.8 GB：Windows 归还内存滞后，
    # free physical 偏低但实际分配得出来；真不够会抛 MemoryError（有 traceback）
    free = _free_mem_gb()
    if free < need:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{what}需要约 {need:.1f} GB 可用内存，当前只有 {free:.1f} GB。"
                f"请把回测区间改短，或关掉其他占用内存的程序；"
                f"超大区间请用命令行 scripts/batch_backtest_all.py（不受 Web 服务内存限制）。"
            ),
        )


@router.get("/bt/capabilities")
def bt_capabilities() -> dict:
    return {
        "periods": ["D1"],
        "periods_planned": ["M30", "combo"],
        "supported": [
            "T+1（当日买入不可卖）",
            "次日开盘成交（不用信号当根收盘价）",
            "涨停开盘不买 / 跌停开盘不卖 / 停牌不成交",
            "100 股整数倍",
            "佣金（最低 5 元）/ 印花税 / 过户费 / 滑点",
            "固定风险仓位（risk_pct）",
            "单票上限 / 最大持仓数",
            "止损 / 止盈 / 移动止损 / 最大持仓天数",
            "跳空止损按开盘价成交（亏损会大于计划）",
        ],
        "not_supported": [
            "盘中订单队列（只有 OHLC，无法还原成交先后）",
            "集合竞价撮合量",
            "30 分钟周期（数据源限制，见 docs/AUDIT-3.0.md）",
            "融资融券 / 做空",
            "历史 ST 状态（只有当前标记）",
        ],
    }


# ---------------------------------------------------------------- 批量回测
def _px(store: GenerationStore, frame: pl.DataFrame) -> tuple[str, pl.DataFrame]:
    gen = store.require().generation
    if gen not in _PX_CACHE:
        _PX_CACHE.clear()  # 只留当前代际，避免 commit 内存越攒越多
        _PX_CACHE[gen] = price_matrix(frame)
    return gen, _PX_CACHE[gen]


def _run_one(engine: StrategyEngine, frame: pl.DataFrame, px: pl.DataFrame, sid: str, top_n: int) -> dict:
    spec = engine.registry.get(sid)
    if spec is None:
        return {"id": sid, "name": sid, "error": "策略不存在"}
    try:
        hits = engine.run(sid, {}, frame)
    except Exception as exc:  # 单个策略失败不该拖垮整张表
        return {"id": sid, "name": spec.name, "tag": ",".join(spec.tags[:1]), "error": str(exc)}
    try:
        return evaluate(sid, spec.name, hits, px, top_n=top_n, tag=",".join(spec.tags[:1]))
    finally:
        del hits
        gc.collect()


def _sort_rows(rows: list[dict]) -> list[dict]:
    def key(r: dict) -> tuple:
        v = r.get("B1")
        return (v is None, -(v or -9))

    return sorted(rows, key=key)


@router.post("/backtest/one")
def backtest_one(
    req: OneRequest,
    store: GenerationStore = Depends(get_store),
    engine: StrategyEngine = Depends(get_engine),
) -> dict:
    frame = _frame(store)
    gen, px = _px(store, frame)
    row = _run_one(engine, frame, px, req.strategy, req.top_n)
    row["generation"] = gen
    row["top_n"] = req.top_n
    return row


@router.post("/backtest/batch")
def backtest_batch(
    req: BatchRequest,
    store: GenerationStore = Depends(get_store),
    engine: StrategyEngine = Depends(get_engine),
) -> dict:
    frame = _frame(store)
    gen, px = _px(store, frame)
    ids = req.strategies or list(engine.registry)
    cache_key = f"{gen}|{req.top_n}|{','.join(ids)}"
    if not req.force and cache_key in _BATCH_CACHE:
        return _BATCH_CACHE[cache_key]

    rows = []
    for sid in ids:
        rows.append(_run_one(engine, frame, px, sid, req.top_n))
    result = {
        "generation": gen,
        "top_n": req.top_n,
        "rows": _sort_rows(rows),
        "strategies": len(rows),
        "symbols": frame["symbol"].n_unique(),
        "dates": frame["date"].n_unique(),
        "from": str(frame["date"].min()),
        "to": str(frame["date"].max()),
    }
    _BATCH_CACHE.clear()
    _BATCH_CACHE[cache_key] = result
    return result
