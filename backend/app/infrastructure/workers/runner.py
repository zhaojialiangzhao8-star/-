"""Worker 子进程入口 —— python -m app.infrastructure.workers.runner <task_id>

为什么用子进程而不是线程：
  · 回测吃 CPU（BarStore 15s + 引擎循环），线程会拖慢 API 主进程
  · 子进程崩了不影响主服务
  · 阶段 1 不引入 Redis/Celery，SQLite 就是任务表
"""
from __future__ import annotations

import json
import sys
import time
import traceback


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def main(task_id: str) -> int:
    from app.core.logging import new_request_id, setup_logging
    from app.infrastructure.storage.db import get_db
    from app.infrastructure.workers.jobs import JOBS

    setup_logging()
    new_request_id()

    db = get_db()
    task = db.get_task(task_id)
    if not task:
        print(f"task {task_id} not found", file=sys.stderr)
        return 2

    params = json.loads(task["params"] or "{}")
    job = JOBS.get(task["type"])
    if job is None:
        db.update_task(
            task_id, status="failed", error=f"未知任务类型: {task['type']}", finished_at=_now()
        )
        return 2

    db.update_task(task_id, status="running", started_at=_now(), progress=0.0)
    logs: list[str] = []

    def progress(p: float, msg: str = "") -> None:
        p = max(0.0, min(1.0, float(p)))
        if msg:
            logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        db.update_task(task_id, progress=round(p, 3), log="\n".join(logs[-200:]))

    try:
        result = job(params, progress)
        db.update_task(
            task_id,
            status="done",
            progress=1.0,
            result=result,
            log="\n".join(logs[-200:]),
            finished_at=_now(),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        logs.append(err)
        logs.append(traceback.format_exc()[-2000:])
        db.update_task(
            task_id,
            status="failed",
            error=err,
            log="\n".join(logs[-200:]),
            finished_at=_now(),
        )
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m app.infrastructure.workers.runner <task_id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
