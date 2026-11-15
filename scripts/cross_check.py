"""双源交叉校验：同一批股票，用第二个数据源核对当天的涨跌幅。

为什么比对比收盘价更靠谱：
  各家的复权基准不同（东财后复权 / 腾讯 hfq / 新浪前复权），收盘价没法直接比；
  但「当天涨跌幅」是基准无关的 —— 两边对同一根 K 线的涨跌幅必须一致。

用法：
    python scripts/cross_check.py                    # 最新日 vs 腾讯
    python scripts/cross_check.py --date 2026-11-12 --sample 300
    python scripts/cross_check.py --source eastmoney --tol 0.3
"""
from __future__ import annotations

import argparse
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.infrastructure.data_sources.base import Capability  # noqa: E402
from app.infrastructure.data_sources.registry import registry  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="YYYY-MM-DD，默认库里最新日")
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--source", default="tencent-daily")
    ap.add_argument("--tol", type=float, default=0.3, help="允许的涨跌幅偏差（百分点）")
    ap.add_argument("--max-bad-rate", type=float, default=0.01, help="超过这个比例就判失败")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    store.require()

    lf = store.scan()
    latest = str(lf.select(pl.col("date").max()).collect()[0, 0])
    day = args.date or latest

    snap = (
        lf.filter(pl.col("date") == pl.lit(day).str.to_date())
        .select(["symbol", "change_pct"])
        .collect()
    )
    if snap.height == 0:
        raise SystemExit(f"{day} 没有数据")

    rng = random.Random(20261115)
    pool = sorted(snap["symbol"].to_list())
    picked = rng.sample(pool, min(args.sample, len(pool)))
    local = {r["symbol"]: r["change_pct"] for r in snap.iter_rows(named=True)}

    print(f"校验 {day}：库里 {snap.height:,} 只，抽样 {len(picked)} 只，对照源 {args.source}")

    provider = registry.get(args.source)

    def fetch(sym: str):
        try:
            bars = provider.fetch_daily(sym, bars=20, adjust="back")
        except Exception:  # noqa: BLE001
            return None
        for b in bars:
            if str(b.date)[:10] == day:
                return (sym, float(b.change_pct))
        return None

    rows = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(fetch, picked):
            if res:
                rows.append(res)

    if not rows:
        raise SystemExit("对照源一条都没取到（可能被限流），无法校验")

    bad = []
    diffs = []
    for sym, remote in rows:
        mine = local.get(sym)
        if mine is None:
            continue
        d = abs(float(mine) - remote)
        diffs.append(d)
        if d > args.tol:
            bad.append((sym, float(mine), remote, d))

    n = len(diffs)
    rate = len(bad) / n if n else 0
    diffs_sorted = sorted(diffs)
    p50 = diffs_sorted[len(diffs_sorted) // 2] if diffs_sorted else 0
    p95 = diffs_sorted[int(len(diffs_sorted) * 0.95)] if diffs_sorted else 0

    print(f"\n比对 {n} 只：中位偏差 {p50:.3f}pp · P95 {p95:.3f}pp · 超 {args.tol}pp 的 {len(bad)} 只（{rate:.2%}）")
    for sym, mine, remote, d in sorted(bad, key=lambda x: -x[3])[:10]:
        print(f"  {sym}  库里 {mine:+.2f}%  对照 {remote:+.2f}%  差 {d:.2f}pp")

    if rate > args.max_bad_rate:
        print(f"\n[FAIL] 偏差率 {rate:.2%} > {args.max_bad_rate:.0%}，数据不可信")
        raise SystemExit(1)
    print("\n[OK] 两源一致")


if __name__ == "__main__":
    main()
