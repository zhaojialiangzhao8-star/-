# Facet Quant Workbench V2.0 · 第一阶段架构审计

> 审计时间：2026-11-10 21:40
> 审计对象：`facet/`（2026-11-10 源码 ZIP，126 文件 / 11,636 行）
> 审计方式：静态代码扫描 + 运行态验证，**未修改任何代码**
> 结论：**可以在现有基础上增量升级，但前端需要重写、数据层有明显缺口、策略质量是最大短板**

---

## A. 当前项目完整目录树

```
facet/
├─ backend/                                  212 项
│  ├─ app/                                   167 项
│  │  ├─ backtest/                            17 项 · 841 行
│  │  │  ├─ models.py      Bar/Order/Fill/Position/Snapshot/Config
│  │  │  ├─ broker.py      T+1·涨跌停·停牌·100股·费用·跳空止损
│  │  │  ├─ portfolio.py   现金/持仓/市值/净值
│  │  │  ├─ risk.py        固定止损金额 → 反推股数
│  │  │  ├─ data.py        parquet 代际 → 按日 Bar 表
│  │  │  ├─ engine.py      开盘撮合→盘中止损→收盘信号→结算
│  │  │  ├─ metrics.py     12 项绩效指标
│  │  │  └─ __init__.py
│  │  ├─ strategies/                         125 项 · 2,900 行
│  │  │  ├─ _quantlib.py       算子层（ts_rank/cs_rank/decay_linear）
│  │  │  ├─ _classic_lib.py    25 个经典技术策略
│  │  │  ├─ _alpha_lib.py      19 个 Alpha101 因子
│  │  │  ├─ _instock_lib.py    10 个 InStock 移植
│  │  │  ├─ terminal_picks.py  终端 picks 反汇编复刻
│  │  │  ├─ composite_ic6.py   IC 加权合成
│  │  │  └─ 44 个薄包装（各 34 行）
│  │  ├─ api.py            14 个端点 · 227 行
│  │  ├─ engine.py         策略加载/校验/执行 · 239 行
│  │  ├─ store.py          代际发布（原子）+ fail-closed · 147 行
│  │  ├─ parquet.py        schema 契约 + 兼容扫描
│  │  ├─ indicators.py     派生列流水线（唯一口径）
│  │  ├─ backtest_stats.py 信号统计（非账户）· 103 行
│  │  ├─ runtime.py        store/engine 单例
│  │  ├─ config.py         路径与环境变量
│  │  ├─ main.py           装配 + 静态托管 + 4 条页面路由
│  │  └─ desktop.py        onedir 桌面封装 · 209 行
│  ├─ tests/
│  │  └─ test_backtest_core.py   13 条 · 307 行
│  ├─ data/                                   524 MB
│  │  ├─ daily/daily.parquet          79.5 MB · 2,060,059 行
│  │  ├─ enriched/gens/<uuid>/        443.8 MB · 7 个代际（应清理到 3）
│  │  └─ benchmark/                    0.7 MB · 沪深300/上证/中证500/创业板指
│  └─ pyproject.toml
├─ web/                                       5 页面 · 1,584 行 · 76 KB
│  ├─ trading.html     295 行  首页（交易终端）
│  ├─ workbench.html   265 行  4 tab 回测中心
│  ├─ account.html     253 行  账户回测
│  ├─ backtest.html    260 行  信号统计
│  └─ index.html       511 行  选股/概览
├─ scripts/                                   14 个 · 1,937 行
│  ├─ fetch_history.py       日线采集（fqt=0/1/2）
│  ├─ fetch_benchmark.py     基准指数
│  ├─ daily_signal.py        每日信号（固定止损金额）
│  ├─ limit_up_stats.py      打板晋级率
│  ├─ grid_reversal.py       120 组参数网格
│  ├─ oos_reversal.py        急跌反弹四关
│  ├─ oos_composite.py       合成 IC6 四关
│  ├─ benchmark_compare.py   基准对比
│  ├─ combo_backtest.py      组合回测
│  ├─ combo_budget.py        风险预算并联
│  ├─ monthly_heatmap.py     月度热力图
│  ├─ eval_picks.py          终端 picks 评估
│  ├─ gen_data.py            合成数据（仅测试）
│  └─ make_bundle_data.py    内置样本
├─ docs/                                      5 篇
├─ reports/                                   9 份
├─ integrations/terminal_inject_facet.py      终端注入
└─ 根：README / CONTRIBUTING / AGENTS / Dockerfile / compose / build.spec / build.ps1 / serve.ps1 / run.ps1
```

