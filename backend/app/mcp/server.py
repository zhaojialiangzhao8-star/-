"""Facet Quant MCP Server —— 零依赖的 JSON-RPC over stdio 实现。

为什么不用官方 `mcp` SDK：本机 pip 装不上（files.pythonhosted.org 超时），
而 MCP 的 stdio 传输就是「每行一个 JSON-RPC 消息」，自己实现更可控也更快。

启动：
    python -m app.mcp.server            # 从 backend/ 目录启动
    python -m app.mcp.server --http 8765  # 顺便起一个 HTTP 入口

客户端配置（Claude Desktop / Cursor / OpenClaw 等）：
    {
      "mcpServers": {
        "facet-quant": {
          "command": "python",
          "args": ["-m", "app.mcp.server"],
          "cwd": "C:/Users/wang1/Documents/deepseek-harness/default-workspace/facet/backend"
        }
      }
    }

工具全部走 HTTP 调 facet API（`FACET_API` 环境变量，默认 http://127.0.0.1:3021），
这样不需要重复实现业务逻辑，也天然继承引擎的撮合规则与验证结论。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

API = os.getenv("FACET_API", "http://127.0.0.1:3021")
PROTOCOL = "2024-11-05"
SERVER_NAME = "facet-quant"
SERVER_VERSION = "2.0.0"


# ---------------------------------------------------------------- HTTP
def _call(method: str, path: str, body: dict | None = None, timeout: int = 180) -> Any:
    url = API.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法连接 facet 服务（{API}）：{exc}") from exc


def _get(path: str, timeout: int = 180) -> Any:
    return _call("GET", path, None, timeout)


def _post(path: str, body: dict, timeout: int = 300) -> Any:
    return _call("POST", path, body, timeout)


# ---------------------------------------------------------------- 工具实现
def tool_market_overview(args: dict) -> str:
    d = _get("/api/v1/overview")
    return (
        f"{d.get('date')} 市场概况：\n"
        f"  上涨 {d.get('advancers')} / 下跌 {d.get('decliners')}（宽度 {d.get('breadth_pct')}%）\n"
        f"  涨停 {d.get('limit_ups')} / 跌停 {d.get('limit_downs')}\n"
        f"  涨跌幅中位数 {d.get('median_ret_pct')}%　成交额 {d.get('amount_yi')} 亿\n"
        f"  覆盖标的 {d.get('symbols')} 只"
    )


def tool_quote(args: dict) -> str:
    symbol = args["symbol"]
    d = _get(f"/api/v1/kline/{symbol}?limit=20")   # API 要求 limit>=20
    rows = d.get("rows") or []
    if not rows:
        return f"{symbol} 无行情数据"
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else last
    chg = (last["close"] / prev["close"] - 1) * 100 if prev["close"] else 0
    return (
        f"{symbol} {last['date']}\n"
        f"  收 {last['close']:.2f}  开 {last['open']:.2f}  高 {last['high']:.2f}  低 {last['low']:.2f}\n"
        f"  涨跌 {chg:+.2f}%  成交量 {last['volume']:,.0f} 手\n"
        f"  MA5 {last.get('ma5') or 0:.2f}  MA20 {last.get('ma20') or 0:.2f}"
    )


def tool_kline(args: dict) -> str:
    symbol = args["symbol"]
    limit = int(args.get("limit", 60))
    d = _get(f"/api/v1/kline/{symbol}?limit={limit}")
    rows = d.get("rows") or []
    if not rows:
        return f"{symbol} 无行情"
    head = f"{symbol} 最近 {len(rows)} 根日线（日期 开 高 低 收 量）\n"
    body = "\n".join(
        f"  {r['date']} {r['open']:.2f} {r['high']:.2f} {r['low']:.2f} {r['close']:.2f} {r['volume']:,.0f}"
        for r in rows[-min(limit, 40):]
    )
    if len(rows) > 40:
        head += f"（只显示最后 40 根，共 {len(rows)} 根）\n"
    return head + body


def tool_strategies(args: dict) -> str:
    d = _get("/api/v1/strategies")
    items = d.get("items") or []
    kw = (args.get("keyword") or "").lower()
    if kw:
        items = [s for s in items if kw in s["id"].lower() or kw in (s.get("name") or "").lower()]
    lines = [f"共 {len(items)} 个策略："]
    for s in items[:40]:
        tag = ",".join(s.get("tags") or [])
        lines.append(f"  {s['id']:<28} {s.get('name', '')}  [{tag}]")
    if len(items) > 40:
        lines.append(f"  …还有 {len(items) - 40} 个")
    return "\n".join(lines)


def tool_screener(args: dict) -> str:
    strategy = args["strategy"]
    limit = int(args.get("limit", 20))
    fallback = int(args.get("fallback_days", 10))
    d = _get(f"/api/v1/screener?strategy={strategy}&limit={limit}&fallback_days={fallback}", timeout=300)
    rows = d.get("rows") or []
    if not rows:
        return f"{strategy} 在最近 {fallback} 天内无信号（历史命中 {d.get('hit_total', 0):,} 次）"
    head = (
        f"{strategy} · 信号日 {d.get('signal_date')} · 当日 {len(rows)} 条"
        f"（历史命中 {d.get('hit_total', 0):,} 次）\n"
    )
    if d.get("signal_date") != d.get("date"):
        head += f"注意：{d.get('date')} 当日无信号，已回溯到 {d.get('signal_date')}\n"
    body = "\n".join(
        f"  {r['symbol']:<12} 收盘 {r.get('close')} 强度 {r.get('score'):.2f} {r.get('note') or ''}"
        if r.get("score") is not None
        else f"  {r['symbol']:<12} 收盘 {r.get('close')}"
        for r in rows
    )
    return head + body


def tool_daily_signal(args: dict) -> str:
    risk = float(args.get("risk", 3000))
    d = _get(f"/api/v1/daily-signal?risk={int(risk)}&max_pos={int(args.get('max_pos', 5))}")
    rows = d.get("rows") or []
    if not rows:
        return f"{d.get('date')} 无信号（3 日跌幅 < −15% 且收阳放量为 0）\n规则：{d.get('rule')}"
    lines = [f"{d.get('date')} 急跌反弹信号 · 每笔固定止损 {risk:,.0f} 元", f"规则：{d.get('rule')}", ""]
    for r in rows:
        lines.append(
            f"  {r['symbol']:<12} {r['board']}  跌幅 {r['drop3']}%  量比 {r['vol_ratio']}\n"
            f"    买入区 {r['buy_low']}~{r['buy_high']}  止损 {r['stop']}  "
            f"{r['qty']} 股  占用 {r['amount']:,.0f} 元  止损额 {r['risk_amount']:,.0f} 元"
        )
    lines.append(f"\n合计占用 {d.get('total_amount', 0):,.0f} 元，合计风险 {d.get('total_risk', 0):,.0f} 元")
    return "\n".join(lines)


def tool_backtest(args: dict) -> str:
    body = {
        "strategy": args["strategy"],
        "params": args.get("params") or {},
        "init_cash": float(args.get("init_cash", 100_000)),
        "risk_amount": args.get("risk_amount"),
        "risk_pct": float(args.get("risk_pct", 0.01)),
        "max_positions": int(args.get("max_positions", 10)),
        "max_position_pct": float(args.get("max_position_pct", 0.25)),
        "stop_loss_pct": float(args.get("stop_loss_pct", 0.08)),
        "take_profit_pct": args.get("take_profit_pct"),
        "max_hold_days": args.get("max_hold_days"),
        "start": args.get("start"),
        "end": args.get("end"),
    }
    d = _post("/api/v1/bt/run", body, timeout=600)
    m = d["metrics"]
    rej = d.get("rejected") or {}
    return (
        f"{args['strategy']} 回测 {m.get('start')} ~ {m.get('end')}\n"
        f"  总收益 {(m.get('total_return') or 0)*100:+.2f}%  年化 {(m.get('annual_return') or 0)*100:+.2f}%\n"
        f"  最大回撤 {(m.get('max_drawdown') or 0)*100:.2f}%（{m.get('max_drawdown_days')} 天）\n"
        f"  夏普 {m.get('sharpe') or 0:.2f}  卡玛 {m.get('calmar') or 0:.2f}  胜率 {(m.get('win_rate') or 0)*100:.1f}%\n"
        f"  交易 {m.get('trades')} 笔  盈亏比 {m.get('payoff') or 0:.2f}  期末 {m.get('final_equity') or 0:,.0f} 元\n"
        f"  拒单 {rej}"
    )


def tool_position_size(args: dict) -> str:
    capital = float(args.get("capital", 100_000))
    risk = float(args.get("risk_amount", 3000))
    entry = float(args["entry"])
    stop = float(args["stop"])
    target = float(args.get("target", entry * 1.2))
    if entry <= stop:
        return "止损价必须低于入场价"
    per_share = entry - stop
    qty = int((risk / per_share) // 100 * 100)
    if qty < 100:
        return f"风险预算 {risk:.0f} 元不够一手（每股风险 {per_share:.2f}）"
    invest = qty * entry
    buy_fee = max(invest * 0.00025, 5) + invest * 0.00001
    sell_value = qty * target
    sell_fee = max(sell_value * 0.00025, 5) + sell_value * 0.0005 + sell_value * 0.00001
    actual_risk = qty * per_share + buy_fee + sell_fee
    profit = (sell_value - qty * entry) - buy_fee - sell_fee
    return (
        f"仓位计算（资金 {capital:,.0f}，单笔风险 {risk:,.0f} 元）\n"
        f"  入场 {entry:.2f}  止损 {stop:.2f}  目标 {target:.2f}\n"
        f"  每股风险 {per_share:.2f}\n"
        f"  建议 {qty} 股　投入 {invest:,.0f} 元（占账户 {invest/capital*100:.1f}%）\n"
        f"  止损亏损 {actual_risk:,.0f} 元　目标盈利 {profit:,.0f} 元\n"
        f"  盈亏比 {profit/actual_risk:.2f}"
    )


def tool_limit_up(args: dict) -> str:
    d = _get("/api/v1/limit-up-stats", timeout=300)
    lines = [f"连板晋级率（{d.get('sample')}，共 {d.get('total_limit_up_days'):,} 个涨停日）", ""]
    lines.append(f"{'档位':<10}{'样本':>8}{'晋级率':>9}{'次日均涨':>10}{'开盘买收盘卖':>14}{'胜率':>8}")
    for b in d.get("buckets") or []:
        lines.append(
            f"{b['label']:<10}{b['n']:>8,}{b['promote']:>8.1f}%{b['next_chg']:>9.2f}%"
            f"{b['oc_return']:>+13.2f}%{b['win_rate']:>7.1f}%"
        )
    lines.append("\n注意：晋级率高 ≠ 赚钱。2 板以上「开盘买收盘卖」全部为负，溢价在开盘一次性兑现。")
    return "\n".join(lines)


def tool_submit_task(args: dict) -> str:
    d = _post("/api/v1/tasks", {"type": args["type"], "params": args.get("params") or {}})
    return f"任务已提交：{d['task_id']}\n轮询：GET {d['poll']}"


def tool_task_status(args: dict) -> str:
    d = _get(f"/api/v1/tasks/{args['task_id']}")
    lines = [
        f"任务 {d['id'][:8]}  {d.get('type')}  {d.get('status')}  {int((d.get('progress') or 0)*100)}%",
        f"  创建 {d.get('created_at')}  开始 {d.get('started_at') or '—'}  结束 {d.get('finished_at') or '—'}",
    ]
    if d.get("error"):
        lines.append(f"  错误：{d['error']}")
    if d.get("log"):
        lines.append("  日志：\n    " + "\n    ".join(str(d["log"]).splitlines()[-6:]))
    if d.get("result"):
        lines.append("  结果：" + json.dumps(d["result"], ensure_ascii=False)[:600])
    return "\n".join(lines)


def tool_data_info(args: dict) -> str:
    h = _get("/api/v1/health")
    s = _get("/api/v1/data-sources", timeout=60)
    lines = [
        f"数据代际 {h['generation'][:8]}  {h['rows']:,} 行  {h['strategies']} 个策略",
        "",
        "数据源能力：",
    ]
    for name, p in (s.get("providers") or {}).items():
        lines.append(
            f"  {name:<12} {p['capabilities']}  日线≤{p['max_daily_bars']} 分钟≤{p['max_minute_bars']}"
            f"  {p.get('note', '')}"
        )
    lines.append("")
    lines.append("路由：")
    for cap, r in (s.get("routing") or {}).items():
        lines.append(f"  {cap:<10} → {r.get('preferred')}  (可选 {r.get('providers')})")
    return "\n".join(lines)


# ---------------------------------------------------------------- 工具清单
TOOLS: list[dict] = [
    {
        "name": "market_overview",
        "description": "查询当日市场概况：涨跌家数、涨停跌停、成交额、涨跌幅中位数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_market_overview,
    },
    {
        "name": "quote",
        "description": "查询个股最新收盘行情（价格、涨跌幅、成交量、MA5/MA20）。",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "如 600519.SH"}},
            "required": ["symbol"],
        },
        "handler": tool_quote,
    },
    {
        "name": "kline",
        "description": "查询个股日线序列（日期/开高低收/成交量），默认最近 60 根。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 5, "maximum": 500, "default": 60},
            },
            "required": ["symbol"],
        },
        "handler": tool_kline,
    },
    {
        "name": "list_strategies",
        "description": "列出全部策略插件（58 个），可按关键词过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {"keyword": {"type": "string", "description": "匹配 id 或名称"}},
        },
        "handler": tool_strategies,
    },
    {
        "name": "run_screener",
        "description": "用指定策略做全市场扫描，返回信号列表。当日无信号会自动回溯。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "description": "策略 id，如 classic_reversal"},
                "limit": {"type": "integer", "default": 20},
                "fallback_days": {"type": "integer", "default": 10, "description": "当日无信号时向前回溯的天数"},
            },
            "required": ["strategy"],
        },
        "handler": tool_screener,
    },
    {
        "name": "daily_signal",
        "description": "获取急跌反弹策略的今日买入清单（含固定止损金额反推的股数）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "risk": {"type": "number", "default": 3000, "description": "每笔固定止损金额（元）"},
                "max_pos": {"type": "integer", "default": 5},
            },
        },
        "handler": tool_daily_signal,
    },
    {
        "name": "run_backtest",
        "description": "运行账户级回测（T+1、次日开盘成交、涨跌停停牌拒绝、100 股整数倍、完整费用）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
                "init_cash": {"type": "number", "default": 100000},
                "risk_amount": {"type": "number", "description": "单笔固定止损金额"},
                "max_positions": {"type": "integer", "default": 10},
                "max_position_pct": {"type": "number", "default": 0.25},
                "stop_loss_pct": {"type": "number", "default": 0.08},
                "max_hold_days": {"type": "integer"},
                "params": {"type": "object", "description": "策略参数覆盖"},
            },
            "required": ["strategy"],
        },
        "handler": tool_backtest,
    },
    {
        "name": "calc_position",
        "description": "按固定止损金额反推仓位：股数、投入、止损亏损、目标盈利、盈亏比（含费用）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry": {"type": "number"},
                "stop": {"type": "number"},
                "target": {"type": "number"},
                "capital": {"type": "number", "default": 100000},
                "risk_amount": {"type": "number", "default": 3000},
            },
            "required": ["entry", "stop"],
        },
        "handler": tool_position_size,
    },
    {
        "name": "limit_up_ladder",
        "description": "连板晋级率统计（1进2/2进3/3进4…），含「开盘买收盘卖」的可执行收益。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_limit_up,
    },
    {
        "name": "submit_task",
        "description": "提交后台任务（scan / backtest / grid），立即返回 task_id，不阻塞。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["scan", "backtest", "grid"]},
                "params": {"type": "object"},
            },
            "required": ["type"],
        },
        "handler": tool_submit_task,
    },
    {
        "name": "task_status",
        "description": "查询后台任务的进度、日志与结果。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        "handler": tool_task_status,
    },
    {
        "name": "data_info",
        "description": "查询数据代际规模与各数据源的能力矩阵、路由。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_data_info,
    },
]

BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------- JSON-RPC
def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "initialized"):
        return None  # 通知不需要响应

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "tools": [
                    {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                    for t in TOOLS
                ]
            },
        }

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = BY_NAME.get(name)
        if tool is None:
            return {
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32602, "message": f"未知工具: {name}"},
            }
        try:
            text = tool["handler"](args)
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": f"工具执行失败：{exc}"}], "isError": True},
            }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未实现的方法: {method}"}}


def serve_stdio() -> None:
    """每行一个 JSON-RPC 消息。stdout 只写协议消息，日志一律走 stderr。"""
    print(f"[facet-mcp] 启动，后端 {API}，{len(TOOLS)} 个工具", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[facet-mcp] 非法 JSON: {exc}", file=sys.stderr, flush=True)
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def serve_http(port: int) -> None:
    """可选：HTTP 入口（POST /mcp，body 是单条 JSON-RPC 或数组）。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self.send_error(400, "bad json")
                return
            batch = isinstance(payload, list)
            items = payload if batch else [payload]
            out = [r for r in (handle(x) for x in items) if r is not None]
            body = json.dumps(out if batch else (out[0] if out else {}), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            body = json.dumps({"server": SERVER_NAME, "tools": len(TOOLS)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):  # noqa: A003
            print(f"[facet-mcp/http] {fmt % a}", file=sys.stderr, flush=True)

    print(f"[facet-mcp] HTTP 模式监听 127.0.0.1:{port}", file=sys.stderr, flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=0, help="同时起 HTTP 入口（端口）")
    ap.add_argument("--list", action="store_true", help="只打印工具清单")
    args = ap.parse_args()

    if args.list:
        for t in TOOLS:
            print(f"{t['name']:<20} {t['description']}")
        return

    if args.http:
        import threading

        threading.Thread(target=serve_http, args=(args.http,), daemon=True).start()
    serve_stdio()


if __name__ == "__main__":
    main()
