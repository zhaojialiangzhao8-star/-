"""Step 0：补数据到 5 年（腾讯分段拉取）

为什么分段：
    腾讯 ifzq 不带日期时只给最近 ~641 根（1.5 年）。
    但实测 param 支持 `code,day,start,end,count,fq` 格式，
    加上日期区间就能拿到对应那段的数据。

    所以分 5 段拉：2021 / 2022 / 2023 / 2024 / 2025-2026，
    每段一次请求，拼接后覆盖 2021-01 至今约 1,400 根。

为什么不用东财：
    2026-11-12 实测 push2his 全部 RemoteDisconnected（限流），
    之前 63 组回测 + 多次采集把配额用光了。腾讯 ifzq 目前仍可用。

输出：
    data/daily/daily_5y.parquet
    逐只缓存 data/daily/_5y_cache/{symbol}.json（支持 --resume）

用法：
    python scripts/fetch_5y.py --limit 50      # 试 50 只
    python scripts/fetch_5y.py                 # 全量
    python scripts/fetch_5y.py --resume        # 断点续跑
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}
URLS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={p}",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={p}",
]

# 五段，覆盖 2021-01 至今
SEGMENTS = [
    ("2021-01-01", "2021-12-31"),
    ("2022-01-01", "2022-12-31"),
    ("2023-01-01", "2023-12-31"),
    ("2024-01-01", "2024-12-31"),
    ("2025-01-01", "2026-12-31"),
]

OUT = config.DATA_DIR / "daily" / "daily_5y.parquet"
CACHE = config.DATA_DIR / "daily" / "_5y_cache"


def tx_code(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    m = mkt.upper()
    if m in ("SH", "SS"):
        return f"sh{code}"
    if m == "BJ":
        return f"bj{code}"
    if m == "SZ":
        return f"sz{code}"
    return f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"


def _req(param: str) -> dict | None:
    for tmpl in URLS:
        url = tmpl.format(p=param)
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429, 501):
                    break
                time.sleep(0.4)
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
    return None


def fetch_symbol(symbol: str, fq: str = "hfq") -> list[dict] | str:
    code = tx_code(symbol)
    all_rows: dict[str, dict] = {}

    for beg, end in SEGMENTS:
        payload = _req(f"{code},day,{beg},{end},640,{fq}")
        if payload is None:
            continue
        data = (payload.get("data") or {}).get(code) or {}
        key = f"{fq}day" if f"{fq}day" in data else "day"
        for p in data.get(key) or []:
            if not p or len(p) < 6:
                continue
            try:
                dt = date.fromisoformat(str(p[0])[:10])
            except ValueError:
                continue
            # 腾讯列序：[日期, 开, 收, 高, 低, 量]
            o, c, h, lo, v = (
                float(p[1] or 0),
                float(p[2] or 0),
                float(p[3] or 0),
                float(p[4] or 0),
                float(p[5] or 0),
            )
            if c <= 0 or v <= 0:
                continue
            all_rows[dt.isoformat()] = {
                "symbol": symbol,
                "date": dt.isoformat(),
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": v,
                "amount": round(c * v * 100, 2),   # 腾讯不给成交额，用均价近似
            }

    if not all_rows:
        return "no data"
    return sorted(all_rows.values(), key=lambda x: x["date"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--fq", default="hfq", choices=["hfq", "qfq", ""])
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--rebuild-only", action="store_true", help="只用已有缓存重建 parquet，不发新请求")
    args = ap.parse_args()

    names = json.loads((config.DATA_DIR / "symbol_names.json").read_text(encoding="utf-8"))
    syms = sorted(names.keys())
    if args.limit:
        syms = syms[: args.limit]
    print(f"标的 {len(syms)}  分段 {len(SEGMENTS)}  并发 {args.workers}  复权 {args.fq or '不复权'}")

    CACHE.mkdir(parents=True, exist_ok=True)
    done = 0
    failed: list[tuple[str, str]] = []
    t0 = time.time()

    def work(sym: str):
        cf = CACHE / f"{sym}.json"
        if args.resume and cf.exists() and cf.stat().st_size > 100:
            return None
        r = fetch_symbol(sym, args.fq)
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
                print(f"  {done}/{len(syms)}  {time.time() - t0:.0f}s  失败 {len(failed)}", flush=True)

    rows: list[dict] = []
    for sym in syms:
        cf = CACHE / f"{sym}.json"
        if not cf.exists():
            continue
        try:
            for x in json.loads(cf.read_text(encoding="utf-8")):
                x["date"] = date.fromisoformat(x["date"])
                rows.append(x)
        except Exception:  # noqa: BLE001
            continue

    if not rows:
        print("没有数据")
        return

    df = pl.DataFrame(rows).sort(["symbol", "date"])

    # 基准：沪深300
    bench = fetch_symbol("000300.SH", args.fq)
    if isinstance(bench, list) and bench:
        bdf = pl.DataFrame(
            [{"date": date.fromisoformat(r["date"]), "benchmark": r["close"]} for r in bench],
            schema={"date": pl.Date, "benchmark": pl.Float64},
        )
        df = df.join(bdf, on="date", how="left")
    else:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("benchmark"))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUT, compression="lz4")
    print(f"\n→ {OUT}")
    print(f"  rows={df.height:,}  symbols={df['symbol'].n_unique()}  失败 {len(failed)}")
    print(f"  日期 {df['date'].min()} ~ {df['date'].max()}")
    per = df.group_by("symbol").agg(pl.len().alias("n"))
    print(f"  每只：最少 {per['n'].min()}  中位 {int(per['n'].median())}  最多 {per['n'].max()}")
    byd = df.group_by("date").agg(pl.len().alias("n")).sort("date")
    thin = byd.filter(pl.col("n") < 1000)
    print(f"  交易日 {byd.height}  不足 1000 只的 {thin.height} 个")

    if failed:
        print(f"\n  失败 {len(failed)} 只（前 10）：")
        for s, e in failed[:10]:
            print(f"    {s}  {e}")
        print(f"  续跑：python scripts/fetch_5y.py --resume")


if __name__ == "__main__":
    main()
