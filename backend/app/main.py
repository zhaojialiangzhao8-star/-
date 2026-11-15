"""应用入口 — 单进程、单端口、前端由自己托管。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import api, config
from .modules.tasks import api as tasks_api
from .modules.monitor import api as monitor_api
from .modules.monitor import realtime_api
from .modules.ai import api as ai_api
from .modules.analysis import levels_api, stock_api
from .modules.monitor import market_api, dashboard_api, tick_compat_api, alerts_api, limit_ladder_api
from .modules.risk import api as risk_api
from .modules.screener import batch as screener_batch
from .modules.strategy_lib import api as strategy_lib_api
from .core.errors import FacetError, facet_error_handler
from .core.logging import new_request_id, setup_logging
from .runtime import ENGINE, STORE

setup_logging()
logger = logging.getLogger("facet")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    gen = STORE.current()
    if gen is None:
        logger.warning("没有可读代际 —— 先跑: python scripts/gen_data.py")
    else:
        logger.info("代际 %s 就绪 rows=%s (%s)", gen.generation[:8], gen.rows, gen.updated_text)
    logger.info("策略 %s 个已加载，失败 %s 个 %s", len(ENGINE.registry), len(ENGINE.errors), ENGINE.errors)

    # 实时行情轮询（只轮询自选股，盘中才动）
    poller = None
    try:
        from .infrastructure.realtime.quotes import Poller
        from .modules.monitor.realtime_api import _watch_symbols

        poller = Poller(_watch_symbols)
        poller.start()
        logger.info("实时行情轮询已启动（3s · 仅盘中 · 自选股 %s 只）", len(_watch_symbols()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("实时行情轮询启动失败: %s", exc)

    # 任务队列恢复：running 标失败、pending 重新派发（否则重启后任务永远卡在排队）
    try:
        from .modules.tasks.service import recover_tasks

        rec = recover_tasks()
        if rec["failed"] or rec["respawned"]:
            logger.info("任务队列恢复：中断 %s 个，重新派发 %s 个", rec["failed"], rec["respawned"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("任务队列恢复失败: %s", exc)

    # 风险库：先算一次（启动即有一份结果），再起 10 秒后台刷新
    try:
        from .modules.risk import evaluator as risk_eval
        from .modules.risk import refresh as risk_refresh

        risk_eval.evaluate()
        risk_refresh.start_refresher()
    except Exception as exc:  # noqa: BLE001
        logger.warning("风险库刷新启动失败: %s", exc)

    yield

    if poller:
        poller.stop()
    logger.info("shutdown")


app = FastAPI(title="Facet · 量化工作台", version=config.VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.add_exception_handler(FacetError, facet_error_handler)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """给每个请求分配 id，写进响应头与日志，便于串联。"""
    rid = new_request_id()
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response
# 双挂载：旧路径 /api 保持兼容，新路径 /api/v1 供 V2 前端使用
app.include_router(api.router, prefix="/api")
app.include_router(api.router, prefix="/api/v1", include_in_schema=False)
app.include_router(tasks_api.router, prefix="/api/v1")   # V2 新增：任务队列
app.include_router(monitor_api.router, prefix="/api/v1")   # V2 新增：SSE 事件流
app.include_router(realtime_api.router, prefix="/api/v1")   # V2 新增：实时行情
app.include_router(screener_batch.router, prefix="/api/v1")   # V2 新增：批量选股
app.include_router(market_api.router, prefix="/api/v1")   # V2 新增：指数 + 板块
app.include_router(dashboard_api.router, prefix="/api/v1")   # V2 新增：首页看板
app.include_router(limit_ladder_api.router, prefix="/api/v1")   # V2 新增：涨停天梯
app.include_router(risk_api.router, prefix="/api/v1")   # V2 新增：股票风险库
app.include_router(tick_compat_api.router, prefix="/api")
app.include_router(alerts_api.router, prefix="/api")   # TSP 监控中心：策略信号流   # TSP 兼容层（看板数据）
app.include_router(levels_api.router, prefix="/api/v1")   # V2 新增：支撑/压力/目标位
app.include_router(stock_api.router, prefix="/api/v1")   # V2 新增：个股档案
app.include_router(ai_api.router, prefix="/api/v1")   # V2 新增：DeepSeek 接入
app.include_router(strategy_lib_api.router, prefix="/api/v1")   # V2 新增：策略库治理

# V2 前端（React 构建产物）挂在 /app，旧页面保持在 / 、/dashboard 等路径
FRONTEND_DIST = config.STATIC_DIR.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    if (FRONTEND_DIST / "assets").exists():
        app.mount(
            "/app/assets",
            StaticFiles(directory=str(FRONTEND_DIST / "assets")),
            name="app-assets",
        )

    @app.get("/app")
    @app.get("/app/{path:path}")
    def spa(path: str = "") -> FileResponse:
        """SPA 路由回退：任何 /app/* 都返回 index.html，由前端 Router 接管。"""
        candidate = FRONTEND_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


# 旧版静态页面已废弃（2026-11）：/ 、/dashboard、/trading、/account、/backtest、/workbench
# 全部 307 到 /app，由 React 版接管。
# STATIC_DIR 仍保留在 config 里——FRONTEND_DIST 是从它推导出来的。
@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/app", status_code=307)


@app.get("/{path:path}")
def legacy_page_redirect(path: str) -> RedirectResponse:
    return RedirectResponse("/app", status_code=307)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=3018)
