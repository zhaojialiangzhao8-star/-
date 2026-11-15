"""基本面因子测试：低 PE / 低 PB / 小市值 / 高 BPS

这是「用随机对照检验基本面因子」的第一批。

数据：
    data/fundamentals_snapshot.parquet   当前快照（pe/pb/mcap/bps/price）
    代际 enriched                        日线（close）

历史因子的推导（近似，只用于分位数排序）：
    PB(t)    = close(t) / bps          ← 假设 BPS 不变
    市值(t)  = close(t) / close(now) × mcap(now)   ← 假设股本不变
    PE(t)    = close(t) / eps          ← eps = close(now) / pe(now)

    局限：跨越财报期会失真。对策：只用于横截面分位数排序，
          每年用当年快照重算一次（这里先用最新快照，标注为局限）。

测试设计：
    每个交易日，按因子值把全市场分 5 组
    每天每组选 5 只（组内随机，模拟「买一篮子」）
    配同样的 8% 止损 + 10 天持有
    三段验证 + 随机对照

用法：
    python scripts/test_value_factors.py
    python scripts/test_value_factors.py --factor pb
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
from app.store import GenerationStore  # noqa: E402

SPLITS = {
    "train": (date(2021, 1, 4), date(2023, 12, 29)),
    "valid": (date(2024, 1, 2), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 2), date(2026, 11, 12)),
}

FUND = config.DATA_DIR / "fundamentals_snapshot.parquet"


def build_factor_frame() -> pl.DataFrame:
    """把快照拼到日线上，算每日因子值。"""
    store = GenerationStore(config.DATA_DIR)
    store.require()
    snap = pl.read_parquet(FUND)

    # 最新收盘价（用于反推市值）
    last = (
        store.scan()
        .select("symbol", "date", "close")
        .sort(["symbol", "date"])
        .group_by("symbol")
        .agg(pl.col("close").last().alias("close_now"))
        .collect()
    )

    base = snap.join(last, on="symbol", how="inner").with_columns(
        # 腾讯没有 bps 字段，用 price / pb 反推每股净资产
        pl.when(pl.col("pb") > 0)
        .then(pl.col("price") / pl.col("pb"))
        .otherwise(None)
        .alias("bps"),
        # EPS：price / pe
        pl.when(pl.col("pe") > 0)
        .then(pl.col("price") / pl.col("pe"))
        .otherwise(None)
        .alias("eps_implied"),
    )

    df = (
        store.scan()
        .select("symbol", "date", "open", "high", "low", "close", "volume", "amount")
        .join(
            base.lazy().select("symbol", "pe", "pb", "bps", "mcap", "close_now", "eps_implied"),
            on="symbol",
            how="inner",
        )
        .collect()
        .sort(["symbol", "date"])
    )

    df = df.with_columns(
        # 反推历史市值（假设股本不变）
        (pl.col("close") / pl.col("close_now") * pl.col("mcap")).alias("mcap_hist"),
        # 反推历史 PB（假设 BPS 不变）
        (pl.col("close") / pl.col("bps")).alias("pb_hist"),
        # 反推历史 PE（假设 EPS 不变）
        pl.when(pl.col("eps_implied") > 0)
        .then(pl.col("close") / pl.col("eps_implied"))
        .otherwise(None)
        .alias("pe_hist"),
        # 60 日动量（对照用）
        (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("mom60"),
    )
    return df


def make_hits(
    df: pl.DataFrame,
    factor: str,
    ascending: bool,
    n_groups: int,
    n_per_day: int,
    seed: int,
) -> pl.DataFrame:
    """按因子分组，取最极端那一组，每天随机选 n_per_day 只。"""
    rnd = random.Random(seed)
    f = df.filter(pl.col(factor).is_not_null() & (pl.col("close") >= 3.0) & (pl.col("volume") > 0))

    # 每天算分位
    f = f.with_columns(
        pl.col(factor).rank(method="ordinal").over("date").alias("_rk"),
        pl.len().over("date").alias("_n"),
    )
    f = f.with_columns((pl.col("_rk") / pl.col("_n") * n_groups).ceil().clip(1, n_groups).alias("_grp"))

    # 取最极端组：ascending=True 表示因子越小越好 → 取第 1 组
    target = 1 if ascending else n_groups
    pool = f.filter(pl.col("_grp") == target)

    rows = []
    for (d,), g in pool.group_by("date", maintain_order=True):
        syms = g["symbol"].to_list()
        if len(syms) < n_per_day:
            continue
        for s in rnd.sample(syms, n_per_day):
            rows.append({"date": d, "symbol": s, "score": 1.0, "note": factor})
    if not rows:
        return pl.DataFrame(
            schema={"date": pl.Date, "symbol": pl.String, "score": pl.Float64, "note": pl.String}
        )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def random_hits(bars: pl.DataFrame, n: int, seed: int) -> pl.DataFrame:
    rnd = random.Random(seed)
    valid = bars.filter((pl.col("close") >= 3.0) & (pl.col("volume") > 0))
    rows = []
    for (d,), g in valid.group_by("date", maintain_order=True):
        syms = g["symbol"].to_list()
        if len(syms) < n:
            continue
        for s in rnd.sample(syms, n):
            rows.append({"date": d, "symbol": s, "score": 1.0, "note": "random"})
    if not rows:
        return pl.DataFrame(
            schema={"date": pl.Date, "symbol": pl.String, "score": pl.Float64, "note": pl.String}
        )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


FACTORS = [
    ("pb_hist", True, "低 PB（价值）"),
    ("pe_hist", True, "低 PE（价值）"),
    ("mcap_hist", True, "小市值（规模）"),
    ("mcap_hist", False, "大市值（对照）"),
    ("mom60", False, "高动量（趋势，已知A股反向）"),
    ("mom60", True, "低动量（反转）"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor", default="")
    ap.add_argument("--per-day", type=int, default=5)
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    if not FUND.exists():
        raise SystemExit(f"缺 {FUND}，先跑 scripts/fetch_fundamentals.py")

    print("构建因子表…")
    t0 = time.time()
    df = build_factor_frame()
    print(f"  {df.height:,} 行  {df['symbol'].n_unique()} 只  {time.time()-t0:.0f}s")

    cfg = BacktestConfig(
        init_cash=100_000,
        risk_pct=0.01,
        max_positions=5,
        max_position_pct=0.20,
        stop_loss_pct=args.stop,
        max_hold_days=args.hold,
    )

    targets = [(f, a, n) for f, a, n in FACTORS if not args.factor or args.factor in f]

    results = []
    for split, (d0, d1) in SPLITS.items():
        print("=" * 96)
        print(f"Split {split}  {d0} ~ {d1}")
        print("=" * 96)

        seg = df.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1 + timedelta(days=15))))
        bars = seg.select(
            "symbol", "date", "open", "high", "low", "close", "volume", "amount"
        ).with_columns(
            pl.lit(0.0).alias("change_pct"),
            pl.lit(0.0).alias("turnover"),
        )
        bar_store = BarStore(bars)

        # 随机基线
        rnd_rets = []
        for i in range(args.runs):
            h = random_hits(bars, args.per_day, seed=9000 + i)
            if h.height == 0:
                continue
            try:
                r = BacktestEngine(cfg, bar_store).run(h, d0, d1).metrics.get("total_return")
                if r is not None:
                    rnd_rets.append(float(r))
            except Exception:  # noqa: BLE001
                pass
        if not rnd_rets:
            print("  随机基线失败")
            continue
        r_mean = sum(rnd_rets) / len(rnd_rets)
        r_lo, r_hi = min(rnd_rets), max(rnd_rets)
        print(f"  随机基线：均值 {r_mean*100:+.1f}%  区间 [{r_lo*100:+.1f}%, {r_hi*100:+.1f}%]")
        print()
        print("{:<26}{:>8}{:>9}{:>9}{:>7}{:>6}".format("因子", "信号", "收益", "回撤", "交易", "判定"))
        print("-" * 72)

        for fname, asc, label in targets:
            h = make_hits(seg, fname, asc, 5, args.per_day, seed=111)
            if h.height < 50:
                print(f"  {label:<26} 信号仅 {h.height}")
                continue
            try:
                res = BacktestEngine(cfg, bar_store).run(h, d0, d1)
                m = res.metrics
                ret = m.get("total_return")
                if ret is None:
                    print(f"  {label:<26} 无成交")
                    continue
                v = "★" if ret > r_hi else ("↓↓" if ret < r_lo else "×")
                print(
                    "{:<26}{:>8}{:>+8.1f}%{:>8.1f}%{:>7}{:>6}".format(
                        label, h.height, ret * 100, (m.get("max_drawdown") or 0) * 100,
                        m.get("trades") or 0, v,
                    )
                )
                results.append({
                    "split": split, "factor": fname, "label": label,
                    "signals": int(h.height), "ret": round(ret, 4),
                    "mdd": round(m.get("max_drawdown") or 0, 4),
                    "trades": m.get("trades"), "win": m.get("win_rate"),
                    "random_mean": round(r_mean, 4), "random_lo": round(r_lo, 4),
                    "random_hi": round(r_hi, 4), "verdict": v,
                })
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:<26} 失败 {type(exc).__name__}")
        print()

    out = ROOT / "reports" / "value_factor_test.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # 汇总
    from collections import defaultdict
    by = defaultdict(list)
    for r in results:
        by[r["label"]].append(r)
    print("=" * 96)
    print("汇总")
    print("=" * 96)
    print("{:<26}{:<9}{:<9}{:<9}{:>10}{:>10}".format("因子", "训练", "验证", "留出", "平均收益", "总评"))
    print("-" * 96)
    for label, rs in by.items():
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
        print("{:<26}{:<9}{:<9}{:<9}{:>+9.1f}%{:>10}".format(
            label, vs.get("train", "—"), vs.get("valid", "—"), vs.get("holdout", "—"), avg * 100, total))
    print()
    print(f"→ {out}")


if __name__ == "__main__":
    main()
