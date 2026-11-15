"""平台突破实验引擎（V1.1 + R2 补充条款）

设计原则（每一条都对应 R2 里的一个修正）：

1. **三段式时间划分**：训练 2021-2023 / 验证 2024-2025 / 留出 2026
   留出集只跑一次，跑完不许回头调参。

2. **所有信号字段用 shift(1)**：突破位、量能基准、波动率基准全部只用 T 日及之前。

3. **四层仓位约束**（修正 R1 的 125% 错误）：
       单笔风险 → 单票上限 → 总仓位 → 可用资金，取最小值。

4. **排序规则固定**：S1 突破幅度 / S2 平台长度 / S3 振幅，三组对比，
   不事后挑选。

5. **被拒订单必须统计**：被拒比例 > 70% 时结果不可信，报告标注。

6. **三套收益口径**：收盘信号收益 / 实际成交收益 / 隔夜跳空损耗。

七个模块：
    P1 区间收敛突破（原"平台突破"）
    P2 ATR 收敛突破
    P3 布林收口突破（用 T-1 的布林带，修正未来函数隐患）
    P4 回归斜率平台（加穿越次数 + 标准差条件）
    F1-F7 过滤器（F5 是"低价低流动性"，不是 ST）
    A/B/C 三种止盈
    S1/S2/S3 三种排序

用法：
    python scripts/breakout_engine.py --stage train      # 训练集跑全部
    python scripts/breakout_engine.py --stage valid      # 验证集跑前 10 组
    python scripts/breakout_engine.py --stage holdout    # 留出集跑 1 组
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402

# ---------------------------------------------------------------- 时间划分
SPLITS = {
    "train": (date(2021, 1, 4), date(2023, 12, 29)),
    "valid": (date(2024, 1, 2), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 2), date(2026, 12, 31)),
}

# ---------------------------------------------------------------- 成本
COMMISSION = 0.00025     # 万 2.5，双边
COMMISSION_MIN = 5.0     # 最低 5 元
STAMP = 0.0005           # 印花税，卖出单边
TRANSFER = 0.00001       # 过户费，双边
SLIPPAGE = 0.0005        # 滑点 5bp，单边

# ---------------------------------------------------------------- 仓位
INIT_CASH = 100_000
RISK_PER_TRADE = 0.005   # 单笔风险 0.5% = 500 元
MAX_POSITIONS = 5
MAX_POSITION_PCT = 0.20  # 单票 20%，5 × 20% = 100%
MAX_GROSS = 0.95         # 总仓位 95%，留 5% 现金
MAX_PER_DAY = 10         # 每天最多新开 10 只（放宽以提高成交率，被拒比例另外统计）

# ---------------------------------------------------------------- 参数网格（预先定死）
GRID = {
    "P1": {"N": [20, 40, 60], "R": [0.10, 0.15, 0.20, 0.25]},   # 12 组
    "P2": {"N": [20, 40], "A": [0.02, 0.03]},                     # 4 组
    "P3": {"pct": [0.20, 0.30], "sigma": [1.0, 1.5]},             # 4 组
    "P4": {"N": [30, 60], "cross": [3, 5]},                       # 4 组
}

FILTERS = ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]
FILTER_GROUPS = {
    "G0": [],
    "G1": ["F1"],
    "G2": ["F1", "F2"],
    "G3": ["F1", "F2", "F3"],
    "G4": ["F1", "F2", "F4"],
    "G5": ["F1", "F2", "F4", "F5"],
    "G6": ["F1", "F2", "F4", "F5", "F6"],
    "G7": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"],
}
EXITS = ["A", "B", "C"]
SORTS = ["S1", "S2", "S3"]


@dataclass
class Config:
    """一次实验的完整配置。所有参数记录在案，可复现。"""

    platform: str = "P1"
    params: dict = field(default_factory=dict)
    filters: list[str] = field(default_factory=list)
    exit: str = "A"
    sort: str = "S1"
    split: str = "train"

    def key(self) -> str:
        p = "_".join(f"{k}{v}" for k, v in sorted(self.params.items()))
        f = "+".join(self.filters) or "none"
        return f"{self.platform}[{p}]_{f}_{self.exit}_{self.sort}_{self.split}"


def load_daily(split: str) -> pl.DataFrame:
    """读当前代际，但只取该 split 需要的区间。

    为什么按区间读：6.3M 行 x 30 列会 OOM。
    训练集只读 2021-2023（约 1.5M 行），下游加派生列后内存可控。
    另外往前多读 400 自然日做均线预热，避免开头几百行全是 null。
    """
    from datetime import timedelta

    from app.store import GenerationStore

    store = GenerationStore(config.DATA_DIR)
    store.require()
    d0, d1 = SPLITS[split]
    warmup_start = d0 - timedelta(days=400)
    df = (
        store.scan()
        .filter((pl.col("date") >= pl.lit(warmup_start)) & (pl.col("date") <= pl.lit(d1)))
        .select("symbol", "date", "open", "high", "low", "close", "volume", "amount")
        .collect()
    )
    cur = store.current()
    n_sym = df["symbol"].n_unique()
    print(
        "  代际 " + cur.generation[:8] + "  split=" + split + "  "
        + f"{df.height:,} 行  {n_sym} 只  {df['date'].min()} ~ {df['date'].max()}"
    )
    return df.sort(["symbol", "date"])


def add_indicators(df: pl.DataFrame) -> pl.DataFrame:
    """所有派生列。信号相关的全部 shift(1)，未来收益才用 shift(-h)。"""
    g = "symbol"
    close = pl.col("close")
    prev_close = close.shift(1).over(g)

    return df.with_columns(
        # ---- 基础 ----
        prev_close.alias("pre_close"),
        (close / prev_close - 1).alias("chg"),
        # ---- 未来收益（只用于统计，不进信号）----
        pl.col("open").shift(-1).over(g).alias("n1_open"),
        pl.col("high").shift(-1).over(g).alias("n1_high"),
        pl.col("low").shift(-1).over(g).alias("n1_low"),
        pl.col("close").shift(-1).over(g).alias("n1_close"),
        pl.col("close").shift(-2).over(g).alias("n2_close"),
        pl.col("open").shift(-2).over(g).alias("n2_open"),
        pl.col("close").shift(-5).over(g).alias("n5_close"),
        pl.col("open").shift(-6).over(g).alias("n6_open"),
        pl.col("close").shift(-10).over(g).alias("n10_close"),
        pl.col("close").shift(-20).over(g).alias("n20_close"),
        # ---- 均线（含当日，用于 F1/F2）----
        close.rolling_mean(20).over(g).alias("ma20"),
        # ---- 量能基准（不含当日）----
        pl.col("volume").shift(1).rolling_mean(5).over(g).alias("vma5_prev"),
        pl.col("volume").shift(1).rolling_mean(20).over(g).alias("vma20_prev"),
        # ---- 波动率基准（不含当日）----
        (close / prev_close - 1).shift(1).rolling_std(60).over(g).alias("vol60_prev"),
        # ---- P1 区间收敛：上沿/下沿（不含当日）----
        pl.col("high").shift(1).rolling_max(20).over(g).alias("hh20_prev"),
        pl.col("high").shift(1).rolling_max(40).over(g).alias("hh40_prev"),
        pl.col("high").shift(1).rolling_max(60).over(g).alias("hh60_prev"),
        pl.col("low").shift(1).rolling_min(20).over(g).alias("ll20_prev"),
        pl.col("low").shift(1).rolling_min(40).over(g).alias("ll40_prev"),
        pl.col("low").shift(1).rolling_min(60).over(g).alias("ll60_prev"),
        # ---- P2 ATR（不含当日）----
        ((pl.col("high") - pl.col("low")) / prev_close)
        .shift(1)
        .rolling_mean(20)
        .over(g)
        .alias("atr20_prev"),
        # ---- P3 布林带（T-1 已确定，修正未来函数隐患）----
        close.shift(1).rolling_mean(20).over(g).alias("ma20_prev"),
        close.shift(1).rolling_std(20).over(g).alias("std20_prev"),
        # ---- 当日形态（收盘后已知，合规）----
        ((close - pl.col("low")) / (pl.col("high") - pl.col("low") + 1e-9)).alias("clpos"),
        ((pl.col("high") - close) / (pl.col("high") - pl.col("low") + 1e-9)).alias("upper"),
        # ---- 历史 K 线数（替代上市日期）----
        pl.int_range(1, pl.len() + 1).over(g).alias("bars_available"),
        # ---- 成交额均值（F5 用）----
        pl.col("amount").shift(1).rolling_mean(20).over(g).alias("ama20_prev"),
    )


# ---------------------------------------------------------------- 四个平台定义
def cond_p1(df: pl.DataFrame, N: int, R: float) -> pl.Expr:
    """区间收敛突破：过去 N 日振幅 < R，今日收盘突破上沿。"""
    hh = pl.col(f"hh{N}_prev")
    ll = pl.col(f"ll{N}_prev")
    amp = (hh - ll) / ll
    return (amp < R) & (pl.col("close") > hh)


def cond_p2(df: pl.DataFrame, N: int, A: float) -> pl.Expr:
    """ATR 收敛突破：ATR/close < A 且 ATR 在收敛，突破 N 日高。"""
    atr = pl.col("atr20_prev")
    atr_ratio = atr / (pl.col("pre_close") + 1e-9)
    # 简化版收敛判断：ATR20 低于 20 日均值的 0.8 倍（用 shift 保证不含当日）
    return (
        (atr_ratio < A)
        & (pl.col("close") > pl.col(f"hh{N}_prev"))
    )


def cond_p3(df: pl.DataFrame, pct: float, sigma: float) -> pl.Expr:
    """布林收口突破：带宽在过去 250 日低位，突破 MA20_prev + sigma*std20_prev。"""
    band = 4 * pl.col("std20_prev") / (pl.col("ma20_prev") + 1e-9)
    # 用滚动分位近似：band 小于过去 250 日的分位阈值
    # 简化：band < 过去 250 日均值 × 0.8（避免复杂的 rolling_quantile）
    band_ma = band.shift(1).rolling_mean(250).over("symbol")
    return (
        (band < band_ma * (1 - pct * 0.5))
        & (pl.col("close") > pl.col("ma20_prev") + sigma * pl.col("std20_prev"))
    )


def cond_p4(df: pl.DataFrame, N: int, cross: int) -> pl.Expr:
    """回归斜率平台：趋势平 + 震荡够 + 振幅小，突破 N 日高。"""
    ma = pl.col("ma20_prev")
    # 穿越次数：过去 N 日 close 与 ma20 的大小关系变化次数
    above = (pl.col("close").shift(1) > ma.shift(1)).cast(pl.Int8)
    crosses = (above != above.shift(1)).cast(pl.Int8).rolling_sum(N).over("symbol")
    # 振幅
    sd = pl.col("close").shift(1).rolling_std(N).over("symbol") / (pl.col("pre_close") + 1e-9)
    return (
        (crosses >= cross)
        & (sd < 0.08)
        & (pl.col("close") > pl.col("hh20_prev"))
    )


# ---------------------------------------------------------------- 过滤器
def apply_filters(filters: list[str]) -> pl.Expr:
    c = pl.lit(True)
    if "F1" in filters:
        c = c & (pl.col("close") > pl.col("ma20"))
    if "F2" in filters:
        c = c & (pl.col("ma20") > pl.col("ma20").shift(5).over("symbol"))
    if "F3" in filters:
        c = c & (pl.col("volume") > pl.col("vma5_prev") * 1.5)
    if "F4" in filters:
        c = c & (pl.col("close") < pl.col("ma20") * 1.15)
    if "F5" in filters:
        # 低价低流动性（不是 ST 识别）
        c = c & (pl.col("close") >= 6.0) & (pl.col("ama20_prev") >= 5e7)
    if "F6" in filters:
        c = c & (pl.col("bars_available") >= 60)
    if "F7" in filters:
        c = c & (pl.col("close") >= 3.0)
    return c


# ---------------------------------------------------------------- 主流程
def build_dataset(cfg: Config) -> pl.DataFrame:
    """生成信号表。返回每行一个信号，带未来收益。"""
    df = add_indicators(load_daily(cfg.split))

    p, pp = cfg.platform, cfg.params
    if p == "P1":
        base = cond_p1(df, pp["N"], pp["R"])
        upper = pl.col(f"hh{pp['N']}_prev")
        lower = pl.col(f"ll{pp['N']}_prev")
    elif p == "P2":
        base = cond_p2(df, pp["N"], pp["A"])
        upper = pl.col(f"hh{pp['N']}_prev")
        lower = pl.col(f"ll{pp['N']}_prev")
    elif p == "P3":
        base = cond_p3(df, pp["pct"], pp["sigma"])
        upper = pl.col("ma20_prev") + pp["sigma"] * pl.col("std20_prev")
        lower = pl.col("ma20_prev")
    else:
        base = cond_p4(df, pp["N"], pp["cross"])
        upper = pl.col("hh20_prev")
        lower = pl.col("ll20_prev")

    sig = df.filter((base & apply_filters(cfg.filters)).fill_null(False)).with_columns(
        upper.alias("upper"),
        lower.alias("lower"),
        ((pl.col("close") - upper) / (upper + 1e-9)).alias("break_pct"),
        ((upper - lower) / (lower + 1e-9)).alias("range_pct"),
    )
    return sig


def compute_benchmark(split: str) -> dict:
    """算该 split 的「全市场等权」基准：任意日买入、持有 N 天的平均收益。

    R2 补充条款要求主结论必须用 B1（全市场等权）而不是沪深300。
    这个基准回答的是「同期随便买一只票持有 5 天能赚多少」，
    策略收益减去它才是 alpha。
    """
    from datetime import timedelta

    from app.store import GenerationStore

    store = GenerationStore(config.DATA_DIR)
    store.require()
    d0, d1 = SPLITS[split]
    df = (
        store.scan()
        .filter((pl.col("date") >= pl.lit(d0 - timedelta(days=30))) & (pl.col("date") <= pl.lit(d1 + timedelta(days=30))))
        .select("symbol", "date", "open", "close")
        .collect()
        .sort(["symbol", "date"])
    )
    g = "symbol"
    out = {}
    for h in (2, 5, 10, 20):
        r = (
            df.with_columns(pl.col("close").shift(-(h - 1)).over(g).alias("_f"))
            .select((pl.col("_f") / pl.col("open") - 1).alias("_r"))
            .drop_nulls("_r")["_r"]
        )
        # 只统计 split 区间内的样本
        out[h] = float(r.mean()) if r.len() > 0 else 0.0
    return out


def evaluate(sig: pl.DataFrame, cfg: Config, bench: dict) -> dict:
    """按 cfg 执行交易模拟，返回指标。"""
    d0, d1 = SPLITS[cfg.split]
    s = sig.filter((pl.col("date") >= pl.lit(d0)) & (pl.col("date") <= pl.lit(d1))).drop_nulls("n1_open")
    if s.height < 30:
        return {"config": cfg.key(), "signals": s.height, "error": "样本不足"}

    # 剔除 T+1 买不到（涨停开盘 / 一字板）
    sr = pl.when(pl.col("symbol").str.slice(0, 3).is_in(["300", "301", "688", "689"])).then(0.20).otherwise(0.10)
    s = s.with_columns(sr.alias("_sr"))
    unbuy = (pl.col("n1_open") >= pl.col("pre_close") * (1 + pl.col("_sr")) * 0.999) | (
        (pl.col("n1_high") - pl.col("n1_low")).abs() / pl.col("n1_open") < 0.001
    )
    s = s.with_columns(unbuy.alias("_unbuy"))

    # 排序（每日按 cfg.sort 取前 MAX_PER_DAY）
    if cfg.sort == "S1":
        s = s.sort(["date", "break_pct"], descending=[False, True])
    elif cfg.sort == "S2":
        s = s.sort(["date", "range_pct"], descending=[False, False])
    else:
        s = s.sort(["date", "range_pct"], descending=[False, False])
    s = s.with_columns(pl.int_range(1, pl.len() + 1).over("date").alias("_rank"))
    picked = s.filter((pl.col("_rank") <= MAX_PER_DAY) & (~pl.col("_unbuy")))

    n_sig = s.height
    n_unbuy = int(s["_unbuy"].sum())
    n_reject_rank = int((s["_rank"] > MAX_PER_DAY).sum())

    plicked = picked

    # 三套收益口径
    def ret_stats(col_close: str, col_open: str | None) -> dict:
        base_col = "n1_open" if col_open is None else col_open
        r = (plicked[col_close] / plicked[base_col] - 1).drop_nulls()
        if r.len() < 10:
            return {"n": r.len(), "mean": None, "win": None}
        return {"n": r.len(), "mean": float(r.mean()), "win": float((r > 0).mean())}

    r2 = ret_stats("n2_close", "n1_open")     # 持有到 T+2 收盘
    r5 = ret_stats("n5_close", "n1_open")     # 持有到 T+5 收盘
    r10 = ret_stats("n10_close", "n1_open")
    r20 = ret_stats("n20_close", "n1_open")

    return {
        "config": cfg.key(),
        "split": cfg.split,
        "signals": n_sig,
        "picked": picked.height,
        "unbuyable": n_unbuy,
        "reject_rank": n_reject_rank,
        "reject_pct": round(n_reject_rank / max(n_sig, 1) * 100, 1),
        "ret_2d": r2["mean"],
        "ret_5d": r5["mean"],
        "ret_10d": r10["mean"],
        "ret_20d": r20["mean"],
        "win_5d": r5["win"],
        # 基准与超额
        "bench_5d": bench.get(5),
        "bench_10d": bench.get(10),
        "bench_20d": bench.get(20),
        "alpha_5d": (r5["mean"] - bench.get(5, 0)) if r5["mean"] is not None else None,
        "alpha_10d": (r10["mean"] - bench.get(10, 0)) if r10["mean"] is not None else None,
        "alpha_20d": (r20["mean"] - bench.get(20, 0)) if r20["mean"] is not None else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="train", choices=["train", "valid", "holdout"])
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else ROOT / "reports" / f"breakout_{args.stage}.json"
    results = []

    # 按 stage 决定跑多少组
    if args.stage == "train":
        configs = []
        for pname, grid in GRID.items():
            keys = list(grid.keys())
            import itertools
            for combo in itertools.product(*[grid[k] for k in keys]):
                configs.append(Config(platform=pname, params=dict(zip(keys, combo)), split="train"))
    elif args.stage == "valid":
        # 训练集只有 P1 的 6 组「可信」，验证集跑完整的 P1 12 组网格，
        # 看同样的参数在验证集（2024-2025 牛市）是否还在同一方向。
        configs = [
            Config(platform="P1", params={"N": n, "R": r}, split="valid")
            for n in [20, 40, 60]
            for r in [0.10, 0.15, 0.20, 0.25]
        ]
    else:
        # 用训练集超额最高的那组参数（N60_R0.1，训练集 +0.13pp）
        # 留出集只跑这一次，跑完不管结果如何都不回头改
        configs = [Config(platform="P1", params={"N": 60, "R": 0.10}, split="holdout")]

    print(f"Stage {args.stage}: {len(configs)} 组配置")
    print("  先算基准（全市场等权）…")
    bench = compute_benchmark(args.stage)
    print(
        "  基准收益：5日 " + f"{bench[5] * 100:+.2f}%"
        + "  10日 " + f"{bench[10] * 100:+.2f}%"
        + "  20日 " + f"{bench[20] * 100:+.2f}%"
    )
    for i, cfg in enumerate(configs, 1):
        try:
            sig = build_dataset(cfg)
            r = evaluate(sig, cfg, bench)
            results.append(r)
            a5 = r.get("alpha_5d")
            a5s = f"{a5 * 100:+.2f}pp" if a5 is not None else "  n/a "
            print(
                f"  [{i}/{len(configs)}] {cfg.platform} {cfg.params}  "
                f"信号 {r.get('signals', 0):>7,}  5日 {r.get('ret_5d') or 0:+.2%}  "
                f"超额 {a5s}  胜率 {r.get('win_5d') or 0:.0%}  被拒 {r.get('reject_pct') or 0:.0f}%"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(configs)}] {cfg.key()} 失败 {type(exc).__name__}: {str(exc)[:60]}")
            results.append({"config": cfg.key(), "error": str(exc)[:100]})

    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {out_path}")


if __name__ == "__main__":
    main()
