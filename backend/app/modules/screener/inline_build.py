"""在服务进程内重算选股缓存。

打包成 exe 后没有独立的 python 解释器，`subprocess` 起不来 `scripts/build_screener_cache.py`，
所以刷新按钮一直是「脚本不存在」。这里把同一套逻辑做成本进程内的函数，用后台线程跑：
只取近 N 天、只算已启用策略、每个策略跑完立刻 gc，峰值内存大约 1 GB。

与 `scripts/build_screener_cache.py` 的差别：
  · 复用 app.runtime 的 STORE / ENGINE 单例（不重复加载数据与策略）
  · 产物路径一致（data/screener_cache.json + screener_cache.meta.json）
"""
from __future__ import annotations

import gc
import json
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl


def build_cache(days: int = 300, limit: int = 20) -> dict:
    """重算并落盘，返回 meta。调用方负责保证不并发。"""
    from app import config
    from app.modules.screener.batch import _parse_ranking, cn_name
    from app.runtime import ENGINE, STORE

    t0 = time.time()
    ENGINE.reload()
    store = STORE
    gen = store.require().generation
    ids = sorted(ENGINE.registry)

    since = date.today() - timedelta(days=days)
    frame = store.scan().filter(pl.col("date") >= since).collect()
    latest = frame["date"].max()

    names: dict[str, str] = {}
    names_path = Path(config.DATA_DIR) / "symbol_names.json"
    if names_path.exists():
        try:
            names = json.loads(names_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    ranking = _parse_ranking()
    items: list[dict] = []
    for sid in ids:
        hits = None
        try:
            hits = ENGINE.run(sid, {}, frame)
            if hits.height == 0:
                items.append(
                    {"id": sid, "name": cn_name(sid, ENGINE.registry[sid].name),
                     "hit_total": 0, "signal_date": None, "rows": []}
                )
            else:
                hit_dates = hits["date"].unique().sort().to_list()
                target = hit_dates[-1] if hit_dates else latest
                today = hits.filter(hits["date"] == target)
                cols = [c for c in ("symbol", "date", "close", "score", "note") if c in hits.columns]
                rows = today.select(cols).head(limit).to_dicts()
                for r in rows:
                    for k, v in list(r.items()):
                        if v is None:
                            continue
                        if not isinstance(v, (int, float, str)):
                            r[k] = str(v)
                    r["name"] = names.get(str(r.get("symbol", "")), "")
                items.append({
                    "id": sid,
                    "name": cn_name(sid, ENGINE.registry[sid].name),
                    "hit_total": int(hits.height),
                    "signal_date": str(target),
                    "rows": rows,
                })
        except Exception as exc:  # noqa: BLE001
            items.append(
                {"id": sid, "name": cn_name(sid, ENGINE.registry[sid].name),
                 "error": f"{type(exc).__name__}: {exc}"[:120], "rows": []}
            )
        finally:
            del hits
            gc.collect()

    for it in items:
        rk = ranking.get(it["id"]) or {}
        it["rank"] = rk.get("rank")
        it["win_rate"] = rk.get("win_rate")
        it["return"] = rk.get("return")
        it["sharpe"] = rk.get("sharpe")

    items.sort(key=lambda x: (x.get("rank") is None, x.get("rank") or 999, -(x.get("hit_total") or 0)))
    total_today = sum(len(x.get("rows") or []) for x in items)

    data_dir = Path(config.DATA_DIR)
    payload = {
        "generation": gen[:8],
        "strategy_count": len(items),
        "total_signals_today": total_today,
        "items": items,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    meta = {
        "generation": gen[:8],
        "days": days,
        "elapsed": round(time.time() - t0, 1),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "strategies": len(items),
        "total_signals": total_today,
    }
    (data_dir / "screener_cache.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (data_dir / "screener_cache.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    del frame
    gc.collect()
    return meta
