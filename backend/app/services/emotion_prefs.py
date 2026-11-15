"""情绪评分口径的持久化 — 一份 JSON, 原子写。

放在 data/user_data/ 下, 跟数据代际解耦: 换代际不影响口径; 删掉文件即回出厂。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..config import DATA_DIR

_PATH = Path(DATA_DIR) / "user_data" / "emotion_score.json"


def load_raw() -> dict | None:
    """用户配置原文; 没有/坏了都返回 None (调用方按出厂口径算)。"""
    try:
        if not _PATH.is_file():
            return None
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — 口径文件坏了不能把看板拖挂
        return None


def save_raw(config: dict | None) -> None:
    """写口径; 传 None 表示恢复出厂 (删文件)。原子写, 避免半截文件。"""
    if config is None:
        try:
            _PATH.unlink(missing_ok=True)
        except OSError:
            pass
        return
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_PATH.parent), prefix=".emotion_score.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
