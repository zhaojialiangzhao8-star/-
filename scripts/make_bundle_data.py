"""从当前代际抽一份精简数据，作为桌面应用的内置样本。

为什么要内置：独立 app 不能依赖 facet 开发目录。抽 250 只 × 全部交易日，
体积约 15 MB，开箱即可跑全部 13 个策略；用户之后可以自己重新拉全量。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

SYMBOLS = 250
OUT = ROOT / "bundled"


def main() -> None:
    store = GenerationStore(config.DATA_DIR)
    frame = store.collect()
    syms = frame["symbol"].unique().sort().to_list()[:SYMBOLS]
    sub = frame.filter(pl.col("symbol").is_in(syms)).sort(["symbol", "date"])

    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    gens = OUT / "enriched" / "gens"
    gens.mkdir(parents=True, exist_ok=True)

    gen = uuid.uuid4().hex
    target = gens / gen
    target.mkdir(parents=True, exist_ok=True)
    sub.write_parquet(target / "data.parquet", compression="lz4")
    (target / "_SUCCESS").write_text(json.dumps({"rows": sub.height}), encoding="utf-8")
    (OUT / "enriched" / "CURRENT.json").write_text(
        json.dumps(
            {
                "generation": gen,
                "rows": sub.height,
                "updated_at": time.time(),
                "status": "ready",
            }
        ),
        encoding="utf-8",
    )
    for extra in ("daily", "gen", "runs"):
        (OUT / extra).mkdir(parents=True, exist_ok=True)

    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"bundled 样本 -> {OUT}")
    print(f"  {sub.height:,} 行 / {sub['symbol'].n_unique()} 只 / "
          f"{sub['date'].min()} ~ {sub['date'].max()}")
    print(f"  体积 {size/1024/1024:.1f} MB")


if __name__ == "__main__":
    main()
