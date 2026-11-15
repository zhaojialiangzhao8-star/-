"""任务 API —— 提交、查询、列表。挂在 /api/v1/tasks。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from . import service

router = APIRouter(prefix="/tasks", tags=["tasks"])

ALLOWED_TYPES = {"backtest", "scan", "grid", "batch_backtest"}


class SubmitRequest(BaseModel):
    type: str = Field(..., description="backtest / scan / grid")
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("")
def submit(req: SubmitRequest) -> dict:
    from app.core.errors import BadRequest

    if req.type not in ALLOWED_TYPES:
        raise BadRequest(f"不支持的任务类型: {req.type}", allowed=sorted(ALLOWED_TYPES))
    tid = service.submit_task(req.type, req.params)
    return {"task_id": tid, "status": "pending", "poll": f"/api/v1/tasks/{tid}"}


@router.get("/{task_id}")
def detail(task_id: str) -> dict:
    return service.get_task(task_id) or {}


@router.get("")
def listing(
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(None, description="pending/running/done/failed"),
) -> dict:
    items = service.list_tasks(limit=limit, status=status)
    return {"count": len(items), "items": items}
