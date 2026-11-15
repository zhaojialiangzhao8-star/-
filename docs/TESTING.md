# 测试

> 107 条检查 · 4 个套件 · 零依赖（不需要 pytest / httpx，stdlib 直接跑）

## 怎么跑

```powershell
cd C:\Users\wang1\Documents\deepseek-harness\default-workspace\facet\backend

# 1. 回测引擎（13 条，不需要服务）
python tests\test_backtest_core.py

# 2. API / 任务队列 / 实时行情（18 条，需要服务在跑）
python tests\test_api_live.py

# 3. MCP（18 条，需要服务在跑）
python tests\test_mcp.py

# 4. 58 个策略（58 条，约 11 分钟）
python tests\test_strategies.py
```

起服务：

```powershell
powershell -ExecutionPolicy Bypass -File ..\serve.ps1
```

指向别的实例：`$env:FACET_API="http://127.0.0.1:3022"`

---

## 套件明细

### `test_backtest_core.py` — 13 条

| 覆盖 | 说明 |
|---|---|
| 信号与成交分离 | 信号日 T，成交日必须是 T+1 开盘 |
| T+1 锁定 | 当日买入当日不可卖 |
| 涨停开盘 | 拒绝买入 |
| 跌停开盘 | 拒绝卖出 |
| 停牌 | 拒绝交易 |
| 跳空止损 | 跌破止损价时按开盘价成交（亏损会大于计划） |
| 持仓上限 | 超出 `max_positions` 的买单被拒 |
| 无未来数据 | 当日信号不会用当日收盘成交 |
| 费用 | 佣金最低 5 元 / 印花税卖出单边 / 过户费 |
| 100 股整数倍 | 不足一手拒绝 |
| 固定止损金额 | 股数按 (买入价 − 止损价) 反推 |

### `test_api_live.py` — 18 条

| 分组 | 检查 |
|---|---|
| 基础接口 | health 字段完整 / 双挂载一致 / overview / kline 正常 / kline 404 / 错误包装 |
| 选股与信号 | screener 回溯 / 非法策略 400 / daily-signal / 连板晋级率 |
| 数据与前端 | data-sources 能力矩阵 / React 入口 |
| 任务队列 | 提交→轮询→完成 / 非法类型 400 |
| 实时行情 | 自选股增删查 / 实时报价 / 轮询器状态 / SSE 快照 |

### `test_mcp.py` — 18 条

协议：initialize / ping / 通知不响应 / tools-list 完整
错误：未知工具 −32602 / 未知方法 −32601
工具：12 个各调一次（跑真服务）

### `test_strategies.py` — 58 条

每个插件检查：
1. `META` 有 `id` / `name`
2. 能在 2026-03 至今的窗口上跑通不抛异常
3. 输出含 `symbol` / `date` 列

---

## 最近一次结果

```
回测引擎    13 / 13
API         18 / 18
MCP         18 / 18
策略        58 / 58（3 个窗口内无信号，属正常）
```

策略层那 3 个"无信号"：

| 策略 | 原因 |
|---|---|
| `classic_week52_high` | 8 个月窗口内没有创 52 周新高（需要更长历史） |
| `instock_backtrace_ma250` | MA250 需要 250 个交易日，窗口不够 |
| `instock_low_atr` | ATR 分位阈值在窗口内没触发 |

---

## 还没覆盖的

| 缺口 | 影响 |
|---|---|
| 前端 UI | 没有端到端浏览器测试（靠手工截图验证） |
| 并发 | 两个回测同时跑的内存/性能未测 |
| 数据质量 | 缺少复权一致性、退市股、停牌复牌的专项检查 |
| 故障注入 | 数据源超时、代际损坏、SQLite 锁竞争都没测 |
| 长稳 | 没做过 24 小时连续运行测试 |
