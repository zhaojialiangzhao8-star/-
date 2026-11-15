"""全策略随机对照：直接扫策略目录，不管 enabled 状态

为什么这么做：
    enabled.json 被改过（只剩 5 个启用，13 个带数字的 disabled）。
    为了不覆盖别人的改动，这个脚本直接加载所有策略文件，独立测试。

    结果写在新文件里，不碰 enabled.json。

流程：
    1. 每个 split 算一次随机分布（8 次随机选股，同样风控）
    2. 逐个策略跑，对比随机区间
    3. 输出完整排名

固定风控：8% 止损 + 10 天持有 + 5 仓 + 单笔风险 1%
成本：佣金 0.025% / 印花 0.05% / 过户费 / 滑点 5bp

用法：
    python scripts/bt_all_strategies_random.py --runs 8
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
from app.engine import StrategyEngine  # noqa: E402
from app.store import GenerationStore  # noqa: E402

SPLITS = {
    "train": (date(2021, 1, 4), date(2023, 12, 29)),
    "valid": (date(2024, 1, 2), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 2), date(2026, 11, 12)),
}


def load_all_strategies(strat_dir: Path) -> dict[str, tuple[str, object]]:
    """绕过 enabled.json，直接扫目录加载所有策略插件。"""
    out: dict[str, tuple[str, object]] = {}
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
            fn = getattr(mod, "filter_frame", None)
            if not meta or not fn:
                continue
            sid = meta.get("id") or f.stem
            out[sid] = (meta.get("name") or sid, mod)
        except Exception:  # noqa: BLE001
            continue
    return out


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
    ap.add_argument("--out", default="all_strategies_random.json")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()

    plugs = load_all_strategies(config.STRATEGY_DIR)
    print(f"扫描到 {len(plugs)} 个策略文件")
    for sid, (name, _) in sorted(plugs.items()):
        print(f"  {sid:<34} {name}")
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
        print("=" * 100)
        print(f"Split {split}  {d0} ~ {d1}")
        print("=" * 100)

        full_bars = (
            store.scan()
            .filter(
                (pl.col("date") >= pl.lit(d0))
                & (pl.col("date") <= pl.lit(d1 + timedelta(days=15)))
            )
            .select(
                "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "change_pct", "turnover",
            )
            .collect()
        )

        rnd_rets = []
        for i in range(args.runs):
            hits = random_hits(full_bars, 5, seed=5000 + i)
            if hits.height == 0:
                continue
            try:
                r = BacktestEngine(cfg, BarStore(full_bars)).run(hits, d0, d1).metrics.get(
                    "total_return"
                )
                if r is not None:
                    rnd_rets.append(float(r))
            except Exception:  # noqa: BLE001
                pass
        if not rnd_rets:
            print("  随机对照失败")
            del full_bars
            continue
        r_mean = sum(rnd_rets) / len(rnd_rets)
        r_lo, r_hi = min(rnd_rets), max(rnd_rets)
        print(
            f"  随机分布：均值 {r_mean*100:+.1f}%  区间 [{r_lo*100:+.1f}%, {r_hi*100:+.1f}%]  n={len(rnd_rets)}"
        )
        print()
        print("{:<34}{:>7}{:>9}{:>9}{:>8}{:>6}{:>9}".format("策略", "信号", "收益", "回撤", "交易", "胜率", "判定"))
        print("-" * 100)
        del full_bars

        # 该段的数据（含预热），所有策略共用
        seg = (
            store.scan()
            .filter(
                (pl.col("date") >= pl.lit(d0 - timedelta(days=400)))
                & (pl.col("date") <= pl.lit(d1))
            )
            .collect()
        )

        for sid, (name, mod) in sorted(plugs.items()):
            try:
                sig = mod.filter_frame(seg, {})
            except Exception as exc:  # noqa: BLE001
                print(f"  {sid:<34} 信号失败 {type(exc).__name__}")
                continue

            if sig is None or sig.height == 0:
                print(f"  {sid:<34} 无信号")
                continue

            sig = sig.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1)))
            if sig.height < 20:
                print(f"  {sid:<34}{sig.height:>7} 样本不足")
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
                    print(f"  {sid:<34}{sig.height:>7} 无成交")
                    del bars
                    continue

                if ret > r_hi:
                    verdict = "★"
                elif ret < r_lo:
                    verdict = "↓↓"
                else:
                    verdict = "×"

                print(
                    "{:<34}{:>7}{:>+8.1f}%{:>8.1f}%{:>8}{:>5.0f}%{:>9}".format(
                        sid,
                        sig.height,
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
                        "name": name,
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
                print(f"  {sid:<34}{sig.height:>7} 回测失败 {type(exc).__name__}")
            del bars

        del seg
        print(f"\n  本段耗时 {time.time()-t0:.0f}s\n")

    out = ROOT / "reports" / args.out
    out.write_text(json.dumps(all_results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # 汇总：按策略统计三段判定
    print("=" * 100)
    print("汇总（★=超出随机上限 / ×=区间内 / ↓↓=低于下限）")
    print("=" * 100)
    from collections import defaultdict

    by: dict[str, list[dict]] = defaultdict(list)
    for r in all_results:
        by[r["strategy"]].append(r)

    rows = []
    for sid, rs in by.items():
        vs = {r["split"]: r["verdict"] for r in rs}
        stars = sum(1 for v in vs.values() if v == "★")
        harms = sum(1 for v in vs.values() if v == "↓↓")
        rets = [r["ret"] for r in rs]
        avg_ret = sum(rets) / len(rets)
        if stars >= 2:
            total = "★ 值得继续"
        elif harms >= 2:
            total = "↓↓ 有害"
        elif stars == 1:
            total = "? 单段有效"
        else:
            total = "× 噪声"
        rows.append(
            {
                "strategy": sid,
                "name": rs[0]["name"],
                "train": vs.get("train", "—"),
                "valid": vs.get("valid", "—"),
                "holdout": vs.get("holdout", "—"),
                "avg_ret": round(avg_ret, 4),
                "total": total,
            }
        )

    rows.sort(key=lambda x: -x["avg_ret"])
    print("{:<34}{:<7}{:<7}{:<7}{:>10}{:>12}".format("策略", "训练", "验证", "留出", "平均收益", "总评"))
    print("-" * 100)
    for r in rows:
        print(
            "{:<34}{:<7}{:<7}{:<7}{:>+9.1f}%{:>12}".format(
                r["strategy"], r["train"], r["valid"], r["holdout"], r["avg_ret"] * 100, r["total"]
            )
        )

    out2 = ROOT / "reports" / "all_strategies_summary.json"
    out2.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print(f"→ {out}")
    print(f"→ {out2}")


if __name__ == "__main__":
    main()
