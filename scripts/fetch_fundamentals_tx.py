"""基本面快照采集：腾讯 qt.gtimg.cn 批量（东财已被限流）

为什么换腾讯：
    东财 push2 全域名 RemoteDisconnected（IP 被封），单只 2 小时只拿到 10 只。
    腾讯 qt.gtimg.cn 批量 100 只只要 2.7 秒，字段齐全。

能拿到（已验证）：
    字段 1   名称
    字段 3   现价
    字段 39  PE(TTM)     亏损股为负
    字段 46  PB
    字段 44  流通市值（亿元）
    字段 45  总市值（亿元）
    字段 38  换手率
    字段 36  量比

数据量：4,800 只 / 100 每批 = 48 批 × 2.7s ≈ 3 分钟

输出：
    data/fundamentals_snapshot.parquet

用法：
    python scripts/fetch_fundamentals_tx.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
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
OUT = config.DATA_DIR / "fundamentals_snapshot.parquet"
BATCH = 100


def to_tx(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    m = mkt.upper()
    if m in ("SH", "SS"):
        return f"sh{code}"
    if m == "BJ":
        return f"bj{code}"
    if m == "SZ":
        return f"sz{code}"
    return f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"


def fetch_batch(codes: list[str]) -> list[dict]:
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("gbk", errors="replace")
            out = []
            for line in raw.strip().split(";"):
                line = line.strip()
                if not line or '="' not in line:
                    continue
                try:
                    f = line.split('="', 1)[1].rstrip('";\n').split("~")
                    if len(f) < 47:
                        continue

                    def num(idx):
                        try:
                            v = f[idx].strip()
                            return float(v) if v else None
                        except (ValueError, IndexError):
                            return None

                    out.append(
                        {
                            "code": f[2],
                            "name": f[1].strip(),
                            "price": num(3),
                            "pe": num(39),
                            "pb": num(46),
                            "float_mcap": num(44),   # 亿元
                            "mcap": num(45),         # 亿元
                            "turnover_rate": num(38),
                            "vol_ratio": num(36),
                        }
                    )
                except Exception:  # noqa: BLE001
                    continue
            return out
        except urllib.error.HTTPError as exc:
            if attempt == 2:
                return []
            time.sleep(2 + attempt * 2)
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return []
            time.sleep(2 + attempt * 2)
    return []


def main() -> None:
    names = json.loads((config.DATA_DIR / "symbol_names.json").read_text(encoding="utf-8"))
    syms = sorted(names.keys())
    print(f"标的 {len(syms)}  每批 {BATCH}")

    rows: list[dict] = []
    t0 = time.time()
    for i in range(0, len(syms), BATCH):
        chunk = syms[i : i + BATCH]
        codes = [to_tx(s) for s in chunk]
        got = fetch_batch(codes)
        # 用 secid 反查 symbol
        by_code = {c[2:]: s for c, s in zip(codes, chunk)}
        for g in got:
            sym = by_code.get(g["code"])
            if sym:
                g["symbol"] = sym
                rows.append(g)
        done = min(i + BATCH, len(syms))
        if (i // BATCH) % 8 == 0:
            print(f"  {done}/{len(syms)}  {time.time()-t0:.0f}s  拿到 {len(rows)}", flush=True)
        time.sleep(0.4)

    if not rows:
        print("没有数据")
        return

    df = pl.DataFrame(rows).select(
        "symbol", "name", "price", "pe", "pb", "float_mcap", "mcap", "turnover_rate", "vol_ratio"
    )
    # 清洗：PE ≤ 0 置空（亏损股的 PE 没有意义）；PB ≤ 0 置空
    df = df.with_columns(
        pl.when(pl.col("pe") > 0).then(pl.col("pe")).otherwise(None).alias("pe"),
        pl.when(pl.col("pb") > 0).then(pl.col("pb")).otherwise(None).alias("pb"),
        # 亿元 → 元
        (pl.col("mcap") * 1e8).alias("mcap_yuan"),
        (pl.col("float_mcap") * 1e8).alias("float_mcap_yuan"),
    )
    df.write_parquet(OUT, compression="lz4")

    print(f"\n→ {OUT}")
    print(f"  rows={df.height:,}  {df['symbol'].n_unique()} 只  {time.time()-t0:.0f}s")
    for c in ("pe", "pb", "mcap", "float_mcap"):
        nn = df[c].null_count()
        print(f"  {c:<12} 非空 {df.height-nn:,} / {df.height:,}  ({(1-nn/df.height)*100:.0f}%)")
    print(f"  PE 中位 {df['pe'].median()}")
    print(f"  PB 中位 {df['pb'].median()}")
    print(f"  市值中位 {df['mcap'].median():,.0f} 亿")


if __name__ == "__main__":
    main()
