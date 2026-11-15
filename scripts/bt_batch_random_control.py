"""批量随机对照：把剩下 15 个策略全部过一遍

原理：
    随机选股的分布和策略无关，所以每个 split 只算一次随机分布（8 次），
    然后 15 个策略各跑 1 次，对比。

    总回测次数 = 3 段 × 8 随机 + 15 策略 × 3 段 = 69 次，比逐个算快 5 倍。

判读：
    策略 > 随机上限   →  有贡献（★）
    策略在区间内       →  噪声（×）
    策略 < 随机下限   →  有害（↓↓）

固定风控：8% 止损 + 10 天持有 + 5 仓 + 单笔风险 1%

用法：
    python scripts/bt_batch_random_control.py
    python scripts/bt_batch_random_control.py --runs 10
"""
from __future__ import annotations

import argparse
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
from app.engine import StrategyEngine  # noqa: E402
from app.store import GenerationStore  # noqa: E402

SPLITS = {
    "train": (date(2021, 1, 4), date(2023, 12, 29)),
    "valid": (date(2024, 1, 2), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 2), date(2026, 11, 12)),
}

# 已单独测过的两个，跳过
SKIP = {"breakout_p1", "instock_low_backtrace_increase"}


def random_hits(bars: pl.DataFrame, n_per_day: int, seed: int) -> pl.DataFrame:
    rnd = random.Random(seed)
    valid = bars.filter((pl.col("close") >= 3.0) & (pl.col("volume") > 0))
    rows = []
    for (d,), g in valid.group_by("date", maintain_order=True):
        syms = g["symbol"].to_list()
        if len(syms) < n_per_day:
            continue
        for s in rnd.sample(syms, n_per_day):
            rows.append({"date": d, "symbol": s, "score": 1.0, "note": "random"})
    if not rows:
        return pl.DataFrame(
            schema={"date": pl.Date, "symbol": pl.String, "score": pl.Float64, "note": pl.String}
        )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--hold", type=int, default=10)
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()
    eng = StrategyEngine(config.STRATEGY_DIR)

    targets = sorted([k for k in eng.registry if k not in SKIP])
    print(f"待测 {len(targets)} 个策略")
    for t in targets:
        print(f"  {t:<34} {eng.registry[t].name}")
    print()

    cfg = BacktestConfig(
        init_cash=100_000,
        risk_pct=0.01,
        max_positions=5,
        max_position_pct=0.20,
        stop_loss_pct=args.stop,
        max_hold_days=args.hold,
    )

    all_results: list[dict] = []

    for split, (d0, d1) in SPLITS.items():
        t0 = time.time()
        print("=" * 96)
        print(f"Split {split}  {d0} ~ {d1}")
        print("=" * 96)

        # ---- 1. 随机分布（全市场，只算一次）----
        full_bars = (
            store.scan()
            .filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1 + timedelta(days=15))))
            .select(
                "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "change_pct", "turnover",
            )
            .collect()
        )
        rnd_rets = []
        for i in range(args.runs):
            hits = random_hits(full_bars, 5, seed=3000 + i)
            if hits.height == 0:
                continue
            try:
                r = BacktestEngine(cfg, BarStore(full_bars)).run(hits, d0, d1).metrics.get("total_return")
                if r is not None:
                    rnd_rets.append(float(r))
            except Exception:  # noqa: BLE001
                pass
        if not rnd_rets:
            print("  随机对照失败，跳过该段")
            del full_bars
            continue
        r_mean = sum(rnd_rets) / len(rnd_rets)
        r_lo, r_hi = min(rnd_rets), max(rnd_rets)
        print(
            f"  随机分布：均值 {r_mean*100:+.1f}%  区间 [{r_lo*100:+.1f}%, {r_hi*100:+.1f}%]  n={len(rnd_rets)}"
        )
        print()
        print("{:<34}{:>9}{:>9}{:>9}{:>8}{:>7}".format("策略", "收益", "回撤", "交易", "胜率", "判定"))
        print("-" * 96)
        del full_bars

        # ---- 2. 每个策略跑一次 ----
        for sid in targets:
            # 分段读数据（避免 OOM）
            seg = (
                store.scan()
                .filter(
                    (pl.col("date") >= pl.lit(d0 - timedelta(days=400)))
                    & (pl.col("date") <= pl.lit(d1))
                )
                .collect()
            )
            try:
                sig = eng.run(sid, {}, seg)
            except Exception as exc:  # noqa: BLE001
                print(f"  {sid:<34} 信号失败 {type(exc).__name__}")
                del seg
                continue
            del seg

            sig = sig.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1)))
            if sig.height < 20:
                print(f"  {sid:<34} 信号仅 {sig.height} 条，跳过")
                continue

            syms = sig["symbol"].unique().to_list()
            bars = (
                store.scan()
                .filter(
                    pl.col("symbol").is_in(syms)
                    & (pl.col("date") >= pl.lit(d0))
                    & (pl.col("date") <= pl.lit(d1 + timedelta(days=15)))
                )
                .select(
                    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                    "change_pct", "turnover",
                )
                .collect()
            )

            try:
                res = BacktestEngine(cfg, BarStore(bars)).run(sig, d0, d1)
                m = res.metrics
                ret = m.get("total_return")
                if ret is None:
                    print(f"  {sid:<34} 无成交")
                    del bars
                    continue

                if ret > r_hi:
                    verdict = "★有贡献"
                elif ret < r_lo:
                    verdict = "↓↓有害"
                else:
                    verdict = "×噪声"

                print(
                    "{:<34}{:>+8.1f}%{:>8.1f}%{:>9}{:>7.0f}%{:>8}".format(
                        sid,
                        ret * 100,
                        (m.get("max_drawdown") or 0) * 100,
                        m.get("trades") or 0,
                        (m.get("win_rate") or 0) * 100,
                        verdict,
                    )
                )
                all_results.append(
                    {
                        "split": split,
                        "strategy": sid,
                        "name": eng.registry[sid].name,
                        "signals": int(sig.height),
                        "ret": round(ret, 4),
                        "mdd": round(m.get("max_drawdown") or 0, 4),
                        "sharpe": m.get("sharpe"),
                        "trades": m.get("trades"),
                        "win": m.get("win_rate"),
                        "random_mean": round(r_mean, 4),
                        "random_lo": round(r_lo, 4),
                        "random_hi": round(r_hi, 4),
                        "verdict": verdict,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  {sid:<34} 回测失败 {type(exc).__name__}: {str(exc)[:50]}")
            del bars

        print(f"\n  本段耗时 {time.time()-t0:.0f}s\n")

    out = ROOT / "reports" / "batch_random_control.json"
    out.write_text(json.dumps(all_results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # 汇总
    print("=" * 96)
    print("汇总：按三段判定统计")
    print("=" * 96)
    from collections import defaultdict

    by_strat: dict[str, list[dict]] = defaultdict(list)
    for r in all_results:
        by_strat[r["strategy"]].append(r)

    print("{:<34}{:<28}{:>8}".format("策略", "三段判定", "总评"))
    print("-" * 96)
    summary = []
    for sid, rows in sorted(by_strat.items()):
        vs = [r["verdict"] for r in rows]
        stars = sum(1 for v in vs if v == "★有贡献")
        harms = sum(1 for v in vs if v == "↓↓有害")
        if stars >= 2:
            total = "★ 值得继续"
        elif harms >= 2:
            total = "↓↓ 有害"
        else:
            total = "× 噪声"
        print("{:<34}{:<28}{:>8}".format(sid, " ".join(vs), total))
        summary.append({"strategy": sid, "name": rows[0]["name"], "verdicts": vs, "total": total})

    out2 = ROOT / "reports" / "batch_random_summary.json"
    out2.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print(f"→ {out}")
    print(f"→ {out2}")


if __name__ == "__main__":
    main()
