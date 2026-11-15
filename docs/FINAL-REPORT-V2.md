# Facet Quant Workbench V2.0 · 交付报告

> 完成时间：2026-11-11 15:10
> 数据：代际 `9d7fb4e6` · 10,946,986 行 · 4,800 只（可交易池）
> 提交：`4e2126b` 起共 15 个提交

---

## 一、七个阶段的完成情况

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 架构审计 | ✅ `docs/AUDIT-V2.0.md` |
| 1 | 基座（SQLite + 日志 + 异常 + API 版本） | ✅ |
| 2 | 前端基座（Vite + React + TS + Tailwind） | ✅ |
| 3 | 八页面迁移 | ✅ 9 个页面 / 2,601 行 |
| 4 | 任务队列（子进程 worker） | ✅ |
| 5 | 数据源抽象（DataProvider） | ✅ |
| 6 | 30 分钟 | ⏸ 按你决定暂缓 |
| 7 | MCP（12 个工具） | ✅ |

---

## 二、数据：从 699 只到 4,800 只可交易

| 阶段 | 标的 | 行数 | 说明 |
|---|---|---|---|
| V1 | 699 | 2,046,760 | 抽样（价格≥3、成交额≥1亿、流通市值≥10亿） |
| 全市场 | 5,218 | 11,599,026 | 东财全量拉取，194 秒 |
| **可交易池** | **4,800** | **10,946,986** | 日均成交额≥5,000万、上市≥250日、价≥3元 |

拉取脚本：`scripts/fetch_all_market.py`（300k 行分片 + sink_parquet，内存恒定）
过滤脚本：`scripts/build_tradable_universe.py`（3 秒，纯 lazy）

---

## 三、策略结论：只有 2 个能赚钱

可交易池（4,800 只）· 2023-2026 · 10 万本金 · 单笔止损 3000 元 · 最多 10 仓 · 单票 25% · 止损 8% · 持仓 20 天

| # | 策略 | 收益 | 年化 | 回撤 | 夏普 | 胜率 | 交易 |
|---|---|---|---|---|---|---|---|
| 1 | **instock_low_backtrace_increase** | **+295.9%** | 43.1% | −58.6% | **0.95** | 26.1% | 739 |
| 2 | **classic_mean_revert** | **+28.4%** | 6.7% | −36.3% | 0.37 | 39.5% | 603 |
| 3 | classic_rsi_oversold | +0.9% | 0.2% | −50.0% | 0.14 | 43.2% | 581 |
| 4 | composite_ic6 | −9.7% | −2.6% | −52.7% | 0.15 | 32.2% | 749 |
| 5 | classic_kdj_cross | −26.6% | −7.7% | −50.1% | −0.17 | 38.5% | 582 |
| 6 | classic_reversal（急跌反弹） | −48.1% | −15.7% | −59.8% | −0.24 | 32.7% | 404 |
| 7 | classic_donchian | −75.8% | −30.8% | −91.8% | −0.30 | 21.3% | 857 |
| 8 | classic_boll_squeeze | −82.2% | −36.2% | −84.2% | −0.98 | 29.2% | 672 |
| 9 | instock_high_tight_flag | −88.2% | −42.7% | −88.2% | −0.88 | 21.4% | 505 |
| 10 | classic_week52_high | −89.4% | −44.2% | −93.7% | −0.27 | 19.8% | 727 |
| 11 | classic_breakout_retest | −92.3% | −48.7% | −93.9% | −1.03 | 22.0% | 699 |
| 12 | terminal_picks | −96.2% | −57.3% | −96.2% | −2.05 | 17.5% | 498 |

完整报告：`reports/account_compare_fullmarket.md`

---

## 四、最重要的三个发现

### 1. 急跌反弹的收益是流动性幻觉

| 池子 | 收益 | 回撤 | 夏普 |
|---|---|---|---|
| 全市场 5,218 只 | +34.4% | −61.7% | 0.41 |
| **可交易 4,800 只** | **−48.1%** | −59.8% | **−0.24** |

只剔除 415 只日均成交额 < 5,000 万的票，策略从赚钱变成亏钱。

**它的全部 alpha 都在那些你买不进、卖不出的票上。**

### 2. 稳健策略会因过滤而变强

| | 全市场 | 可交易池 |
|---|---|---|
| instock_low_backtrace_increase | +45.0%（0.45） | **+295.9%（0.95）** |

剔除不可交易的尾部之后反而更好——这才是策略应有的表现。

### 3. 信号统计口径在扩池后彻底失效

`compare_full_market.py`（V1 的「每日 TopN 等权持有」）：

```
breakout_20d              +2,531,815.8%
classic_donchian          +1,030,368.1%
classic_dual_thrust       +1,071,135.2%
```

不约束资金、不看涨跌停、不管 T+1、不考虑仓位上限，纯复利累积。
**策略评价只能看账户级回测。**

---

## 五、技术交付清单

### 后端新增

```
backend/app/core/            logging.py / errors.py
backend/app/infrastructure/
  ├─ storage/db.py           SQLite 四表（tasks/runs/configs/watchlist）
  ├─ workers/                jobs.py / runner.py（子进程任务）
  └─ data_sources/           base.py / eastmoney.py / sina.py / registry.py
backend/app/modules/
  ├─ tasks/                  api.py / service.py
  └─ monitor/                api.py（SSE）
backend/app/mcp/server.py    12 个工具的 MCP server（零依赖）
```

### 前端

