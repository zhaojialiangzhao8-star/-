"""对照组：随机选股 + 同样的风控（8% 止损 + 10 天持有 + 5 仓）

为什么要做这个：
    平台突破的账户回测在训练集 +5.5%、验证集 +4.6%。
    但事件研究显示入场信号本身是负 alpha。

    所以必须回答：那 +5.5% 是信号的功劳，还是「8% 止损 + 10 天持有」这套风控的功劳？

方法：
    每天从全市场随机选 5 只（价格 > 3 元、有成交），
    配同样的 8% 止损 + 10 天持有上限 + 5 仓，跑一遍账户回测。

    重复 10 次取平均（消除随机性）。

判读：
    随机组 ≈ 策略组   →  信号贡献为 0，赚的是风控的钱
    随机组 << 策略组  →  信号有正贡献
    随机组 >> 策略组  →  信号有负贡献

用法：
    python scripts/bt_random_control.py
    python scripts/bt_random_control.py --runs 20
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
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

# 平台突破策略的实测结果（对照用）
STRATEGY = {
    "train": 0.055388,
    "valid": 0.046,
    "holdout": -0.039,
}


def random_hits(bars: pl.DataFrame, n_per_day: int, seed: int) -> pl.DataFrame:
    """每天随机选 n_per_day 只，生成 hits 表（date, symbol, score, note）。"""
    rnd = random.Random(seed)

    # 只保留有成交、价格 > 3 元的标的
    valid = bars.filter((pl.col("close") >= 3.0) & (pl.col("volume") > 0))

    rows = []
    for (d,), g in valid.group_by("date", maintain_order=True):
        syms = g["symbol"].to_list()
        if len(syms) < n_per_day:
            continue
        picks = rnd.sample(syms, n_per_day)
        for s in picks:
            rows.append({"date": d, "symbol": s, "score": 1.0, "note": "random"})

    if not rows:
        return pl.DataFrame(
            schema={"date": pl.Date, "symbol": pl.String, "score": pl.Float64, "note": pl.String}
        )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--per-day", type=int, default=5)
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--hold", type=int, default=10)
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()

    results = []
    for split, (d0, d1) in SPLITS.items():
        print("=" * 80)
        print(f"Split {split}  {d0} ~ {d1}  每天随机 {args.per_day} 只  × {args.runs} 次")
        print("=" * 80)

        # 行情：整个区间
        bars = (
            store.scan()
            .filter(
                (pl.col("date") >= pl.lit(d0))
                & (pl.col("date") <= pl.lit(d1))
            )
            .select(
                "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "change_pct", "turnover",
            )
            .collect()
        )
        print(f"  行情 {bars.height:,} 行  {bars['symbol'].n_unique()} 只")

        cfg = BacktestConfig(
            init_cash=100_000,
            risk_pct=0.01,
            max_positions=5,
            max_position_pct=0.20,
            stop_loss_pct=args.stop,
            max_hold_days=args.hold,
        )

        rets = []
        for i in range(args.runs):
            hits = random_hits(bars, args.per_day, seed=1000 + i)
            if hits.height == 0:
                continue
            try:
                bs = BarStore(bars)          # 每次新建，避免状态污染
                eng = BacktestEngine(cfg, bs)
                res = eng.run(hits, d0, d1)
                r = res.metrics.get("total_return")
                if r is not None:
                    rets.append(float(r))
            except Exception as exc:  # noqa: BLE001
                print(f"    第 {i+1} 次失败 {type(exc).__name__}: {str(exc)[:60]}")

        if not rets:
            print("  全部失败")
            continue

        avg = sum(rets) / len(rets)
        lo, hi = min(rets), max(rets)
        strat = STRATEGY[split]
        diff = strat - avg

        print(f"  随机组：均值 {avg*100:+.2f}%  区间 [{lo*100:+.2f}%, {hi*100:+.2f}%]  n={len(rets)}")
        print(f"  策略组：{strat*100:+.2f}%")
        print(f"  差值：  {diff*100:+.2f}pp  ", end="")
        if abs(diff) < 0.02:
            print("→ 信号贡献接近 0，赚的是风控的钱")
        elif diff > 0:
            print("→ 信号有正贡献")
        else:
            print("→ 信号有负贡献")
        print()

        results.append(
            {
                "split": split,
                "random_mean": round(avg, 4),
                "random_min": round(lo, 4),
                "random_max": round(hi, 4),
                "runs": len(rets),
                "strategy": strat,
                "diff": round(diff, 4),
            }
        )
        del bars

    out = ROOT / "reports" / "breakout_random_control.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→ " + str(out))

    # 总结
    print()
    print("=" * 80)
    print("总结")
    print("=" * 80)
    for r in results:
        verdict = (
            "信号无贡献"
            if abs(r["diff"]) < 0.02
            else ("信号有正贡献" if r["diff"] > 0 else "信号有负贡献")
        )
        print(
            "{:<10} 随机 {:+.1f}%  策略 {:+.1f}%  差 {:+.1f}pp  {}".format(
                r["split"], r["random_mean"] * 100, r["strategy"] * 100, r["diff"] * 100, verdict
            )
        )


if __name__ == "__main__":
    main()
