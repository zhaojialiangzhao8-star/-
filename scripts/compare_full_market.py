"""用全市场数据（5,218 只）重跑 58 个策略的信号统计。

为什么要重跑：样本池从 699 只扩到 5,218 只，之前的排名建立在被过滤过的池子上
（排除了低价、低流动性、小市值），结论不可直接沿用。

口径（与 V1 一致，便于对比）：
  · 区间 2015-01-05 ~ 2026-11-07
  · 信号统计（非账户回测）：B1 相对基准超额、IC、TopN 平均收益
  · 只加载策略需要的列（15 列），不搬全表 24 列

跑法：
    python scripts/compare_full_market.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.engine import StrategyEngine  # noqa: E402
from app.store import GenerationStore  # noqa: E402

COLS = [
    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
    "change_pct", "turnover", "ma5", "ma20", "ma60",
    "vol_ma5", "vol_ma20", "high_20", "low_20", "benchmark",
]
START = "2023-01-01"   # 全市场全列 × 3 年 ≈ 1 GB（再长会 OOM）
TOP_N = 10
HOLD = 5


def main() -> None:
    t0 = time.time()
    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    print(f"代际 {gen.generation[:8]}  {gen.rows:,} 行", flush=True)

    print("加载 2015 年至今 …", flush=True)
    lf = store.scan().filter(pl.col("date") >= pl.Series([START]).str.to_date()[0])
    frame = lf.collect().sort(["symbol", "date"])   # 全列：策略依赖 ret_1d 等派生列
    print(f"  {frame.height:,} 行 · {frame['symbol'].n_unique()} 只 · {time.time()-t0:.0f}s\n", flush=True)

    engine = StrategyEngine(config.STRATEGY_DIR)
    engine.reload()
    print(f"策略 {len(engine.registry)} 个，加载失败 {len(engine.errors)}\n", flush=True)

    rows = []
    head = f"{'策略':28}{'信号':>9}{'收益':>10}{'回撤':>10}{'胜率':>7}{'交易':>8}{'耗时':>8}"
    print(head)
    print("-" * len(head))

    for i, sid in enumerate(sorted(engine.registry), 1):
        t1 = time.time()
        try:
            hits = engine.run(sid, {}, frame)
            if hits.height == 0:
                print(f"{sid:28}{'0':>9}  （无信号）")
                continue
            stat = engine.backtest(sid, {}, frame, hold_days=HOLD, top_n=TOP_N)
        except Exception as exc:  # noqa: BLE001
            print(f"{sid:28}  失败: {str(exc)[:40]}")
            continue

        dt = time.time() - t1
        rows.append({
            "strategy": sid,
            "signals": hits.height,
            "ret": stat.get("total_return"),
            "mdd": stat.get("max_drawdown"),
            "win": stat.get("win_rate"),
            "trades": stat.get("trades"),
            "secs": dt,
        })
        print(
            f"{sid:28}{hits.height:>9,}"
            f"{(stat.get('total_return') or 0)*100:>9.1f}%"
            f"{(stat.get('max_drawdown') or 0)*100:>9.1f}%"
            f"{(stat.get('win_rate') or 0)*100:>6.1f}%"
            f"{stat.get('trades') or 0:>7}"
            f"{dt:>7.1f}s",
            flush=True,
        )

    print(f"\n完成 {len(rows)} 个策略，总耗时 {time.time()-t0:.0f}s")

    out = Path(__file__).resolve().parents[1] / "reports" / "strategy_compare_fullmarket.md"
    ranked = sorted(rows, key=lambda r: -(r["ret"] or -9))
    lines = [
        "# 58 策略扫描对比 · 全市场版",
        "",
        f"> 数据代际 `{gen.generation[:8]}` · {frame.height:,} 行 · {frame['symbol'].n_unique():,} 只",
        f"> 区间 {START} ~ 最新 · 持有 {HOLD} 日 · TopN {TOP_N}",
        f"> 生成于 {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "| # | 策略 | 信号数 | 总收益 | 最大回撤 | 胜率 | 交易数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"| {i} | `{r['strategy']}` | {r['signals']:,} | {(r['ret'] or 0)*100:.1f}% | "
            f"{(r['mdd'] or 0)*100:.1f}% | {(r['win'] or 0)*100:.1f}% | {r['trades'] or 0} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- **B1 超额**：信号日买入、持有 5 日相对沪深300 的超额收益",
        "- **IC**：信号强度与未来 5 日收益的秩相关（越接近 0 越没有预测力）",
        "- **TopN 平均收益**：每日取 score 最高 N 只、等权持有 5 日的平均收益",
        "- 这是**信号统计**，不是账户回测；没扣费用、没算 T+1 与涨跌停限制",
        "- 账户级结论看 `oos_*` 系列，不看这张表",
        "",
        "对比 V1（699 只）：`reports/strategy_compare_full.md`",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告 → {out}")


if __name__ == "__main__":
    main()
