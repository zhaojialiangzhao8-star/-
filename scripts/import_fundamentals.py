"""把财报字段导入风险库（data/risk/fundamentals.json）。

两种输入都支持：
  1) JSON：{"600519.SH": {"deducted_net_profit": 7.4e10, "revenue": 1.5e11, ...}, ...}
  2) CSV ：symbol,deducted_net_profit,revenue,...（第一行表头，金额用「元」）

字段清单见 backend/app/modules/risk/rules.py 顶部注释；只认白名单字段，写错的字段会被忽略并提示。

用法：
  python scripts/import_fundamentals.py --json C:\\path\\fund.json
  python scripts/import_fundamentals.py --csv  C:\\path\\fund.csv
  python scripts/import_fundamentals.py --show          # 看当前导入情况
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.modules.risk import store as S  # noqa: E402

FIELDS = {
    "deducted_net_profit", "revenue", "net_assets", "audit_opinion",
    "fund_occupation", "fund_occupation_ratio", "rectified",
    "illegal_guarantee", "illegal_guarantee_ratio",
    "bank_account_frozen", "dishonest_debtor", "ic_nonstandard_years",
    "dividend_total_3y", "net_profit_3y", "profit_years", "rd_exempt",
    "goodwill", "goodwill_ratio", "pledge_ratio",
    "noncore_profit_ratio", "report_delayed", "fraud_major",
}
BOOL_FIELDS = {"rectified", "bank_account_frozen", "dishonest_debtor", "rd_exempt",
               "report_delayed", "fraud_major"}
INT_FIELDS = {"ic_nonstandard_years", "profit_years"}


def _clean(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if k not in FIELDS or v in (None, "", "--"):
            continue
        if k in BOOL_FIELDS:
            out[k] = str(v).strip().lower() in ("1", "true", "yes", "y", "是")
        elif k in INT_FIELDS:
            out[k] = int(float(v))
        else:
            out[k] = float(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--csv")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    cur = S.load_fundamentals()

    if args.show or (not args.json and not args.csv):
        print(f"已导入 {len(cur)} 只")
        for sym, rec in list(cur.items())[:5]:
            print(" ", sym, json.dumps(rec, ensure_ascii=False)[:120])
        if len(cur) < 1:
            print("（空）用 --json / --csv 导入；schema 见 app/modules/risk/rules.py")
        return 0

    incoming: dict[str, dict] = {}
    if args.json:
        data = json.loads(Path(args.json).read_text(encoding="utf-8-sig"))
        for sym, rec in data.items():
            incoming[sym.upper()] = _clean(rec)
    if args.csv:
        with Path(args.csv).open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                sym = (row.pop("symbol", "") or "").strip().upper()
                if sym:
                    incoming[sym] = _clean(row)

    if not incoming:
        print("没有可导入的数据")
        return 1

    cur.update({k: v for k, v in incoming.items() if v})
    S.FILES["fundamentals"].parent.mkdir(parents=True, exist_ok=True)
    S.FILES["fundamentals"].write_text(
        json.dumps(cur, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"导入 {len(incoming)} 只，累计 {len(cur)} 只 → {S.FILES['fundamentals']}")
    print("下一步：POST /api/v1/risk/refresh 让规则立刻生效（或等 10 秒后台刷新）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
