"""SSE 事件流 —— 把任务进度、引擎健康、数据代际的变化实时推给前端。

为什么用 SSE 而不是 WebSocket：
  · 单向推送够用（前端只接收）
  · 走 HTTP/1.1，不需要额外握手与协议升级
  · EventSource 自带重连

设计：
  · 每 2 秒检查一次快照，只有变化才推（省带宽）
  · 每 15 秒无条件发一次心跳注释（防代理超时）
  · 客户端断开时退出生成器
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/events", tags=["monitor"])

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0


def _snapshot() -> dict:
    """轻量快照：只查 SQLite 和内存，不碰 parquet。"""
    from app.infrastructure.storage.db import get_db
    from app.runtime import ENGINE, STORE

    db = get_db()
    running = db.list_tasks(limit=8, status="running")
    pending = db.list_tasks(limit=8, status="pending")
    recent = db.list_tasks(limit=6)

    try:
        gen = STORE.require()
        health = {
            "ok": True,
            "generation": gen.generation[:8],
            "rows": gen.rows,
            "strategies": len(ENGINE.registry),
            "errors": len(ENGINE.errors),
        }
    except Exception as exc:  # 数据代际不可用 —— 如实上报，不返回假的
        health = {"ok": False, "error": str(exc)[:120]}

    return {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "health": health,
        "running": [
            {"id": t["id"], "type": t["type"], "progress": t["progress"], "log": (t["log"] or "").splitlines()[-1:]}
            for t in running + pending
        ],
        "recent": [
            {
                "id": t["id"],
                "type": t["type"],
                "status": t["status"],
                "progress": t["progress"],
                "finished_at": t["finished_at"],
                "error": t["error"],
            }
            for t in recent
        ],
    }


@router.get("/snapshot")
def snapshot() -> dict:
    """一次性快照（非流式）。供截图、健康检查、或前端不支持 SSE 时降级。"""
    return _snapshot()


@router.get("")
async def stream(request: Request) -> StreamingResponse:
    async def gen():
        last_key = None
        last_beat = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            try:
                snap = _snapshot()
                # 只按「除时间戳以外」的内容判断是否变化，否则每秒都会推
                key = json.dumps({k: v for k, v in snap.items() if k != "ts"}, ensure_ascii=False)
                if key != last_key:
                    yield f"event: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
                    last_key = key
                    last_beat = time.monotonic()
                elif time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                    yield ": ping\n\n"
                    last_beat = time.monotonic()
            except Exception as exc:  # 单次快照失败不应中断流
                yield f"event: error\ndata: {json.dumps({'message': str(exc)[:200]}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
