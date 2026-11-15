"""全量自检：把每个接口都打一遍，报告哪个坏了。

跑法：
    python scripts/smoke_test.py
    python scripts/smoke_test.py --base http://127.0.0.1:3022

输出分三档：
  OK   200 且返回结构符合预期
  WARN 200 但内容可疑（空列表、字段缺失、耗时 >5s）
  FAIL 非 200 或抛异常
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

RESULTS: list[tuple[str, str, str]] = []


def call(base: str, path: str, method: str = "GET", body: dict | None = None, timeout: int = 120):
    url = base + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            dt = time.time() - t0
            try:
                return r.status, json.loads(raw), dt
            except json.JSONDecodeError:
                return r.status, raw.decode("utf-8", errors="replace")[:200], dt
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        try:
            return e.code, json.loads(e.read()), dt
        except Exception:  # noqa: BLE001
            return e.code, None, dt
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}", time.time() - t0


def check(name: str, base: str, path: str, *, expect: str = "", min_count: int | None = None,
          max_ms: int | None = None, body: dict | None = None, method: str = "GET",
          timeout: int = 120) -> dict | None:
    status, payload, dt = call(base, path, method=method, body=body, timeout=timeout)
    ms = int(dt * 1000)

    if status != 200:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("error") or "")[:90]
        RESULTS.append((name, "FAIL", f"HTTP {status} {detail}"))
        print(f"  FAIL  {name:<26} HTTP {status}  {detail}")
        return None

    if not isinstance(payload, dict):
        RESULTS.append((name, "WARN", f"非 JSON: {str(payload)[:60]}"))
        print(f"  WARN  {name:<26} 非 JSON")
        return None

    # 结构检查
    warn = ""
    cnt = payload.get("count") or payload.get("strategy_count") or payload.get("total")
    if min_count is not None and isinstance(cnt, (int, float)) and cnt < min_count:
        warn = f"条数 {cnt} < 期望 {min_count}"
    if expect and expect not in payload:
        warn = warn or f"缺字段 {expect}"
    if max_ms and ms > max_ms:
        warn = warn or f"{ms}ms > {max_ms}ms"

    if warn:
        RESULTS.append((name, "WARN", warn))
        print(f"  WARN  {name:<26} {ms:>6}ms  {warn}")
    else:
        extra = f"{cnt} 条" if cnt is not None else ""
        RESULTS.append((name, "OK", f"{ms}ms {extra}"))
        print(f"  OK    {name:<26} {ms:>6}ms  {extra}")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3021")
    args = ap.parse_args()
    b = args.base

    print(f"=== 基础接口 ===  {b}")
    h = check("health", b, "/api/v1/health", expect="generation", max_ms=60_000)
    check("overview", b, "/api/v1/overview", expect="advancers", max_ms=60_000)
    check("data-sources", b, "/api/v1/data-sources", expect="providers")
    check("bt/capabilities", b, "/api/v1/bt/capabilities")
    check("generations", b, "/api/v1/generations")

    print("\n=== 指数 / 板块 ===")
    idx = check("indices", b, "/api/v1/indices", min_count=4, max_ms=20_000)
    if idx:
        avail = [x for x in idx.get("items", []) if x.get("available")]
        print(f"        可用指数 {len(avail)}/{idx.get('count')}: " +
              ", ".join(f"{x['name']} {x.get('change_pct'):+.2f}%" for x in avail[:6]))
    bd = check("boards industry", b, "/api/v1/boards?kind=industry&limit=20", min_count=10, max_ms=30_000)
    if bd and bd.get("items"):
        top = bd["items"][0]
        print(f"        涨幅第一: {top['name']} {top['change_pct']:+.2f}% 龙头 {top.get('leader')}")
        code = top.get("code")
        if code:
            check(f"board detail {code}", b, f"/api/v1/boards/{code}?limit=10", min_count=1, max_ms=30_000)

    print("\n=== 行情 ===")
    check("kline 600519", b, "/api/v1/kline/600519.SH?limit=30", min_count=0)
    check("kline 不存在", b, "/api/v1/kline/999999.SH?limit=5")   # 预期 404 -> FAIL 也算对
    check("realtime", b, "/api/v1/realtime?symbols=600519.SH,000001.SZ")
    check("watchlist", b, "/api/v1/watchlist")
    check("events snapshot", b, "/api/v1/events/snapshot")

    print("\n=== 策略 / 选股 ===")
    st = check("strategies", b, "/api/v1/strategies", min_count=1)
    if st:
        print(f"        策略 {len(st.get('items', []))} 个，加载错误 {len(st.get('errors') or {})} 个")
    rk = check("strategy-ranking", b, "/api/v1/strategy-ranking", min_count=1)
    if rk:
        print(f"        {rk['count']} 个，有排名 {rk['ranked']} 个")
    sa = check("screener/all", b, "/api/v1/screener/all?limit_per_strategy=3", max_ms=120_000)
    if sa:
        print(f"        cached={sa.get('cached')} 策略 {sa.get('strategy_count')} 信号 {sa.get('total_signals_today')}")
        for it in (sa.get("items") or [])[:3]:
            names = [(r.get("name") or r.get("symbol")) for r in (it.get("rows") or [])]
            print(f"          {it['name']}: {', '.join(names[:4])}")
    check("screener/all/state", b, "/api/v1/screener/all/state")
    check("daily-signal", b, "/api/v1/daily-signal?risk=3000")
    check("limit-up-stats", b, "/api/v1/limit-up-stats", max_ms=60_000)

    print("\n=== 任务 / 回测 ===")
    tk = check("tasks", b, "/api/v1/tasks?limit=5")
    if tk is not None:
        print(f"        {tk.get('count', 0)} 条任务")
    bt = check(
        "bt/run 近1年", b, "/api/v1/bt/run", method="POST",
        body={"strategy": "classic_mean_revert", "risk_amount": 3000, "max_positions": 10,
              "max_position_pct": 0.25, "stop_loss_pct": 0.08, "max_hold_days": 20,
              "start": "2025-11-01", "end": "2026-11-07"},
        timeout=600,
    )
    if bt and "metrics" in bt:
        m = bt["metrics"]
        print(f"        收益 {m['total_return']*100:+.1f}%  回撤 {m['max_drawdown']*100:.1f}%  "
              f"夏普 {m['sharpe']:.2f}  交易 {m['trades']}")
    check("bt/run 非法策略", b, "/api/v1/bt/run", method="POST", body={"strategy": "nope"}, timeout=60)

    print("\n=== 前端 ===")
    for p, label in [("/app", "React 入口"), ("/app/assets", "assets 目录"), ("/", "旧版首页")]:
        status, payload, dt = call(b, p)
        ok = status == 200
        RESULTS.append((label, "OK" if ok else "FAIL", f"{int(dt*1000)}ms"))
        print(f"  {'OK   ' if ok else 'FAIL '} {label:<26} {int(dt*1000):>6}ms  HTTP {status}")

    # ---- 汇总 ----
    fails = [r for r in RESULTS if r[1] == "FAIL" and "不存在" not in r[0] and "非法策略" not in r[0]]
    warns = [r for r in RESULTS if r[1] == "WARN"]
    print("\n" + "=" * 58)
    print(f"总共 {len(RESULTS)} 项 · OK {len([r for r in RESULTS if r[1]=='OK'])} · "
          f"WARN {len(warns)} · FAIL {len(fails)}")
    if warns:
        print("\nWARN:")
        for n, _, d in warns:
            print(f"  {n}: {d}")
    if fails:
        print("\nFAIL:")
        for n, _, d in fails:
            print(f"  {n}: {d}")
    print("\n（「kline 不存在」「bt/run 非法策略」预期返回 4xx/5xx，不计入失败）")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
