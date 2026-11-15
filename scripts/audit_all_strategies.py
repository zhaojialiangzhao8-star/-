"""全策略重跑 —— 用当前代际重算所有策略的账户回测，生成可读的清单。

为什么要重跑：
    排名报告 reports/account_compare_fullmarket.md 是 2026-11-11 用旧代际
    （9d7fb4e6，455 万行）生成的。中间换过数据源（东财 → 腾讯）、剔除了
    14 只复权异常的股票、历史起点从 2021-01 变成 2021-05。
    新旧数据算出来的收益完全不同（低回撤增仓从 +99.2% 掉到 +25.3%），
    所以那些数字现在不能信。

输出两份：
    reports/strategy_audit.md    给人读的完整分析（带结论和建议）
    reports/strategy_audit.txt   记事本也能打开的纯文本版

设计要点：
    · 结果落盘到 reports/_audit_raw.json，中断后重跑会跳过已完成的组合
    · 每次回测之间 sleep 4 秒，避免内存没释放导致 503
    · 同时记录 rejected 明细（持仓已满/涨停买不进/跌停卖不出）

用法：
    python scripts/audit_all_strategies.py
    python scripts/audit_all_strategies.py --holds 5        # 只跑持有 5 天
    python scripts/audit_all_strategies.py --resume         # 接着上次跑
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

BASE = "http://127.0.0.1:3021"
RAW = ROOT / "reports" / "_audit_raw.json"
OUT_MD = ROOT / "reports" / "strategy_audit.md"
OUT_TXT = ROOT / "reports" / "strategy_audit.txt"


def load_raw() -> dict:
    if RAW.exists():
        try:
            return json.loads(RAW.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_raw(data: dict) -> None:
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def run_one(sid: str, hold: int, stop: float, start: str, end: str) -> dict | None:
    body = json.dumps(
        {
            "strategy": sid,
            "params": {},
            "init_cash": 100000,
            "risk_amount": 3000,
            "max_positions": 10,
            "max_position_pct": 0.25,
            "stop_loss_pct": stop,
            "max_hold_days": hold,
            "start": start,
            "end": end,
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/api/v1/bt/run", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            d = json.loads(r.read())
        m = d["metrics"]
        # sharpe 可能是 None（交易太少或收益全 0 时算不出来），兜底成 0.0
        # 否则 f"{x:.2f}" 会抛 TypeError，整个脚本在第 46 组崩掉
        def _f(key, default=0.0):
            v = m.get(key)
            return default if v is None else v

        return {
            "ret": _f("total_return"),
            "mdd": _f("max_drawdown"),
            "sharpe": _f("sharpe"),
            "win": _f("win_rate"),
            "trades": int(_f("trades", 0)),
            "rejected": d.get("rejected") or {},
        }
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:100]}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def fmt_pct(x: float | None) -> str:
    return f"{x * 100:+.1f}%" if x is not None else "—"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holds", default="5,10,20")
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--start", default="2021-11-01")
    ap.add_argument("--end", default="2026-11-07")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    from app.engine import StrategyEngine
    from app import config

    engine = StrategyEngine(config.STRATEGY_DIR)
    sids = sorted(engine.registry.keys())
    holds = [int(x) for x in args.holds.split(",")]

    raw = load_raw() if args.resume else {}
    total = len(sids) * len(holds)
    done = 0
    t0 = time.time()

    print(f"策略 {len(sids)} 个 × 持有期 {holds} = {total} 次回测")
    print(f"止损 {args.stop * 100:.0f}%  区间 {args.start} ~ {args.end}")
    print()

    for sid in sids:
        spec = engine.registry[sid]
        for hold in holds:
            key = f"{sid}@{hold}"
            done += 1
            if key in raw and raw[key].get("ret") is not None:
                print(f"  [{done}/{total}] {spec.name} hold={hold}  跳过（已有）")
                continue
            print(f"  [{done}/{total}] {spec.name} hold={hold} …", end="", flush=True)
            r = run_one(sid, hold, args.stop, args.start, args.end)
            if r and "error" not in r:
                raw[key] = r
                print(
                    f" {fmt_pct(r['ret'])}  回撤 {fmt_pct(r['mdd'])}  "
                    f"夏普 {r['sharpe']:.2f}  胜率 {r['win'] * 100:.1f}%  {r['trades']} 笔"
                )
            else:
                raw[key] = {"error": (r or {}).get("error", "unknown")}
                print(f" 失败 {(r or {}).get('error', '')[:60]}")
            save_raw(raw)
            time.sleep(4)
        raw[f"_name@{sid}"] = spec.name
        save_raw(raw)

    elapsed = time.time() - t0
    print(f"\n完成 {total} 次，用时 {elapsed / 60:.1f} 分钟")

    # ---------- 生成报告 ----------
    lines: list[str] = []
    lines.append("# 全部策略回测审计")
    lines.append("")
    lines.append(f"> 生成于 {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> 区间 {args.start} ~ {args.end} · 10 万本金 · 单笔止损 3000 元 · 最多 10 仓 · 止损 {args.stop * 100:.0f}%")
    lines.append("> 数据：代际 aafc0d2a（2,704,359 行 / 4,786 只 / 2021-05 ~ 2026-11）")
    lines.append("")

    # 主表：hold=5
    primary = holds[0]
    rows = []
    for sid in sids:
        r = raw.get(f"{sid}@{primary}") or {}
        if r.get("ret") is None:
            continue
        rows.append((sid, raw.get(f"_name@{sid}", sid), r))
    rows.sort(key=lambda x: -(x[2]["ret"] or -99))

    lines.append(f"## 排名（持有 {primary} 天）")
    lines.append("")
    lines.append("| # | 策略 | 收益 | 回撤 | 夏普 | 胜率 | 交易 | 被拒 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, (sid, name, r) in enumerate(rows, start=1):
        rej = r.get("rejected") or {}
        rej_s = " ".join(f"{k}={v}" for k, v in rej.items() if v) or "—"
        sharpe = r.get("sharpe")
        win = r.get("win")
        lines.append(
            f"| {i} | {name} | {fmt_pct(r.get('ret'))} | {fmt_pct(r.get('mdd'))} | "
            f"{(sharpe if sharpe is not None else 0):.2f} | "
            f"{(win * 100 if win is not None else 0):.1f}% | "
            f"{r.get('trades') or 0} | {rej_s} |"
        )

    # 持有期敏感性
    if len(holds) > 1:
        lines.append("")
        lines.append("## 持有期敏感性")
        lines.append("")
        header = "| 策略 | " + " | ".join(f"持有{h}天" for h in holds) + " |"
        lines.append(header)
        lines.append("|---" * (len(holds) + 1) + "|")
        for sid in sids:
            name = raw.get(f"_name@{sid}", sid)
            cells = []
            for h in holds:
                r = raw.get(f"{sid}@{h}") or {}
                cells.append(fmt_pct(r.get("ret")))
            lines.append(f"| {name} | " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("## 怎么看这份表")
    lines.append("")
    lines.append("- **收益列为正** 的策略才有讨论价值；负的说明这套参数在 5 年里没有优势")
    lines.append("- **被拒 = max_positions** 说明信号太多、仓位不够，实际成交远少于信号")
    lines.append("- **胜率 35~45% + 正收益** 属于正常形态：靠盈亏比赚钱，不靠胜率")
    lines.append("- 不要拿单个数字下结论，先看同一策略在不同持有期是否稳定")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    # 纯文本版（记事本友好）
    txt: list[str] = []
    txt.append("=" * 78)
    txt.append("全部策略回测审计")
    txt.append("=" * 78)
    txt.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}")
    txt.append(f"区间：{args.start} ~ {args.end}   止损 {args.stop * 100:.0f}%   最多10仓")
    txt.append("")
    txt.append(f"排名（持有 {primary} 天）")
    txt.append("-" * 78)
    txt.append(f"{'#':<4}{'策略':<30}{'收益':>9}{'回撤':>9}{'夏普':>7}{'胜率':>7}{'交易':>6}")
    txt.append("-" * 78)
    for i, (sid, name, r) in enumerate(rows, start=1):
        sharpe = r.get("sharpe")
        win = r.get("win")
        txt.append(
            f"{i:<4}{name:<30}{fmt_pct(r.get('ret')):>9}{fmt_pct(r.get('mdd')):>9}"
            f"{(sharpe if sharpe is not None else 0):>7.2f}"
            f"{(win * 100 if win is not None else 0):>6.1f}%{r.get('trades') or 0:>6}"
        )
    if len(holds) > 1:
        txt.append("")
        txt.append("持有期敏感性")
        txt.append("-" * 78)
        txt.append(f"{'策略':<30}" + "".join(f"{'持有' + str(h):>10}" for h in holds))
        txt.append("-" * 78)
        for sid in sids:
            name = raw.get(f"_name@{sid}", sid)
            txt.append(f"{name:<30}" + "".join(f"{fmt_pct((raw.get(f'{sid}@{h}') or {}).get('ret')):>10}" for h in holds))
    OUT_TXT.write_text("\n".join(txt), encoding="utf-8")

    print(f"\n→ {OUT_MD}")
    print(f"→ {OUT_TXT}")


if __name__ == "__main__":
    main()
