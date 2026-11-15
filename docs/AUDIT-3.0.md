# Facet Quant Workbench 3.0 · 第一阶段审计报告

> 审计时间：2026-11-09 16:20
> 审计对象：`facet/`（v0.1.0，62 个策略，73 MB 数据）
> 备份：`facet-backup-20261109-1617/`（15.1 MB，已排除 data/dist/build）
> 结论：**可以增量开发，但"30 分钟连续回测一年"在当前数据源下不成立**（见 §4）

---

## 1. 现有代码审计

### 1.1 文件清单与职责

| 层 | 文件 | 行数 | 职责 | 3.0 是否需要改 |
|---|---|---|---|---|
| 入口 | `backend/app/main.py` | 60 | FastAPI 装配、静态托管 | 加路由，**逻辑不动** |
| 入口 | `backend/app/desktop.py` | 209 | 桌面封装（onedir + 浏览器） | 不动 |
| 配置 | `backend/app/config.py` | 29 | 路径与环境变量 | **需扩展**：加 30m/交易日历路径 |
| 契约 | `backend/app/parquet.py` | 52 | schema 定义 + 兼容扫描 | **需扩展**：加 M30 schema |
| 存储 | `backend/app/store.py` | 147 | 代际发布（原子切指针 + fail-closed） | 复用，新增 `store_30m` |
| 计算 | `backend/app/indicators.py` | 48 | 日线派生列（唯一口径） | **需扩展**：30m 派生列 |
| 引擎 | `backend/app/engine.py` | 239 | 策略加载/校验/执行 | **复用**，信号输出契约不变 |
| 统计 | `backend/app/backtest_stats.py` | 103 | **信号表现统计**（非账户） | **改名隔离**为 signal_stats |
| API | `backend/app/api.py` | 227 | 11 个端点 | **加 12+ 个新端点** |
| 策略 | `strategies/*.py` | 62 个文件 / 2,900 行 | 插件：`META` + `filter_frame` | **全部保留**，只加新策略 |
| 前端 | `web/index.html` | 511 | 选股/概览/回测 | 不动 |
| 前端 | `web/backtest.html` | 260 | 全策略对比台 | **重写为 4 个 tab** |
| 脚本 | `scripts/fetch_history.py` | 203 | 东财日线采集 | **重构**为 fetch_daily_history + fetch_intraday |

### 1.2 模块依赖图

```
                    ┌─────────────┐
                    │  main.py    │
                    └──┬───┬───┬──┘
             ┌─────────┘   │   └──────────┐
             ▼             ▼              ▼
      ┌──────────┐  ┌──────────┐   ┌────────────┐
      │  api.py  │  │ runtime  │   │ 静态托管    │
      └──┬───┬───┘  └──┬───┬───┘   └────────────┘
         │   │         │   │
         │   └─────────┼───┘
         ▼             ▼
   ┌────────────┐  ┌────────┐      ┌──────────────┐
   │ backtest_  │  │engine  │─────▶│ strategies/* │
   │ stats.py   │  └───┬────┘      └──────┬───────┘
   └────┬───────┘      │                  │
        │              ▼                  ▼
        │        ┌──────────┐      ┌─────────────┐
        └───────▶│ store.py │◀─────│ _quantlib   │
                 └────┬─────┘      │ _classic_lib│
                      ▼            │ _alpha_lib  │
                ┌──────────┐       │ _instock_lib│
                │parquet.py│       └─────────────┘
                └────┬─────┘
                     ▼
              data/enriched/gens/<uuid>/data.parquet
```

**关键发现**：依赖是单向的（api → engine/store → parquet），没有循环；
`_quantlib.py` 是所有策略库的公共算子层，新增策略库必须依赖它而不是重写算子。

### 1.3 现有 API 契约（11 个）

