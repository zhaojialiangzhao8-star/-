"""抓全市场「真实成交额」快照（新浪行情，未复权口径）。

为什么需要：库里的 `amount` = 后复权价 × 成交量，比真实成交额大几十倍，
页面上的「成交额 79.8 万亿」就是这么来的。这里单独存一份真实值，
只用于展示，不参与任何策略计算。

产物：data/real_amount/<YYYY-MM-DD>.json  →  {symbol: 成交额(元)}
跑法：python scripts/fetch_real_amount.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

CHUNK = 80
URL = "http://hq.sinajs.cn/list={codes}"
HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}


def to_sina(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    if mkt == "SH":
        return f"sh{code}"
    if mkt == "SZ":
        return f"sz{code}"
    return f"bj{code}"


def main() -> None:
    store = GenerationStore(config.DATA_DIR)
    names = (
        store.scan()
        .select("symbol")
        .unique()
        .collect()["symbol"]
        .to_list()
    )
    symbols = sorted(str(s) for s in names)
    print(f"全市场 {len(symbols)} 只", flush=True)

    amounts: dict[str, float] = {}
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        codes = ",".join(to_sina(s) for s in chunk)
        try:
            req = urllib.request.Request(URL.format(codes=codes), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=12) as resp:
                text = resp.read().decode("gbk", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            print(f"  块 {i} 失败 {type(exc).__name__}", flush=True)
            continue
        for line in text.split("\n"):
            if "=" not in line:
                continue
            head, _, payload = line.partition("=")
            sym = head.strip().replace("var hq_str_", "")
            fields = payload.strip().strip('";').split(",")
            if len(fields) < 10:
                continue
            try:
                amt = float(fields[9])       # 第 10 个字段是成交额（元）
            except ValueError:
                continue
            if amt > 0:
                code = sym[2:]
                suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym[:2], "")
                if suffix:
                    amounts[f"{code}.{suffix}"] = amt
        print(f"  {min(i + CHUNK, len(symbols))}/{len(symbols)}  已拿到 {len(amounts)}", flush=True)
        time.sleep(0.15)

    total = sum(amounts.values())
    out = Path(config.DATA_DIR) / "real_amount"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{date.today().isoformat()}.json"
    path.write_text(json.dumps(amounts, ensure_ascii=False), encoding="utf-8")
    print(f"\n覆盖 {len(amounts)} 只 · 全市场成交额 {total / 1e8:,.0f} 亿")
    print(f"→ {path}")


if __name__ == "__main__":
    main()
