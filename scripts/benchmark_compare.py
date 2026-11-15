"""策略 vs 基准对比 — 回答"这收益是选股赚的还是市场给的"。

指标：
  · 基准收益 / 超额收益（绝对）
  · Beta（对基准的敞口）、年化 Alpha
  · 信息比率（超额收益均值 / 超额收益标准差 × √252）
  · 相关系数、上涨捕获 / 下跌捕获

对齐方式：按策略快照的交易日序列左连接基准收盘价，缺失用前值填充（停牌/休市）。
"""
from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "app" / "strategies"))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig, FeeConfig  # noqa: E402
import _classic_lib as C  # noqa: E402
import composite_ic6 as M  # noqa: E402

BENCH = "000300"  # 沪深300
START, END = date(2015, 1, 5), date(2026, 11, 7)


def load_bench(code: str) -> pl.DataFrame:
    p = config.DATA_DIR / "benchmark" / f"{code}.parquet"
    if not p.exists():
        raise SystemExit(f"缺基准数据 {p}，先跑 scripts/fetch_benchmark.py")
    return pl.read_parquet(p).select("date", pl.col("close").alias("bench_close")).sort("date")


def align(snapshots, bench: pl.DataFrame) -> tuple[list[float], list[float]]:
    """返回 (策略净值序列, 基准净值序列)，按同一批交易日对齐。"""
    dates = pl.DataFrame({"date": [s.date for s in snapshots]})
    nav = pl.DataFrame({"date": [s.date for s in snapshots], "nav": [s.nav for s in snapshots]})
    merged = (
        dates.join(bench, on="date", how="left")
        .join(nav, on="date", how="left")
        .with_columns(pl.col("bench_close").forward_fill())
        .drop_nulls("bench_close")
    )
    b0 = merged["bench_close"][0]
    return (
        merged["nav"].to_list(),
        [v / b0 for v in merged["bench_close"].to_list()],
    )


def stats(nav: list[float], bnav: list[float]) -> dict:
    n = min(len(nav), len(bnav))
    nav, bnav = nav[-n:], bnav[-n:]
    r = [nav[i] / nav[i - 1] - 1 for i in range(1, n)]
    b = [bnav[i] / bnav[i - 1] - 1 for i in range(1, n)]
    if not r:
        return {}

    def mean(x):
        return sum(x) / len(x)

    mr, mb = mean(r), mean(b)
    var_b = sum((x - mb) ** 2 for x in b) / max(len(b) - 1, 1)
    cov = sum((r[i] - mr) * (b[i] - mb) for i in range(len(r))) / max(len(r) - 1, 1)
    beta = cov / var_b if var_b > 0 else 0.0

    total = nav[-1] - 1
    btotal = bnav[-1] - 1
    days = n - 1
    years = days / 252 if days else 1
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else 0
    bann = (1 + btotal) ** (1 / years) - 1 if years > 0 else 0

    excess = [r[i] - b[i] for i in range(len(r))]
    me, se = mean(excess), (sum((x - mean(excess)) ** 2 for x in excess) / max(len(excess) - 1, 1)) ** 0.5
    ir = me / se * math.sqrt(252) if se > 0 else None

    sr = (sum((x - mr) ** 2 for x in r) / max(len(r) - 1, 1)) ** 0.5
    corr = cov / (sr * var_b ** 0.5) if sr * var_b > 0 else None

    up = [r[i] for i in range(len(r)) if b[i] > 0]
    upb = [b[i] for i in range(len(r)) if b[i] > 0]
    dn = [r[i] for i in range(len(r)) if b[i] < 0]
    dnb = [b[i] for i in range(len(r)) if b[i] < 0]

    return {
        "total": total, "bench_total": btotal, "excess": total - btotal,
        "annual": ann, "bench_annual": bann,
        "alpha": ann - beta * bann,
        "beta": beta, "ir": ir, "corr": corr,
        "up_capture": mean(up) / mean(upb) if upb and mean(upb) != 0 else None,
        "down_capture": mean(dn) / mean(dnb) if dnb and mean(dnb) != 0 else None,
    }


CASES = [
    ("急跌反弹 -18/12/5", lambda f: C.run(f, "classic_reversal"), dict(stop_loss_pct=0.12, max_hold_days=5)),
    ("急跌反弹 -12/8/20", lambda f: C.run(f, "classic_reversal"), dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("合成 IC6", lambda f: M.filter_frame(f, {"min_votes": 2, "min_score": 0.0}), dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("52周新高", lambda f: C.run(f, "classic_week52_high"), dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("RSI超卖", lambda f: C.run(f, "classic_rsi_oversold"), dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("均值回归", lambda f: C.run(f, "classic_mean_revert"), dict(stop_loss_pct=0.08, max_hold_days=20)),
]


def main() -> None:
    raw = GenerationStore(config.DATA_DIR).collect()
    frame = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )
    bars = BarStore(frame)
    bench = load_bench(BENCH)
    base = dict(init_cash=100_000, risk_pct=0.01, max_positions=5, max_position_pct=0.2,
                fees=FeeConfig())

    print(f"基准：沪深300 · 区间 {START} ~ {END}\n")
    header = f"{'策略':20}{'收益':>9}{'基准':>9}{'超额':>9}{'年化':>8}{'Alpha':>8}{'Beta':>7}{'IR':>7}{'上涨捕获':>9}{'下跌捕获':>9}"
    print(header)
    print("-" * len(header))

    rows = []
    for name, fnh, ov in CASES:
        hits = fnh(frame)
        cfg = BacktestConfig(**{**base, **ov})
        res = BacktestEngine(cfg, bars).run(hits, start=START, end=END)
        nav, bnav = align(res.snapshots, bench)
        s = stats(nav, bnav)
        rows.append((name, s))
        p = lambda v, d=1: "—" if v is None else f"{v*100:+.{d}f}%"
        print(f"{name:20}{p(s['total']):>9}{p(s['bench_total']):>9}{p(s['excess']):>9}"
              f"{p(s['annual'],1):>8}{p(s['alpha'],1):>8}{(s['beta'] or 0):>7.2f}"
              f"{(s['ir'] or 0):>7.2f}{(s['up_capture'] or 0):>9.2f}{(s['down_capture'] or 0):>9.2f}")

    print("-" * len(header))
    print("\n判读要点：")
    print("  · 超额 > 0 且 IR > 0.5   → 有真实 alpha")
    print("  · 上涨捕获 > 1 且 下跌捕获 < 1 → 理想形态（涨得多跌得少）")
    print("  · Beta ≈ 1 说明就是在赌大盘，选股贡献有限")


if __name__ == "__main__":
    main()