| 端点 | 用途 | 3.0 处置 |
|---|---|---|
| `GET /api/health` | 代际/行数/策略数 | 保留，加 `engine_version` |
| `GET /api/overview` | 市场概览 | 保留 |
| `GET /api/strategies` | 策略清单 + 参数表单 | 保留，加 `period` 字段（D1/M30） |
| `POST /api/strategies/reload` | 热加载 | 保留 |
| `GET /api/screener` | 当日选股 | 保留 |
| `GET /api/kline/{symbol}` | 日 K | 保留，加 `period=30m` |
| `POST /api/backtest` | 旧等权轮动 | **改名** `/api/signal-stats` |
| `POST /api/backtest/one` | 单策略统计 | **改名** `/api/signal-stats/one` |
| `POST /api/backtest/batch` | 批量统计 | **改名** `/api/signal-stats/batch` |
| `GET /api/generations` | 代际列表 | 保留，加 `dataset=daily\|30m` |

### 1.4 现有数据字段（enriched 日线）

```
symbol:Utf8  date:Date  open/high/low/close:Float64  volume/amount:Float64
ret_1d  ma5/ma10/ma20/ma60  vol_ma5/vol_ma20  high_20/low_20
amount_ma5  ret_20d  turnover_proxy
change_pct  amplitude  turnover  benchmark
```

行数 149,261（内置样本 250 只）或 409,994（全量 696 只 × 634 日）。

---

## 2. 现状与 3.0 目标的差距

| 3.0 要求 | 现状 | 差距等级 |
|---|---|---|
| 账户级回测（10 万本金、真实资金流转） | **没有**。现有 `backtest_stats.py` 是"信号后 N 日收益平均" | **P0 缺失** |
| T+1 / 100 股 / 涨跌停 / 停牌 / 费用 | 全部没有建模 | **P0 缺失** |
| 30 分钟周期 | **没有**。只有日线 | **P0 缺失** |
| 双周期组合账户 | 没有 | P1 |
| 止盈止损 / ATR / 移动止损 | 没有 | P1 |
| 绩效指标（夏普/索提诺/卡玛/回撤持续期） | 只有 B1/B3/胜率/盈亏比/IC | P1 |
| 参数优化 / 网格搜索 | 没有 | P2 |
| 样本外验证 | 没有 | P2 |
| 历史股票池（含退市股） | 用今日在世股票，**有幸存者偏差** | P1 |
| 策略擂台 | 有"批量对比表"，但基于信号统计不是账户 | P2 |

**结论**：现有系统的价值在"信号研究"（62 个策略、代际数据、IC 统计），
3.0 要在它之上**新建一层账户模拟**，不是推翻重写。任务书要求的"旧回测标记为信号表现分析"
这个判断是对的——两者口径完全不同，混在一起会得出错误的收益率。

---

## 3. 真实连通性测试结果（2026-11-09 实测）

### 3.1 数据源探测

| 数据源 | 周期 | 单次可得根数 | 实测覆盖范围 | 结论 |
|---|---|---|---|---|
| **东财 push2his** | 日线 | **6,423** | 1999-11-10 ~ 2026-11-09 | ✅ 可做 20 年日线回测 |
| 东财 push2his | 30 分钟 | **256** | 2026-09-17 ~ 2026-11-09（32 交易日） | ❌ 太短 |
| 东财 push2his | 15 分钟 | 512 | 同期 | ❌ 太短 |
| 东财 push2his | 60 分钟 | 128 | 同期 | ❌ 太短 |
| **新浪** | 30 分钟 | **1,023** | **2026-04-30 ~ 2026-11-09（约 128 交易日 / 6 个月）** | ⚠️ 目前最长，但**不到一年** |
| 腾讯 | 30 分钟 | 320 | 2026-09-07 ~ 2026-11-09 | ❌ 太短 |

### 3.2 退市股数据（历史股票池可行性）

| 代码 | 名称 | 东财返回 | 最后交易日 |
|---|---|---|---|
| 600001 | 邯郸钢铁 | ✅ 有 | 2009-12-15 |
| 000003 | PT金田A | ✅ 有 | 2002-04-26 |
| 600087 | 退市长油 | ✅ 有 | 2014-06-04 |

**结论**：东财能返回已退市股票的历史日线，**历史股票池可以重建**。

