"""风险库后台刷新 —— 默认 10 秒一轮。

为什么频率能这么高：快照 parquet 只在 mtime 变化时才重读（缓存），
一轮求值主要是 4800 行的内存遍历，实测 < 60ms。
"""
from __future__ import annotations

import logging
import threading

from . import evaluator

logger = logging.getLogger("facet.risk")

INTERVAL = 10.0


def start_refresher(interval: float = INTERVAL) -> threading.Thread:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                evaluator.evaluate()
            except Exception as exc:  # noqa: BLE001
                logger.warning("风险库刷新失败: %s", exc)

    thread = threading.Thread(target=loop, name="risk-refresher", daemon=True)
    thread.start()
    logger.info("风险库刷新线程已启动（每 %.0f 秒）", interval)
    return thread
