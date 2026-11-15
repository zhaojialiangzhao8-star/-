"""造 mock 日线数据 -> 算 enriched -> 原子发布一个代际。

跑法：
    python scripts/gen_data.py              # 造一份新代际
    python scripts/gen_data.py --symbols 80 --days 400

数据是合成的（随机游走 + 中周期趋势 + 偶发放量），只用于把管道跑通，
不代表任何真实行情。
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.indicators import build_enriched  # noqa: E402
from app.store import GenerationStore  # noqa: E402

SEED = 20261109


def trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def make_symbols(count: int) -> list[str]:
    symbols: list[str] = []
    for i in range(count):
        n = i + 1
        if i % 3 == 0:
            symbols.append(f"600{n:03d}.SH")
        elif i % 3 == 1:
            symbols.append(f"000{n:03d}.SZ")
        else:
            symbols.append(f"300{n:03d}.SZ")
    return symbols


def build_daily(symbols: list[str], days: list[date], rng: random.Random) -> pl.DataFrame:
    rows: list[dict] = []
    for sym in symbols:
        price = rng.uniform(4, 60)
        phase = rng.uniform(0, math.tau)
        period = rng.uniform(30, 90)
        drift = rng.uniform(-0.0004, 0.0009)
        for i, day in enumerate(days):
            trend = math.sin(phase + i / period * math.tau) * 0.004
            shock = rng.gauss(0, 0.017)
            ret = drift + trend + shock
            price = max(1.2, price * (1 + ret))
            volume = rng.uniform(3e6, 2.5e7) * (1 + abs(ret) * 12)
            high = price * (1 + abs(rng.gauss(0, 0.008)))
            low = price * (1 - abs(rng.gauss(0, 0.008)))
            rows.append(
                {
                    "symbol": sym,
                    "date": day,
                    "open": round(price * (1 + rng.gauss(0, 0.004)), 2),
                    "high": round(max(high, price), 2),
                    "low": round(min(low, price), 2),
                    "close": round(price, 2),
                    "volume": round(volume, 0),
                    "amount": round(price * volume, 0),
                }
            )
    return pl.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, default=40)
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--start", default="2025-01-02")
    args = parser.parse_args()

    rng = random.Random(SEED)
    config.ensure_dirs()
    days = trading_days(date.fromisoformat(args.start), args.days)
    daily = build_daily(make_symbols(args.symbols), days, rng)

    daily_path = config.DATA_DIR / "daily" / "daily.parquet"
    daily.write_parquet(daily_path)
    print(f"daily   -> {daily_path}  rows={daily.height} symbols={daily['symbol'].n_unique()}")

    enriched = build_enriched(daily)
    store = GenerationStore(config.DATA_DIR)
    gen = store.publish(enriched)
    print(f"enriched-> 代际 {gen.generation[:8]}  rows={enriched.height}  {gen.updated_text}")
    print(f"日期范围: {enriched['date'].min()} ~ {enriched['date'].max()}")
    print(f"代际历史: {[g['generation'][:8] for g in store.history()]}")


if __name__ == "__main__":
    main()
