"""风险库持久化 —— 全部落在 data/risk/ 下的 JSON，避免引入新表结构。

  flags.json       计算出来的命中结果 {symbol: {level, rules:[{id,detail}], at}}
  manual.json      人工拉黑 {symbol: {reason, at}}
  snapshots.json   每日快照 {date: {symbol: {price, mcap}}}，给「连续 N 日」规则用
  fundamentals.json 财报字段（外部导入或人工维护），schema 见 rules.py 顶部注释
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ...config import DATA_DIR

RISK_DIR = DATA_DIR / "risk"
_LOCK = threading.RLock()

FILES = {
    "flags": RISK_DIR / "flags.json",
    "manual": RISK_DIR / "manual.json",
    "snapshots": RISK_DIR / "snapshots.json",
    "fundamentals": RISK_DIR / "fundamentals.json",
    "state": RISK_DIR / "state.json",
}


def _read(key: str, default: Any) -> Any:
    path = FILES[key]
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def _write(key: str, data: Any) -> None:
    RISK_DIR.mkdir(parents=True, exist_ok=True)
    path = FILES[key]
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def load_flags() -> dict:
    return _read("flags", {})


def save_flags(data: dict) -> None:
    with _LOCK:
        _write("flags", data)


def load_manual() -> dict:
    return _read("manual", {})


def add_manual(symbol: str, reason: str = "") -> None:
    with _LOCK:
        data = load_manual()
        data[symbol.upper()] = {"reason": reason or "人工拉黑", "at": _now()}
        _write("manual", data)


def remove_manual(symbol: str) -> None:
    with _LOCK:
        data = load_manual()
        data.pop(symbol.upper(), None)
        _write("manual", data)


def load_snapshots() -> dict:
    return _read("snapshots", {})


def push_snapshot(today: str, per_symbol: dict[str, dict]) -> None:
    """写入当天快照（同一天只写一次，覆盖即可），只保留最近 40 个日历日的桶。"""
    with _LOCK:
        data = load_snapshots()
        data[today] = per_symbol
        keep = sorted(data.keys())[-40:]
        _write("snapshots", {k: data[k] for k in keep})


def load_fundamentals() -> dict:
    return _read("fundamentals", {})


def load_state() -> dict:
    return _read("state", {})


def save_state(data: dict) -> None:
    with _LOCK:
        _write("state", data)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return date.today().isoformat()


def blacklist() -> dict[str, str]:
    """最终黑名单：人工 ∪ 任何命中规则的票。返回 {symbol: 原因摘要}。"""
    out: dict[str, str] = {}
    for sym, item in load_manual().items():
        out[sym] = item.get("reason") or "人工拉黑"
    for sym, item in load_flags().items():
        rules = item.get("rules") or []
        if not rules:
            continue
        title = rules[0].get("title") or rules[0].get("id")
        more = f" 等 {len(rules)} 条" if len(rules) > 1 else ""
        out.setdefault(sym, f"{title}{more}")
    return out


def blacklist_set() -> set[str]:
    return set(blacklist().keys())


def stats() -> dict:
    flags = load_flags()
    snap = load_snapshots()
    manual = load_manual()
    return {
        "flagged_symbols": len(flags),
        "manual": len(manual),
        "snapshot_days": len(snap),
        "latest_snapshot": sorted(snap.keys())[-1] if snap else None,
        "updated_at": load_state().get("updated_at"),
    }


def risk_dir() -> Path:
    return RISK_DIR
