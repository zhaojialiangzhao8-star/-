"""抓全市场股票名称，缓存成 JSON，给前端显示用。

数据里只有代码（600519.SH），没有名称。腾讯接口一次能拿 60 只，
4,800 只分 80 批，大约 10 秒。抓一次缓存到 data/symbol_names.json，
之后前端直接读，不再请求外部。

产物：
    backend/data/symbol_names.json   {"600519.SH": "贵州茅台", ...}

跑法：
    python scripts/fetch_symbol_names.py
    python scripts/fetch_symbol_names.py --force   # 忽略缓存重抓
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://gu.qq.com/"}
URL = "https://qt.gtimg.cn/q={codes}"
BATCH = 60


def tx_code(symbol: str) -> str:
    code, _, market = symbol.partition(".")
    m = market.upper()
    prefix = "sh" if m in ("SH", "SS") else "bj" if m == "BJ" else "sz"
    if not market:
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return f"{prefix}{code}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    out = Path(config.DATA_DIR) / "symbol_names.json"
    if out.exists() and not args.force:
        d = json.loads(out.read_text(encoding="utf-8"))
        print(f"已有缓存 {len(d)} 只 → {out}")
        print("加 --force 重抓")
        return

    store = GenerationStore(config.DATA_DIR)
    # GenerationStore 只有 collect/scan，标的清单从 parquet 里取一次
    symbols = sorted(store.scan().select("symbol").unique().collect()["symbol"].to_list())
    print(f"代际 {store.require().generation[:8]} · {len(symbols):,} 只待抓")

    names: dict[str, str] = {}
    t0 = time.time()
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i : i + BATCH]
        codes = ",".join(tx_code(s) for s in chunk)
        try:
            req = urllib.request.Request(URL.format(codes=codes), headers=UA)
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read().decode("gbk", errors="replace")
        except Exception as exc:  # noqa: BLE001
            print(f"  批次 {i//BATCH} 失败 {type(exc).__name__}", flush=True)
            continue
        for line in raw.splitlines():
            if '="' not in line:
                continue
            key = line.split('="', 1)[0].replace("v_", "").strip()
            body = line.split('="', 1)[1].rstrip('";').strip()
            f = body.split("~")
            if len(f) < 3:
                continue
            code = f[2]
            market = "SH" if key.startswith("sh") else "BJ" if key.startswith("bj") else "SZ"
            sym = f"{code}.{market}"
            name = f[1].strip()
            if name:
                names[sym] = name
        if (i // BATCH) % 10 == 9:
            print(f"  {i + len(chunk):,}/{len(symbols):,}  {time.time()-t0:.0f}s", flush=True)
        time.sleep(0.04)     # 腾讯不封，但别把它当无限接口

    out.write_text(json.dumps(names, ensure_ascii=False, indent=0), encoding="utf-8")
    missing = [s for s in symbols if s not in names]
    print(f"\n抓到 {len(names):,} 个名称 · 缺 {len(missing):,} 个 · {time.time()-t0:.0f}s")
    print(f"→ {out}  ({out.stat().st_size/1024:.0f} KB)")
    if missing[:5]:
        print("缺的样本:", missing[:5])


if __name__ == "__main__":
    main()
