"""任务服务 — 提交任务（spawn 子进程）、查询状态。

提交后立刻返回 task_id，前端轮询 /api/v1/tasks/{id} 看进度。
子进程用 CREATE_NO_WINDOW 在 Windows 下不弹黑窗。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]  # backend/
STALE_SECONDS = 180   # running 任务超过 3 分钟没心跳 → 判定 worker 已死

# Windows: CREATE_NO_WINDOW；其他平台为 0
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def submit_task(type_: str, params: dict | None = None) -> str:
    from app.infrastructure.storage.db import get_db

    db = get_db()
    tid = db.create_task(type_, params or {})
    _spawn(tid)
    return tid


def _spawn(tid: str) -> None:
    subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "app.infrastructure.workers.runner", tid],
        cwd=str(BACKEND_DIR),
        creationflags=_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def recover_tasks() -> dict:
    """服务重启后恢复任务队列。

    两种情况：
      · running —— 宿主进程已死，worker 也早没了，标成 failed（不假装它还在跑）
      · pending —— 从没派发出去，重新 spawn 一个 worker 接着做

    返回 {"failed": n, "respawned": m}，写进启动日志。
    """
    import json as _json
    import time as _time

    from app.infrastructure.storage.db import get_db

    db = get_db()
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    failed = 0
    respawned = 0

    for t in db.list_tasks(limit=200, status="running"):
        db.update_task(
            t["id"],
            status="failed",
            error="服务重启，任务中断（worker 已随宿主退出）",
            finished_at=now,
        )
        failed += 1

    for t in db.list_tasks(limit=200, status="pending"):
        try:
            _json.loads(t["params"] or "{}")
        except Exception:  # noqa: BLE001
            db.update_task(t["id"], status="failed", error="参数损坏", finished_at=now)
            failed += 1
            continue
        _spawn(t["id"])
        respawned += 1

    return {"failed": failed, "respawned": respawned}


def get_task(task_id: str) -> dict | None:
    from app.core.errors import TaskNotFound
    from app.infrastructure.storage.db import get_db

    db = get_db()
    t = db.get_task(task_id)
    if not t:
        raise TaskNotFound(f"任务不存在: {task_id}")
    # 解析 JSON 字段，方便前端直接用
    for k in ("params", "result"):
        if t.get(k):
            try:
                t[k] = json.loads(t[k])
            except (json.JSONDecodeError, TypeError):
                pass
    return t


def list_tasks(limit: int = 50, status: str | None = None) -> list[dict]:
    """列表。顺手做一次「僵尸任务」清扫。

    worker 是独立子进程，内存不足时会被系统硬杀（没有机会跑 finally）。
    结果就是任务永远停在 running。这里用 updated_at 判断心跳：
    超过 STALE_SECONDS 没动静的 running 任务，标成 failed。
    """
    from app.infrastructure.storage.db import get_db

    db = get_db()
    now = time.time()
    for t in db.list_tasks(limit=200, status="running"):
        hb = t.get("updated_at") or t.get("started_at")
        if not hb:
            continue
        try:
            ts = time.mktime(time.strptime(hb, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            continue
        if now - ts > STALE_SECONDS:
            db.update_task(
                t["id"],
                status="failed",
                error=f"worker 无心跳 {int(now - ts)} 秒（多半是内存不足被系统杀掉）",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )

    rows = db.list_tasks(limit=limit, status=status)
    for t in rows:
        for k in ("params", "result"):
            if t.get(k):
                try:
                    t[k] = json.loads(t[k])
                except (json.JSONDecodeError, TypeError):
                    pass
    return rows
