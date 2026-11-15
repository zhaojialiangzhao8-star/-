"""从通达信 gbbq（除权除息）算后复权因子（向量化版，几秒跑完）。

gbbq 字段（pytdx 解出）：
  hongli_panqianliutong  每 10 股分红（元）
  peigujia_qianzongguben 配股价（元）
  songgu_qianzongguben   每 10 股送转（股）
  peigu_houzongguben     每 10 股配股（股）

除权日 d 的价格连续条件：
  ratio = (prev_raw + (peigu/10)*配股价 - hongli/10) / (prev_raw * (1 + (songgu+peigu)/10))
  factor(d) = factor(d-1) / ratio      # 后复权：越往后因子越大
  adj_close = raw_close * factor

用法：
    python scripts/build_adj_factors.py --check
    python scripts/build_adj_factors.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

GBBQ = Path(r"D:\new_tdx\T0002\hq_cache\gbbq")


def load_events() -> pl.DataFrame:
    from pytdx.reader.gbbq_reader import GbbqReader

    ev = pl.from_pandas(GbbqReader().get_df(str(GBBQ)))
    ev = ev.filter(pl.col("category") == 1)
    ev = ev.with_columns(
        pl.col("code").cast(pl.Utf8),
        pl.col("market").cast(pl.Int32),
        pl.col("datetime").cast(pl.Utf8),
    )
    ev = ev.with_columns(
        pl.concat_str(
            [
                pl.col("datetime").str.slice(0, 4),
                pl.lit("-"),
                pl.col("datetime").str.slice(4, 2),
                pl.lit("-"),
                pl.col("datetime").str.slice(6, 2),
            ]
        )
        .str.to_date()
        .alias("date")
    )
    mkt = pl.when(pl.col("market") == 1).then(pl.lit("SH")).otherwise(pl.lit("SZ"))
    return ev.with_columns((pl.col("code") + "." + mkt).alias("symbol")).select(
        [
            "symbol",
            "date",
            pl.col("hongli_panqianliutong").alias("hongli"),
            pl.col("peigujia_qianzongguben").alias("peigu_price"),
            pl.col("songgu_qianzongguben").alias("songgu"),
            pl.col("peigu_houzongguben").alias("peigu"),
        ]
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    ev = load_events()
    print(f"除权除息事件 {ev.height:,} 条 / {ev['symbol'].n_unique():,} 只")

    px = (
        pl.read_parquet(Path(config.DATA_DIR) / "vipdoc_daily.parquet", columns=["symbol", "date", "close"])
        .filter(pl.col("symbol").is_in(ev["symbol"].unique().to_list()))
        .sort(["symbol", "date"])
    )

    ev2 = ev.sort(["symbol", "date"]).join_asof(
        px.select(["symbol", "date", "close"]).rename({"close": "prev_close"}),
        on="date",
        by="symbol",
        strategy="backward",
        allow_exact_matches=False,
    )
    ev2 = ev2.with_columns(
        (
            (pl.col("prev_close") + pl.col("peigu") / 10 * pl.col("peigu_price") - pl.col("hongli") / 10)
            / (pl.col("prev_close") * (1 + (pl.col("songgu") + pl.col("peigu")) / 10))
        ).alias("ratio")
    ).filter(pl.col("ratio") > 0)

    ev2 = ev2.with_columns((1.0 / pl.col("ratio")).alias("mult")).with_columns(
        pl.col("mult").cum_prod().over("symbol").alias("factor")
    )
    factors = ev2.select(["symbol", "date", "factor"])
    print(f"因子点 {factors.height:,} 个 / {factors['symbol'].n_unique():,} 只")

    if args.check:
        for sym in ("000001.SZ", "600519.SH"):
            sub = factors.filter(pl.col("symbol") == sym).sort("date")
            latest = px.filter(pl.col("symbol") == sym).sort("date").tail(1)
            if sub.height == 0 or latest.height == 0:
                print(f"{sym}: 无数据")
                continue
            f = sub["factor"][-1]
            print(f"{sym}: 最新原始价 {latest['close'][0]:.2f} × 因子 {f:.2f} = 后复权 {latest['close'][0] * f:.2f}")

    out = Path(config.DATA_DIR) / "adj_factors.parquet"
    factors.write_parquet(out, compression="lz4")
    print(f"写出 {out}（{out.stat().st_size / 1e6:.1f} MB）")


if __name__ == "__main__":
    main()
