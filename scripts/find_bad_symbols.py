"""找数据异常的股票 —— 后复权拼接错乱的那种。

症状（用户截图里的样子）：
    现价 575，但 MA5 = 78387、压力位 133395、3日涨跌 −99.4%
说明同一只股票的历史序列里混进了完全不同的价格量级。

成因：腾讯 hfq 接口对某些股票返回的是「分段拼接」的后复权，
某一段的复权基准和另一段不一致。东财 fqt=2 也可能有同样问题。

判据：一只票的收盘价最大 / 最小 > 30 倍，基本可以断定序列被污染。
      正常股票 5 年内涨 30 倍已经是极端（A 股极少），
      但复权错乱经常给出几百上千倍的比值。

用法：
    python scripts/find_bad_symbols.py            # 列出所有异常
    python scripts/find_bad_symbols.py --ratio 10 # 阈值调低
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ratio", type=float, default=30.0, help="最大/最小收盘价比值阈值")
    ap.add_argument("--top", type=int, default=40, help="列多少个")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()
    df = store.scan().select("symbol", "date", "close", "high", "low").collect()

    stat = (
        df.group_by("symbol")
        .agg(
            pl.col("close").max().alias("mx"),
            pl.col("close").min().alias("mn"),
            pl.len().alias("n"),
            pl.col("date").min().alias("d0"),
            pl.col("date").max().alias("d1"),
        )
        .with_columns((pl.col("mx") / pl.col("mn")).alias("ratio"))
        .sort("ratio", descending=True)
    )

    bad = stat.filter(pl.col("ratio") > args.ratio)
    print(f"总标的 {stat.height}  比值 > {args.ratio:g} 的有 {bad.height} 只 ({bad.height / stat.height * 100:.1f}%)")
    print()
    print(f"{'代码':<13}{'最大':>13}{'最小':>11}{'比值':>10}   {'行数':>5}  区间")
    for r in bad.head(args.top).iter_rows(named=True):
        print(
            f"{r['symbol']:<13}{r['mx']:>13.2f}{r['mn']:>11.2f}{r['ratio']:>10.1f}   "
            f"{r['n']:>5}  {r['d0']} ~ {r['d1']}"
        )

    if bad.height:
        print()
        print("示例：取第一只，看它在哪一天跳变")
        sym = bad["symbol"][0]
        g = df.filter(pl.col("symbol") == sym).sort("date")
        g = g.with_columns((pl.col("close") / pl.col("close").shift(1) - 1).alias("chg"))
        jumps = g.filter(pl.col("chg").abs() > 0.5)
        print(f"  {sym} 共 {g.height} 行，跳变日 {jumps.height} 个：")
        for r in jumps.head(10).iter_rows(named=True):
            print(f"    {r['date']}  close {r['close']:>12.2f}  chg {r['chg'] * 100:>+8.1f}%")


if __name__ == "__main__":
    main()
