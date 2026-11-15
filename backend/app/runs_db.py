"""回测记录仓库（SQLite）—— 一次回测一行，可筛选、可统计、可导出。

为什么从 JSON 文件改成 SQLite：
  JSON 适合"存一次结果"，不适合"按策略筛 / 按收益排 / 汇总十几次回测"。
  表结构把参数、指标、大盘状态分开存 JSON 列，查询用平铺的列。

表 runs：
  id / created_at / kind / strategy / title / start / end / generation
  params(JSON) / metrics(JSON) / regime(JSON) / extras(JSON) / result(JSON)
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    strategy    TEXT,
    title       TEXT,
    start_date  TEXT,
    end_date    TEXT,
    generation  TEXT,
    params      TEXT,
    metrics     TEXT,
    regime      TEXT,
    extras      TEXT,
    result      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_strategy ON runs(strategy);
"""


def _conn(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _loads(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def save(db: Path, payload: dict) -> str:
    """写一条回测记录，返回 id。payload 见模块 docstring 的字段。"""
    rid = payload.get("id") or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn = _conn(db)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (id, created_at, kind, strategy, title, start_date, end_date, generation,
                params, metrics, regime, extras, result)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rid,
                payload.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                payload.get("kind") or "account",
                payload.get("strategy") or "",
                payload.get("title") or payload.get("strategy") or "",
                payload.get("start_date") or "",
                payload.get("end_date") or "",
                payload.get("generation") or "",
                _dumps(payload.get("params") or {}),
                _dumps(payload.get("metrics") or {}),
                _dumps(payload.get("regime") or {}),
                _dumps(payload.get("extras") or {}),
                _dumps(payload.get("result") or {}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return rid


def list_runs(
    db: Path,
    limit: int = 100,
    strategy: str = "",
    order: str = "created_at",
    desc: bool = True,
) -> list[dict]:
    """列表只回元信息（不含 result 大字段，避免列表接口拖几百 MB）。"""
    order_col = order if order in ("created_at", "strategy") else "created_at"
    sql = f"""SELECT id, created_at, kind, strategy, title, start_date, end_date,
                     generation, params, metrics, regime, extras
              FROM runs {'WHERE strategy = ?' if strategy else ''}
              ORDER BY {order_col} {'DESC' if desc else 'ASC'} LIMIT ?"""
    args: list[Any] = ([strategy] if strategy else []) + [limit]
    conn = _conn(db)
    try:
        rows = [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()
    for r in rows:
        r["params"] = _loads(r.get("params")) or {}
        r["metrics"] = _loads(r.get("metrics")) or {}
        r["regime"] = _loads(r.get("regime")) or {}
        r["extras"] = _loads(r.get("extras")) or {}
    return rows


def get(db: Path, rid: str) -> dict | None:
    conn = _conn(db)
    try:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    for k in ("params", "metrics", "regime", "extras", "result"):
        out[k] = _loads(out.get(k)) or {}
    # 兼容前端旧字段名：result 里带 metrics/trades/nav
    if not out["result"].get("metrics") and out["metrics"]:
        out["result"]["metrics"] = out["metrics"]
    return out


def delete(db: Path, rid: str) -> bool:
    conn = _conn(db)
    try:
        cur = conn.execute("DELETE FROM runs WHERE id = ?", (rid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def migrate_json_dir(db: Path, runs_dir: Path) -> int:
    """把老的 data/runs/*.json 一次性搬进 SQLite（幂等）。"""
    if not runs_dir.exists():
        return 0
    conn = _conn(db)
    n = 0
    try:
        existing = {r[0] for r in conn.execute("SELECT id FROM runs")}
    finally:
        conn.close()
    for f in sorted(runs_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rid = d.get("id") or f.stem
        if rid in existing:
            continue
        save(
            db,
            {
                "id": rid,
                "created_at": d.get("created_at"),
                "kind": d.get("kind") or "simple",
                "strategy": (d.get("params") or {}).get("strategy") or d.get("title") or "",
                "title": d.get("title") or "",
                "params": d.get("params") or {},
                "metrics": d.get("metrics") or {},
                "result": d.get("result") or {},
            },
        )
        n += 1
    return n

def update_regime(db: Path, rid: str, regime: dict) -> None:
    conn = _conn(db)
    try:
        conn.execute("UPDATE runs SET regime = ? WHERE id = ?", (_dumps(regime), rid))
        conn.commit()
    finally:
        conn.close()


def missing_regime(db: Path, limit: int = 50) -> list[dict]:
    """找出没有大盘状态的老记录（迁移进来的），用于补算。"""
    conn = _conn(db)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, params, start_date, end_date, result FROM runs "
                "WHERE regime IS NULL OR regime = '' OR regime = '{}' LIMIT ?",
                (limit,),
            )
        ]
    finally:
        conn.close()
    for r in rows:
        r["params"] = _loads(r.get("params")) or {}
        r["result"] = _loads(r.get("result")) or {}
    return rows

def update_extras(db: Path, rid: str, extras: dict) -> None:
    conn = _conn(db)
    try:
        conn.execute("UPDATE runs SET extras = ? WHERE id = ?", (_dumps(extras), rid))
        conn.commit()
    finally:
        conn.close()
