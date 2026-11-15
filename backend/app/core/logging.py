"""统一日志 — 带请求 ID 的结构化日志，落到 logs/app.log 与控制台。

为什么要自己写：V1 只有 uvicorn 输出和 boot.log，出问题时无法把一次请求的
所有日志串起来。这里用 contextvar 存 request_id，日志格式里带上它。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.req_id = REQUEST_ID.get()
        return True


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_facet_configured", False):
        return

    target = Path(log_dir or LOG_DIR)
    target.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(req_id)s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    req_filter = RequestIdFilter()

    fh = logging.handlers.RotatingFileHandler(
        target / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.addFilter(req_filter)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.addFilter(req_filter)
    root.addHandler(sh)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root._facet_configured = True  # type: ignore[attr-defined]


def new_request_id() -> str:
    rid = uuid.uuid4().hex[:8]
    REQUEST_ID.set(rid)
    return rid


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class Timer:
    """with Timer(log, "backtest"): ... → 自动记录耗时。"""

    def __init__(self, log: logging.Logger, label: str, level: int = logging.INFO) -> None:
        self.log, self.label, self.level = log, label, level

    def __enter__(self) -> "Timer":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        ms = (time.perf_counter() - self.t0) * 1000
        if ms > 1000:
            self.log.log(self.level, "%s 耗时 %.2fs", self.label, ms / 1000)
        else:
            self.log.log(self.level, "%s 耗时 %.0fms", self.label, ms)
