"""把 enriched 数据裁到近 N 年，生成新代际（不动原数据，可回滚）。

为什么：全量是 2008-2026 共 1,095 万行，解码进内存 2.0 GB。用户只需要近 5 年
回测，18 年历史除了让每次启动都多解码 1 GB，没有任何用处。

裁剪规则：
  · 保留 `--years` 年 + `--warmup` 天预热。预热是必须的：回测 5 年，
    第一天的 MA250 也要有 250 天数据才算得出来。
  · 换手代理、均线这些派生列已经在 indicators.py 里算好落盘，这里只做行过滤，
    不重算任何指标（AGENTS.md 第 1 条：不要在策略里重算派生列）。

默认：5 年 + 300 天预热 → 2020-01 至今，约 620 万行（原来的 57%）。

跑法：
    python scripts/trim_generation.py --years 5
    python scripts/trim_generation.py --years 5 --dry-run
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5, help="保留最近多少年的回测区间")
    ap.add_argument("--warmup", type=int, default=300, help="额外往前的预热天数（覆盖 MA250）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = GenerationStore(config.DATA_DIR)
    gen = store.require()
    print(f"当前代际 {gen.generation[:8]} · {gen.rows:,} 行")

    df = store.collect()
    dmin, dmax = df["date"].min(), df["date"].max()
    print(f"日期范围 {dmin} ~ {dmax}")

    cut = date.today() - timedelta(days=365 * args.years + args.warmup)
    kept = df.filter(pl.col("date") >= cut)
    print(f"裁剪到 >= {cut}  （{args.years} 年 + {args.warmup} 天预热）")
    print(f"  保留 {kept.height:,} 行（{kept.height / df.height * 100:.0f}%）· "
          f"{kept['symbol'].n_unique():,} 只 · {kept['date'].n_unique():,} 个交易日")
    print(f"  丢弃 {df.height - kept.height:,} 行")

    if args.dry_run:
        print("\ndry-run，没有写入。去掉 --dry-run 才会真的生成新代际。")
        return

    del df

    # 写临时 parquet，再走 publish_file（搬文件，不重新编码 DataFrame）
    tmp = Path(config.DATA_DIR) / "_trim_tmp.parquet"
    kept.write_parquet(tmp)
    del kept
    rows = pl.scan_parquet(tmp).select(pl.len()).collect()[0, 0]
    print(f"  临时文件 {tmp.stat().st_size / 1024**2:.0f} MB · {rows:,} 行")

    new_gen = store.publish_file(tmp, rows=int(rows))
    print(f"\n新代际 {new_gen.generation[:8] if hasattr(new_gen, 'generation') else new_gen}")
    print("服务重启后会自动用新代际。旧代际保留，想回滚就改 CURRENT.json。")

    # 清理旧代际（KEEP_GENERATIONS 默认 1，这里显式提示一下磁盘占用）
    gens_dir = Path(config.DATA_DIR) / "enriched" / "gens"
    total = sum(f.stat().st_size for f in gens_dir.rglob("*") if f.is_file())
    print(f"gens 目录当前 {total / 1024**2:.0f} MB")


if __name__ == "__main__":
    main()