---

## B. 当前架构图

```
                    ┌────────────────────────────────────────┐
                    │  浏览器（5 个独立 HTML 页面，无构建）      │
                    │  trading / workbench / account /        │
                    │  backtest / index                       │
                    └───────────────┬────────────────────────┘
                                    │ HTTP (JSON) + 静态托管
                    ┌───────────────▼────────────────────────┐
                    │  FastAPI 单体（main.py 装配）             │
                    │  ├─ api.py        14 端点                │
                    │  ├─ auth          ❌ 无                  │
                    │  ├─ 中间件         ❌ 无                 │
                    │  └─ 静态托管      web/*.html             │
                    └───┬─────────┬──────────┬───────────────┘
                        │         │          │
          ┌─────────────▼──┐  ┌───▼──────┐  ┌▼──────────────┐
          │ engine.py      │  │ store.py │  │ backtest/     │
          │ 策略加载/执行    │  │ 代际发布  │  │ 账户回测 841 行│
          │ 58 个插件       │  │ fail-    │  │ broker/portfolio│
          │                │  │ closed   │  │ risk/metrics   │
          └───────┬────────┘  └───┬──────┘  └───────┬───────┘
                  │               │                 │
          ┌───────▼───────────────▼─────────────────▼───────┐
          │  parquet.py（schema 契约 + 兼容扫描）              │
          └───────────────────────┬──────────────────────────┘
                                  │
          ┌───────────────────────▼──────────────────────────┐
          │  backend/data/                                    │
          │  ├─ daily/daily.parquet      79.5 MB · 206 万行   │
          │  ├─ enriched/gens/<uuid>/    443.8 MB · 7 代际    │
          │  └─ benchmark/*.parquet       0.7 MB · 4 指数     │
          └───────────────────────┬──────────────────────────┘
                                  │
                     ┌────────────▼─────────────┐
                     │  scripts/ 离线采集与验证    │
                     │  东财 · 新浪 · 14 个脚本   │
                     └──────────────────────────┘

  缺失的层（V2 要补）：
    ❌ SQLite（配置/任务/回测记录）
    ❌ Worker（任务队列）
    ❌ DataProvider 抽象（现在供应商逻辑散在 scripts）
    ❌ SSE/WebSocket（实时推送）
    ❌ 鉴权与权限
    ❌ 统一日志
```

---

## C. 现有 API 清单及调用关系

| # | 方法 | 路径 | 用途 | 谁调 | 前端页面 |
|---|---|---|---|---|---|
| 1 | GET | `/api/health` | 代际/行数/策略数 | 全部页面 | 5 个全用 |
| 2 | GET | `/api/overview` | 市场概览（涨跌/涨停/成交额） | dashboard | trading / index |
| 3 | GET | `/api/strategies` | 策略清单 + 参数表单 | 回测页面 | account / workbench |
| 4 | POST | `/api/strategies/reload` | 热加载策略 | 运维 | — |
| 5 | GET | `/api/screener` | 当日选股（含参数覆盖） | 选股 | index |
| 6 | GET | `/api/kline/{symbol}` | 日 K | 图表 | index |
| 7 | POST | `/api/backtest` | 旧等权轮动（**应改名**） | — | — |
| 8 | POST | `/api/backtest/one` | 单策略信号统计 | 对比台 | backtest |
| 9 | POST | `/api/backtest/batch` | 批量信号统计 | 对比台 | backtest |
| 10 | GET | `/api/generations` | 代际列表 | 运维 | — |
| 11 | POST | `/api/bt/run` | **账户级回测** | 账户回测 | account |
| 12 | GET | `/api/bt/capabilities` | 能力声明（支持/不支持） | 账户回测 | account |
| 13 | GET | `/api/daily-signal` | 每日信号（固定止损金额） | 交易首页 | trading |
| 14 | GET | `/api/limit-up-stats` | 打板晋级率 | 交易首页 | trading |

