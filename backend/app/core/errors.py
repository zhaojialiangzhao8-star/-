"""统一异常 — 让 API 层返回一致的错误结构，前端只需处理一种格式。

响应体：
    {"error": {"code": "STRATEGY_NOT_FOUND", "message": "...", "detail": {...}}}
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class FacetError(Exception):
    """所有业务异常的基类。"""

    code = "INTERNAL_ERROR"
    status = 500

    def __init__(self, message: str = "", **detail) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail


class NotFound(FacetError):
    code = "NOT_FOUND"
    status = 404


class BadRequest(FacetError):
    code = "BAD_REQUEST"
    status = 400


class StrategyNotFound(NotFound):
    code = "STRATEGY_NOT_FOUND"


class DataUnavailable(FacetError):
    """没有可读的数据代际 —— 绝不能用空数据糊弄过去。"""

    code = "DATA_UNAVAILABLE"
    status = 503


class TaskNotFound(NotFound):
    code = "TASK_NOT_FOUND"


async def facet_error_handler(request: Request, exc: FacetError) -> JSONResponse:
    from .logging import get_logger

    get_logger("api").warning("%s %s → %s: %s", request.method, request.url.path, exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
    )