### 3.3 全市场快照可用字段

`f26` = 上市日期、`f100` = 行业、`f14` = 当前名称（含 `*ST`/`ST` 标记）、`f13` = 市场。
**但只有当前 ST 状态，没有历史变更记录。**

---

## 4. 数据缺口（必须先决策）

### 4.1 硬约束：30 分钟回测最多做到 6 个月

任务书要求"30 分钟连续回测一年"。实测免费公开接口：

- 一年需要的 30m 根数 = 242 交易日 × 8 根 = **1,936 根**
- 最长的新浪接口只能给 **1,023 根**（约 6 个月）
- 东财只有 256 根（32 个交易日）

**因此：不换数据源，30 分钟回测的上限是 6 个月，不能声称"一年"。**

### 4.2 可选方案（需要项目负责人选一个）

| 方案 | 成本 | 能覆盖 | 备注 |
|---|---|---|---|
| **A. 付费数据源**（Tushare Pro 分钟线 / RiceQuant / JQData） | 200~2000 元/年 | 3~10 年 30m | 最直接，需注册与积分 |
| **B. 券商 QMT（迅投 miniQMT）** | 免费，需开户 | 完整 30m 历史 | 需券商开通权限，本机装 QMT 客户端 |
| **C. 通达信本地数据（tdxaidata）** | 免费 | 取决于客户端下载的数据年限 | 本机**当前没装** `tdxaidata`、通达信进程未运行 |
| **D. 自建增量采集** | 免费 | 从今天起每天 +8 根 | **一年后才有一年数据**；但可以并行跑，不阻塞其他阶段 |
| **E. 接受 6 个月** | 免费 | 6 个月 | 30m 回测标注"样本外仅 6 个月"，日线照常做 20 年 |

**建议**：**E 打底 + D 并行 + B 备选**。先用新浪 1,023 根把 30m 引擎和回测链路跑通（引擎正确性不依赖历史长度），
同时每天增量采集，等 QMT 或付费源到位后直接补齐历史，引擎不用改。

### 4.3 其余数据缺口

| 需求 | 现状 | 补齐方案 | 难度 |
|---|---|---|---|
| 历史 ST 状态 | 只有当前 | Tushare `namechange`；或用"名称含 ST 的日线收盘数据 + 涨跌幅 5% 特征"反推 | 中 |
| 涨跌停价格 | 无 | `pre_close × 限幅`（主板 10%、创业板/科创板 20%、ST 5%、北交所 30%、新股特殊） | 低 |
| 停牌标记 | 无 | 交易日历左连接，缺失即停牌 | 低 |
| 复权因子 | 无（只有前复权价） | 同时拉 `fqt=0`（原价）与 `fqt=1`（前复权），比值即因子 | 低 |
| 30m 分钟撮合（盘中最高最低） | 无 | 用 30m 的 high/low 判断限价单是否触及 | 中 |
| 交易日历 | 无独立表 | 从指数日线日期集合生成 | 低 |

---

## 5. 技术实施方案

### 5.1 目录结构（新增部分）

```
facet/
├─ backend/app/
│  ├─ backtest/                    ← 新增，账户级引擎
│  │  ├─ models.py      数据类：Bar/Order/Fill/Position/Account
│  │  ├─ data.py        行情读取（日线/30m 统一接口 + 交易日历）
│  │  ├─ broker.py      A股撮合：T+1/涨跌停/停牌/100股/费用
│  │  ├─ portfolio.py   账户：现金/持仓/市值/净值/快照
│  │  ├─ orders.py      订单生成与优先级
│  │  ├─ positions.py   持仓生命周期（止损/止盈/移动止损/超时）
│  │  ├─ risk.py        固定风险仓位、最大持仓、单票上限
│  │  ├─ metrics.py     绩效指标（收益/风险/交易）
│  │  ├─ optimizer.py   网格/随机搜索 + 样本外切分
│  │  └─ engine.py      回测编排器（D1 / M30 / 组合）
│  ├─ signal_stats.py   ← 由 backtest_stats.py 改名，标注"信号表现"
│  └─ api.py            新增 /api/bt/* 一组端点
├─ scripts/
│  ├─ fetch_daily_history.py   ← 重构自 fetch_history.py，支持 fqt=0/1、退市股
│  └─ fetch_intraday.py        ← 新浪 30m 增量采集（断点续传）
└─ data/
   ├─ daily/     已存在
   └─ 30m/       新增，按 symbol 分区
```

