"""抓全市场 A 股代码 + 名称（新浪分页接口），产出 data/symbol_names.json。

东财 clist 现在 502、同花顺列表 401，新浪这个接口还能用：
    /Market_Center.getHQNodeData?page=N&num=100&node=hs_a
返回 [{symbol: "sh600519", code: "600519", name: "贵州茅台"}, ...]，含北交所（bj920xxx）。

用法：
    python scripts/fetch_symbol_list.py            # 抓全量并合并进 symbol_names.json
    python scripts/fetch_symbol_list.py --dry-run  # 只统计，不写文件
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

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://finance.sina.com.cn"}
URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a"
)


def norm(sym: str, code: str) -> str:
    """sh600519 → 600519.SH；bj920000 → 920000.BJ；sz000001 → 000001.SZ"""
    s = (sym or "").lower()
    c = code or s[2:] if len(s) > 2 else code
    if s.startswith("sh"):
        mkt = "SH"
    elif s.startswith("bj"):
        mkt = "BJ"
    else:
        mkt = "SZ"
    return f"{c}.{mkt}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-pages", type=int, default=80)
    args = ap.parse_args()

    found: dict[str, str] = {}
    for page in range(1, args.max_pages + 1):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(URL.format(page=page), headers=UA), timeout=20
            ).read().decode("utf-8", errors="replace")
            rows = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! page {page}: {exc}")
            break
        if not rows:
            break
        for r in rows:
            code, name = str(r.get("code") or ""), str(r.get("name") or "")
            if not code or not name:
                continue
            found[norm(str(r.get("symbol") or ""), code)] = name
        if page % 10 == 0:
            print(f"  page {page}: 累计 {len(found)} 只", flush=True)
        time.sleep(0.15)

    print(f"新浪全市场清单: {len(found)} 只")
    if args.dry_run:
        return

    path = Path(config.DATA_DIR) / "symbol_names.json"
    old: dict[str, str] = {}
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
    merged = {**old, **found}
    path.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"→ {path}  合并后 {len(merged)} 只（新增 {len(set(found) - set(old))}）")


if __name__ == "__main__":
    main()
