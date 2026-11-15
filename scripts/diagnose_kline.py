"""诊断 K 线数据 —— 找出「显示不对」到底是哪里不对。

三种可能：
  A. 后复权量级问题：数据是 hfq，和行情软件的真实价差几倍（正常，但要确认倍数一致）
  B. 复权拼接跳变：除权日前后价格不连续，K 线出现断崖
  C. 数据本身错位：open/high/low/close 之间有矛盾（如 high < low）

用法：
    python scripts/diagnose_kline.py                 # 抽查 8 只
    python scripts/diagnose_kline.py --symbol 600519.SH
    python scripts/diagnose_kline.py --scan          # 全市场扫描异常
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

UA_TX = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://gu.qq.com/"}


def real_price(symbol: str) -> float | None:
    """腾讯实时价（真实价，不是复权价）。"""
    code, _, mkt = symbol.partition(".")
    p = "sh" if mkt.upper() in ("SH", "SS") else "sz"
    try:
        req = urllib.request.Request(f"https://qt.gtimg.cn/q={p}{code}", headers=UA_TX)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("gbk", errors="replace")
        f = raw.split('="', 1)[1].rstrip('";\n').split("~")
        return float(f[3])
    except Exception:  # noqa: BLE001
        return None


def check_symbol(df: pl.DataFrame, sym: str) -> dict:
    g = df.filter(pl.col("symbol") == sym).sort("date")
    if g.height < 10:
        return {"symbol": sym, "error": "数据不足"}

    closes = g["close"].to_list()
    highs = g["high"].to_list()
    lows = g["low"].to_list()
    opens = g["open"].to_list()
    dates = g["date"].to_list()

    # B. 复权跳变：单日涨跌幅绝对值 > 21%（A 股最大 20%）
    jumps = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        chg = closes[i] / closes[i - 1] - 1
        if abs(chg) > 0.21:
            jumps.append((dates[i], chg))

    # C. OHLC 内部矛盾
    bad_ohlc = 0
    for i in range(len(closes)):
        h, l, o, c = highs[i], lows[i], opens[i], closes[i]
        if h < l or o > h * 1.001 or o < l * 0.999 or c > h * 1.001 or c < l * 0.999:
            bad_ohlc += 1

    rp = real_price(sym)
    latest = closes[-1]
    factor = latest / rp if rp and rp > 0 else None

    return {
        "symbol": sym,
        "n": g.height,
        "range": (str(dates[0]), str(dates[-1])),
        "last_hfq": round(latest, 2),
        "real": rp,
        "factor": round(factor, 3) if factor else None,
        "min": round(min(lows), 2),
        "max": round(max(highs), 2),
        "jumps": len(jumps),
        "jump_sample": [(str(d), round(c * 100, 1)) for d, c in jumps[:3]],
        "bad_ohlc": bad_ohlc,
    }


def scan_anomalies(df: pl.DataFrame) -> None:
    """全市场扫描：找 OHLC 矛盾和日内跳变。"""
    print("=== 全市场扫描异常 ===")

    # OHLC 内部矛盾
    bad = df.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("open") > pl.col("high") * 1.001)
        | (pl.col("open") < pl.col("low") * 0.999)
        | (pl.col("close") > pl.col("high") * 1.001)
        | (pl.col("close") < pl.col("low") * 0.999)
    )
    print(f"  OHLC 内部矛盾的行: {bad.height:,}")
    if bad.height:
        for r in bad.head(10).iter_rows(named=True):
            print(
                f"    {r['symbol']} {r['date']}  O{r['open']:.2f} H{r['high']:.2f} "
                f"L{r['low']:.2f} C{r['close']:.2f}"
            )

    # 复权跳变
    x = df.sort(["symbol", "date"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_chg")
    )
    jumps = x.filter(pl.col("_chg").abs() > 0.21)
    print(f"\n  复权跳变（|涨跌| > 21%）的行: {jumps.height:,}")
    by_sym = jumps.group_by("symbol").agg(pl.len().alias("n")).sort("n", descending=True)
    print(f"  涉及 {by_sym.height} 只标的，最多的 10 只：")
    for r in by_sym.head(10).iter_rows(named=True):
        print(f"    {r['symbol']}  {r['n']} 次")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()
    cur = store.current()
    df = store.scan().select("symbol", "date", "open", "high", "low", "close").collect()
    print(f"代际 {cur.generation[:8]}  {df.height:,} 行  {df['symbol'].n_unique()} 只")
    print(f"日期 {df['date'].min()} ~ {df['date'].max()}")
    print()

    if args.scan:
        scan_anomalies(df)
        return

    syms = [args.symbol] if args.symbol else (
        df["symbol"].unique().sort().to_list()[: args.n]
    )

    print("=== 逐只检查（最后一个是实时价对比）===")
    print(
        f"{'代码':<13}{'行数':>6}{'后复权收':>11}{'真实价':>10}{'倍数':>8}"
        f"{'跳变':>6}{'坏OHLC':>8}"
    )
    print("-" * 76)
    for s in syms:
        r = check_symbol(df, s)
        if "error" in r:
            print(f"{s:<13}  {r['error']}")
            continue
        print(
            f"{r['symbol']:<13}{r['n']:>6}{r['last_hfq']:>11.2f}"
            f"{(r['real'] or 0):>10.2f}{(r['factor'] or 0):>8.3f}"
            f"{r['jumps']:>6}{r['bad_ohlc']:>8}"
        )
        if r["jump_sample"]:
            print(f"    跳变样本: {r['jump_sample']}")


if __name__ == "__main__":
    main()
