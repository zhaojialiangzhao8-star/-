"""基本面数据采集：东财 push2 单只快照

能拿到（已验证）：
    f43  现价
    f92  每股净资产（BPS）
    f116 总市值
    f117 流通市值
    f162 PE(TTM)
    f167 PB

历史序列的推导方式（业界常用近似）：
    假设股本和 BPS/EPS 在短期内不变
    市值(t) = 市值(now) × close(t) / close(now)
    PB(t)   = close(t) / BPS(now)
    PE(t)   = close(t) / EPS(now)
            其中 EPS(now) = close(now) / PE(now)

    局限：跨越财报期会失真（BPS/EPS 变了但没更新）
    对策：只用于「分位数排序」，不用于绝对值判断；且每年重算一次快照

数据量：4,800 只 × 单只请求，限速 0.35s → 约 30 分钟

输出：
    data/fundamentals_snapshot.parquet   （symbol, pe, pb, bps, eps, mcap, float_mcap, price）

用法：
    python scripts/fetch_fundamentals.py --limit 20    # 试 20 只
    python scripts/fetch_fundamentals.py               # 全量
    python scripts/fetch_fundamentals.py --resume
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
OUT = config.DATA_DIR / "fundamentals_snapshot.parquet"
CACHE = config.DATA_DIR / "_fund_cache"


def secid(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    return f"1.{code}" if mkt.upper() in ("SH", "SS") else f"0.{code}"


def fetch(symbol: str) -> dict | str:
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        f"secid={secid(symbol)}&fields=f43,f57,f58,f92,f116,f117,f162,f167,f168"
    )
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8")).get("data") or {}
            if not d or not d.get("f58"):
                return "empty"
            return {
                "symbol": symbol,
                "name": d.get("f58"),
                "price": d.get("f43"),
                "bps": d.get("f92"),
                "mcap": d.get("f116"),
                "float_mcap": d.get("f117"),
                "pe": d.get("f162"),
                "pb": d.get("f167"),
                "turnover_rate": d.get("f168"),
            }
        except urllib.error.HTTPError as exc:
            if attempt == 2:
                return f"HTTP {exc.code}"
            time.sleep(1.5 + attempt * 2)
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                return f"{type(exc).__name__}"
            time.sleep(1.5 + attempt * 2)
    return "unreachable"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    names = json.loads((config.DATA_DIR / "symbol_names.json").read_text(encoding="utf-8"))
    syms = sorted(names.keys())
    if args.limit:
        syms = syms[: args.limit]
    print(f"标的 {len(syms)}  并发 {args.workers}")

    CACHE.mkdir(parents=True, exist_ok=True)
    done = 0
    failed: list[tuple[str, str]] = []
    t0 = time.time()

    def work(sym: str):
        cf = CACHE / f"{sym}.json"
        if args.resume and cf.exists() and cf.stat().st_size > 20:
            return None
        r = fetch(sym)
        if isinstance(r, str):
            return (sym, r)
        cf.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        return None

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(work, syms):
            done += 1
            if res:
                failed.append(res)
            if done % 300 == 0:
                print(f"  {done}/{len(syms)}  {time.time()-t0:.0f}s  失败 {len(failed)}", flush=True)

    rows = []
    for sym in syms:
        cf = CACHE / f"{sym}.json"
        if not cf.exists():
            continue
        try:
            rows.append(json.loads(cf.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue

    if not rows:
        print("没有数据")
        return

    df = pl.DataFrame(rows)
    # 东财返回的 PE/PB 乘了 100（茅台 PE 2051 = 20.51），先还原
    # 然后清洗：PE 为负或 0 的置空（亏损股的 PE 没有意义）
    df = df.with_columns(
        (pl.col("pe").cast(pl.Float64) / 100).alias("pe_raw"),
        (pl.col("pb").cast(pl.Float64) / 100).alias("pb_raw"),
    ).with_columns(
        pl.when(pl.col("pe_raw") > 0).then(pl.col("pe_raw")).otherwise(None).alias("pe"),
        pl.when(pl.col("pb_raw") > 0).then(pl.col("pb_raw")).otherwise(None).alias("pb"),
    ).drop(["pe_raw", "pb_raw"])
    df.write_parquet(OUT, compression="lz4")

    print(f"\n→ {OUT}")
    print(f"  rows={df.height:,}  失败 {len(failed)}")
    for c in ("pe", "pb", "mcap", "bps"):
        nn = df[c].null_count()
        print(f"  {c:<12} 非空 {df.height - nn:,} / {df.height:,}  ({(1-nn/df.height)*100:.0f}%)")
    print(f"  PE 中位 {df['pe'].median()}")
    print(f"  PB 中位 {df['pb'].median()}")
    if df["mcap"].max():
        print(f"  市值范围 {df['mcap'].min():,.0f} ~ {df['mcap'].max():,.0f}")

    if failed:
        print(f"\n  失败 {len(failed)} 只（前 8）：")
        for s, e in failed[:8]:
            print(f"    {s}  {e}")
        print(f"  续跑：python scripts/fetch_fundamentals.py --resume")


if __name__ == "__main__":
    main()