**调用关系**：

```
/health ─────────────┬─→ trading / workbench / account / backtest / index
/overview ───────────┼─→ trading / index
/strategies ─────────┼─→ account / workbench
/kline ──────────────┼─→ index
/screener ───────────┼─→ index
/backtest/one|batch ─┴─→ backtest

/bt/run ───────────────→ account
/bt/capabilities ──────→ account
/daily-signal ─────────→ trading
/limit-up-stats ───────→ trading
```

**问题**：
- `/api/backtest`（旧等权）与 `/api/bt/run`（账户）并存，命名混淆，V2 应把旧的改名 `/api/signal-stats`
- 无版本前缀（`/api/v1/`），未来破坏性变更无处兼容
- 无鉴权、无速率限制

---

## D. 现有回测引擎执行流程

### 日循环（`backtest/engine.py::run`）

```
for d in calendar[start:end]:
    day_bars = bars.day(d)                    # 当日所有标的的 OHLC
    if not day_bars: continue

    ① 09:25 开盘撮合 _open_auction(d)
       for order in pending:                  # 上一交易日收盘产生的单
           bar = day_bars.get(order.symbol)
           buy:  check_buy → 涨停开盘拒 / 停牌拒 / 一字板拒
                 price = open × (1 + slip)
                 stop = 订单级参数 or 策略级参数
                 qty = risk_amount / (price − stop)   ← 固定止损金额
                 受 max_position_pct 与现金约束，向下取整 100 股
                 预算检查：该 source 已占用 + 本次 ≤ equity × budget
           sell: check_sell → 跌停拒 / T+1 拒（open_dt == d）
       pending = [仍是 PENDING 的]

    ② 09:30 盘中止损止盈 _intraday_exits(d)
       for pos in positions:
           if pos.open_dt.date() >= d: continue          ← T+1
           stop_triggered(bar.low, stop) → 跳空按 open，盘中按 stop
           target_triggered(bar.high, target)
           max_hold_days 到期 → 按 close 卖
           移动止损上移 high_water

    ③ 15:00 收盘信号（只用当日已完成 K 线）
       for sym, score, src in sig[d]:
           if 已持仓 或 slots<=0: continue
           生成 Order(PENDING)，T+1 开盘执行

    ④ 15:05 结算
       equity = cash + Σ(qty × close)
       写 Snapshot(nav, fees, realized, unrealized)
```

### 关键设计（V2 必须保留）

| 设计 | 位置 | 为什么不能重写 |
|---|---|---|
| 信号与成交分离（T+1 开盘） | `engine.run` 步骤 ③→次日 ① | 这是"不追当日收盘价"的唯一保证 |
| 确定性 tiebreaker `(-score, symbol)` | `engine.run` | 没有它多因子结果不可复现（已踩过坑） |
| 订单级风控参数 | `Order.stop_loss_pct` | 多策略并联时每个策略用自己的止损 |
| 风险预算 `strategy_budget` | `engine._open_auction` | 防止信号量大的策略抢光仓位 |
| 固定止损金额 → 反推股数 | `risk.size_by_risk` | 用户明确要求的仓位方式 |
| 跳空止损按开盘价 | `broker.stop_triggered` | 保守假设，亏损会大于计划 |
| 权益 = 现金 + 持仓市值 | `portfolio.snapshot` | 绝不用收益率平均 |
| 13 条测试 | `tests/test_backtest_core.py` | 覆盖 T+1/涨跌停/停牌/跳空/费用/100股/无未来数据 |

### 未覆盖（V2 要补）

| 缺失 | 影响 |
|---|---|
| 30 分钟周期 | `period` 字段预留但未实现 |
| 复权因子管理 | 只存后复权价，无原始价与因子表 |
| 除权除息事件 | 分红送转在数据里已体现，但回测无"除权日"处理 |
| 交易日历表 | 从行情日期隐式推导，无独立表 |
| 多个策略共用一个账户时的资金优先级策略 | 目前只有 score 排序 |

