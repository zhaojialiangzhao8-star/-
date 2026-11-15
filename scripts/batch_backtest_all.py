"""一键回测所有策略 —— 独立脚本，不经过 Web 服务。

为什么不走服务里的 worker：
  全市场 4800 只 × 400 万行信号窗口 + BarStore，峰值要 3~4 GB。
  而 Web 服务自己常驻 2~3 GB（全表缓存），两者同跑必然 OOM。

所以批量回测交给独立进程：
  · 先停 Web 服务（或至少不碰数据）
  · 脚本跑完把结果写进 SQLite runs 表
  · 前端只读 runs 表出排名，不触发计算

跑法：
    python scripts/batch_backtest_all.py                       # 12 个已验证策略
    python scripts/batch_backtest_all.py --scope all           # 全部 58 个
    python scripts/batch_backtest_all.py --start 2023-01-01
"""
from __future__ import annotations

import argparse
import gc
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig, FeeConfig  # noqa: E402
from app.engine import StrategyEngine  # noqa: E402
from app.infrastructure.storage.db import get_db  # noqa: E402
from app.modules.screener.batch import _parse_ranking  # noqa: E402
from app.store import GenerationStore  # noqa: E402

BAR_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "change_pct"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="ranked", choices=["ranked", "all"])
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--risk-amount", type=float, default=3000)
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--window-days", type=int, default=420, help="信号回看天数")
    args = ap.parse_args()

    t0 = time.time()
    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else None
    print(f"代际 {gen.generation[:8]}  {gen.rows:,} 行 · 回测 {start} ~ {end or '最新'}", flush=True)

    engine = StrategyEngine(config.STRATEGY_DIR)
    engine.reload()
    print(f"策略 {len(engine.registry)} 个", flush=True)

    ids = sorted(engine.registry)
    if args.scope == "ranked":
        ranked = _parse_ranking()
        ids = [s for s in ids if s in ranked] or ids[:12]
    print(f"本次回测 {len(ids)} 个策略", flush=True)

    # BarStore：回测区间 + 400 天缓冲（覆盖最长持仓与均线窗口）
    print("构建 BarStore …", flush=True)
    lf = store.scan()
    names = lf.collect_schema().names()
    bars = BarStore(
        lf.filter(pl.col("date") >= start - timedelta(days=400))
        .select([c for c in BAR_COLS if c in names])
        .collect()
    )
    print(f"  {len(bars.calendar)} 个交易日", flush=True)

    # 信号窗口：只加载 start 前 window_days 天至今，不碰全表
    print(f"加载信号窗口（{args.window_days} 天回看）…", flush=True)
    frame = (
        store.scan()
        .filter(pl.col("date") >= start - timedelta(days=args.window_days))
        .collect()
    )
    print(f"  {frame.height:,} 行 · {frame['symbol'].n_unique():,} 只", flush=True)

    cfg = BacktestConfig(
        init_cash=100_000, risk_amount=args.risk_amount, max_positions=args.max_positions,
        max_position_pct=0.25, stop_loss_pct=0.08, max_hold_days=20, fees=FeeConfig(),
    )
    db = get_db()

    rows: list[dict] = []
    head = f"{'策略':34}{'信号':>9}{'收益':>10}{'年化':>8}{'回撤':>9}{'夏普':>7}{'胜率':>7}{'交易':>7}{'耗时':>7}"
    print("\n" + head)
    print("-" * len(head), flush=True)

    for sid in ids:
        t1 = time.time()
        hits = None
        try:
            hits = engine.run(sid, {}, frame)
            if hits.height == 0:
                print(f"{sid:34}{'0':>9}  （窗口内无信号）", flush=True)
                rows.append({"strategy": sid, "error": "无信号"})
                continue
            res = BacktestEngine(cfg, bars).run(hits, start=start, end=end)
            m = res.metrics
            db.save_run(
                strategy=sid,
                params={"start": str(start), "end": str(end), "risk_amount": cfg.risk_amount,
                        "window_days": args.window_days},
                generation=gen.generation,
                engine_version="v2.0-script",
                metrics=m,
                equity=float(m.get("final_equity") or 0),
                trades=int(m.get("trades") or 0),
            )
            rows.append({
                "strategy": sid,
                "signals": int(hits.height),
                "return": m.get("total_return"),
                "annual": m.get("annual_return"),
                "mdd": m.get("max_drawdown"),
                "sharpe": m.get("sharpe"),
                "win_rate": m.get("win_rate"),
                "trades": m.get("trades"),
            })
            print(
                f"{sid:34}{hits.height:>9,}"
                f"{(m.get('total_return') or 0)*100:>9.1f}%"
                f"{(m.get('annual_return') or 0)*100:>7.1f}%"
                f"{(m.get('max_drawdown') or 0)*100:>8.1f}%"
                f"{(m.get('sharpe') or 0):>7.2f}"
                f"{(m.get('win_rate') or 0)*100:>6.1f}%"
                f"{m.get('trades') or 0:>7}"
                f"{time.time()-t1:>6.1f}s",
                flush=True,
            )
            del res
        except Exception as exc:  # noqa: BLE001
            print(f"{sid:34}  失败 {type(exc).__name__}: {str(exc)[:44]}", flush=True)
            rows.append({"strategy": sid, "error": str(exc)[:120]})
        finally:
            del hits
            gc.collect()

    ok = [r for r in rows if r.get("sharpe") is not None]
    ok.sort(key=lambda x: -(x["sharpe"] or -9))
    print(f"\n完成 {len(ok)}/{len(ids)} 个 · 总耗时 {time.time()-t0:.0f}s")
    print("\n排名（按夏普）：")
    for i, r in enumerate(ok, 1):
        print(f"  #{i:<2} {r['strategy']:<34} 收益 {r['return']*100:+7.1f}%  夏普 {r['sharpe']:>5.2f}  胜率 {r['win_rate']*100:>4.0f}%")

    # 结果写一份 markdown，前端/报告都能读
    out = Path(__file__).resolve().parents[1] / "reports" / "account_compare_fullmarket.md"
    lines = [
        "# 账户级回测对比 · 全市场版",
        "",
        f"> 代际 `{gen.generation[:8]}` · {frame.height:,} 行 · 回测 {start} ~ {end or '最新'}",
        f"> 10 万本金 · 单笔止损 {cfg.risk_amount:.0f} 元 · 最多 {cfg.max_positions} 仓 · 止损 8% · 持仓 20 天",
        f"> 生成于 {time.strftime('%Y-%m-%d %H:%M')} · scripts/batch_backtest_all.py",
        "",
        "| # | 策略 | 信号数 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 交易 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ok, 1):
        lines.append(
            f"| {i} | `{r['strategy']}` | {r['signals']:,} | {r['return']*100:.1f}% | "
            f"{r['annual']*100:.1f}% | {r['mdd']*100:.1f}% | {r['sharpe']:.2f} | {r['win_rate']*100:.1f}% | {r['trades']} |"
        )
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告 → {out}")


if __name__ == "__main__":
    main()
