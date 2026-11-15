"""SQLite 存储 — 任务、回测记录、配置、自选股。

V1 的回测结果只存在进程内存，关掉就没了。这里补上持久化：
  · tasks      任务队列（阶段 4 的 Worker 用）
  · runs       回测记录（可追溯 generation / strategy_version / params）
  · configs    用户配置
  · watchlist  自选股

不做 ORM：四张表、十几个查询，sqlite3 + 手写 SQL 更透明，也不引入依赖。
WAL 模式保证「一个写 + 多个读」不互相阻塞。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

def _default_db_path() -> Path:
    """数据库放在数据目录下，跟 parquet 代际同源。

    不能用 `Path(__file__).parents[4]` 推导：源码里那是 `facet/`，打包后是
    `_internal/` 的上一级，两者不一致；而且 exe 通过 FACET_DATA_DIR 把数据
    指向 %LOCALAPPDATA%\\Facet\\data，推导法会让 tasks 表落在程序目录，
    跟回测记录分裂成两个库。
    """
    from app import config

    return Path(config.DATA_DIR) / "database" / "facet.db"


DEFAULT_DB = _default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,              -- scan / backtest / grid / fetch
    status      TEXT NOT NULL,              -- pending / running / done / failed / cancelled
    progress    REAL NOT NULL DEFAULT 0,    -- 0~1
    params      TEXT,                       -- JSON
    result      TEXT,                       -- JSON
    error       TEXT,
    log         TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    task_id        TEXT,
    strategy       TEXT NOT NULL,
    params         TEXT,                    -- JSON
    generation     TEXT,                    -- 数据代际哈希
    engine_version TEXT,                    -- 引擎/规则版本
    metrics        TEXT,                    -- JSON（12 项指标）
    equity         REAL,
    trades         INTEGER,
    nav_path       TEXT,
    trades_path    TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_strategy ON runs(strategy, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);

CREATE TABLE IF NOT EXISTS configs (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,               -- JSON
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    name     TEXT,
    grp      TEXT NOT NULL DEFAULT 'default',
    note     TEXT,
    added_at TEXT NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)

    # ---------- 连接 ----------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # 轻量迁移：老库缺列就补（SQLite 没有 ADD COLUMN IF NOT EXISTS）
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "updated_at" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT")

    # ---------- 通用 ----------
    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, params)

    # ---------- tasks ----------
    def create_task(self, type_: str, params: dict | None = None) -> str:
        tid = uuid.uuid4().hex
        self.execute(
            "INSERT INTO tasks (id, type, status, progress, params, created_at) VALUES (?,?,?,?,?,?)",
            (tid, type_, "pending", 0.0, json.dumps(params or {}, ensure_ascii=False), _now()),
        )
        return tid

    def update_task(self, tid: str, **fields: Any) -> None:
        allowed = {"status", "progress", "result", "error", "log", "started_at", "finished_at"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("result",) and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False, default=str)
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return
        vals.append(tid)
        self.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(vals))

    def get_task(self, tid: str) -> dict | None:
        rows = self.query("SELECT * FROM tasks WHERE id=?", (tid,))
        return rows[0] if rows else None

    def list_tasks(self, limit: int = 50, status: str | None = None) -> list[dict]:
        if status:
            return self.query(
                "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        return self.query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))

    # ---------- runs ----------
    def save_run(
        self,
        strategy: str,
        params: dict,
        generation: str,
        engine_version: str,
        metrics: dict,
        equity: float,
        trades: int,
        task_id: str | None = None,
    ) -> str:
        rid = uuid.uuid4().hex
        self.execute(
            """INSERT INTO runs
               (id, task_id, strategy, params, generation, engine_version, metrics, equity, trades, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                rid, task_id, strategy,
                json.dumps(params, ensure_ascii=False),
                generation, engine_version,
                json.dumps(metrics, ensure_ascii=False, default=str),
                equity, trades, _now(),
            ),
        )
        return rid

    def list_runs(self, limit: int = 50, strategy: str | None = None) -> list[dict]:
        if strategy:
            return self.query(
                "SELECT * FROM runs WHERE strategy=? ORDER BY created_at DESC LIMIT ?",
                (strategy, limit),
            )
        return self.query("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))

    # ---------- configs ----------
    def get_config(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM configs WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return default

    def set_config(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), _now()),
        )

    # ---------- watchlist ----------
    def add_watch(self, symbol: str, name: str = "", grp: str = "default", note: str = "") -> None:
        self.execute(
            "INSERT INTO watchlist (symbol, name, grp, note, added_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, grp=excluded.grp, note=excluded.note",
            (symbol, name, grp, note, _now()),
        )

    def list_watch(self, grp: str | None = None) -> list[dict]:
        if grp:
            return self.query("SELECT * FROM watchlist WHERE grp=? ORDER BY added_at DESC", (grp,))
        return self.query("SELECT * FROM watchlist ORDER BY added_at DESC")

    def remove_watch(self, symbol: str) -> None:
        self.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))


def get_db() -> Database:
    """模块级单例。路径可用 FACET_DB 覆盖（测试用）。"""
    import os

    path = os.getenv("FACET_DB")
    db = Database(Path(path) if path else DEFAULT_DB)
    db.init_schema()
    return db
