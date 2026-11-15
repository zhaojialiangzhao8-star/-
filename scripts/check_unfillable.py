"""检测策略信号里有多少「次日买不到」的情况。

用户指出的问题：
    「低回撤增仓里面，我看还会买到一字板，但是一字板是买不到的」

一字板 = 开盘就涨停，全天封死，K 线是一根横线（open = high = low = close）。
这种票次日开盘价就是涨停价，挂单排在几十万手后面，**根本成交不了**。

但回测引擎假设「次日开盘价成交」，一字板会照单全收 —— 这是假收益。

判据（用 T+1 那根 K 线）：
    一字板：  open == high == low == close（容差 0.1%）
    开盘涨停：open 相对昨收涨幅 ≥ 涨停幅度 × 0.99（开盘就在涨停价）

只要命中任一条，这笔交易在实盘就买不到，回测必须剔除。

用法：
    python scripts/check_unfillable.py                       # 默认查低回撤增仓
    python scripts/check_unfillable.py --strategy board_2to3
    python scripts/check_unfillable.py --all                 # 查全部策略
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.engine import StrategyEngine  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def limit_rate(symbol: str) -> float:
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if symbol.endswith(".BJ"):
        return 0.30
    return 0.10


def analyse(sid: str, name: str, frame: pl.DataFrame, engine: StrategyEngine) -> dict | None:
    try:
        hits = engine.run(sid, {}, frame)
    except Exception as exc:  # noqa: BLE001
        print(f"  {sid:<34} 运行失败: {exc}")
        return None
    if not hits.height:
        return None

    # 把每笔信号的 T+1 那根 K 线拼上来
    nxt = frame.select("symbol", "date", "open", "high", "low", "close", "change_pct").with_columns(
        pl.col("date").alias("_next_date")
    )
    # 用「该股票的下一个交易日」做 join
    nxt = nxt.sort(["symbol", "date"]).with_columns(
        pl.col("date").shift(-1).over("symbol").alias("_prev_date"),
        pl.col("open").shift(-1).over("symbol").alias("n1_open"),
        pl.col("high").shift(-1).over("symbol").alias("n1_high"),
        pl.col("low").shift(-1).over("symbol").alias("n1_low"),
        pl.col("close").shift(-1).over("symbol").alias("n1_close"),
        pl.col("change_pct").shift(-1).over("symbol").alias("n1_chg"),
    )

    joined = hits.select("symbol", "date").join(
        nxt.select("symbol", "date", "n1_open", "n1_high", "n1_low", "n1_close", "n1_chg", "close"),
        on=["symbol", "date"],
        how="left",
    ).drop_nulls("n1_open")

    if not joined.height:
        return None

    rows = []
    for r in joined.iter_rows(named=True):
        sr = limit_rate(r["symbol"])
        t1 = r["n1_open"]
        th, tl, tc = r["n1_high"], r["n1_low"], r["n1_close"]
        prev = r["close"]
        if not t1 or not prev:
            continue
        open_chg = t1 / prev - 1
        # 一字板：开=高=低=收
        flat = (abs(th - tl) / t1 < 0.001) if t1 else False
        # 开盘就在涨停价
        open_limit = open_chg >= sr * 0.99
        # 一字板涨停：平盘 + 涨幅够
        one_word = flat and open_chg >= sr * 0.95
        rows.append({"one_word": one_word, "open_limit": open_limit, "open_chg": open_chg, "n1_chg": r["n1_chg"]})

    if not rows:
        return None

    n = len(rows)
    ow = sum(1 for x in rows if x["one_word"])
    ol = sum(1 for x in rows if x["open_limit"])
    both = sum(1 for x in rows if x["one_word"] or x["open_limit"])
    # 剔除买不到的之后，次日涨幅均值
    keep = [x["n1_chg"] for x in rows if not (x["one_word"] or x["open_limit"]) and x["n1_chg"] is not None]
    allr = [x["n1_chg"] for x in rows if x["n1_chg"] is not None]

    return {
        "sid": sid,
        "name": name,
        "n": n,
        "one_word": ow,
        "open_limit": ol,
        "unfillable": both,
        "all_avg": sum(allr) / len(allr) if allr else 0,
        "keep_avg": sum(keep) / len(keep) if keep else 0,
        "keep_n": len(keep),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="instock_low_backtrace_increase")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--start", default="2021-11-01")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()
    frame = store.scan().collect()
    frame = frame.filter(pl.col("date") >= pl.Date(2021, 11, 1) if False else pl.lit(True))
    engine = StrategyEngine(config.STRATEGY_DIR)

    if args.all:
        targets = sorted(engine.registry.keys())
    else:
        targets = [args.strategy]

    print(f"{'策略':<34}{'信号':>7}{'一字板':>8}{'开盘涨停':>10}{'买不到':>9}{'占比':>8}{'次日均值':>10}{'剔除后':>10}")
    print("-" * 100)
    results = []
    for sid in targets:
        spec = engine.registry[sid]
        r = analyse(sid, spec.name, frame, engine)
        if not r:
            continue
        results.append(r)
        pct = r["unfillable"] / r["n"] * 100
        print(
            f"{r['name']:<34}{r['n']:>7}{r['one_word']:>8}{r['open_limit']:>10}"
            f"{r['unfillable']:>9}{pct:>7.1f}%{r['all_avg']:>+9.2f}%{r['keep_avg']:>+9.2f}%"
        )

    if results and not args.all:
        r = results[0]
        print()
        print("解读：")
        print(f"  信号总数 {r['n']}，其中买不到的 {r['unfillable']} 笔（{r['unfillable']/r['n']*100:.1f}%）")
        print(f"  全样本次日均值 {r['all_avg']:+.2f}%")
        print(f"  剔掉买不到的之后 {r['keep_avg']:+.2f}%（{r['keep_n']} 笔）")
        print(f"  虚高 {r['all_avg'] - r['keep_avg']:+.2f} 个百分点")


if __name__ == "__main__":
    main()