---

## E. 现有策略插件清单（58 个）

| 家族 | 数量 | 文件 | 验证结论 |
|---|---|---|---|
| **经典技术** | 25 | `_classic_lib.py` + `classic_*.py` | 急跌反弹✅通过；52周新高⚠️谨慎；其余❌ |
| **Alpha101** | 19 | `_alpha_lib.py` + `alpha*.py` | 全部 IC 接近 0，单因子无效 |
| **InStock 移植** | 10 | `_instock_lib.py` + `instock_*.py` | 高窄旗形❌（账户回测 0%）；其余不建议 |
| **终端 picks** | 1 | `terminal_picks.py` | ❌ 样本外 4 年 +2.0%，水下 3.5 年 |
| **合成** | 1 | `composite_ic6.py` | ❌ 与 picks 同源，同样证伪 |
| **示例** | 2 | `ma_trend.py` / `breakout_20d.py` | 无判定 |

**插件协议**（`engine.py` 约定）：

```python
META = {
    "id": "唯一ID", "name": "显示名", "description": "...",
    "tags": [...], "params": [{"id","label","type","default",...}],
    "order_by": "score", "descending": True, "limit": 0,
}
def filter_frame(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    # 返回必须含 symbol / date 两列
```

**V2 需要扩展的字段**（任务书 §七要求）：`version`、`periods`（D1/M30）、`data_requirements`、`risk_rules`、`output_schema`。

---

## F. 前端五个页面的功能和依赖

| 页面 | 行数 | 功能 | 依赖 API | 技术 |
|---|---|---|---|---|
| **trading.html** | 295 | 首页：指数条 + 今日信号 + 打板晋级率 + 市场概览 + 策略状态 + 实盘规则 | health / overview / daily-signal / limit-up-stats | 原生 JS + 手写 CSS |
| **workbench.html** | 265 | 4 tab：策略体检 / 信号表现(iframe) / 日线回测(iframe) / 30分钟(占位) / 组合 | health + 2 个 iframe | iframe 套壳 |
| **account.html** | 253 | 账户回测：参数表单 + 10 指标卡 + 净值 SVG + 成交明细 | health / strategies / bt/run / bt/capabilities | 原生 JS + SVG |
| **backtest.html** | 260 | 58 策略对比表（可排序/并发 2） | health / overview / strategies / backtest/one | 原生 JS |
| **index.html** | 511 | 选股 + K 线 + 概览 + 离线 mock 回退 | health / overview / strategies / screener / kline | 原生 JS + SVG |

**共性问题**：
- **零构建**：无打包、无模块化，5 个页面各写一遍 CSS（约 300 行 × 5）
- **无组件复用**：卡片/表格/图表每个页面重写
- **无类型**：全 JS，API 契约靠约定
- **无状态管理**：每个页面自己 fetch，无缓存
- **图表是手写 SVG**：K 线不支持缩放/十字光标
- **workbench 用 iframe 套壳**：架构上是临时方案

---

## G. 数据存储与数据流向

### 存储（backend/data/，524 MB）

| 路径 | 大小 | 内容 | 生产者 | 消费者 |
|---|---|---|---|---|
| `daily/daily.parquet` | 79.5 MB | 2,060,059 行 · 700 只 · 1996-2026 | `fetch_history.py` | `indicators.build_enriched` |
| `enriched/gens/<uuid>/data.parquet` | 443.8 MB（7 代） | 2,046,760 行 × 24 列 | `indicators` + `store.publish` | engine / backtest / api |
| `enriched/CURRENT.json` | <1 KB | 指针 + 行数 + 时间 | `store.publish` | `store.require` |
| `benchmark/*.parquet` | 0.7 MB | 4 个指数 × 5,000 根 | `fetch_benchmark.py` | `benchmark_compare.py` |
| `bundled/` | 14.9 MB | 250 只 × 634 天（exe 内置） | `make_bundle_data.py` | 桌面 exe |

### 数据流

