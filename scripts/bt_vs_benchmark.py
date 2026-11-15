"""算账户回测的同期基准（全市场等权 buy&hold）

为什么需要：
    +5.5% 在 2021-2023 可能是"市场整体跌了 20%，策略只跌 5%"（好的防守）
    也可能是"市场涨了 10%，策略只涨 5%"（差的进攻）

    必须和同期等权 buy&hold 对比才知道是 alpha 还是 beta。

三种基准：
    B1 全市场等权 buy&hold   —— 买入所有股票等权持有到期末
    B2 沪深300 buy&hold      —— 大盘代表
    B3 随机选 5 只等权        —— 和策略同样仓位，只是不挑票（这个最难做，跳过）
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

SPLITS = {
    "train": (date(2021, 1, 4), date(2023, 12, 29)),
    "valid": (date(2024, 1, 2), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 2), date(2026, 11, 12)),
}

BT = {
    "train": {"ret": 0.055388, "mdd": -0.112559, "sharpe": 0.302, "trades": 301, "win": 0.4385},
    "valid": {"ret": 0.046, "mdd": -0.052, "sharpe": 0.42, "trades": 191, "win": 0.497},
    "holdout": {"ret": -0.039, "mdd": -0.099, "sharpe": -0.80, "trades": 93, "win": 0.419},
}


def main() -> None:
    store = GenerationStore(config.DATA_DIR)
    store.require()
    df = store.scan().select("symbol", "date", "close", "benchmark").collect().sort(["symbol", "date"])

    print("=" * 92)
    print("账户回测 vs 同期基准")
    print("=" * 92)
    print("{:<10}{:>14}{:>12}{:>12}{:>12}{:>10}".format("Split", "策略收益", "等权买持", "沪深300", "超额(对等权)", "结论"))
    print("-" * 92)

    rows = []
    for split, (d0, d1) in SPLITS.items():
        g = df.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1)))

        # B1 全市场等权 buy&hold：所有股票从区间首日到末日的平均收益
        per_sym = (
            g.group_by("symbol")
            .agg(
                pl.col("close").sort_by("date").first().alias("c0"),
                pl.col("close").sort_by("date").last().alias("c1"),
                pl.len().alias("n"),
            )
            .filter(pl.col("n") >= 100)     # 至少 100 个交易日才算
        )
        r_eq = ((per_sym["c1"] / per_sym["c0"] - 1)).drop_nulls()
        eq = float(r_eq.mean()) if r_eq.len() else 0.0

        # B3 沪深300：用 benchmark 列（同一天所有行相同，取首末）
        # 注意：benchmark 列两种口径——指数点位（3000+，fetch_5y/history）或当日涨跌幅（%，fetch_all_market）
        bd = g.filter(pl.col("benchmark").is_not_null()).select("date", "benchmark").unique().sort("date")
        if bd.height >= 2:
            first, last = float(bd["benchmark"][0]), float(bd["benchmark"][-1])
            if abs(last) > 1000:          # 点位口径
                hs300 = last / first - 1
            else:                          # 已是涨跌幅(%) → 复利累计
                hs300 = float((1 + bd["benchmark"] / 100).product() - 1)
        else:
            hs300 = 0.0

        strat = BT[split]["ret"]
        alpha = strat - eq

        if alpha > 0.02:
            verdict = "有 alpha"
        elif alpha > -0.02:
            verdict = "持平"
        else:
            verdict = "跑输"

        print(
            "{:<10}{:>13.1f}%{:>11.1f}%{:>11.1f}%{:>11.1f}pp{:>10}".format(
                split, strat * 100, eq * 100, hs300 * 100, alpha * 100, verdict
            )
        )
        rows.append(
            {
                "split": split,
                "strat": round(strat, 4),
                "eq_buyhold": round(eq, 4),
                "hs300": round(hs300, 4),
                "alpha_vs_eq": round(alpha, 4),
                "mdd": BT[split]["mdd"],
                "sharpe": BT[split]["sharpe"],
                "trades": BT[split]["trades"],
                "win": BT[split]["win"],
            }
        )

    print()
    print("读法：")
    print("  · 超额为正 = 策略选股/择时带来了价值")
    print("  · 超额接近 0 = 策略只是「市场 beta」，不如直接买指数")
    print("  · 超额为负 = 策略跑输「随便买一堆股票」")

    out = ROOT / "reports" / "breakout_vs_benchmark.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print("→ " + str(out))


if __name__ == "__main__":
    main()
