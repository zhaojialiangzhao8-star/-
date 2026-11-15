"""风险规则求值 —— 每 10 秒由后台线程跑一次（便宜规则），财报规则跟着 fundamentals.json 变。

设计要点：
  · 可自动判定的（ST 名称、股价 < 1 元、市值 < 5/3 亿）直接算；
  · 需要财报字段的，缺字段时**不判通过**，记为 unknown 并在接口里暴露"数据缺口"；
  · 「连续 20 个交易日」规则靠 store 里按天累积的快照，不足 20 天时给进度。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from ...config import DATA_DIR
from . import rules as R
from . import store as S

SNAPSHOT_PARQUET = DATA_DIR / "fundamentals_snapshot.parquet"
NAMES_JSON = DATA_DIR / "symbol_names.json"

_WINDOW = 20  # 「连续 20 个交易日」

# 快照缓存：只有 parquet 变了才重读（每 10 秒一轮，不能每轮都读盘）
_SNAP_CACHE: dict[str, object] = {"mtime": None, "rows": []}


def _load_names() -> dict[str, str]:
    import json

    if not NAMES_JSON.exists():
        return {}
    try:
        with NAMES_JSON.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k).upper(): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        return {}


def _load_snapshot() -> list[dict]:
    """基本面快照：现价 / 总市值 / 名称。没有就返回空（规则记 unknown）。"""
    if not SNAPSHOT_PARQUET.exists():
        return []
    mtime = SNAPSHOT_PARQUET.stat().st_mtime
    if _SNAP_CACHE["mtime"] == mtime:
        return _SNAP_CACHE["rows"]  # type: ignore[return-value]
    try:
        df = pl.read_parquet(SNAPSHOT_PARQUET)
    except Exception:  # noqa: BLE001
        return []
    cols = df.columns
    keep = [c for c in ("symbol", "name", "price", "mcap_yuan", "mcap") if c in cols]
    df = df.select(keep)
    rows = []
    for r in df.iter_rows(named=True):
        mcap = r.get("mcap_yuan") or r.get("mcap")
        rows.append(
            {
                "symbol": str(r["symbol"]).upper(),
                "name": r.get("name") or "",
                "price": float(r["price"]) if r.get("price") is not None else None,
                "mcap": float(mcap) if mcap is not None else None,
            }
        )
    _SNAP_CACHE["mtime"] = mtime
    _SNAP_CACHE["rows"] = rows
    return rows


def _streak(snapshots: dict, field: str, ok, symbol: str) -> tuple[int, int]:
    """返回 (连续满足天数, 已积累天数)。从最近往前数。"""
    days = sorted(snapshots.keys())
    streak = 0
    seen = 0
    for d in reversed(days):
        rec = (snapshots[d] or {}).get(symbol)
        if not rec:
            break
        val = rec.get(field)
        seen += 1
        if val is None:
            break
        if ok(val):
            streak += 1
        else:
            break
    return streak, seen


def evaluate(force: bool = False) -> dict:
    """跑一遍全部规则，写回 flags.json，返回本次结果摘要。

    每 10 秒被后台线程调一次，所以要极便宜：快照 parquet 和 fundamentals.json
    都没变化、且今天已经算过，就直接返回上次状态，不再碰 polars（避免和选股/
    回测的大表 collect 抢内存——实测并发时会把进程 OOM 掉）。
    """
    state_prev = S.load_state()
    snap_mtime = SNAPSHOT_PARQUET.stat().st_mtime if SNAPSHOT_PARQUET.exists() else 0
    fin_mtime = S.FILES["fundamentals"].stat().st_mtime if S.FILES["fundamentals"].exists() else 0
    if (
        not force
        and state_prev.get("snap_mtime") == snap_mtime
        and state_prev.get("fin_mtime") == fin_mtime
        and state_prev.get("day") == S.today()
        and S.FILES["flags"].exists()
    ):
        return state_prev

    names = _load_names()
    fin = S.load_fundamentals()
    snap_rows = _load_snapshot()
    today = S.today()

    # 每天做一次现价/市值快照（同一天重复跑只覆盖）
    per_symbol = {
        r["symbol"]: {"price": r["price"], "mcap": r["mcap"]}
        for r in snap_rows
        if r.get("symbol")
    }
    snapshots = S.load_snapshots()
    if per_symbol and snapshots.get(today) != per_symbol:
        S.push_snapshot(today, per_symbol)
        snapshots = S.load_snapshots()

    flags: dict[str, dict] = {}
    rule_hits: dict[str, int] = {r.id: 0 for r in R.ALL_RULES}
    unknown_rules: set[str] = set()

    def hit(sym: str, rule: R.Rule, detail: str) -> None:
        item = flags.setdefault(sym, {"level": "latent", "rules": []})
        order = {"star": 3, "st": 2, "latent": 1}
        if order[rule.level] > order[item["level"]]:
            item["level"] = rule.level
        item["rules"].append({"id": rule.id, "title": rule.title, "level": rule.level, "detail": detail})
        rule_hits[rule.id] = rule_hits.get(rule.id, 0) + 1

    for row in snap_rows:
        sym = row["symbol"]
        name = row.get("name") or names.get(sym, "")
        price = row.get("price")
        mcap = row.get("mcap")
        board = R.board_of(sym)
        f = fin.get(sym) or {}

        # ---- *ST / ST（名称）：交易所已经戴帽的，直接进对应档 ----
        if "*ST" in name.upper() or name.startswith("S*ST"):
            hit(sym, R.RULES_BY_ID["name_star"], "名称含 *ST")
        elif "ST" in name.upper()[:4]:
            hit(sym, R.RULES_BY_ID["name_st"], "名称含 ST")

        # ---- 11 连续 20 日股价 < 1 元 ----
        if price is not None:
            streak, seen = _streak(snapshots, "price", lambda v: v < 1.0, sym)
            if streak >= _WINDOW:
                hit(sym, R.RULES_BY_ID["star_11"], f"连续 {streak} 日收盘 < 1 元（现价 {price:.2f}）")
            elif price < 1.0 and seen:
                # 还没凑满 20 天：先记成"观察中"，不拉黑
                flags.setdefault(sym, {"level": "latent", "rules": [], "watching": []}).setdefault(
                    "watching", []
                ).append({"id": "star_11", "title": "股价 < 1 元", "progress": f"{streak}/{_WINDOW}"})

        # ---- 12 / 13 连续 20 日市值低于下限 ----
        if mcap is not None and board in ("main", "gem", "star_mkt"):
            limit = 5e8 if board == "main" else 3e8
            rule_id = "star_12" if board == "main" else "star_13"
            streak, seen = _streak(snapshots, "mcap", lambda v, lim=limit: v < lim, sym)
            if streak >= _WINDOW:
                hit(sym, R.RULES_BY_ID[rule_id], f"连续 {streak} 日市值 < {limit/1e8:.0f} 亿（当前 {mcap/1e8:.2f} 亿）")
            elif mcap < limit and seen:
                flags.setdefault(sym, {"level": "latent", "rules": [], "watching": []}).setdefault(
                    "watching", []
                ).append({"id": rule_id, "title": f"市值 < {limit/1e8:.0f} 亿", "progress": f"{streak}/{_WINDOW}"})

        # ---- 财报类规则 ----
        def need(*keys: str) -> dict | None:
            """字段齐全才返回，否则把缺的字段记进缺口。"""
            missing = [k for k in keys if f.get(k) is None]
            if missing:
                for rid in _rules_needing(keys):
                    unknown_rules.add(rid)
                return None
            return f

        # 1 主板扣非亏损 + 营收 < 3 亿
        if board == "main":
            if need("deducted_net_profit", "revenue"):
                if f["deducted_net_profit"] < 0 and f["revenue"] < 3e8:
                    hit(sym, R.RULES_BY_ID["star_1"], f"扣非 {f['deducted_net_profit']/1e8:.2f} 亿 / 营收 {f['revenue']/1e8:.2f} 亿")
        # 2 双创扣非亏损 + 营收 < 1 亿
        if board in ("gem", "star_mkt"):
            if need("deducted_net_profit", "revenue"):
                if f["deducted_net_profit"] < 0 and f["revenue"] < 1e8:
                    hit(sym, R.RULES_BY_ID["star_2"], f"扣非 {f['deducted_net_profit']/1e8:.2f} 亿 / 营收 {f['revenue']/1e8:.2f} 亿")

        # 3 净资产为负
        if need("net_assets") and f["net_assets"] < 0:
            hit(sym, R.RULES_BY_ID["star_3"], f"净资产 {f['net_assets']/1e8:.2f} 亿")

        # 4 / 5 审计意见
        if need("audit_opinion"):
            if f["audit_opinion"] == "adverse":
                hit(sym, R.RULES_BY_ID["star_4"], "年报否定意见")
            if f["audit_opinion"] == "disclaimer":
                hit(sym, R.RULES_BY_ID["star_5"], "年报无法表示意见")

        # 6 / 7 资金占用
        if need("fund_occupation") and f["fund_occupation"] >= 2e8:
            hit(sym, R.RULES_BY_ID["star_6"], f"占用 {f['fund_occupation']/1e8:.2f} 亿")
        if need("fund_occupation_ratio", "rectified"):
            if f["fund_occupation_ratio"] >= 0.3 and not f["rectified"]:
                hit(sym, R.RULES_BY_ID["star_7"], f"占净资产 {f['fund_occupation_ratio']*100:.0f}% 未整改")

        # 8 长期大额违规担保未解除
        if need("illegal_guarantee", "rectified"):
            if f["illegal_guarantee"] >= 1e8 and not f["rectified"]:
                hit(sym, R.RULES_BY_ID["star_8"], f"违规担保 {f['illegal_guarantee']/1e8:.2f} 亿未解除")

        # 9 逾期未披露
        if need("report_delayed") and f["report_delayed"]:
            hit(sym, R.RULES_BY_ID["star_9"], "逾期未披露定期报告")

        # 10 重大财务造假
        if need("fraud_major") and f["fraud_major"]:
            hit(sym, R.RULES_BY_ID["star_10"], "重大财务造假")

        # 14 / 15 / 16 担保与占用（ST 档）
        if need("illegal_guarantee") and f["illegal_guarantee"] >= 1e7:
            hit(sym, R.RULES_BY_ID["st_14"], f"违规担保 {f['illegal_guarantee']/1e8:.2f} 亿")
        if need("illegal_guarantee_ratio", "rectified"):
            if f["illegal_guarantee_ratio"] >= 0.05 and not f["rectified"]:
                hit(sym, R.RULES_BY_ID["st_15"], f"占净资产 {f['illegal_guarantee_ratio']*100:.1f}% 未整改")
        if need("fund_occupation", "rectified"):
            if 0 < f["fund_occupation"] < 2e8 and not f["rectified"]:
                hit(sym, R.RULES_BY_ID["st_16"], f"小额占用 {f['fund_occupation']/1e4:.0f} 万未归还")

        # 17 / 18 / 19
        if need("bank_account_frozen") and f["bank_account_frozen"]:
            hit(sym, R.RULES_BY_ID["st_17"], "主力账户被冻结")
        if need("dishonest_debtor") and f["dishonest_debtor"]:
            hit(sym, R.RULES_BY_ID["st_18"], "失信被执行人")
        if need("ic_nonstandard_years") and f["ic_nonstandard_years"] >= 2:
            hit(sym, R.RULES_BY_ID["st_19"], f"内控连续 {f['ic_nonstandard_years']} 年非标")

        # 20 / 21 分红
        if board == "main":
            if need("profit_years", "dividend_total_3y", "net_profit_3y"):
                if (
                    f["profit_years"] >= 3
                    and f["dividend_total_3y"] < 5e7
                    and f["dividend_total_3y"] < f["net_profit_3y"] * 0.3
                ):
                    hit(sym, R.RULES_BY_ID["st_20"], f"三年分红 {f['dividend_total_3y']/1e4:.0f} 万")
        if board in ("gem", "star_mkt"):
            if need("profit_years", "dividend_total_3y", "rd_exempt"):
                if f["profit_years"] >= 3 and f["dividend_total_3y"] < 3e7 and not f["rd_exempt"]:
                    hit(sym, R.RULES_BY_ID["st_21"], f"三年分红 {f['dividend_total_3y']/1e4:.0f} 万")

        # 22 营收临界
        if need("revenue"):
            limit = 3e8 if board == "main" else 1e8
            if limit <= f["revenue"] < limit * 1.2:
                hit(sym, R.RULES_BY_ID["lat_22"], f"营收 {f['revenue']/1e8:.2f} 亿贴近 {limit/1e8:.0f} 亿线")

        # 23 非经常性损益撑利润
        if need("noncore_profit_ratio") and f["noncore_profit_ratio"] >= 0.5:
            hit(sym, R.RULES_BY_ID["lat_23"], f"非经常性损益占 {f['noncore_profit_ratio']*100:.0f}%")

        # 24 商誉过高
        if need("goodwill_ratio") and f["goodwill_ratio"] >= 0.3:
            hit(sym, R.RULES_BY_ID["lat_24"], f"商誉/净资产 {f['goodwill_ratio']*100:.0f}%")

        # 25 质押过高
        if need("pledge_ratio") and f["pledge_ratio"] >= 0.5:
            hit(sym, R.RULES_BY_ID["lat_25"], f"质押 {f['pledge_ratio']*100:.0f}%")

    now = S._now()
    for sym, item in flags.items():
        item["at"] = now
        item.setdefault("name", next((r["name"] for r in snap_rows if r["symbol"] == sym), ""))
    S.save_flags(flags)

    state = S.load_state()
    state.update(
        {
            "snap_mtime": snap_mtime,
            "fin_mtime": fin_mtime,
            "day": today,
            "updated_at": now,
            "scanned": len(snap_rows),
            "flagged": len(flags),
            "unknown_rules": sorted(unknown_rules),
            "rule_hits": rule_hits,
        }
    )
    S.save_state(state)
    return state


def _rules_needing(keys: tuple[str, ...]) -> list[str]:
    out = []
    for r in R.ALL_RULES:
        need = {n[4:] for n in r.needs if n.startswith("fin:")}
        if need & set(keys):
            out.append(r.id)
    return out