```
东财 push2his ──fetch_history.py──→ daily.parquet（79.5 MB）
                                        │
                                   indicators.build_enriched（Polars 一次算完 68 列）
                                        │
                                   store.publish()  原子切指针
                                        ↓
                              enriched/gens/<uuid>/data.parquet
                                        │
                          ┌─────────────┼──────────────┐
                          ↓             ↓              ↓
                     engine.run    BarStore       indicators.market_overview
                     （策略信号）   （按日 Bar）      （市场概览）
                          │             │
                          └──────┬──────┘
                                 ↓
                        BacktestEngine.run → Snapshot / Fill / Order
                                 ↓
                        metrics.compute → 12 项指标
                                 ↓
                              api.py → 前端
```

### 关键机制（V2 保留）

- **代际发布**：写 `gens/<uuid>/` + `_SUCCESS` → 原子切 `CURRENT.json`；读者只认有 `_SUCCESS` 的代际，拿不到抛 `StoreUnavailable` → 503（fail-closed）
- **schema 兼容**：`missing_columns="insert"` / `extra_columns="ignore"`，只增不改
- **缓存**：`api._BARS_CACHE[generation]`（BarStore 2~3 秒构建）+ `_PX_CACHE` + `_BATCH_CACHE`

### 缺口

| 缺口 | 影响 | V2 方案 |
|---|---|---|
| **无 SQLite** | 配置/任务/回测记录无持久化 | 新增 4 张表 |
| **7 个代际占用 443 MB** | 磁盘浪费 | GC 保留 3 代（代码有逻辑但未生效） |
| **无复权因子表** | 换复权口径要重拉 | 新增 `adjust_factor` 列 + 单独表 |
| **无交易日历表** | 停牌判定靠隐式推导 | 从指数日期生成 |
| **无分钟线** | 30 分钟回测无法做 | 见 §H |

---

## H. 当前系统的主要技术债务

按严重程度排序。

### P0（阻塞 V2 目标）

| # | 债务 | 具体表现 | 影响 |
|---|---|---|---|
| 1 | **30 分钟数据缺失** | 免费源最长 1,023 根（6 个月），一年需 1,936 根 | 任务书的"日线+30分钟"直接做不了 |
| 2 | **无任务队列** | 批量回测跑在 HTTP 请求里，57 个策略 92 秒，参数网格 120 组 72 秒 | 前端超时、无法并发、无进度 |
| 3 | **无实时数据** | 只有盘后采集，无行情订阅 | 「实时监控」模块无数据基础 |
| 4 | **策略质量** | 58 个策略只有 1 个通过四关验证 | 系统再好，没有可用信号 |

### P1（影响可用性）

| # | 债务 | 表现 |
|---|---|---|
| 5 | **前端零构建** | 5 个 HTML、1,584 行、CSS 重复 5 份、手写 SVG |
| 6 | **无鉴权** | 任何能连 3021 的进程都能跑回测、读数据 |
| 7 | **无持久化** | 回测结果只在内存，关掉就没了 |
| 8 | **API 无版本** | `/api/backtest` 与 `/api/bt/run` 语义混淆 |
| 9 | **无统一日志** | 只有 `boot.log` 和 uvicorn 输出 |

### P2（工程质量）

| # | 债务 | 表现 |
|---|---|---|
| 10 | 测试覆盖窄 | 13 条只覆盖引擎，API/前端/策略 0 条 |
| 11 | 策略代码重复 | 44 个薄包装各 34 行，可合并 |
| 12 | 代际未清理 | 7 代 443 MB，`_gc` 逻辑存在但 `KEEP_GENERATIONS` 未生效 |
| 13 | 脚本无测试 | 14 个脚本 1,937 行，零单测 |
| 14 | 数据源耦合 | 东财/新浪逻辑散在 `fetch_*.py`，无 Provider 抽象 |

---

## I. 对标 TSP 的差距分析

