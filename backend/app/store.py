"""代际（generation）发布存储 — 让「写」和「读」不会互相看见半成品。

问题：盘后流水线在重写 enriched 表，前端和回测同时在读同一个目录。
      直接覆盖写 = 读者可能读到只写了一半的文件。

做法（和 TSP 同构，砍掉多写者竞争，保留核心三条）：
  1) 写新代际：gens/<uuid>/data.parquet.tmp -> rename 成 data.parquet
  2) 写 _SUCCESS（带行数、写入时间）
  3) 原子替换 CURRENT.json（os.replace 在同分区是原子操作）
  读者只认 CURRENT.json 指向的、且有 _SUCCESS 的目录 —— 拿不到就直接报错，
  绝不降级去扫一个不确定的目录（fail-closed）。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from . import parquet

CURRENT_NAME = "CURRENT.json"
KEEP_GENERATIONS = 1   # 每代 ~200 MB，保留当前 + 1 个旧代际足够回滚
STALE_LOCK_SECONDS = 600.0


class StoreUnavailable(RuntimeError):
    """没有可读的稳定代际。调用方应把 503 暴露给前端，而不是返回空数据。"""


@dataclass(frozen=True)
class Generation:
    generation: str
    rows: int
    updated_at: float
    path: Path

    @property
    def updated_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.updated_at))


class GenerationStore:
    def __init__(self, root: Path, name: str = "enriched") -> None:
        self.root = Path(root) / name
        self.gens_dir = self.root / "gens"
        self.gens_dir.mkdir(parents=True, exist_ok=True)
        self.current_file = self.root / CURRENT_NAME
        self._lock_file = self.root / ".publish.lock"

    # ---------- 写 ----------
    def _acquire_lock(self) -> None:
        if self._lock_file.exists():
            age = time.time() - self._lock_file.stat().st_mtime
            if age < STALE_LOCK_SECONDS:
                pid = self._lock_file.read_text(encoding="utf-8").strip()
                raise RuntimeError(f"另一个发布进程正在写入（pid={pid}, {age:.0f}s）")
        self._lock_file.write_text(str(os.getpid()), encoding="utf-8")

    def _release_lock(self) -> None:
        self._lock_file.unlink(missing_ok=True)

    def publish_file(self, source: Path, rows: int | None = None) -> Generation:
        """从已存在的 parquet 文件发布代际（流式搬运，不加载到内存）。

        用于全市场级数据：1600 万行不能从 DataFrame 写一次，只能按文件搬。
        """
        self._acquire_lock()
        gen = uuid.uuid4().hex
        target = self.gens_dir / gen
        try:
            target.mkdir(parents=True, exist_ok=True)
            dst = target / "data.parquet"
            shutil.move(str(source), dst)
            if rows is None:
                rows = int(pl.scan_parquet(dst).select(pl.len()).collect()[0, 0])
            (target / "_SUCCESS").write_text(
                json.dumps({"rows": rows, "at": time.time()}), encoding="utf-8"
            )
            payload = {
                "generation": gen,
                "rows": rows,
                "updated_at": time.time(),
                "status": "ready",
            }
            tmp_json = self.current_file.with_suffix(".json.tmp")
            tmp_json.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_json, self.current_file)  # 原子切换点
        finally:
            self._release_lock()
        self._gc()
        return Generation(gen, rows, time.time(), target)

    def publish(self, frame: pl.DataFrame) -> Generation:
        self._acquire_lock()
        gen = uuid.uuid4().hex
        target = self.gens_dir / gen
        try:
            target.mkdir(parents=True, exist_ok=True)
            tmp = target / "data.parquet.tmp"
            frame.write_parquet(tmp, compression="lz4")
            os.replace(tmp, target / "data.parquet")
            (target / "_SUCCESS").write_text(
                json.dumps({"rows": frame.height, "at": time.time()}), encoding="utf-8"
            )
            payload = {
                "generation": gen,
                "rows": frame.height,
                "updated_at": time.time(),
                "status": "ready",
            }
            tmp_json = self.current_file.with_suffix(".json.tmp")
            tmp_json.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_json, self.current_file)  # 原子切换点
        finally:
            self._release_lock()
        self._gc()
        return Generation(gen, frame.height, time.time(), target)

    def _gc(self) -> None:
        gens = sorted(
            (p for p in self.gens_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        current = self.current()
        keep = {g.path.name for g in [current] if g}
        for i, path in enumerate(gens):
            if path.name in keep or i < KEEP_GENERATIONS:
                continue
            shutil.rmtree(path, ignore_errors=True)

    # ---------- 读 ----------
    def current(self) -> Generation | None:
        if not self.current_file.exists():
            return None
        try:
            payload = json.loads(self.current_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        gen_dir = self.gens_dir / payload.get("generation", "")
        if not (gen_dir / "_SUCCESS").exists() or not (gen_dir / "data.parquet").exists():
            return None
        return Generation(
            generation=payload["generation"],
            rows=int(payload.get("rows", 0)),
            updated_at=float(payload.get("updated_at", 0)),
            path=gen_dir,
        )

    def require(self) -> Generation:
        gen = self.current()
        if gen is None:
            raise StoreUnavailable("没有可读的稳定代际，请先运行 scripts/gen_data.py")
        return gen

    def scan(self) -> pl.LazyFrame:
        return parquet.scan_enriched(str(self.require().path / "data.parquet"))

    def collect(self) -> pl.DataFrame:
        return self.scan().collect()

    def history(self) -> list[dict]:
        rows = []
        for path in sorted(self.gens_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_dir():
                continue
            ok = (path / "_SUCCESS").exists()
            rows.append(
                {
                    "generation": path.name,
                    "ready": ok,
                    "size_kb": round((path / "data.parquet").stat().st_size / 1024) if ok else 0,
                    "updated_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)
                    ),
                }
            )
        return rows