```
frontend/
├─ src/layouts/AppShell.tsx          8 导航 · 实时健康
├─ src/components/                   ui.tsx / EquityChart / KLineChart
├─ src/pages/                        Dashboard / Screener / StockAnalysis /
│                                    TradingPlan / StrategyBacktest /
│                                    Monitor / Tasks / DataManage / Placeholder
└─ src/services/api.ts               类型化 API 客户端
```

构建：**451 KB（142 KB gzip）**，tsc 零报错。

### 数据库表

| 表 | 用途 |
|---|---|
| `tasks` | 任务队列（状态/进度/日志/结果） |
| `runs` | 回测记录（含 generation 哈希，可追溯） |
| `configs` | 用户配置 |
| `watchlist` | 自选股 |

### 新增 API

```
GET  /api/v1/data-sources      数据源能力矩阵与路由
GET  /api/v1/events            SSE 事件流
GET  /api/v1/events/snapshot   一次性快照
POST /api/v1/tasks             提交任务
GET  /api/v1/tasks/{id}        任务详情
GET  /api/v1/tasks             任务列表
```

旧 `/api/*` 全部保留（双挂载）。

---

## 六、性能实测（1,095 万行）

| 操作 | 耗时 |
|---|---|
| 服务启动 | 3s（只读元数据） |
| `health` | 0.17s |
| `overview` | 0.81s → 0.04s（缓存） |
| `kline/单只` | 0.08s |
| `daily-signal` | 0.25s |
| `limit-up-stats` | 2.37s → 0.00s（缓存） |
| 全市场扫描 | 17~20s |
| 账户回测（3 年） | 33~50s |
| 常驻内存 | 约 3~4 GB |

---

## 七、今天踩过并修复的坑

| # | 问题 | 修复 |
|---|---|---|
| 1 | 前复权产生负价格（茅台 2005 年 −302 元） | 统一用后复权 fqt=2 |
| 2 | 排序缺 tiebreaker → 回测结果不可复现 | `sort(key=(-score, symbol))` |
| 3 | 预算占用用当日收盘价（未来数据） | 改用上一交易日收盘 |
| 4 | `BarStore` 全量转 Python 对象 → 全市场 OOM | 重写为 polars 分片 + 惰性构造 |
| 5 | 轻量接口加载全表 → 两个请求就崩 | `_frame_recent` / `_frame_symbol` |
| 6 | 连板统计每次重算 → 崩 | 按代际缓存 |
| 7 | `vite build` 改代码但产物哈希不变 | 路由没接上，模块被 tree-shake |
| 8 | pnpm 11 不读 `package.json` 的 `pnpm` 字段 | 挪到 `pnpm-workspace.yaml` |
| 9 | `Set-Content -Encoding UTF8` 写 BOM → PostCSS 崩 | 统一用 `UTF8Encoding($false)` |
| 10 | 试跑脚本覆盖了真实数据 | 加备份 + 教训：生产数据脚本要先 dry-run |

---

## 八、已知限制（不掩盖）

| 限制 | 说明 |
|---|---|
| **30 分钟回测不可用** | 免费源最长 1,023 根（6 个月），一年需 1,936 根 |
| **无实时行情** | 只有盘后日线；盘中异动、封板监控做不了 |
| **无历史 ST 标记** | 东财快照只给当前名称，无法还原历史 ST 状态 |
| **无退市股完整数据** | 部分退市股能拉，但不完整，仍有幸存者偏差 |
| **回测与实盘有差距** | 保守假设（涨停开盘不买），实盘可能更差 |
| 单策略容量未知 | 4,800 只池子里，10 万本金假设下的收益不代表 1,000 万也成立 |

---

## 九、怎么用

### 启动

```powershell
# 工作区版本（1,095 万行）
cd C:\Users\wang1\Documents\deepseek-harness\default-workspace\facet
powershell -ExecutionPolicy Bypass -File serve.ps1

# 或桌面版（打包好的）
双击 C:\Users\wang1\Desktop\Facet回测台\Facet回测台.exe
```

### 页面

| 地址 | 页面 |
|---|---|
| `http://127.0.0.1:3021/app` | **React 版**（8 个页面） |
| `/` | 旧版交易终端 |
| `/dashboard` | 旧版选股页 |
| `/backtest` `/account` | 旧版回测页 |

### MCP

```powershell
cd backend
python -m app.mcp.server          # stdio
python -m app.mcp.server --http 8765
python -m app.mcp.server --list   # 工具清单
```

配置见 `docs/MCP.md`。

### 命令行

```powershell
python scripts\daily_signal.py --risk 3000       # 今日信号
python scripts\account_compare.py                # 12 策略对比
python scripts\build_tradable_universe.py        # 重建可交易池
python scripts\fetch_all_market.py --bars 3000   # 拉全市场
```

---

## 十、如果要继续

**优先级从高到低**：

1. **验证 `instock_low_backtrace_increase`**（唯一高夏普策略）
   - 样本内外分割：2019-2022 调参 → 2023-2026 验证
   - 逐年分解 + 水下期
   - 换不同池子（沪深300 / 中证500）看是否仍有效

2. **补 30 分钟数据源**（阶段 6）
   - 需要你选一个：付费源 / QMT / 通达信本地 / 自建增量

3. **实时行情**（目前完全缺失）
   - 盘中异动、封板/炸板监控都依赖它

4. **补测试**（现在 13 条只覆盖引擎）

5. **加行业/市值中性化**（避免策略押注单一板块）