| 维度 | TSP（tick-stock-panel） | Facet 现状 | 差距 |
|---|---|---|---|
| **后端规模** | 40 个 router、100 个 service、82 万行 | 14 端点、8 模块、1.16 万行 | 约 70 倍 |
| **前端** | React 18 + Vite + TS + TanStack Query + ECharts + lightweight-charts | 5 个独立 HTML + 手写 SVG | **架构代差** |
| **测试** | 482 个测试文件、3000+ 用例 | 13 条 | 约 200 倍 |
| **数据层** | Parquet + DuckDB + 68 列指标 + 多数据源能力路由 | Parquet + 24 列 + 单源 | 中 |
| **回测** | numba 矩阵引擎（156 KB）+ 独立 worker 进程 + 完整 T+1 | Python 逐日循环 + 单进程 | 性能差距大 |
| **实时** | APScheduler + SSE + 语音/飞书推送 | 无 | 全缺 |
| **扩展系统** | 插槽/注册表/外部扩展包/页面插件 | 仅策略插件 | 缺 3 层 |
| **任务** | APScheduler + 重任务限流 + 内存预算 | 无 | 全缺 |
| **AI** | 内置 Codex CLI + 65 端点 Open API + MCP 12 工具 | 无 | 全缺 |
| **数据源** | TickFlow/fuyao/TdxQuant 三源 + 能力矩阵 | 东财 + 新浪 | 中 |
| **二进制分发** | GHCR 多架构 + PyInstaller 桌面 | onedir 桌面 | 相当 |

**结论**：TSP 是 5 个月、单人 + AI 代理、日均 10 次提交堆出来的 82 万行系统。
Facet 现在 1.16 万行，**不可能在一个阶段追平**，但**架构上可以对齐**——关键是前端换成 React、加任务队列、加 SQLite、加 DataProvider。

---

## J. V2.0 目标架构图

```
┌─────────────────────────────────────────────────────────────────┐
│  React SPA（Vite + TS + Tailwind + TanStack Query）              │
│  ├─ layouts/  AppShell（左侧 6 导航 + 顶部状态条）                  │
│  ├─ pages/    dashboard · screener · analysis ·                  │
│  │            trading-plan · backtest · account · monitor        │
│  ├─ components/  Card / DataTable / KLineChart / MetricGrid      │
│  ├─ services/    api.ts（统一 fetch + zod 校验）                   │
│  └─ stores/      zustand（任务进度 / 筛选条件 / 用户配置）           │
└──────────────┬──────────────────────────────────────────────────┘
               │ HTTP /api/v1/* + SSE /api/v1/events
┌──────────────▼──────────────────────────────────────────────────┐
│  FastAPI 模块化单体                                               │
│  ├─ api/v1/      路由层（薄，只做校验与序列化）                     │
│  ├─ core/        配置 / 日志 / 鉴权 / 异常 / 依赖注入               │
│  └─ modules/                                                    │
│     ├─ market/     行情查询（overview / kline / 指标）             │
│     ├─ screener/   选股引擎（复用 engine.py + 58 策略）            │
│     ├─ analysis/   个股分析（指标计算）                            │
│     ├─ plan/       交易计划（固定止损金额）← 复用 risk.py           │
│     ├─ backtest/   ← **原样保留 backtest/ 841 行**                │
│     ├─ portfolio/  ← 复用 portfolio.py                           │
│     ├─ risk/       ← 复用 risk.py                                │
│     ├─ sentiment/  情绪周期 / 连板晋級率                           │
│     ├─ monitor/    监控规则 + SSE 推送                            │
│     └─ tasks/      任务提交 / 状态 / 日志                          │
├─────────────────────────────────────────────────────────────────┤
│  infrastructure/                                                │
│  ├─ data_sources/  DataProvider 抽象                             │
│  │   ├─ eastmoney.py   日线/快照（现有逻辑迁入）                    │
│  │   ├─ sina.py        30 分钟（1,023 根）                        │
│  │   ├─ qmt.py         预留（阶段 6）                             │
│  │   └─ registry.py    能力声明 + 路由矩阵                         │
│  ├─ storage/       GenerationStore（保留） + SQLite + Parquet     │
│  └─ workers/       子进程任务执行器（SQLite 轮询）                  │
└──────────────┬──────────────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────────────┐
│  数据层                                                          │
│  ├─ data/daily/*.parquet        日线（现有）                      │
│  ├─ data/minute/*.parquet       30 分钟（新增）                    │
│  ├─ data/gens/<uuid>/           代际（现有）                       │
│  ├─ data/database/facet.db      SQLite                           │
│  │   ├─ tasks       id/type/status/progress/log/start/end        │
│  │   ├─ runs        id/strategy/params/metrics/result_path       │
│  │   ├─ configs     key/value                                    │
│  │   └─ watchlist   symbol/group/note                            │
│  └─ data/benchmark/*.parquet    基准（现有）                       │
└─────────────────────────────────────────────────────────────────┘
```

