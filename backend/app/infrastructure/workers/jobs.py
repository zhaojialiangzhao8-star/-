"""任务实现 — 每个 job 是一个纯函数 (params, progress) -> result。

约定：
  · progress(p: float, msg: str = "") 把进度写回 SQLite，前端轮询能看到
  · 返回值必须是 JSON 可序列化
  · 抛异常 = 任务失败，错误信息会写进 tasks.error

当前支持：
  · backtest  单策略账户回测
  · grid      参数网格搜索（仓位/止损/持仓天数）
  · scan      全市场扫描（只出信号，不撮合）
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable

ProgressFn = Callable[[float, str], None]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_frame(progress: ProgressFn):
    from app.runtime import STORE

    progress(0.05, "读取数据代际")
    frame = STORE.collect()
    progress(0.20, f"{frame.height:,} 行 · {frame['symbol'].n_unique()} 只")
    return frame


def job_backtest(params: dict[str, Any], progress: ProgressFn) -> dict:
    """{"strategy": "...", "params": {...}, "init_cash": 100000, ...}"""
    import polars as pl

    from app.backtest.data import BarStore
    from app.backtest.engine import BacktestEngine
    from app.backtest.models import BacktestConfig, FeeConfig
    from app.infrastructure.storage.db import get_db
    from app.runtime import ENGINE

    frame = _load_frame(progress)
    clean = frame.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )

    progress(0.30, "构建 BarStore")
    bars = BarStore(clean)

    progress(0.45, f"执行策略 {params.get('strategy')}")
    hits = ENGINE.run(params["strategy"], params.get("params") or {}, clean)
    progress(0.55, f"信号 {hits.height} 条")

    cfg = BacktestConfig(
        init_cash=float(params.get("init_cash", 100_000)),
        risk_pct=float(params.get("risk_pct", 0.01)),
        risk_amount=params.get("risk_amount"),
        max_positions=int(params.get("max_positions", 10)),
        max_position_pct=float(params.get("max_position_pct", 0.25)),
        stop_loss_pct=params.get("stop_loss_pct", 0.08),
        take_profit_pct=params.get("take_profit_pct"),
        max_hold_days=params.get("max_hold_days", 20),
        fees=FeeConfig(
            commission_rate=float(params.get("commission_rate", 0.00025)),
            stamp_tax_rate=float(params.get("stamp_tax_rate", 0.0005)),
            slippage_bps=float(params.get("slippage_bps", 5.0)),
        ),
    )

    progress(0.60, "账户回测中")
    start = date.fromisoformat(params["start"]) if params.get("start") else None
    end = date.fromisoformat(params["end"]) if params.get("end") else None
    res = BacktestEngine(cfg, bars).run(hits, start=start, end=end)
    progress(0.90, "汇总绩效")

    m = res.metrics
    db = get_db()
    from app.runtime import STORE as _S

    gen = _S.require().generation
    rid = db.save_run(
        strategy=params["strategy"],
        params=params.get("params") or {},
        generation=gen,
        engine_version="v2.0",
        metrics=m,
        equity=float(m.get("final_equity") or 0),
        trades=int(m.get("trades") or 0),
    )
    return {
        "run_id": rid,
        "strategy": params["strategy"],
        "signals": hits.height,
        "metrics": m,
        "rejected": res.rejected,
        "nav_points": len(res.snapshots),
        "generation": gen,
    }


def job_scan(params: dict[str, Any], progress: ProgressFn) -> dict:
    """全市场扫描：只跑策略出信号，不撮合。"""
    from app.runtime import ENGINE

    frame = _load_frame(progress)
    strategy = params["strategy"]
    progress(0.40, f"扫描 {strategy}")
    hits = ENGINE.run(strategy, params.get("params") or {}, frame)
    progress(0.85, f"命中 {hits.height}")

    latest = frame["date"].max()
    today = hits.filter(hits["date"] == latest) if hits.height else hits
    cols = [c for c in ("symbol", "date", "close", "score") if c in hits.columns]
    rows = today.select(cols).head(int(params.get("limit", 50))).to_dicts() if today.height else []
    for r in rows:
        r["date"] = str(r["date"])
    return {
        "strategy": strategy,
        "date": str(latest),
        "hit_total": hits.height,
        "hit_today": today.height,
        "rows": rows,
    }


def job_grid(params: dict[str, Any], progress: ProgressFn) -> dict:
    """参数网格：仓位上限 × 止损 × 持仓天数，按夏普排序。"""
    import itertools

    import polars as pl

    from app.backtest.data import BarStore
    from app.backtest.engine import BacktestEngine
    from app.backtest.models import BacktestConfig, FeeConfig
    from app.runtime import ENGINE

    frame = _load_frame(progress)
    clean = frame.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )
    bars = BarStore(clean)
    progress(0.35, "数据就绪")

    hits = ENGINE.run(params["strategy"], params.get("params") or {}, clean)
    progress(0.45, f"信号 {hits.height}")

    stops = params.get("stops") or [0.06, 0.08, 0.12]
    holds = params.get("holds") or [5, 10, 20]
    caps = params.get("caps") or [0.25, 0.5]
    combos = list(itertools.product(stops, holds, caps))
    start = date.fromisoformat(params["start"]) if params.get("start") else None
    end = date.fromisoformat(params["end"]) if params.get("end") else None

    out = []
    for i, (stop, hold, cap) in enumerate(combos):
        cfg = BacktestConfig(
            init_cash=float(params.get("init_cash", 100_000)),
            risk_amount=params.get("risk_amount"),
            risk_pct=float(params.get("risk_pct", 0.01)),
            max_positions=int(params.get("max_positions", 10)),
            max_position_pct=cap,
            stop_loss_pct=stop,
            max_hold_days=hold,
            fees=FeeConfig(),
        )
        r = BacktestEngine(cfg, bars).run(hits, start=start, end=end)
        m = r.metrics
        out.append({
            "stop": stop, "hold": hold, "cap": cap,
            "return": m.get("total_return"), "sharpe": m.get("sharpe"),
            "mdd": m.get("max_drawdown"), "trades": m.get("trades"),
            "win_rate": m.get("win_rate"),
        })
        progress(0.45 + 0.5 * (i + 1) / len(combos), f"{i+1}/{len(combos)}")

    out.sort(key=lambda x: -(x["sharpe"] or -9))
    return {"strategy": params["strategy"], "combos": len(out), "top": out[:20]}


def job_batch_backtest(params: dict[str, Any], progress: ProgressFn) -> dict:
    """一键回测所有策略，结果写 SQLite runs 表，返回排名。

    在子进程里跑（内存独立），不会把主服务拖垮。
    params:
      strategies: [id...]，留空则跑"有排名的 12 个"或 scope=all 的全部
      start / end: 区间
      scope: ranked（默认）| all
    """
    import time as _time

    import polars as pl

    from app.backtest.data import BarStore
    from app.backtest.engine import BacktestEngine
    from app.backtest.models import BacktestConfig, FeeConfig
    from app.infrastructure.storage.db import get_db
    from app.runtime import ENGINE, STORE

    t0 = _time.time()
    gen = STORE.require()
    progress(0.02, f"代际 {gen.generation[:8]} · {gen.rows:,} 行")

    ids = params.get("strategies") or []
    if not ids:
        scope = params.get("scope", "ranked")
        if scope == "all":
            ids = sorted(ENGINE.registry)
        else:
            # 有账户回测排名的 12 个
            from app.modules.screener.batch import _parse_ranking

            ranked = _parse_ranking()
            ids = [s for s in sorted(ENGINE.registry) if s in ranked] or sorted(ENGINE.registry)[:12]

    start = date.fromisoformat(params["start"]) if params.get("start") else date(2024, 1, 1)
    end = date.fromisoformat(params["end"]) if params.get("end") else None

    progress(0.05, "加载行情（区间裁剪）")
    lf = STORE.scan()
    names = lf.collect_schema().names()
    bar_cols = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "change_pct"]
    bars = BarStore(
        lf.filter(pl.col("date") >= start).select([c for c in bar_cols if c in names]).collect()
    )
    progress(0.10, f"BarStore 就绪 · {len(bars.calendar)} 个交易日")

    progress(0.12, "加载策略信号窗口")
    # 不加载全部 1150 万行：回测只要 start 前 500 天至今（覆盖 MA250 / 52 周窗口）
    from datetime import timedelta

    sig_from = start - timedelta(days=260)   # 覆盖 MA250；52周新高会因此报无信号
    frame = STORE.scan().filter(pl.col("date") >= sig_from).collect()
    progress(0.15, f"信号窗口 {frame.height:,} 行 · {frame['symbol'].n_unique():,} 只")

    cfg = BacktestConfig(
        init_cash=float(params.get("init_cash", 100_000)),
        risk_amount=params.get("risk_amount", 3000),
        risk_pct=float(params.get("risk_pct", 0.01)),
        max_positions=int(params.get("max_positions", 10)),
        max_position_pct=float(params.get("max_position_pct", 0.25)),
        stop_loss_pct=float(params.get("stop_loss_pct", 0.08)),
        max_hold_days=params.get("max_hold_days", 20),
        fees=FeeConfig(),
    )

    db = get_db()
    rows: list[dict] = []
    for i, sid in enumerate(ids):
        try:
            hits = ENGINE.run(sid, {}, frame)
            if hits.height == 0:
                rows.append({"strategy": sid, "error": "无信号"})
                continue
            res = BacktestEngine(cfg, bars).run(hits, start=start, end=end)
            m = res.metrics
            db.save_run(
                strategy=sid,
                params={"start": str(start), "end": str(end), "risk_amount": cfg.risk_amount},
                generation=gen.generation,
                engine_version="v2.0",
                metrics=m,
                equity=float(m.get("final_equity") or 0),
                trades=int(m.get("trades") or 0),
            )
            rows.append(
                {
                    "strategy": sid,
                    "signals": int(hits.height),
                    "return": m.get("total_return"),
                    "annual": m.get("annual_return"),
                    "mdd": m.get("max_drawdown"),
                    "sharpe": m.get("sharpe"),
                    "win_rate": m.get("win_rate"),
                    "trades": m.get("trades"),
                }
            )
            del hits, res
        except Exception as exc:  # noqa: BLE001
            rows.append({"strategy": sid, "error": f"{type(exc).__name__}: {exc}"[:120]})
        progress(0.12 + 0.85 * (i + 1) / len(ids), f"{i+1}/{len(ids)} {sid}")

    del frame
    ranked = sorted(
        [r for r in rows if r.get("sharpe") is not None],
        key=lambda x: -(x["sharpe"] or -9),
    )
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    return {
        "generation": gen.generation[:8],
        "strategies": len(ids),
        "ok": len(ranked),
        "failed": len(rows) - len(ranked),
        "start": str(start),
        "end": str(end) if end else "最新",
        "elapsed": round(_time.time() - t0, 1),
        "ranking": ranked,
        "errors": [r for r in rows if r.get("error")],
    }


JOBS: dict[str, Callable[[dict, ProgressFn], dict]] = {
    "backtest": job_backtest,
    "scan": job_scan,
    "grid": job_grid,
    "batch_backtest": job_batch_backtest,
}
