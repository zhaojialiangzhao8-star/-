"""风险预算版多策略并联 — 每个策略有自己的资金份额和风控参数。

解决的病：上一版把三策略简单合并，信号量最大的 52 周新高（28,280 个信号）
抢光仓位，把组合拖成 −78% 回撤。

现在每个策略：
  · 有自己的资金份额（strategy_budget）
  · 有自己的止损/持仓期（strategy_params）
  · 超出份额的买单被拒（rejected['budget']）
"""
from __future__ import annotations

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

START, END = date(2015, 1, 5), date(2026, 11, 7)


def rev(f, th=-0.18):
    d = f.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("_r3"),
        (pl.col("volume") / pl.col("volume").shift(1).over("symbol")).alias("_vr"),
    )
    h = d.filter(
        (pl.col("_r3") < th) & (pl.col("close") > pl.col("open"))
        & (pl.col("_vr") > 1.2) & pl.col("_r3").is_not_null()
    )
    return h.select("symbol", "date", (-pl.col("_r3") * 100).alias("score"))


def tag(df: pl.DataFrame, name: str) -> pl.DataFrame:
    return df.select("symbol", "date", "score").with_columns(pl.lit(name).alias("source"))


# 每个策略自己最合适的参数（来自之前的网格与单策略测试）
PROFILE = {
    "急跌反弹": {"stop_loss_pct": 0.12, "max_hold_days": 5},
    "合成IC6": {"stop_loss_pct": 0.08, "max_hold_days": 20},
    "52周新高": {"stop_loss_pct": 0.08, "max_hold_days": 20},
}


def main() -> None:
    raw = GenerationStore(config.DATA_DIR).collect()
    frame = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )
    bars = BarStore(frame)

    h_rev = tag(rev(frame, -0.18), "急跌反弹")
    h_ic6 = tag(M.filter_frame(frame, {"min_votes": 2, "min_score": 0.0}), "合成IC6")
    h_52w = tag(C.run(frame, "classic_week52_high"), "52周新高")

    BASE = dict(init_cash=100_000, risk_pct=0.03, max_positions=10, max_position_pct=0.25,
                stop_loss_pct=0.10, max_hold_days=10, fees=FeeConfig())

    def run(hits, budgets=None, params=None, label=""):
        cfg = BacktestConfig(**BASE, strategy_budget=budgets or {}, strategy_params=params or {})
        r = BacktestEngine(cfg, bars).run(hits, start=START, end=END)
        util = [s.market_value / s.equity for s in r.snapshots if s.equity > 0]
        return r, sum(util) / len(util) * 100, sum(1 for u in util if u < 0.01) / len(util) * 100

    head = f"{'方案':30}{'信号':>7}{'收益':>10}{'年化':>8}{'回撤':>9}{'夏普':>7}{'卡玛':>7}{'日均仓位':>9}{'空仓%':>7}"
    print(head)
    print("-" * len(head))

    cases = [
        ("① 单跑 急跌反弹", h_rev, None, {"急跌反弹": PROFILE["急跌反弹"]}),
        ("② 单跑 合成IC6", h_ic6, None, {"合成IC6": PROFILE["合成IC6"]}),
        ("③ 简单并联（无预算）", pl.concat([h_rev, h_ic6]), None, PROFILE),
        ("④ 并联+预算 40/60", pl.concat([h_rev, h_ic6]), {"急跌反弹": 0.4, "合成IC6": 0.6}, PROFILE),
        ("⑤ 并联+预算 30/70", pl.concat([h_rev, h_ic6]), {"急跌反弹": 0.3, "合成IC6": 0.7}, PROFILE),
        ("⑥ 三策略+预算 25/50/25", pl.concat([h_rev, h_ic6, h_52w]),
         {"急跌反弹": 0.25, "合成IC6": 0.5, "52周新高": 0.25}, PROFILE),
    ]

    rows = []
    for label, hits, budgets, params in cases:
        r, util, zero = run(hits, budgets, params, label)
        m = r.metrics
        rows.append((label, m, util, zero, r))
        print(f"{label:30}{hits.height:>7}{m['total_return']*100:>9.1f}%{m['annual_return']*100:>7.1f}%"
              f"{m['max_drawdown']*100:>8.1f}%{(m['sharpe'] or 0):>7.2f}{(m['calmar'] or 0):>7.2f}"
              f"{util:>8.1f}%{zero:>6.0f}%", flush=True)

    print("\n预算效果（④ vs ③）：")
    for label, m, util, zero, r in rows:
        if label.startswith("④"):
            print(f"  {label}: 预算拒单 {r.rejected.get('budget', 0)} 次")
    print("\n三策略预算版的按策略归因：")
    for label, m, util, zero, r in rows:
        if label.startswith("⑥"):
            by: dict[str, int] = {}
            for f in r.fills:
                if f.side.value == "buy":
                    by[f.source] = by.get(f.source, 0) + 1
            print("  " + " · ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda x: -x[1])))
            print(f"  拒单分布: {r.rejected}")


if __name__ == "__main__":
    main()