### 5.2 核心数据结构

```python
@dataclass(frozen=True)
class Bar:        # 统一 K 线
    symbol: str; dt: datetime; freq: str  # 'D1'|'M30'
    open/high/low/close: float; volume/amount: float
    pre_close: float; change_pct: float; turnover: float

@dataclass
class Order:
    id: str; symbol: str; side: str      # buy|sell
    qty: int; signal_dt: datetime; signal_price: float
    exec_dt: datetime | None; exec_price: float | None
    status: str                          # pending|filled|rejected
    reject_reason: str                   # limit_up|limit_down|suspended|no_cash|lot

@dataclass
class Fill:
    order_id: str; dt: datetime; price: float; qty: int
    commission/stamp_tax/transfer_fee/slippage_cost: float

@dataclass
class Account:
    init_cash: float; cash: float; frozen: float
    positions: dict[str, Position]
    equity: float; nav: float
    realized_pnl: float; unrealized_pnl: float; total_fees: float
```

### 5.3 撮合规则（写成确定性伪代码，可被测试逐条验证）

```
对每个交易日 T：
  1. 开盘前：读取 T 日行情（open/high/low/close/pre_close/是否停牌）
  2. 开盘撮合（处理 T-1 收盘产生的信号）：
       price = open(T) × (1 ± slippage)
       if is_suspended(T):                    reject('suspended')
       if buy  and open==high==low and change_pct>=limit: reject('limit_up_sealed')
       if sell and open==high==low and change_pct<=-limit: reject('limit_down_sealed')
       if buy  and cash < qty*price+fees:     qty = floor_to_100(cash/(price*(1+fee)))
       if qty < 100:                          reject('lot')
  3. 盘中（仅 M30 可用）：对每根 30m 检查止损/止盈触发
       若 bar.low <= stop_price <= bar.high：按 stop_price 成交（保守：先止损后止盈）
       若跳空 low > stop_price：按 open 成交（实际亏损可能大于计划）
  4. 收盘后：计算信号（用 T 日完整 K 线，绝不用未完成 K 线）
             生成 T+1 开盘执行的订单
  5. 收盘结算：equity = cash + Σ(qty × close)，记录净值快照
```

### 5.4 双周期组合账户

- 单一 `Account`，`positions` 里带 `source: 'D1'|'M30'` 标签
- 同一天两个周期都发信号时：按**信号强度分降序 → 周期优先级（D1 优先）→ symbol 字典序** 确定性排序
- 现金只有一个池，先到先得，抢不到的记 `reject('no_cash')`

### 5.5 与现有系统隔离

| 项 | 旧（信号表现） | 新（账户回测） |
|---|---|---|
| 模块 | `signal_stats.py` | `backtest/` |
| API | `/api/signal-stats/*` | `/api/bt/*` |
| 输出 | 信号后 N 日平均收益、IC | 净值曲线、成交明细、夏普、回撤 |
| 前端 tab | 信号表现 | 日线回测 / 30分钟回测 / 组合回测 |
| 数据 | enriched 日线 | daily + 30m |

---

## 6. 十阶段开发计划与验收

