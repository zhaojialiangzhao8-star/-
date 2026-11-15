"""分年度胜率稳定性 —— 判断一个策略的胜率是真本事还是幸存者偏差。

为什么需要：reports/account_compare_fullmarket.md 里那些胜率都是「2024-01 至今」
一个区间跑出来的。一个策略在 3 年里胜率 41.9%，可能真实情况是
「2024 年 55%、2025 年 48%、2026 年 22%」—— 那就是在衰减，明年可能归零。

做法：把回测区间切成逐年，每年单独跑一次，看胜率的分布。

跑法：
    python scripts/yearly_winrate.py                     # 全部启用策略
    python scripts/yearly_winrate.py --strategies classic_mean_revert,pattern_reinforcement
    python scripts/yearly_winrate.py --years 2022,2023,2024,2025,2026
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
from app.store import GenerationStore  # noqa: E402

BAR_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "change_pct"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="", help="逗号分隔，留空=全部启用策略")
    ap.add_argument("--years", default="2023,2024,2025,2026")
    ap.add_argument("--risk-amount", type=float, default=3000)
    ap.add_argument("--out", default="reports/yearly_winrate.md")
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",") if y.strip()]
    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    engine = StrategyEngine(config.STRATEGY_DIR)
    engine.reload()

    ids = [s.strip() for s in args.strategies.split(",") if s.strip()] or sorted(engine.registry)
    print(f"代际 {gen.generation[:8]} · {len(ids)} 个策略 · {len(years)} 个年份\n", flush=True)

    cfg = BacktestConfig(
        init_cash=100_000, risk_amount=args.risk_amount, max_positions=10,
        max_position_pct=0.25, stop_loss_pct=0.08, max_hold_days=20, fees=FeeConfig(),
    )

    results: dict[str, dict[int, dict]] = {}

    for yi, year in enumerate(years):
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        # 每年重新建 BarStore（区间小，内存友好），预跑 100 天让均线有值
        lf = store.scan().filter(
            (pl.col("date") >= start - timedelta(days=120)) & (pl.col("date") <= end)
        )
        names = lf.collect_schema().names()
        bars = BarStore(lf.select([c for c in BAR_COLS if c in names]).collect())
        frame = store.scan().filter(
            (pl.col("date") >= start - timedelta(days=100)) & (pl.col("date") <= end)
        ).collect()
        print(f"[{year}] {len(bars.calendar)} 个交易日 · 信号窗口 {frame.height:,} 行", flush=True)

        for sid in ids:
            hits = None
            try:
                hits = engine.run(sid, {}, frame)
                if hits.height == 0:
                    results.setdefault(sid, {})[year] = {"trades": 0, "win_rate": None, "return": None}
                    continue
                res = BacktestEngine(cfg, bars).run(hits, start=start, end=end)
                m = res.metrics
                results.setdefault(sid, {})[year] = {
                    "signals": int(hits.height),
                    "trades": int(m.get("trades") or 0),
                    "win_rate": m.get("win_rate"),
                    "return": m.get("total_return"),
                    "sharpe": m.get("sharpe"),
                    "mdd": m.get("max_drawdown"),
                }
                del res
            except Exception as exc:  # noqa: BLE001
                results.setdefault(sid, {})[year] = {"error": f"{type(exc).__name__}: {str(exc)[:60]}"}
            finally:
                del hits
                gc.collect()
        del bars, frame
        gc.collect()
        print(flush=True)

    # ---- 输出 ----
    multi = [s for s in ids if len([y for y in years if results.get(s, {}).get(y, {}).get("win_rate")]) >= 2]
    multi.sort(
        key=lambda s: -(
            sum((results[s][y].get("win_rate") or 0) for y in years if results[s].get(y))
            / max(1, len(years))
        )
    )

    head = f"{'策略':<26}" + "".join(f"{y:>9}" for y in years) + f"{'均值':>8}{'极差':>7}{'稳定':>6}"
    print(head)
    print("-" * len(head))

    rows_md = []
    for sid in multi:
        wrs = [(results[sid].get(y) or {}).get("win_rate") for y in years]
        vals = [w for w in wrs if w is not None]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        spread = max(vals) - min(vals)
        stable = "高" if spread < 0.10 else "中" if spread < 0.20 else "低"
        cells = "".join(f"{w*100:>8.1f}%" if w is not None else f"{'—':>9}" for w in wrs)
        print(f"{sid:<26}{cells}{avg*100:>7.1f}%{spread*100:>6.1f}%{stable:>6}")
        rows_md.append(
            f"| `{sid}` | " + " | ".join(f"{w*100:.1f}%" if w is not None else "—" for w in wrs)
            + f" | {avg*100:.1f}% | {spread*100:.1f}% | {stable} |"
        )

    out = Path(__file__).resolve().parents[1] / args.out
    lines = [
        "# 分年度胜率稳定性",
        "",
        f"> 代际 `{gen.generation[:8]}` · 每年单独跑一次账户回测",
        f"> 10 万本金 · 单笔止损 {cfg.risk_amount:.0f} 元 · 最多 {cfg.max_positions} 仓 · 止损 8% · 持仓 20 天",
        f"> 生成于 {time.strftime('%Y-%m-%d %H:%M')} · scripts/yearly_winrate.py",
        "",
        "「极差」= 最高年胜率 − 最低年胜率。低于 10pp 算稳定，超过 20pp 说明这个胜率不可信，",
        "很可能只是某一年行情的产物。",
        "",
        "| 策略 | " + " | ".join(str(y) for y in years) + " | 均值 | 极差 | 稳定 |",
        "|---|" + "---|" * len(years) + "---|---|---|",
        *rows_md,
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告 → {out}")


if __name__ == "__main__":
    main()
