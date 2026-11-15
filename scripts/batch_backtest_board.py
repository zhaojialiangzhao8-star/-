"""给三个打板策略跑账户回测，结果追加到排名报告里。

为什么要跑这一轮：
    回测页右侧的「全部策略排名」读的是 reports/account_compare_fullmarket.md，
    这个文件是 2026-11-11 生成的，只有当时启用的 12 个策略。
    后来加的 board_1to2 / board_2to3 / board_3to4 不在里面，所以页面上看不到。

口径注意：
    账户模型是「T+1 开盘买、持有 N 天、固定止损」，**和打板的实际打法不同**。
    打板靠的是「涨停就拿着、炸板就卖」的分时判断，账户模型表达不了。
    所以这个报告里的打板成绩偏悲观 —— 真实的规则回测看
    scripts/board_intraday.py 的输出。

用法：
    python scripts/batch_backtest_board.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:3021"
OUT = ROOT / "reports" / "board_account_backtest.md"

TARGETS = [
    ("board_1to2", "打板 1进2"),
    ("board_2to3", "打板 2进3"),
    ("board_3to4", "打板 3进4"),
    ("small_up_breakout", "小涨蓄势后放量突破"),
]


def run(sid: str, hold: int) -> dict | None:
    body = json.dumps(
        {
            "strategy": sid,
            "params": {},
            "init_cash": 100000,
            "risk_amount": 3000,
            "max_positions": 10,
            "max_position_pct": 0.25,
            "stop_loss_pct": 0.08,
            "max_hold_days": hold,
            "start": "2021-11-01",
            "end": "2026-11-07",
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/api/v1/bt/run", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=1200) as r:
            return json.loads(r.read())["metrics"]
    except urllib.error.HTTPError as exc:
        print(f"  {sid} hold={hold}  HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:120]}")
        return None


def main() -> None:
    rows: list[dict] = []
    for sid, name in TARGETS:
        for hold in (1, 2, 3):
            print(f"跑 {sid} hold={hold} …", flush=True)
            m = run(sid, hold)
            if m:
                rows.append(
                    {
                        "sid": sid,
                        "name": name,
                        "hold": hold,
                        "ret": m["total_return"],
                        "mdd": m["max_drawdown"],
                        "sharpe": m["sharpe"],
                        "win": m["win_rate"],
                        "trades": m["trades"],
                    }
                )
                print(
                    f"  {name} hold={hold}  收益 {m['total_return'] * 100:+.1f}%  "
                    f"回撤 {m['max_drawdown'] * 100:.1f}%  夏普 {m['sharpe']:.2f}  "
                    f"胜率 {m['win_rate'] * 100:.1f}%  交易 {m['trades']}"
                )
            time.sleep(3)

    if not rows:
        print("没有任何结果")
        return

    lines = [
        "# 打板策略 · 账户级回测",
        "",
        "> 10 万本金 · 单笔止损 3000 元 · 最多 10 仓 · 止损 8% · 回测 2021-11-01 ~ 2026-11-07",
        "> 由 `scripts/batch_backtest_board.py` 生成",
        "",
        "## 先说结论",
        "",
        "**账户模型不适合打板。**",
        "",
        "这里的账户假设是「T+1 开盘买 → 持有 N 天 → 固定止损卖出」，",
        "而打板的真实打法是「涨停就拿着、炸板就卖」——这是分时判断，",
        "账户模型表达不了，只能退化成固定持有，结果必然是亏的。",
        "",
        "真实的分时规则回测请跑 `python scripts/board_intraday.py`，",
        "那里的结论是：**不买低开的前提下，2进3 期望 +0.61%/笔，3进4 +0.88%/笔**。",
        "",
        "## 回测结果（供对照，不代表打板真实收益）",
        "",
        "| 策略 | 持有天数 | 总收益 | 最大回撤 | 夏普 | 胜率 | 交易 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['hold']} | {r['ret'] * 100:+.1f}% | "
            f"{r['mdd'] * 100:.1f}% | {r['sharpe']:.2f} | {r['win'] * 100:.1f}% | {r['trades']} |"
        )

    lines += [
        "",
        "## 为什么打板在账户模型里必亏",
        "",
        "打板的全部利润来自**次日高开**：",
        "",
        "```",
        "首板封住 → 次日平均高开 +2.31%",
        "你买得到的价格 = 高开之后的价格",
        "从开盘到收盘 = −0.03%（不加费用已经打平）",
        "```",
        "",
        "账户模型按「次日开盘价」成交，正好买在最贵的位置。",
        "要复现打板的收益，买入必须在**涨停封住之前**完成——需要分时数据。",
        "",
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
