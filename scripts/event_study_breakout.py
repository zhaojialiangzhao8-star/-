"""突破与回踩 · 事件研究（不是回测，是先看统计事实）

七个子项里，先用最干净的日线数据把「事件发生后未来 N 天收益分布」测出来。
这一步不做交易、不算费用、不考虑仓位 —— 只回答一个问题：

    这个"事件"到底有没有信息量？如果有，多大？什么条件下更大？

方法论要点（避免自我欺骗）：

1. **信号必须只用 T 日及之前的数据**
   突破位、平台区间、量能基准全部用 shift(1) 之前的窗口算，
   绝不能让"当天的突破"污染"当天的判定窗口"。

2. **收益从 T+1 开盘算起**
   用户要求「次日开盘买入」，所以收益 = close[T+k] / open[T+1] - 1。
   不能用 close[T] 当买入价 —— 那是收盘价，实盘买不到那个价。

3. **剔除买不到的样本**
   T+1 开盘涨停 / 一字板的样本标记出来单独统计，不计入主口径。

4. **对照组**
   同一时段全市场同持有期的平均收益，用来算超额。

用法：
    python scripts/event_study_breakout.py
    python scripts/event_study_breakout.py --horizons 1,3,5,10,20
    python scripts/event_study_breakout.py --only platform
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def limit_rate(symbol: str) -> float:
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def load() -> pl.DataFrame:
    store = GenerationStore(config.DATA_DIR)
    store.require()
    df = (
        store.scan()
        .select("symbol", "date", "open", "high", "low", "close", "volume", "amount")
        .sort(["symbol", "date"])
        .collect()
    )
    # 复权跳变剔除（|涨跌|>21% 的行及其后该股数据不参与）
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_chg")
    )
    # 用 change_pct 特征标记异常行
    return df


def add_features(df: pl.DataFrame) -> pl.DataFrame:
    """所有特征都只用「当日及之前」的数据，没有任何未来信息。"""
    g = "symbol"
    return df.with_columns(
        # 未来收益（这是唯一"未来"的东西，且只用于统计，不参与信号）
        pl.col("open").shift(-1).over(g).alias("n1_open"),
        pl.col("close").shift(-1).over(g).alias("n1_close"),
        pl.col("high").shift(-1).over(g).alias("n1_high"),
        pl.col("low").shift(-1).over(g).alias("n1_low"),
        pl.col("volume").shift(-1).over(g).alias("n1_vol"),
        # 前一日收盘（算涨停用）
        pl.col("close").shift(1).over(g).alias("pre_close"),
        # 量能基准：过去 20 日均量（不含今天）
        pl.col("volume").shift(1).rolling_mean(20).over(g).alias("_vma20_prev"),
        # 突破位：过去 N 日最高（不含今天 —— shift(1) 是关键）
        pl.col("high").shift(1).rolling_max(20).over(g).alias("_hh20_prev"),
        pl.col("high").shift(1).rolling_max(60).over(g).alias("_hh60_prev"),
        pl.col("high").shift(1).rolling_max(120).over(g).alias("_hh120_prev"),
        # 波动率：过去 60 日收益标准差
        (pl.col("close") / pl.col("close").shift(1).over(g) - 1)
        .shift(1)
        .rolling_std(60)
        .over(g)
        .alias("_vol60_prev"),
        # 平台：过去 40 日的 (最高-最低)/最低
        (
            (pl.col("high").shift(1).rolling_max(40).over(g)
             - pl.col("low").shift(1).rolling_min(40).over(g))
            / pl.col("low").shift(1).rolling_min(40).over(g)
        ).alias("_range40_prev"),
        # 过去 20 日振幅均值（判断是否横盘）
        ((pl.col("high") - pl.col("low")) / pl.col("close").shift(1).over(g))
        .shift(1)
        .rolling_mean(20)
        .over(g)
        .alias("_atr20_prev"),
        # 当日涨跌幅
        (pl.col("close") / pl.col("close").shift(1).over(g) - 1).alias("_chg"),
        # 当日收盘在当日区间的位置（1 = 收在最高）
        ((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low") + 1e-9)).alias("_clpos"),
        # 上影线占比
        ((pl.col("high") - pl.col("close")) / (pl.col("high") - pl.col("low") + 1e-9)).alias("_upper"),
    )


def add_forward(df: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    g = "symbol"
    exprs = []
    for h in horizons:
        exprs.append(pl.col("close").shift(-h).over(g).alias(f"f{h}_close"))
    return df.with_columns(exprs)


def stats(df: pl.DataFrame, mask: pl.Expr, label: str, horizons: list[int], base: dict) -> dict | None:
    sub = df.filter(mask).drop_nulls("n1_open")
    if sub.height < 100:
        print(f"  {label:<34} 样本不足（{sub.height}）")
        return None

    # 剔除 T+1 开盘涨停 / 一字板（买不到）
    sr = pl.col("symbol").map_elements(limit_rate, return_dtype=pl.Float64)
    sub = sub.with_columns(sr.alias("_sr"))
    unbuyable = (
        (pl.col("n1_open") >= pl.col("pre_close") * (1 + pl.col("_sr")) * 0.999)
        | ((pl.col("n1_high") - pl.col("n1_low")).abs() / pl.col("n1_open") < 0.001)
    )
    buyable = sub.filter(~unbuyable)
    n_unbuy = sub.height - buyable.height

    out = {"label": label, "n": sub.height, "n_unbuy": n_unbuy, "excess": {}}
    cells = []
    for h in horizons:
        col = f"f{h}_close"
        r = (buyable[col] / buyable["n1_open"] - 1).drop_nulls()
        if r.len() < 50:
            cells.append("—")
            continue
        exp = r.mean()
        win = (r > 0).sum() / r.len()
        exc = exp - base.get(h, 0)
        cells.append(f"{exp * 100:+.2f}%({win * 100:.0f}%|{exc * 100:+.2f}pp)")
        out["excess"][h] = exc
    print(f"  {label:<34} {sub.height:>7,}  买不到{n_unbuy:>5}  " + "  ".join(f"{c:>17}" for c in cells))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="1,3,5,10,20")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]

    df = add_forward(add_features(load()), horizons)
    df = df.filter(pl.col("_chg").is_not_null() & (pl.col("_chg").abs() <= 0.21))

    print("=" * 130)
    print(f"数据 {df.height:,} 行  {df['symbol'].n_unique()} 只  {df['date'].min()} ~ {df['date'].max()}")
    print("=" * 130)

    # 对照组：全市场任意一天买入、持有 h 天的平均收益
    base: dict[int, float] = {}
    for h in horizons:
        col = f"f{h}_close"
        r = (df[col] / df["n1_open"] - 1).drop_nulls()
        base[h] = float(r.mean())
    print("\n对照组（全市场任意日买入，次日开盘价成交）：")
    for h in horizons:
        print(f"  持有 {h:>2} 天  均值 {base[h] * 100:+.2f}%")

    hdr = "  " + f"{'事件':<34}{'样本':>8}{'剔除':>7}  " + "  ".join(f"{'T+'+str(h):>17}" for h in horizons)
    print("\n" + hdr)
    print("  " + "-" * 126)
    print("  （每个格子 = 平均收益(盈利占比|超额)，全部从 T+1 开盘价算起）")

    tests = [
        # (标签, 条件, 标签用于 --only 过滤)
        (
            "突破前高 20 日",
            pl.col("close") > pl.col("_hh20_prev"),
            "hh20",
        ),
        (
            "突破前高 60 日",
            pl.col("close") > pl.col("_hh60_prev"),
            "hh60",
        ),
        (
            "突破前高 120 日",
            pl.col("close") > pl.col("_hh120_prev"),
            "hh120",
        ),
        (
            "突破 20 日高 + 放量>1.5倍",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("volume") > pl.col("_vma20_prev") * 1.5),
            "hh20_vol",
        ),
        (
            "突破 20 日高 + 缩量<1倍",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("volume") < pl.col("_vma20_prev") * 1.0),
            "hh20_shrink",
        ),
        (
            "突破 20 日高 + 收盘在最高",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("_clpos") > 0.9),
            "hh20_strong",
        ),
        (
            "突破 20 日高 + 上影线长",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("_upper") > 0.5),
            "hh20_weak",
        ),
        (
            "平台突破（40日振幅<25%）",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("_range40_prev") < 0.25),
            "platform",
        ),
        (
            "平台突破（40日振幅<15%）",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("_range40_prev") < 0.15),
            "platform_tight",
        ),
        (
            "高波动下的突破（vol>3%）",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("_vol60_prev") > 0.03),
            "hh20_highvol",
        ),
        (
            "低波动下的突破（vol<2%）",
            (pl.col("close") > pl.col("_hh20_prev")) & (pl.col("_vol60_prev") < 0.02),
            "hh20_lowvol",
        ),
    ]

    results = []
    for label, cond, key in tests:
        if args.only and args.only not in key:
            continue
        m = cond.fill_null(False)
        r = stats(df, m, label, horizons, base)
        if r:
            results.append(r)

    print("\n" + "=" * 130)
    print("读法：超额为正说明这个事件有 alpha；为负说明是反向指标；接近 0 说明无效。")
    print("注意：以上全部未扣交易成本。A 股双边成本约 0.11%（佣金+印花+过户+滑点），")
    print("      所以持有 1 天的超额必须 > 0.11% 才有意义，持有 5 天要 > 0.11% 且胜率稳定。")


if __name__ == "__main__":
    main()
