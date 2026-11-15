# Facet Quant MCP Server

把 facet 的量化能力暴露给任何支持 MCP 的 AI 客户端（Claude Desktop、Cursor、OpenClaw、Cherry Studio…）。

**零依赖**：不用官方 `mcp` SDK（本机 pip 装不上 `files.pythonhosted.org`），直接实现 MCP 的 stdio JSON-RPC 传输。

---

## 启动

```powershell
cd C:\Users\wang1\Documents\deepseek-harness\default-workspace\facet\backend
python -m app.mcp.server                # stdio 模式
python -m app.mcp.server --http 8765    # 同时起 HTTP 入口（POST /mcp）
python -m app.mcp.server --list         # 只看工具清单
```

前置条件：facet API 必须在跑（默认 `http://127.0.0.1:3021`）。
用 `FACET_API` 环境变量可以指向别的实例。

---

## 客户端配置

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "facet-quant": {
      "command": "C:\\Users\\wang1\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe",
      "args": ["-m", "app.mcp.server"],
      "cwd": "C:\\Users\\wang1\\Documents\\deepseek-harness\\default-workspace\\facet\\backend"
    }
  }
}
```

### Cursor

`.cursor/mcp.json`（项目级）或全局设置里同样的一段。

### HTTP 客户端

```bash
curl -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"quote","arguments":{"symbol":"605258.SH"}}}'
```

---

## 12 个工具

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `market_overview` | 市场概况：涨跌家数、涨停跌停、成交额 | — |
| `quote` | 个股最新行情（收盘、涨跌幅、量、MA5/MA20） | `symbol` |
| `kline` | 日线序列 | `symbol`, `limit` |
| `list_strategies` | 58 个策略清单 | `keyword` |
| `run_screener` | 全市场扫描（当日无信号自动回溯） | `strategy`, `limit`, `fallback_days` |
| `daily_signal` | 急跌反弹今日买入清单（固定止损反推股数） | `risk`, `max_pos` |
| `run_backtest` | 账户级回测（T+1/涨跌停/停牌/费用全含） | `strategy`, `start`, `end`, `risk_amount` |
| `calc_position` | 仓位计算（股数/止损额/盈亏比，含费用） | `entry`, `stop`, `target`, `risk_amount` |
| `limit_up_ladder` | 连板晋级率（1进2/2进3/3进4…） | — |
| `submit_task` | 提交后台任务（不阻塞） | `type`, `params` |
| `task_status` | 任务进度与结果 | `task_id` |
| `data_info` | 数据代际规模 + 数据源能力矩阵 | — |

---

## 实测输出

**calc_position**

```
仓位计算（资金 100,000，单笔风险 3,000 元）
  入场 42.11  止损 37.06  目标 50.00
  每股风险 5.05
  建议 500 股　投入 21,055 元（占账户 21.1%）
  止损亏损 2,549 元　目标盈利 3,921 元
  盈亏比 1.54
```

**market_overview**

```
2026-11-11 市场概况：
  上涨 2703 / 下跌 2303（宽度 51.8%）
  涨停 40 / 跌停 2
  涨跌幅中位数 0.1%　成交额 9266.08 亿
  覆盖标的 5214 只
```

**limit_up_ladder**（全市场 101,007 个涨停日）

```
档位              样本      晋级率      次日均涨        开盘买收盘卖      胜率
1板→2板       77,879    17.7%     1.91%        -0.04%   45.5%
2板→3板       13,751    35.9%     2.89%        -0.38%   41.4%
3板→4板        4,935    43.5%     3.41%        -0.28%   38.7%
```

---

## 用法示例（对话里直接说）

> 「现在市场什么情况？」
> → 调 `market_overview`

> 「605258 现在多少钱，值不值得按急跌反弹买？」
> → 调 `quote` + `calc_position`

> 「用急跌反弹跑一遍 2020 到现在的回测，每笔止损 3000」
> → 调 `run_backtest`

> 「今天有急跌反弹的信号吗？」
> → 调 `daily_signal`

---

## 协议细节

- 传输：stdio，**每行一个 JSON-RPC 2.0 消息**
- 协议版本：`2024-11-05`
- 支持方法：`initialize` / `tools/list` / `tools/call` / `ping` / `resources/list` / `prompts/list`
- 通知（无 `id`）不响应
- stdout **只写协议消息**，日志一律走 stderr

---

## 安全边界

- 只读 + 回测，**没有任何下单接口**
- 所有调用都经过 facet API 的参数校验
- `run_backtest` 会占用服务内存（全市场 1150 万行），同时并发多个会拖慢
- 建议单客户端使用，不要在同一个实例上并行跑多个重回测