**关键原则**：
1. `backtest/` 841 行**一行不改**（只加包一层 `modules/backtest` 转发）
2. `store.py` / `parquet.py` / `strategies/` **保留**
3. 现有 14 个 API **全部保留**，新 API 走 `/api/v1/`，旧 API 加 deprecation 标记
4. 前端**重写但保留旧页面**（`/legacy/*`），迁移一页验证一页

---

## K. 分阶段迁移方案

| 阶段 | 内容 | 关键动作 | 前端可用性 | 预估 |
|---|---|---|---|---|
| **0** | 审计（本报告） | 不改代码 | 现状 | ✅ 本轮 |
| **1** | **基座** | git 初始化 + 分支；SQLite 建 4 表；`core/`（配置/日志/异常/依赖）；`api/v1` 前缀 + 旧 API 别名 | 不变 | 1 天 |
| **2** | **前端基座** | Vite + React + TS + Tailwind + 路由 + AppShell + api 客户端；先把 `dashboard` 迁过去读现有 API | 新旧并存 | 2 天 |
| **3** | **六页面迁移** | screener → analysis → trading-plan → backtest → account → monitor，**每迁一页验证旧功能** | 逐页替换 | 4 天 |
| **4** | **任务层** | SQLite 任务表 + 子进程 worker + 进度回写 + SSE 推送；批量回测/参数网格迁进出 | 前端显示进度 | 2 天 |
| **5** | **数据源抽象** | `DataProvider` 接口 + 东财/新浪 Provider + 能力声明；`fetch_*.py` 改为调 Provider | 不变 | 2 天 |
| **6** | **30 分钟** | 数据源决策后：采集 + schema + `BarStore` 支持 M30 + 引擎 `period` 分支 | 新页面 | 3 天（依赖数据源） |
| **7** | **MCP** | 13 个工具接口 + 参数校验 + 权限 + 审计日志 | 不变 | 1 天 |

**总计约 15 个工作日**（不含 30 分钟数据源等待）。

**顺序理由**：
- 阶段 1 先做 SQLite 和 `core/`，后面所有模块都依赖
- 阶段 2 前端基座先跑通 dashboard，验证技术栈可行再批量迁
- 阶段 4 任务层必须在阶段 3 之后（页面需要展示进度）
- 阶段 6 卡数据源，可以并行推进，不阻塞 1-5

---

## L. 风险与兼容性分析

### 高风险（可能破坏现有功能）

| 风险 | 触发点 | 缓解 |
|---|---|---|
| **前端重写导致功能丢失** | 阶段 3 迁页面时漏掉旧页面细节（如 account 的拒单明细、backtest 的排序） | 每页迁移前列"旧功能清单"，迁完逐项打勾；旧页面保留在 `/legacy/*` 直到确认 |
| **API 变更破坏前端** | 加 `/api/v1/` 前缀 | 旧路径全部保留（同一 handler 双挂载），加 `Deprecation` 响应头 |
| **SQLite 与 Parquet 数据不一致** | 回测记录写 SQLite，数据在 Parquet | 记录里存 `generation` 哈希，可追溯 |
| **任务进程并发写数据** | 两个回测同时跑 | 只读 Parquet，写结果到各自目录；SQLite 用 WAL 模式 |
| **策略插件协议扩展破坏现有 58 个** | 加 `version`/`periods` 必填 | 新字段全部可选，缺省推导；`engine.py` 加兼容层 |

### 中风险

| 风险 | 缓解 |
|---|---|
| React 生态学习成本（如果以后要你自己改） | 保持组件极简，不引入复杂状态库（zustand 足够） |
| 30 分钟数据源未定 | 阶段 6 独立，不阻塞 1-5；用新浪 1,023 根先跑通链路 |
| 数据目录 524 MB，多代际堆积 | 阶段 1 顺手修 `_gc`（保留 3 代，省约 300 MB） |

