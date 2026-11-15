# 股票风险库（Risk Library）

把「会退市 / 会被 ST / 大概率明年变 ST」的票识别出来，**拉黑后不出现在选股结果、首页榜单、回测里**。

## 一、规则

25 条编号规则 + 2 条名称规则，分三档（命中任意一条即拉黑）：

| 档位 | 规则 | 命中处理 |
|---|---|---|
| `star` *ST 退市风险 | 1–13（扣非+营收、净资产为负、审计否定/无法表示、资金占用、违规担保、逾期披露、造假、连续 20 日股价 < 1 元、连续 20 日市值 < 5 亿/3 亿） | 红色标注 + 拉黑 |
| `st` ST 风险警示 | 14–21（违规担保 1000 万、占净资产 5%、小额占用、账户冻结、失信被执行人、内控非标、主板/双创分红不达标） | 拉黑 |
| `latent` 隐性预雷 | 22–25（营收临界、靠变卖资产/补贴撑利润、商誉过高、实控人高质押） | 拉黑 |
| 名称识别 | 名称含 `*ST` / `ST` | 直接进对应档 |

规则定义在 `backend/app/modules/risk/rules.py`，每条带 `needs`（需要哪些字段）。

## 二、数据来源与「数据缺口」

| 字段 | 来源 | 状态 |
|---|---|---|
| 名称（ST/*ST） | `data/symbol_names.json` | ✅ 自动 |
| 现价、总市值 | `data/fundamentals_snapshot.parquet`（4800 只） | ✅ 自动 |
| 扣非净利、营收、净资产、审计意见、资金占用、违规担保、分红、商誉、质押… | `data/risk/fundamentals.json` | ⏳ 需导入 |

**缺数据不判通过**：需要财报字段的规则在缺字段时状态是 `no-data`（页面显示「缺数据」），
不会被当成"没问题"，也不会误拉黑。

## 三、导入财报字段

```bash
# JSON：{"600519.SH": {"deducted_net_profit": 7.4e10, "revenue": 1.5e11, ...}}
python scripts/import_fundamentals.py --json 财报.json

# CSV：symbol,deducted_net_profit,revenue,...
python scripts/import_fundamentals.py --csv 财报.csv

# 看当前导入了多少
python scripts/import_fundamentals.py --show
```

字段白名单与含义见 `rules.py` 顶部注释（金额单位：**元**；比例字段 0–1；布尔字段
`rectified / bank_account_frozen / dishonest_debtor / rd_exempt / report_delayed / fraud_major`）。

导入后 `POST /api/v1/risk/refresh` 立即生效，否则等后台 10 秒刷新。

## 四、刷新与存储

- 后台线程每 **10 秒**跑一次（`modules/risk/refresh.py`）。
  没有新闻财报数据时**直接跳过**，不碰 polars —— 避免和选股/回测的大表抢内存。
- 每天第一次求值时把「现价 / 市值」写进 `data/risk/snapshots.json`，
  「连续 20 个交易日」类规则靠它累积，不足 20 天时显示进度（`watching`）。
- 文件：
  - `data/risk/flags.json` 命中结果
  - `data/risk/manual.json` 人工拉黑
  - `data/risk/snapshots.json` 每日快照（保留 40 天）
  - `data/risk/state.json` 上次运行状态

## 五、生效范围（黑名单）

| 位置 | 行为 |
|---|---|
| 智能选股 `/api/v1/screener/all` | 结果里剔除黑名单标的 |
| 首页榜单 `/api/v1/dashboard` 的 gain/loss/amount/turnover | 剔除 |
| 策略回测 | `BarStore` 装载时按 symbol 过滤，缓存里不含这些票 |
| 个股分析 | **不拦**（你主动查某只票时应该能看到它，页面有风险标记入口） |

## 六、接口

```
GET    /api/v1/risk/overview        规则清单 + 命中统计 + 缺口
GET    /api/v1/risk/list            命中明细（level / rule 过滤）
GET    /api/v1/risk/blacklist       最终黑名单
POST   /api/v1/risk/blacklist       人工拉黑 {symbol, reason}
DELETE /api/v1/risk/blacklist/{sym} 解除人工拉黑
POST   /api/v1/risk/refresh         立即重算
```

## 七、已知限制

- 财报字段没有自动抓取（审计意见、资金占用、违规担保这类数据没有稳定公开接口），
  需要人工/脚本导入；未导入时 22 条规则处于 `no-data`。
- 「连续 20 个交易日」规则要等快照攒够 20 天；刚上线时只有进度、不会误判。
- 单策略冷路径 `/api/v1/screener?strategy=...`（页面不用）在全市场冷算时内存峰值高，
  与本功能无关，属既有问题。
