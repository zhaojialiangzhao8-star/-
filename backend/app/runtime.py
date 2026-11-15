"""全局单例 — store / engine 各一份，api 与 main 都从这里拿。

为什么单独一个模块：FastAPI 的 Depends 需要可复用的可调用对象，
把实例放在 api 模块里会导致循环 import（main 也要用）。
"""
from __future__ import annotations

from . import config
from .engine import StrategyEngine
from .store import GenerationStore

STORE = GenerationStore(config.DATA_DIR)
ENGINE = StrategyEngine(config.STRATEGY_DIR)


def get_store() -> GenerationStore:
    return STORE


def get_engine() -> StrategyEngine:
    return ENGINE
