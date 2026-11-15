"""把「全部策略的当日信号」算好写到磁盘，Web 服务只读结果。

为什么要拆出来：15 个策略 × 全市场，峰值要 3~4 GB（frame 1.2 GB + 每个策略的
hits 和中间量）。在 16 GB 机器上让 Web 服务自己算，结果就是把 exe 撑到 4.5 GB
然后 OOM —— 用户看到的就是「智能选股不出股票」和接口 500。

拆成独立进程后：
  · 计算进程跑完退出，内存立刻还给系统
  · Web 服务不加载 frame，常驻内存从 2.5 GB 降到 300 MB 量级
  · 页面秒开（读 JSON），不再等 5 分钟

产物：
    backend/data/screener_cache.json    全部策略结果（供 /api/v1/screener/all 读）
    backend/data/screener_cache.meta.json 生成时间、代际、耗时

跑法：
    python scripts/build_screener_cache.py
    python scripts/build_screener_cache.py --years 5 --limit 20
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.engine import StrategyEngine  # noqa: E402
from app.modules.screener.batch import NAME_OVERRIDE, _parse_ranking, cn_name  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=300, help="信号回看天数：算指标够用就行，300 天覆盖 MA250")
    ap.add_argument("--limit", type=int, default=20, help="每个策略最多保留多少只")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t0 = time.time()
    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    engine = StrategyEngine(config.STRATEGY_DIR)
    engine.reload()
    ids = sorted(engine.registry)
    ranking = _parse_ranking()

    print(f"代际 {gen.generation[:8]} · {len(ids)} 个策略 · 回看 {args.days} 天", flush=True)

    # 当日信号只需要「够算指标」的历史。策略里最长的窗口是 MA250 / 52 周新高，
    # 300 天足够；不需要 5 年（那会让选股这一步白吃 1 GB 内存）。
    since = date.today() - timedelta(days=args.days)
    frame = store.scan().filter(pl.col("date") >= since).collect()
    print(f"信号窗口 {frame.height:,} 行 · {frame['symbol'].n_unique():,} 只", flush=True)

    latest = frame["date"].max()
    items: list[dict] = []

    # 股票名称：前端要显示「贵州茅台」而不是「600519.SH」。
    # scripts/fetch_symbol_names.py 抓好的缓存，读不到就退化成只有代码。
    names: dict[str, str] = {}
    names_path = Path(config.DATA_DIR) / "symbol_names.json"
    if names_path.exists():
        try:
            names = json.loads(names_path.read_text(encoding="utf-8"))
            print(f"名称表 {len(names):,} 条", flush=True)
        except Exception:  # noqa: BLE001
            pass

    for i, sid in enumerate(ids, 1):
        hits = None
        try:
            hits = engine.run(sid, {}, frame)
            if hits.height == 0:
                items.append({"id": sid, "name": cn_name(sid, engine.registry[sid].name),
                              "hit_total": 0, "signal_date": None, "rows": []})
            else:
                hit_dates = hits["date"].unique().sort().to_list()
                target = hit_dates[-1] if hit_dates else latest
                today = hits.filter(hits["date"] == target)
                cols = [c for c in ("symbol", "date", "close", "score", "note") if c in hits.columns]
                rows = today.select(cols).head(args.limit).to_dicts()
                # Categorical / date 不能直接 json，统一成 str；顺手补上名称
                for r in rows:
                    for k, v in list(r.items()):
                        if v is None:
                            continue
                        if not isinstance(v, (int, float, str)):
                            r[k] = str(v)
                    sym = str(r.get("symbol", ""))
                    r["name"] = names.get(sym, "")
                items.append({
                    "id": sid,
                    "name": cn_name(sid, engine.registry[sid].name),
                    "hit_total": int(hits.height),
                    "signal_date": str(target),
                    "rows": rows,
                })
            print(f"  [{i:>2}/{len(ids)}] {sid:<34} {hits.height:>7,} 条", flush=True)
        except Exception as exc:  # noqa: BLE001
            items.append({"id": sid, "name": cn_name(sid, engine.registry[sid].name),
                          "error": f"{type(exc).__name__}: {exc}"[:120], "rows": []})
            print(f"  [{i:>2}/{len(ids)}] {sid:<34} 失败 {type(exc).__name__}", flush=True)
        finally:
            del hits
            gc.collect()

    # 排名信息合进去
    for it in items:
        rk = ranking.get(it["id"]) or {}
        it["rank"] = rk.get("rank")
        it["win_rate"] = rk.get("win_rate")
        it["return"] = rk.get("return")
        it["sharpe"] = rk.get("sharpe")

    items.sort(key=lambda x: (x.get("rank") is None, x.get("rank") or 999, -(x.get("hit_total") or 0)))
    total_today = sum(len(x.get("rows") or []) for x in items)

    payload = {
        "generation": gen.generation[:8],
        "strategy_count": len(items),
        "total_signals_today": total_today,
        "items": items,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    meta = {
        "generation": gen.generation[:8],
        "days": args.days,
        "elapsed": round(time.time() - t0, 1),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "strategies": len(items),
        "total_signals": total_today,
    }

    out = Path(args.out) if args.out else Path(config.DATA_DIR) / "screener_cache.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (out.parent / "screener_cache.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n完成 {len(items)} 个策略 · {total_today} 个今日信号 · {time.time()-t0:.0f}s")
    print(f"→ {out}  ({out.stat().st_size/1024:.0f} KB)")
    print(f"→ {out.parent / 'screener_cache.meta.json'}")


if __name__ == "__main__":
    main()
