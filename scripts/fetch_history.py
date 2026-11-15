"""拉真实历史日线，落成与 facet 一致的 parquet，并发布代际。

数据源：东方财富 push2his（和你终端、InStock 用的是同一个上游）。
股票池：东财全市场快照，按「短线可交易」硬性过滤后抽样本
        （排除 ST / 北交所 / 低价 / 低流动性）。

跑法：
    python scripts/fetch_history.py                 # 默认 600 只 + 260 根
    python scripts/fetch_history.py --limit 80 --bars 120
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.indicators import build_enriched  # noqa: E402
from app.store import GenerationStore  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SNAPSHOT_URL = (
    "http://82.push2.eastmoney.com/api/qt/clist/get"
    "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
    "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    "&fields=f12,f14,f2,f3,f5,f6,f7,f8,f13,f20,f21"
)
KLINE_URL = (
    "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt=101&fqt={fqt}&end=20500101&lmt={bars}"
)


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def secid(code: str) -> str:
    return f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"


def snapshot() -> list[dict]:
    """全市场快照，100 条一页翻到底（东财单页上限就是 100）。"""
    out: list[dict] = []
    total = None
    for pn in range(1, 80):
        try:
            data = get_json(SNAPSHOT_URL.format(pn=pn)).get("data") or {}
        except Exception as exc:
            print(f"  ! snapshot pn={pn}: {exc}")
            break
        if total is None:
            total = data.get("total")
        rows = data.get("diff") or []
        if not rows:
            break
        for r in rows:
            code = str(r.get("f12") or "")
            name = str(r.get("f14") or "")
            if not code or not name:
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            if r.get("f13") == 1 and not code.startswith(("6", "9")):  # 北交所
                continue
            out.append(
                {
                    "code": code,
                    "name": name,
                    "price": r.get("f2"),
                    "change_pct": r.get("f3"),
                    "amount": r.get("f6"),
                    "free_cap": r.get("f21"),
                }
            )
        if len(rows) < 100:
            break
    print(f"  快照拉到 {len(out)} 只（东财声明总数 {total}）")
    return out


def fetch_bars(code: str, bars: int, fqt: int = 2) -> list[dict]:
    """通过 DataProvider 取日线（V2：供应商逻辑下沉到 infrastructure/data_sources）。

    fqt: 0=不复权 1=前复权 2=后复权
         长周期必须用后复权 —— 前复权在分红送转多的股票上会把早期价格减成负数
         （实测茅台 2005 年 -302 元）。
    """
    from app.infrastructure.data_sources.base import Capability
    from app.infrastructure.data_sources.registry import registry

    adjust = {0: "none", 1: "forward", 2: "back"}[fqt]
    symbol = f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"
    provider = registry.best(Capability.DAILY)
    bars_out = provider.fetch_daily(symbol, bars=bars, adjust=adjust)

    rows = []
    for b in bars_out:
        rows.append(
            {
                "symbol": symbol,
                "date": date.fromisoformat(b.date),
                "open": b.open,
                "close": b.close,
                "high": b.high,
                "low": b.low,
                "volume": b.volume / 100,   # 落盘仍用「手」，与既有 parquet 保持一致
                "amount": b.amount,
                "amplitude": b.amplitude,
                "change_pct": b.change_pct,
                "turnover": b.turnover,
            }
        )
    return rows


def fetch_benchmark(bars: int, fqt: int = 2) -> pl.DataFrame:
    """上证指数当日涨跌幅，供 rs 因子用。"""
    payload = get_json(KLINE_URL.format(secid="1.000001", bars=bars, fqt=fqt))
    rows = []
    for line in ((payload.get("data") or {}).get("klines") or []):
        p = line.split(",")
        if len(p) >= 9:
            rows.append({"date": date.fromisoformat(p[0]), "benchmark": float(p[8])})
    return pl.DataFrame(rows)


def _num(value) -> float:
    """东财停牌/无数据字段会返回 '-'，统一折成 0。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--bars", type=int, default=260)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument(
        "--fqt",
        type=int,
        default=2,
        choices=[0, 1, 2],
        help="0=不复权 1=前复权 2=后复权（长周期必须用 2，前复权会把早期价格减成负数）",
    )
    args = ap.parse_args()

    rng = random.Random(20261109)
    config.ensure_dirs()

    print("拉全市场快照 …")
    snap = snapshot()
    pool = [
        s
        for s in snap
        if _num(s["price"]) >= 3
        and _num(s["amount"]) >= 1e8
        and _num(s["free_cap"]) >= 1e9
    ]
    print(f"快照 {len(snap)} 只，硬性过滤后 {len(pool)} 只，抽样 {args.limit}")
    rng.shuffle(pool)
    picked = pool[: args.limit]

    print(f"拉日线（{args.bars} 根，{args.workers} 并发）…")
    t0 = time.time()
    rows: list[dict] = []
    done = 0

    def work(item: dict) -> list[dict]:
        try:
            return fetch_bars(item["code"], args.bars, args.fqt)
        except Exception as exc:
            print(f"  ! {item['code']} {exc}")
            return []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for batch in ex.map(work, picked):
            rows.extend(batch)
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(picked)}  {len(rows)} 行  {time.time()-t0:.0f}s")

    if not rows:
        raise SystemExit("没拉到任何数据")

    daily = pl.DataFrame(rows).sort(["symbol", "date"])
    bench = fetch_benchmark(args.bars, args.fqt)
    daily = daily.join(bench, on="date", how="left")

    daily_path = config.DATA_DIR / "daily" / "daily.parquet"
    daily.write_parquet(daily_path, compression="lz4")
    print(f"\ndaily -> {daily_path}  rows={daily.height}  symbols={daily['symbol'].n_unique()}")
    print(f"日期范围: {daily['date'].min()} ~ {daily['date'].max()}")

    enriched = build_enriched(daily)
    store = GenerationStore(config.DATA_DIR)
    gen = store.publish(enriched)
    print(f"enriched 代际 {gen.generation[:8]}  rows={enriched.height}")


if __name__ == "__main__":
    main()
