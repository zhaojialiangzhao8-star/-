"""用同一套流程测 InStock·低回撤增长（含随机对照）

为什么测它：
    2026-11-12 的 63 组策略审计里，它是唯一在三段持有期都为正的策略：
        5 天 +47.6% / 10 天 +48.2% / 20 天 +25.3%
    但被拒 6,812 次（max_positions），数字可信度存疑。

    现在有 5 年数据（中位 1,421 根）+ 随机对照工具，可以重新判断。

流程（与平台突破完全一致）：
    1. 事件研究：训练/验证/留出三段，算超额（对全市场等权）
    2. 账户回测：8% 止损 + 10 天持有 + 5 仓
    3. 随机对照：同样风控随机选股，看差值
    4. 判读

判读标准：
    策略组显著高于随机组  →  信号有正贡献
    差值在随机区间内      →  信号无贡献（噪声）

用法：
    python scripts/bt_low_backtrace.py
"""
from __future__ import annotations

import argparse
import json
import random
import sys
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

STRATEGY_ID = "instock_low_backtrace_increase"


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

    if STRATEGY_ID not in eng.registry:
        raise SystemExit(f"策略 {STRATEGY_ID} 未启用")

    results = []
    for split, (d0, d1) in SPLITS.items():
        print("=" * 84)
        print(f"Split {split}  {d0} ~ {d1}")
        print("=" * 84)

        # 分段读数据算信号（6.3M 行一次读会 OOM）
        # 往前多读 400 天做预热（策略需要 MA60 / 回撤窗口）
        seg = (
            store.scan()
            .filter(
                (pl.col("date") >= pl.lit(d0 - timedelta(days=400)))
                & (pl.col("date") <= pl.lit(d1))
            )
            .collect()
        )
        sig_raw = eng.run(STRATEGY_ID, {}, seg)
        del seg
        sig = sig_raw.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1)))
        print(f"  区间内信号 {sig.height:,} 条  {sig['symbol'].n_unique()} 只")
        if sig.height == 0:
            print("  无信号")
            continue

        # 行情
        syms = sig["symbol"].unique().to_list()
        bars = (
            store.scan()
            .filter(
                pl.col("symbol").is_in(syms)
                & (pl.col("date") >= pl.lit(d0))
                & (pl.col("date") <= pl.lit(d1 + timedelta(days=args.hold + 5)))
            )
            .select(
                "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "change_pct", "turnover",
            )
            .collect()
        )

        cfg = BacktestConfig(
            init_cash=100_000,
            risk_pct=0.01,
            max_positions=5,
            max_position_pct=0.20,
            stop_loss_pct=args.stop,
            max_hold_days=args.hold,
        )

        # --- 策略组 ---
        try:
            res = BacktestEngine(cfg, BarStore(bars)).run(sig, d0, d1)
            m = res.metrics
            strat_ret = m.get("total_return")
            print(
                "  策略：收益 {:+.1f}%  回撤 {:.1f}%  夏普 {:.2f}  胜率 {:.1f}%  交易 {:d}".format(
                    (strat_ret or 0) * 100,
                    (m.get("max_drawdown") or 0) * 100,
                    m.get("sharpe") or 0,
                    (m.get("win_rate") or 0) * 100,
                    m.get("trades") or 0,
                )
            )
            print(f"  被拒 {res.rejected}")
        except Exception as exc:  # noqa: BLE001
            print(f"  策略回测失败 {type(exc).__name__}: {str(exc)[:80]}")
            continue

        # --- 随机对照 ---
        # 只对策略用到的股票池做随机（控制股票池一致）
        pool = bars.filter(pl.col("symbol").is_in(syms))
        rnd_rets = []
        for i in range(args.runs):
            hits = random_hits(pool, 5, seed=2000 + i)
            if hits.height == 0:
                continue
            try:
                r = BacktestEngine(cfg, BarStore(pool)).run(hits, d0, d1).metrics.get("total_return")
                if r is not None:
                    rnd_rets.append(float(r))
            except Exception:  # noqa: BLE001
                pass

        if rnd_rets:
            avg = sum(rnd_rets) / len(rnd_rets)
            lo, hi = min(rnd_rets), max(rnd_rets)
            diff = (strat_ret or 0) - avg
            print(
                f"  随机：均值 {avg*100:+.1f}%  区间 [{lo*100:+.1f}%, {hi*100:+.1f}%]  n={len(rnd_rets)}"
            )
            print(f"  差值：{diff*100:+.1f}pp  ", end="")
            if (strat_ret or 0) > hi:
                print("→ 策略超出随机区间上限，信号有正贡献")
            elif (strat_ret or 0) < lo:
                print("→ 策略低于随机区间下限，信号有负贡献")
            else:
                print("→ 落在随机区间内，信号无贡献")

            results.append(
                {
                    "split": split,
                    "signals": int(sig.height),
                    "strategy": round(strat_ret or 0, 4),
                    "random_mean": round(avg, 4),
                    "random_min": round(lo, 4),
                    "random_max": round(hi, 4),
                    "diff": round(diff, 4),
                    "mdd": round(m.get("max_drawdown") or 0, 4),
                    "sharpe": m.get("sharpe"),
                    "trades": m.get("trades"),
                    "win": m.get("win_rate"),
                }
            )
        print()

    out = ROOT / "reports" / "low_backtrace_test.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print("=" * 84)
    print("总结")
    print("=" * 84)
    if not results:
        print("  无结果")
        return
    for r in results:
        inside = r["random_min"] <= r["strategy"] <= r["random_max"]
        print(
            "{:<10} 策略 {:+.1f}%  随机 {:+.1f}% [{:+.1f}%, {:+.1f}%]  {}".format(
                r["split"],
                r["strategy"] * 100,
                r["random_mean"] * 100,
                r["random_min"] * 100,
                r["random_max"] * 100,
                "区间内（无贡献）" if inside else "区间外（有贡献）",
            )
        )

    outside = [r for r in results if not (r["random_min"] <= r["strategy"] <= r["random_max"])]
    print()
    if len(outside) == len(results):
        print("结论：三段全部落在随机区间外 → 信号有贡献，值得继续")
    elif outside:
        print(f"结论：{len(outside)}/{len(results)} 段在区间外 → 部分有效，需要更多验证")
    else:
        print("结论：三段全部落在随机区间内 → 信号无贡献")
    print()
    print(f"→ {out}")


if __name__ == "__main__":
    main()