### 低风险

| 风险 | 说明 |
|---|---|
| PyInstaller 打包体积 | 加 React dist 后 +2 MB，可忽略 |
| 现有测试失效 | 13 条只测 `backtest/`，不动它就不会失效 |

### 兼容性承诺（V2 全程有效）

1. `backtest/broker|portfolio|risk|engine|metrics` —— **一行不改**
2. `store.py` / `parquet.py` / `strategies/` —— **保留**
3. 现有 14 个 API —— **全部可用**（旧路径保留）
4. 现有 5 个 HTML —— **保留** `/legacy/*`
5. `backend/data/` —— **不迁移、不删除**，只增不改

---

## M. 每阶段验收标准

| 阶段 | 验收标准（全部满足才算完成） |
|---|---|
| **1 基座** | ① `facet.db` 建表成功，4 张表结构正确 ② `/api/v1/health` 与 `/api/health` 同时返回 200 ③ 旧 14 个端点全部 200 ④ 13 条测试仍全绿 ⑤ 日志落到 `logs/app.log`，含请求 id |
| **2 前端基座** | ① `pnpm build` 成功，产物 < 500 KB ② dashboard 页面数据与旧版一致（截图对比） ③ 旧页面仍可访问 ④ 无 console 错误 |
| **3 六页面迁移** | 每页：① 功能清单逐项打勾 ② 与旧页面同数据同结果（数值 diff = 0） ③ 响应式（1366/1920/768 三档） ④ 无空壳按钮 |
| **4 任务层** | ① 提交批量回测返回 task_id ② 轮询能看到 progress 0→100 ③ 完成后结果落 SQLite ④ 杀进程重启后任务状态可恢复 ⑤ 同一时刻两个任务不互相污染 |
| **5 数据源** | ① `DataProvider` 接口有 ≥2 个实现 ② 能力声明可查 ③ 换源后 `daily.parquet` 内容一致（除时间戳） ④ 采集失败有明确错误码 |
| **6 30 分钟** | ① 数据覆盖 ≥6 个月（新浪） ② schema 校验通过 ③ 引擎能跑 M30 回测 ④ 信号时间/成交时间可核对 ⑤ 未完成 K 线不触发 |
| **7 MCP** | ① 13 个工具全部可调用 ② 参数校验拒绝非法输入 ③ 每次调用有审计日志 ④ 无写权限（只能读+回测） |

### 全局验收（V2 完成时）

1. 现有 13 条测试 + 新增测试全部通过
2. 旧 5 个页面在 `/legacy/*` 可用
3. 现有 14 个 API 全部可用
4. 六页面全部有真实数据（无 mock、无空壳）
5. 回测结果可追溯到 `generation + strategy_version + params`
6. 文档：`docs/architecture/` 下有架构图与迁移记录

---

## 附：本次审计发现的具体问题清单

| # | 问题 | 位置 | 建议 |
|---|---|---|---|
| 1 | enriched 7 个代际占 443.8 MB | `data/enriched/gens/` | 修 `KEEP_GENERATIONS` GC，保留 3 代 |
| 2 | `/api/backtest` 与 `/api/bt/run` 语义混淆 | `api.py` | 旧的改名 `/api/signal-stats` |
| 3 | 44 个策略薄包装各 34 行重复 | `strategies/` | 生成器保留，运行时用注册表 |
| 4 | workbench 用 iframe 套壳 | `web/workbench.html` | React 迁移时改为组件 |
| 5 | 无 API 版本前缀 | `api.py` | 阶段 1 加 `/api/v1` |
| 6 | 缓存全在进程内存 | `api.py` `_BARS_CACHE` 等 | 阶段 1 加进程间缓存（SQLite 或文件） |
| 7 | 数据目录未纳入 git | 根 `.gitignore`（无） | 阶段 1 建 `.gitignore` |
| 8 | 脚本无 CLI 统一入口 | `scripts/` | 加 `scripts/cli.py` 子命令 |

---

**审计完成。等待确认后进入阶段 1。**
