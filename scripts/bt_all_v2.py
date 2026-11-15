"""全策略随机对照 v2：分批 + 增量保存

v1 崩了（60 个策略 × 3 段一次跑完，内存/超时爆掉）。

v2 改动：
    1. 每跑完一个策略就写一次盘（崩溃不丢进度）
    2. 支持 --resume 跳过已完成
    3. 支持 --only 只跑指定的（先跑关键 20 个）
    4. 随机分布按 split 缓存到文件，不重复算

用法：
    # 先跑重点策略（enabled 5 个 + disabled 里提到的）
    python scripts/bt_all_v2.py --only key

    # 跑全部
    python scripts/bt_all_v2.py --resume
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig  # noqa: E402
from app.store import GenerationStore  # noqa: E402

SPLITS = {
    "train": (date(2021, 1, 4), date(2023, 12, 29)),
    "valid": (date(2024, 1, 2), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 2), date(2026, 11, 12)),
}

OUT = ROOT / "reports" / "all_strategies_v2.json"
BENCH = ROOT / "reports" / "_random_bench.json"

# 重点策略：enabled 的 5 个 + disabled 里提到过的
KEY = [
    "classic_rsi_oversold",
    "classic_mean_revert",
    "classic_boll_squeeze",
    "pattern_kline_forms",
    "instock_low_backtrace_increase",
    "classic_kdj_cross",
    "classic_donchian",
    "classic_week52_high",
    "classic_reversal",
    "instock_high_tight_flag",
    "consec_up_breakout",
    "small_up_breakout",
    "pattern_five_up_limit",
    "board_1to2",
    "board_2to3",
    "board_3to4",
    "breakout_p1",
]


def load_plugins(strat_dir: Path) -> dict[str, tuple[str, object]]:
    out = {}
    for f in sorted(strat_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"plug_{f.stem}", f)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            meta = getattr(mod, "META", None)
            if not meta or not getattr(mod, "filter_frame", None):
                continue
            sid = meta.get("id") or f.stem
            out[sid] = (meta.get("name") or sid, mod)
        except Exception:  # noqa: BLE001
            continue
    return out


def get_bench(store, cfg, split: str, runs: int) -> dict:
    """随机分布，缓存到文件。"""
    cache = {}
    if BENCH.exists():
        try:
            cache = json.loads(BENCH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    if split in cache:
        return cache[split]

    d0, d1 = SPLITS[split]
    bars = (
        store.scan()
        .filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1 + timedelta(days=15))))
        .select("symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "change_pct", "turnover")
        .collect()
    )
    rnd = random.Random(7000 + hash(split) % 1000)
    valid = bars.filter((pl.col("close") >= 3.0) & (pl.col("volume") > 0))
    # 关键优化：BarStore 只构造一次（partition_by 3M 行很慢）
    bar_store = BarStore(bars)
    rets = []
    for i in range(runs):
        rows = []
        for (d,), g in valid.group_by("date", maintain_order=True):
            syms = g["symbol"].to_list()
            if len(syms) < 5:
                continue
            for s in rnd.sample(syms, 5):
                rows.append({"date": d, "symbol": s, "score": 1.0, "note": "r"})
        if not rows:
            continue
        hits = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
        try:
            r = BacktestEngine(cfg, bar_store).run(hits, d0, d1).metrics.get("total_return")
            if r is not None:
                rets.append(float(r))
        except Exception:  # noqa: BLE001
            pass
    del bars, bar_store

    if not rets:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0, "n": 0}
    res = {
        "mean": round(sum(rets) / len(rets), 4),
        "lo": round(min(rets), 4),
        "hi": round(max(rets), 4),
        "n": len(rets),
    }
    cache[split] = res
    BENCH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--only", default="", choices=["", "key"])
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--hold", type=int, default=10)
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()
    plugs = load_plugins(config.STRATEGY_DIR)

    targets = sorted(plugs)
    if args.only == "key":
        targets = [t for t in KEY if t in plugs]
    print(f"待测 {len(targets)} 个策略")
    for t in targets:
        print(f"  {t:<34} {plugs[t][0]}")
    print(flush=True)

    done: dict[str, dict] = {}
    if args.resume and OUT.exists():
        try:
            for r in json.loads(OUT.read_text(encoding="utf-8")):
                done[f"{r['strategy']}@{r['split']}"] = r
        except Exception:  # noqa: BLE001
            pass
        print(f"  已恢复 {len(done)} 条记录", flush=True)

    cfg = BacktestConfig(
        init_cash=100_000,
        risk_pct=0.01,
        max_positions=5,
        max_position_pct=0.20,
        stop_loss_pct=args.stop,
        max_hold_days=args.hold,
    )

    # 预读每个 split 的 seg（含预热），复用
    segs: dict[str, pl.DataFrame] = {}

    results = list(done.values())

    for split, (d0, d1) in SPLITS.items():
        print("=" * 100, flush=True)
        print(f"Split {split}  {d0} ~ {d1}", flush=True)
        bench = get_bench(store, cfg, split, args.runs)
        print(
            f"  随机分布：均值 {bench['mean']*100:+.1f}%  "
            f"区间 [{bench['lo']*100:+.1f}%, {bench['hi']*100:+.1f}%]  n={bench['n']}",
            flush=True,
        )
        print()
        print("{:<34}{:>7}{:>9}{:>9}{:>7}{:>6}{:>6}".format("策略", "信号", "收益", "回撤", "交易", "胜率", "判定"))
        print("-" * 88, flush=True)

        if split not in segs:
            segs[split] = (
                store.scan()
                .filter(
                    (pl.col("date") >= pl.lit(d0 - timedelta(days=400)))
                    & (pl.col("date") <= pl.lit(d1))
                )
                .collect()
            )
        seg = segs[split]

        for sid in targets:
            key = f"{sid}@{split}"
            if key in done:
                r = done[key]
                print(
                    "{:<34}{:>7}{:>+8.1f}%{:>8.1f}%{:>7}{:>5.0f}%{:>6}  (已跑)".format(
                        sid, r["signals"], r["ret"] * 100, r["mdd"] * 100, r["trades"], r["win"] * 100, r["verdict"]
                    ),
                    flush=True,
                )
                continue

            name, mod = plugs[sid]
            try:
                sig = mod.filter_frame(seg, {})
                if sig is None or sig.height == 0:
                    print(f"  {sid:<34} 无信号", flush=True)
                    continue
                sig = sig.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1)))
                if sig.height < 20:
                    print(f"  {sid:<34}{sig.height:>7} 样本不足", flush=True)
                    continue

                syms = sig["symbol"].unique().to_list()
                bars = (
                    store.scan()
                    .filter(
                        pl.col("symbol").is_in(syms)
                        & (pl.col("date") >= pl.lit(d0))
                        & (pl.col("date") <= pl.lit(d1 + timedelta(days=15)))
                    )
                    .select("symbol", "date", "open", "high", "low", "close", "volume",
                            "amount", "change_pct", "turnover")
                    .collect()
                )
                res = BacktestEngine(cfg, BarStore(bars)).run(sig, d0, d1)
                m = res.metrics
                ret = m.get("total_return")
                del bars

                if ret is None:
                    print(f"  {sid:<34}{sig.height:>7} 无成交", flush=True)
                    continue

                if ret > bench["hi"]:
                    verdict = "★"
                elif ret < bench["lo"]:
                    verdict = "↓↓"
                else:
                    verdict = "×"

                print(
                    "{:<34}{:>7}{:>+8.1f}%{:>8.1f}%{:>7}{:>5.0f}%{:>6}".format(
                        sid, sig.height, ret * 100, (m.get("max_drawdown") or 0) * 100,
                        m.get("trades") or 0, (m.get("win_rate") or 0) * 100, verdict,
                    ),
                    flush=True,
                )
                rec = {
                    "split": split,
                    "strategy": sid,
                    "name": name,
                    "signals": int(sig.height),
                    "ret": round(ret, 4),
                    "mdd": round(m.get("max_drawdown") or 0, 4),
                    "sharpe": m.get("sharpe"),
                    "trades": m.get("trades"),
                    "win": m.get("win_rate"),
                    "random_mean": bench["mean"],
                    "random_lo": bench["lo"],
                    "random_hi": bench["hi"],
                    "verdict": verdict,
                }
                results.append(rec)
                done[key] = rec
                # 增量保存
                OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                print(f"  {sid:<34} 失败 {type(exc).__name__}: {str(exc)[:40]}", flush=True)
        print(flush=True)

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # 汇总
    print("=" * 100)
    print("汇总")
    print("=" * 100, flush=True)
    from collections import defaultdict

    by: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by[r["strategy"]].append(r)

    rows = []
    for sid, rs in by.items():
        vs = {r["split"]: r["verdict"] for r in rs}
        stars = sum(1 for v in vs.values() if v == "★")
        harms = sum(1 for v in vs.values() if v == "↓↓")
        avg = sum(r["ret"] for r in rs) / len(rs)
        if stars >= 2:
            total = "★ 值得继续"
        elif harms >= 2:
            total = "↓↓ 有害"
        elif stars == 1:
            total = "? 单段有效"
        else:
            total = "× 噪声"
        rows.append({"strategy": sid, "name": rs[0]["name"], "train": vs.get("train", "—"),
                     "valid": vs.get("valid", "—"), "holdout": vs.get("holdout", "—"),
                     "avg_ret": round(avg, 4), "total": total})

    rows.sort(key=lambda x: -x["avg_ret"])
    print("{:<34}{:<7}{:<7}{:<7}{:>10}{:>14}".format("策略", "训练", "验证", "留出", "平均收益", "总评"))
    print("-" * 100)
    for r in rows:
        print("{:<34}{:<7}{:<7}{:<7}{:>+9.1f}%{:>14}".format(
            r["strategy"], r["train"], r["valid"], r["holdout"], r["avg_ret"] * 100, r["total"]))

    (ROOT / "reports" / "all_strategies_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print(f"→ {OUT}")
    print(f"→ {ROOT / 'reports' / 'all_strategies_summary.json'}")


if __name__ == "__main__":
    main()
