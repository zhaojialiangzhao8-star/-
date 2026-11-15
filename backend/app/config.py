"""全局配置 — 路径一律从环境变量取，容器与本地共用一份代码。"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


def _path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else default.resolve()


DATA_DIR = _path_from_env("DATA_DIR", BACKEND_DIR / "data")
STATIC_DIR = _path_from_env("STATIC_DIR", PROJECT_DIR / "web")
STRATEGY_DIR = _path_from_env("STRATEGY_DIR", BACKEND_DIR / "app" / "strategies")

# 交易日历不引入第三方依赖：A 股周一至周五，节假日不处理（demo 范围）
MARKET_TZ_OFFSET_HOURS = 8
DEFAULT_HOLD_DAYS = 5
DEFAULT_TOP_N = 10

VERSION = "0.1.0"


def ensure_dirs() -> None:
    for sub in ("daily", "enriched", "gen", "runs"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
