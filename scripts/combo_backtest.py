"""阶段 6 · 双策略/多策略并联账户回测。

核心问题：单策略仓位用不满（急跌反弹 85% 空仓、52周新高 6% 空仓），
把它们的信号合并进**同一个账户**，能不能既填满仓位、又不放大回撤？

一个账户只有一份现金，同一天信号超过仓位上限时按确定性规则拒单：
    score 降序 → symbol 字典序（tiebreaker）

每笔持仓带 source 标签，事后可按策略归因。
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
    return h.select("symbol", "date", "close", (-pl.col("_r3") * 100).alias("score"))


def tagged(df: pl.DataFrame, tag: str) -> pl.DataFrame:
    """只保留引擎需要的列，避免各策略列宽不一致（有的带 note、有的不带）。"""
    return df.select("symbol", "date", "score").with_columns(pl.lit(tag).alias("source"))


def main() -> None:
    raw = GenerationStore(config.DATA_DIR).collect()
    frame = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )
    bars = BarStore(frame)

    # 三个策略的信号，各自带 source 标签
    h_rev = tagged(rev(frame, -0.18), "急跌反弹")
    h_52w = tagged(C.run(frame, "classic_week52_high"), "52周新高")
    h_ic6 = tagged(M.filter_frame(frame, {"min_votes": 2, "min_score": 0.0}), "合成IC6")

    base = dict(init_cash=100_000, risk_pct=0.03, max_positions=10, max_position_pct=0.25,
                stop_loss_pct=0.12, max_hold_days=5, fees=FeeConfig())

    SINGLES = [
        ("急跌反弹 −18/12/5", h_rev),
        ("52周新高", h_52w),
        ("合成 IC6", h_ic6),
    ]
    COMBOS = [
        ("急跌反弹 + 52周新高", pl.concat([h_rev, h_52w])),
        ("急跌反弹 + 合成IC6", pl.concat([h_rev, h_ic6])),
        ("三策略并联", pl.concat([h_rev, h_52w, h_ic6])),
    ]

    def run(hits, cfg_over=None):
        cfg = BacktestConfig(**{**base, **(cfg_over or {})})
        r = BacktestEngine(cfg, bars).run(hits, start=START, end=END)
        util = [s.market_value / s.equity for s in r.snapshots if s.equity > 0]
        zero = sum(1 for u in util if u < 0.01) / len(util) * 100
        by_src: dict[str, int] = {}
        for f in r.fills:
            if f.side.value == "buy":
                by_src[f.source] = by_src.get(f.source, 0) + 1
        return r, sum(util) / len(util) * 100, zero, by_src

    head = f"{'组合':26}{'信号':>7}{'收益':>10}{'年化':>8}{'回撤':>9}{'夏普':>7}{'卡玛':>7}{'日均仓位':>9}{'空仓%':>7}"
    print(head)
    print("-" * len(head))

    results = []
    for name, hits in SINGLES + COMBOS:
        r, util, zero, by = run(hits)
        m = r.metrics
        results.append((name, m, util, zero, by))
        print(f"{name:26}{hits.height:>7}{m['total_return']*100:>9.1f}%{m['annual_return']*100:>7.1f}%"
              f"{m['max_drawdown']*100:>8.1f}%{(m['sharpe'] or 0):>7.2f}{(m['calmar'] or 0):>7.2f}"
              f"{util:>8.1f}%{zero:>6.0f}%", flush=True)

    print("\n组合的按策略归因（买入次数）：")
    for name, m, util, zero, by in results:
        if len(by) > 1:
            detail = " · ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
            print(f"  {name:24} {detail}")

    # 风险预算调整：组合用更低的 risk_pct 会怎样
    print("\n组合在不同 risk_pct 下的表现：")
    print(f"{'组合':26}{'risk':>6}{'收益':>10}{'回撤':>9}{'夏普':>7}{'日均仓位':>9}")
    print("-" * 70)
    for rp in (0.01, 0.02, 0.03):
        r, util, zero, _ = run(pl.concat([h_rev, h_52w, h_ic6]), {"risk_pct": rp})
        m = r.metrics
        print(f"{'三策略并联':26}{rp*100:>5.0f}%{m['total_return']*100:>9.1f}%"
              f"{m['max_drawdown']*100:>8.1f}%{(m['sharpe'] or 0):>7.2f}{util:>8.1f}%", flush=True)


if __name__ == "__main__":
    main()
