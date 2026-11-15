"""打板接力的分时规则回测 —— 用日线近似用户描述的那套打法。

用户的规则（原话）：
    「无论次日涨跌都在开盘买入，如果次日低开直接卖，如果高开，不能跌破成本线，
      跌破前卖出，如果次日涨停，那么就拿着，炸板卖出。」

拆成可执行的判据（全部用 T+1 那根 K 线的 OHLC 判断，因为分时数据还没有）：

    1. T 日收盘封住涨停（2 连板 / 3 连板），T+1 开盘买入
    2. 低开（open < 昨收 × (1 − gap)) → 按开盘价卖出，立刻止损
    3. 高开但盘中跌破成本（low ≤ 成本）→ 按成本价卖出（收益 0）
       —— 「跌破前卖出」用成本价成交，是日线能给的唯一近似；
          真实成交会比成本价更低，所以这个口径偏乐观。
    4. 盘中摸到涨停且收盘仍在涨停 → 封住，持有到 T+2 再看（递归同一套规则）
    5. 盘中摸到涨停但收盘没封住 → 炸板，按收盘价卖出

为什么低开要卖：
    打板的利润全部来自高开。低开说明资金不认，接力盘已经跑了，
    拿着就是等它跌。

口径诚实说明：
    · 「跌破成本」用 low ≤ 成本 判断，忽略「跌破后是否立刻拉回」——
      日线看不到顺序，只能假设跌破就成交。
    · 「炸板」用 high ≥ 涨停 但 close < 涨停 判断，同样忽略盘中反复开合。
    · 所有成交按日线价格，没有滑点；实际会有 0.3~0.8% 的滑点损耗。

用法：
    python scripts/board_intraday.py                    # 全部（2进3 + 3进4）
    python scripts/board_intraday.py --board 3          # 只看 3进4
    python scripts/board_intraday.py --gap 0.02         # 低开阈值改成 2%
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def limit_rate(symbol: str) -> float:
    """涨停幅度：创业板/科创板 20%，主板 10%，北交所 30%。"""
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if symbol.endswith(".BJ"):
        return 0.30
    return 0.10


def simulate(bars: list[dict], idx: int, gap_threshold: float) -> dict | None:
    """从 idx（T 日，已封板）开始，按规则走完这笔交易。

    bars 是该股票的完整日线（list of dict，按日期升序）。
    返回 None 表示数据不够，否则返回成交结果。
    """
    if idx + 1 >= len(bars):
        return None

    t = bars[idx]
    sr = limit_rate(str(t["symbol"]))
    entry_i = idx + 1
    entry = bars[entry_i]

    prev_close = float(t["close"])
    price = float(entry["open"])                 # 开盘买入
    if price <= 0:
        return None

    gap = price / prev_close - 1

    # 规则 2：低开 → 立刻卖（按开盘价，收益就是低开幅度）
    if gap < -gap_threshold:
        return {
            "reason": "低开止损",
            "gap": gap,
            "pnl": gap,
            "days": 1,
            "entry": price,
            "exit": price,
        }

    # 逐日持有：先判断当天是否涨停封住 / 炸板 / 跌破成本
    cost = price
    day = entry
    held = 1
    while True:
        high = float(day["high"])
        low = float(day["low"])
        close = float(day["close"])
        prev = float(bars[entry_i + held - 1 - 1]["close"]) if entry_i + held - 1 - 1 >= 0 else cost
        lu = prev * (1 + sr)          # 当日涨停价（按前一日收盘算）

        hit_limit = high >= lu * 0.999
        sealed = hit_limit and close >= lu * 0.999

        if held == 1:
            # 规则 3：跌破成本线 → 按成本价卖
            if low <= cost * 0.999:
                return {
                    "reason": "跌破成本",
                    "gap": gap,
                    "pnl": cost / price - 1,
                    "days": held,
                    "entry": price,
                    "exit": cost,
                }
        else:
            # 后续日子：跌破成本也走
            if low <= cost * 0.999:
                return {
                    "reason": "跌破成本",
                    "gap": gap,
                    "pnl": cost / price - 1,
                    "days": held,
                    "entry": price,
                    "exit": cost,
                }

        if sealed:
            # 规则 4：涨停封住 → 继续持有
            nxt = entry_i + held
            if nxt >= len(bars):
                return {
                    "reason": "封板未卖（数据结束）",
                    "gap": gap,
                    "pnl": close / price - 1,
                    "days": held,
                    "entry": price,
                    "exit": close,
                }
            day = bars[nxt]
            held += 1
            continue

        if hit_limit and not sealed:
            # 规则 5：炸板 → 按收盘卖
            return {
                "reason": "炸板卖出",
                "gap": gap,
                "pnl": close / price - 1,
                "days": held,
                "entry": price,
                "exit": close,
            }

        # 没涨停也没跌破 → 按收盘卖（隔日就走，不留）
        return {
            "reason": "收盘卖出",
            "gap": gap,
            "pnl": close / price - 1,
            "days": held,
            "entry": price,
            "exit": close,
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", type=int, default=0, help="2=只要 2进3，3=只要 3进4，0=两个都要")
    ap.add_argument("--gap", type=float, default=0.02, help="低开阈值（默认 2%%）")
    ap.add_argument("--start", type=str, default="", help="YYYY-MM-DD")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()
    df = store.scan().select("symbol", "date", "open", "high", "low", "close", "change_pct").sort(
        ["symbol", "date"]
    ).collect()
    df = df.filter(pl.col("change_pct").is_not_null())

    print(f"数据 {df.height:,} 行 {df['symbol'].n_unique()} 只 {df['date'].min()} ~ {df['date'].max()}")

    boards = [args.board] if args.board in (2, 3) else [2, 3]

    for want in boards:
        label = f"{want} 进 {want + 1}"
        trades: list[dict] = []
        n_signals = 0

        for (sym,), g in df.group_by("symbol", maintain_order=True):
            sr = limit_rate(sym)
            rows = g.to_dicts()
            n = len(rows)
            for i in range(n - 1):
                chg = rows[i]["change_pct"]
                if chg is None or chg < sr * 100 - 2:      # 今日涨停（阈值放宽 2 个点兼容 9.98）
                    continue
                # 收盘封住
                if rows[i]["close"] < rows[i]["high"] * 0.998:
                    continue
                # 检查前面连续几个涨停
                streak = 1
                for k in range(1, 10):
                    if i - k < 0:
                        break
                    pc = rows[i - k]["change_pct"]
                    if pc is not None and pc >= sr * 100 - 2:
                        streak += 1
                    else:
                        break
                if streak != want:
                    continue
                if args.start and str(rows[i]["date"]) < args.start:
                    continue

                n_signals += 1
                r = simulate(rows, i, args.gap)
                if r:
                    r["symbol"] = sym
                    r["date"] = rows[i]["date"]
                    trades.append(r)

        if not trades:
            print(f"\n=== {label}：无样本 ===")
            continue

        pnls = [t["pnl"] for t in trades]
        # ±0.05% 内算打平（跌破成本按成本价成交，pnl 恰好 0，不能算输）
        wins = [p for p in pnls if p > 0.0005]
        flats = [p for p in pnls if -0.0005 <= p <= 0.0005]
        losses = [p for p in pnls if p < -0.0005]
        reasons = Counter(t["reason"] for t in trades)

        print(f"\n{'=' * 62}")
        print(f"=== {label}：{n_signals} 个信号，{len(trades)} 笔成交 ===")
        print(
            f"  盈利 {len(wins) / len(trades) * 100:.1f}%   "
            f"打平 {len(flats) / len(trades) * 100:.1f}%   "
            f"亏损 {len(losses) / len(trades) * 100:.1f}%   "
            f"平均收益 {sum(pnls) / len(pnls) * 100:+.2f}%"
        )
        print(f"  平均盈 {sum(wins) / len(wins) * 100 if wins else 0:+.2f}%   平均亏 {sum(losses) / len(losses) * 100 if losses else 0:+.2f}%")
        if wins and losses:
            pf = abs(sum(wins) / sum(losses))
            print(f"  盈亏比 {pf:.2f}")
        print(f"  累计期望 {sum(pnls) / len(pnls) * 100:+.2f}%/笔")
        print(f"  平均持有 {sum(t['days'] for t in trades) / len(trades):.2f} 天")
        print("\n  卖出原因分布：")
        for reason, cnt in reasons.most_common():
            sub = [t["pnl"] for t in trades if t["reason"] == reason]
            print(
                f"    {reason:<16} {cnt:>5} 笔 ({cnt / len(trades) * 100:>5.1f}%)  "
                f"平均 {sum(sub) / len(sub) * 100:>+7.2f}%"
            )
        print("\n  按低开幅度分组：")
        for lo, hi, name in [
            (-1, -0.05, "低开 >5%"),
            (-0.05, -0.02, "低开 2~5%"),
            (-0.02, 0, "低开 0~2%"),
            (0, 0.02, "高开 0~2%"),
            (0.02, 0.05, "高开 2~5%"),
            (0.05, 1, "高开 >5%"),
        ]:
            sub = [t["pnl"] for t in trades if lo <= t["gap"] < hi]
            if sub:
                print(
                    f"    {name:<12} {len(sub):>5} 笔  "
                    f"盈利 {sum(1 for x in sub if x > 0.0005) / len(sub) * 100:>5.1f}%  "
                    f"平均 {sum(sub) / len(sub) * 100:>+7.2f}%"
                )

        print("\n  过滤方案对照：")
        for cond, cn in [
            (lambda g: g >= -0.02, "低开<2%才买"),
            (lambda g: g >= 0, "只买高开"),
            (lambda g: g >= 0.02, "只买高开>2%"),
            (lambda g: g >= 0.05, "只买高开>5%"),
        ]:
            sub = [t["pnl"] for t in trades if cond(t["gap"])]
            if sub:
                print(
                    f"    {cn:<16} {len(sub):>5} 笔  "
                    f"盈利 {sum(1 for x in sub if x > 0.0005) / len(sub) * 100:>5.1f}%  "
                    f"平均 {sum(sub) / len(sub) * 100:>+7.2f}%"
                )


if __name__ == "__main__":
    main()
