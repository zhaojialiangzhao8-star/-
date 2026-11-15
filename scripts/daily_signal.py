"""每日信号 · 盘后可跑的实盘清单。

跑法：
    python scripts/daily_signal.py                      # 用最新数据
    python scripts/daily_signal.py --risk 3000          # 每笔固定止损 3000 元
    python scripts/daily_signal.py --date 2026-11-07    # 指定日期（复盘用）
    python scripts/daily_signal.py --holdings holdings.json   # 附带持仓监控

仓位一律由「固定止损金额」反推：
    股数 = 止损金额 ÷ (买入价 − 止损价)，向下取整到 100 股
这样每笔最多亏的钱是固定的，账户不会因为某一只票的波动而失控。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402
from app.backtest.broker import Broker  # noqa: E402
from app.backtest.models import FeeConfig, Side  # noqa: E402

# 实盘参数（来自四关验证 + 参数稳健性检验）
THRESHOLD = -0.15      # 3 日累计跌幅
VOL_RATIO = 1.2        # 当日量 / 前日量
STOP_PCT = 0.12        # 止损 12%（相对买入价）
BUY_ZONE = 0.01        # 买入区 = 昨收 ±1%


def board_of(symbol: str) -> str:
    code = symbol.split(".")[0]
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


def lot_floor(x: float, lot: int = 100) -> int:
    return int(x // lot) * lot if x > 0 else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=3000, help="每笔固定止损金额（元）")
    ap.add_argument("--date", default=None, help="指定信号日 YYYY-MM-DD")
    ap.add_argument("--holdings", default=None, help="持仓 JSON 文件")
    ap.add_argument("--max-pos", type=int, default=5, help="最多持仓数")
    args = ap.parse_args()

    raw = GenerationStore(config.DATA_DIR).collect()
    df = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    ).sort(["symbol", "date"])

    target = date.fromisoformat(args.date) if args.date else df["date"].max()
    latest = df.filter(pl.col("date") <= target)
    cur = latest.filter(pl.col("date") == latest["date"].max())
    if cur.height == 0:
        raise SystemExit("没有该日数据")

    # 3 日累计跌幅 + 量比
    d = latest.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("_r3"),
        (pl.col("volume") / pl.col("volume").shift(1).over("symbol")).alias("_vr"),
        pl.col("volume").shift(1).over("symbol").alias("_prev_vol"),
    )
    sig = d.filter(pl.col("date") == latest["date"].max()).filter(
        (pl.col("_r3") < THRESHOLD)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("_vr") > VOL_RATIO)
        & pl.col("_r3").is_not_null()
    ).sort("_r3")  # 跌得最狠的排前面

    broker = Broker(FeeConfig())

    print()
    print("═" * 92)
    print(f"  急跌反弹 · {latest['date'].max()} 收盘信号"
          f"　·　每笔固定止损 {args.risk:,.0f} 元　·　最多 {args.max_pos} 仓")
    print("═" * 92)

    if sig.height == 0:
        print("\n  今日无信号（跌幅 ≥15% + 收阳 + 量比>1.2 的票为 0）")
    else:
        print(f"\n【明日买入候选】共 {sig.height} 只，建议最多取前 {args.max_pos} 只\n")
        header = f"{'代码':>12}{'板块':>6}{'3日跌幅':>9}{'量比':>7}{'昨收':>9}{'买入区':>16}{'止损':>9}{'股数':>7}{'金额':>11}{'止损额':>9}"
        print(header)
        print("-" * len(header))
        for row in sig.head(args.max_pos).iter_rows(named=True):
            px = row["close"]
            lo, hi = round(px * (1 - BUY_ZONE), 2), round(px * (1 + BUY_ZONE), 2)
            entry = px                     # 以昨收估算，实际按次日开盘
            stop = round(entry * (1 - STOP_PCT), 2)
            per_share = entry - stop
            qty = lot_floor(args.risk / per_share) if per_share > 0 else 0
            amount = qty * entry
            print(f"{row['symbol']:>12}{board_of(row['symbol']):>6}"
                  f"{row['_r3']*100:>8.1f}%{row['_vr']:>7.2f}{px:>9.2f}"
                  f"{f'{lo:.2f}~{hi:.2f}':>16}{stop:>9.2f}{qty:>7}{amount:>10,.0f}{qty*per_share:>9,.0f}")
        print("-" * len(header))
        total = sum(lot_floor(args.risk / (r["close"] * (1 - STOP_PCT) and (r["close"] - r["close"] * (1 - STOP_PCT))))
                    * r["close"] for r in sig.head(args.max_pos).iter_rows(named=True))
        print(f"{'合计占用资金':>60}{total:>20,.0f} 元"
              f"（占总资金 {total/100000*100:.0f}%，按 10 万本金）")

    # 持仓监控
    if args.holdings and Path(args.holdings).exists():
        hold = json.loads(Path(args.holdings).read_text(encoding="utf-8"))
        print(f"\n【持仓监控】{len(hold)} 只\n")
        print(f"{'代码':>12}{'成本':>9}{'现价':>9}{'盈亏':>9}{'止损价':>9}{'状态':>8}{'持有天数':>9}")
        print("-" * 74)
        for h in hold:
            sym, cost = h["symbol"], h["cost"]
            row = cur.filter(pl.col("symbol") == sym)
            if row.height == 0:
                print(f"{sym:>12}  无行情（停牌？）")
                continue
            px = row["close"][0]
            stop = float(h.get("stop") or cost * (1 - STOP_PCT))
            pnl = px / cost - 1
            held = (latest["date"].max() - date.fromisoformat(h["entry_date"])).days if h.get("entry_date") else 0
            flag = "⚠️ 止损" if px <= stop else ("到期" if held >= 5 else "持有")
            print(f"{sym:>12}{cost:>9.2f}{px:>9.2f}{pnl*100:>8.1f}%{stop:>9.2f}{flag:>8}{held:>9}")

    print("\n" + "─" * 92)
    print("  规则：次日开盘买入（不追高，超过买入区上沿放弃）· 止损 = 买入价 × 0.88 · 持有 5 个交易日")
    print("  来源：四关验证通过 + 18 组参数样本外 78% 为正（详见 docs/PHASE-8-REPORT.md）")
    print("─" * 92)
    print()


if __name__ == "__main__":
    main()
