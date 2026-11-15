"""采集基准指数日线（沪深300 / 上证指数 / 中证500），存成 parquet。

为什么需要：现在所有回测都是绝对收益，无法区分"选股 alpha"和"市场 beta"。
评策略必须先有基准。

输出：backend/data/benchmark/{code}.parquet
字段：date, open, high, low, close, volume, amount, change_pct
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
from app import config  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0"}
URL = (
    "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt=101&fqt=0&end=20500101&lmt=5000"
)

BENCHMARKS = {
    "000300": ("1.000300", "沪深300"),
    "000001": ("1.000001", "上证指数"),
    "000905": ("1.000905", "中证500"),
    "399006": ("0.399006", "创业板指"),
}


def fetch(secid: str) -> pl.DataFrame:
    req = urllib.request.Request(URL.format(secid=secid), headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        payload = json.loads(r.read().decode("utf-8"))
    data = payload.get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 11:
            continue
        rows.append(
            {
                "date": p[0],
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]), "amount": float(p[6]),
                "change_pct": float(p[8]),
            }
        )
    return pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())


def main() -> None:
    out = config.DATA_DIR / "benchmark"
    out.mkdir(parents=True, exist_ok=True)
    for code, (secid, name) in BENCHMARKS.items():
        try:
            df = fetch(secid)
            path = out / f"{code}.parquet"
            df.write_parquet(path, compression="lz4")
            print(f"  {name:8} {code}  {df.height:>5} 根  {df['date'].min()} ~ {df['date'].max()}  → {path.name}")
        except Exception as exc:
            print(f"  {name:8} {code}  失败: {exc}")
    print(f"\n输出目录: {out}")


if __name__ == "__main__":
    main()