| 阶段 | 内容 | 产出 | 验收标准 | 预估 |
|---|---|---|---|---|
| **1** | 审计 + 备份 + 数据探测 | 本文档 | 三份文档齐备 | ✅ 本轮 |
| **2** | 数据层：日线 20 年 + 30m（新浪 1023 根）+ 交易日历 + 退市股 | `fetch_daily_history.py`、`fetch_intraday.py`、`data/30m/` | 跑通 696 只日线 + 100 只 30m；断点续传可验证 | 3h |
| **3** | 账户与撮合引擎 | `backtest/` 全套 + `models.py` | 17 条测试用例中 1~11 通过 | 6h |
| **4** | 日线回测 | `/api/bt/daily` | 10 万本金、单笔 1% 风险、能出净值曲线与成交明细 | 3h |
| **5** | 30 分钟回测 | `/api/bt/m30` | 信号时间/成交时间/延迟三者可核对；未完成 K 线不触发 | 3h |
| **6** | 组合账户回测 | `/api/bt/combo` | 同一天两周期竞争资金，无重复分配（测试用例 12） | 2h |
| **7** | 绩效中心 | `metrics.py` + `/api/bt/metrics` | 12 项指标与手工核算一致 | 2h |
| **8** | 参数优化 + 样本外 | `optimizer.py` + `/api/bt/optimize` | 滚动窗口、综合评分、过拟合提示 | 4h |
| **9** | 前端 4 tab | `web/backtest.html` 重写 | 与现有视觉语言一致，图表+明细+擂台 | 6h |
| **10** | 测试与验收 | `backend/tests/test_backtest_*.py` | 17 条全部通过 + 小规模可手算数据 | 4h |

合计约 **36 小时**（不含等待付费数据源的时间）。

**风险点**：
- 阶段 3 是全部后续工作的地基，必须先过测试再进阶段 4
- 阶段 5 的"未完成 K 线"判定要用**交易所推送时刻**而不是 K 线标签时间（30m K 线标记 10:00 表示 09:30–10:00，信号只能在 10:00 之后产生）
- 阶段 8 若样本外窗口不足（30m 只有 6 个月），必须显式标注"样本外不可用"

---

## 7. 明确的不可行项（不掩盖）

1. **30 分钟回测做不到一年**：免费源最长 1,023 根 ≈ 6 个月（128 交易日）。要么换付费源/QMT，要么接受 6 个月。
2. **盘中订单队列无法还原**：只有 OHLC 数据时，无法判断同一价格上谁先成交。所有撮合只能用保守假设（开盘价成交 + 涨跌停/停牌拒绝），**不能声称是真实成交模拟**。
3. **历史 ST 状态不完整**：只有当前名称。用涨跌幅特征反推是近似，会有误差。
4. **历史退市股清单不完整**：东财能查单只退市股，但没有"某年某月全部在册股票"的现成接口，需要自行拼接（用上市日期 + 最后交易日过滤）。
5. **"世界上所有的策略"不可能穷举**：策略库能覆盖公开范式（技术形态、Alpha101、InStock 的 10 个、巴菲特/价值类需要财务数据），但"所有"是营销语言。当前 62 个 + 计划新增 30~40 个 = 约 100 个，是现实上限。
6. **巴菲特式价值策略缺财务数据**：现有数据只有价量。要做得补 `daily_basic`（PE/PB/ROE）和财报数据，属于阶段 2 的扩展。

---

## 8. 需要项目负责人确认的三件事

1. **30 分钟数据源**：选 A（付费）/ B（QMT）/ C（通达信本地）/ D（自建采集）/ E（接受 6 个月）？
   我的建议是 **E + D 并行**，不阻塞开发。
2. **是否允许改现有 API 路径**：把 `/api/backtest/*` 改成 `/api/signal-stats/*`（语义更准确），
   旧路径保留一个版本作为别名。这会动到已有的 `web/backtest.html`（正好阶段 9 要重写）。
3. **策略库的目标规模**：先做 100 个公开范式，还是要接入 Tushare/财报数据做价值因子？

---

## 9. 下一步（等确认后执行）

阶段 2 的第一步（不依赖任何决策，现在就能做）：

```bash
# 日线扩到 20 年 + 退市股 + 复权因子
python scripts/fetch_daily_history.py --limit 700 --bars 5000

# 30 分钟用新浪源，先把链路跑通
python scripts/fetch_intraday.py --limit 100 --bars 1023
```

在数据到位前，阶段 3（账户引擎）可以并行开发——它只依赖 `Bar` 数据结构，不依赖数据源。
