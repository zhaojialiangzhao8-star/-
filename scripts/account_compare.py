"""账户级回测对比 —— 信号统计口径失效后的正确做法。

为什么需要这个：
  `compare_full_market.py` 用的是 V1 的「每日 TopN 等权持有」信号统计。
  样本池从 699 只扩到 5,215 只后，这个口径彻底失效：
  breakout_20d 报出 +2,531,815%、classic_donchian 报出 +1,030,368%。
  原因是它不约束资金、不看涨跌停、不考虑同一天信号数远超仓位上限，
  纯复利累积出来的数字没有任何现实意义。

  唯一可信的口径是 `BacktestEngine`：一份现金、T+1、次日开盘成交、
  涨跌停/停牌拒绝、100 股整数倍、完整费用。

跑法：
    python scripts/account_compare.py                       # 默认 12 个关键策略
    python scripts/account_compare.py --strategies a,b,c    # 指定
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig, FeeConfig  # noqa: E402
from app.engine import StrategyEngine  # noqa: E402
from app.store import GenerationStore  # noqa: E402

DEFAULT = [
    "classic_reversal", "classic_week52_high", "classic_rsi_oversold",
    "classic_mean_revert", "classic_breakout_retest", "classic_boll_squeeze",
    "classic_donchian", "classic_kdj_cross", "composite_ic6", "terminal_picks",
    "instock_high_tight_flag", "instock_low_backtrace_increase",
]
BAR_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "change_pct"]
START, END = date(2023, 1, 3), date(2026, 11, 7)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="")
    ap.add_argument("--start", default=str(START))
    ap.add_argument("--end", default=str(END))
    args = ap.parse_args()

    sids = [s.strip() for s in args.strategies.split(",") if s.strip()] or DEFAULT
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    t0 = time.time()
    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    print(f"代际 {gen.generation[:8]}  {gen.rows:,} 行  {start} ~ {end}", flush=True)

    # 信号用全表算；BarStore 只加载回测区间
    print("加载策略信号 …", flush=True)
    frame = store.scan().collect()
    print(f"  {frame.height:,} 行  {time.time()-t0:.0f}s", flush=True)

    print("构建 BarStore（区间裁剪）…", flush=True)
    lf = store.scan()
    names = lf.collect_schema().names()
    bars = BarStore(
        lf.filter(pl.col("date") >= start).select([c for c in BAR_COLS if c in names]).collect()
    )
    print(f"  {len(bars.calendar)} 个交易日  {time.time()-t0:.0f}s\n", flush=True)

    engine = StrategyEngine(config.STRATEGY_DIR)
    engine.reload()

    cfg = BacktestConfig(
        init_cash=100_000, risk_amount=3000, max_positions=10, max_position_pct=0.25,
        stop_loss_pct=0.08, max_hold_days=20, fees=FeeConfig(),
    )

    rows = []
    head = f"{'策略':30}{'信号':>9}{'收益':>10}{'年化':>8}{'回撤':>9}{'夏普':>7}{'胜率':>7}{'交易':>7}{'耗时':>7}"
    print(head)
    print("-" * len(head))

    for sid in sids:
        t1 = time.time()
        try:
            hits = engine.run(sid, {}, frame)
            if hits.height == 0:
                print(f"{sid:30}{'0':>9}  （无信号）")
                continue
            r = BacktestEngine(cfg, bars).run(hits, start=start, end=end)
        except Exception as exc:  # noqa: BLE001
            print(f"{sid:30}  失败 {str(exc)[:44]}")
            continue
        m = r.metrics
        dt = time.time() - t1
        rows.append({"sid": sid, "signals": hits.height, "m": m, "secs": dt})
        print(
            f"{sid:30}{hits.height:>9,}"
            f"{(m.get('total_return') or 0)*100:>9.1f}%"
            f"{(m.get('annual_return') or 0)*100:>7.1f}%"
            f"{(m.get('max_drawdown') or 0)*100:>8.1f}%"
            f"{m.get('sharpe') or 0:>7.2f}"
            f"{(m.get('win_rate') or 0)*100:>6.1f}%"
            f"{m.get('trades') or 0:>7}"
            f"{dt:>6.1f}s",
            flush=True,
        )

    print(f"\n完成 {len(rows)} 个，总耗时 {time.time()-t0:.0f}s")

    ranked = sorted(rows, key=lambda r: -(r["m"].get("sharpe") or -9))
    out = Path(__file__).resolve().parents[1] / "reports" / "account_compare_fullmarket.md"
    lines = [
        "# 账户级回测对比 · 全市场版",
        "",
        f"> 代际 `{gen.generation[:8]}` · {frame.height:,} 行 · {frame['symbol'].n_unique():,} 只",
        f"> 区间 {start} ~ {end} · 10 万本金 · 单笔止损 3000 元 · 最多 10 仓 · 单票 25% · 止损 8% · 持仓 20 天",
        f"> 生成于 {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "| # | 策略 | 信号数 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 交易 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ranked, 1):
        m = r["m"]
        lines.append(
            f"| {i} | `{r['sid']}` | {r['signals']:,} | {(m.get('total_return') or 0)*100:.1f}% | "
            f"{(m.get('annual_return') or 0)*100:.1f}% | {(m.get('max_drawdown') or 0)*100:.1f}% | "
            f"{m.get('sharpe') or 0:.2f} | {(m.get('win_rate') or 0)*100:.1f}% | {m.get('trades') or 0} |"
        )
    lines += [
        "",
        "## 口径说明",
        "",
        "- **这是账户级回测**：一份 10 万现金、T+1、次日开盘成交、涨跌停/停牌拒绝、100 股整数倍、佣金印花税过户费全含",
        "- 信号统计口径在同一份数据上会给出 +2,531,815% 这种数字（`compare_full_market.py`），**不可用**",
        "- 单笔止损 3000 元 → 股数按 (买入价 − 止损价) 反推，受单票 25% 与现金约束",
        "",
        "## 信号统计为什么失效",
        "",
        "V1 的 `engine.backtest()` 每日取 score 最高的 10 只等权持有 5 日，纯复利累积：",
        "",
        "| 口径缺陷 | 后果 |",
        "|---|---|",
        "| 不约束资金 | 同一天 500 个信号也能「全买」，现实里只有 10 万现金 |",
        "| 不看涨跌停 | 一字板买入在实盘中成交不了 |",
        "| 不看 T+1 | 当日买当日卖被算成可行 |",
        "| 不考虑仓位上限 | 10 只票各占 100% 资金，重复计 |",
        "| 样本池扩 7 倍后 | 每天可选标的从 700 → 5,215，命中率暴涨，数字失控 |",
        "",
        "**结论：策略评价只看账户级回测，不看信号统计。**",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告 → {out}")


if __name__ == "__main__":
    main()
