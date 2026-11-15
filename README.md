# 文一股票策略 · Stock Strategy

A 股量化研究与盯盘工作台：本地数据仓库 + 策略插件 + 回测 + 实时看板 + 桌面壳。

> **本仓库不包含任何策略实现。** `backend/app/strategies/` 只有一个空清单，
> 放进你自己的策略 `.py` 就会被加载（一个文件 = 一个策略）。

## 它是什么

| 模块 | 说明 |
|---|---|
| **数据仓库** | parquet 不可变「代际」存全市场日线（含复权因子），可回滚、可校验 |
| **策略引擎** | 一个 `.py` 文件 = 一个策略，读 enriched 表，输出信号 |
| **账户回测** | T+1、次日开盘成交、涨跌停/停牌拒单、100 股整数倍、真实费用 |
| **实时看板** | 涨跌分布、情绪雷达、涨停梯队（盘中走实时报价）、概念/行业热度 |
| **风险库** | 24 条规则（*ST/ST/潜雷）+ 手动黑名单 |
| **桌面壳** | Windows 打包版：内嵌 WebView2 的原生窗口，双击即用 |

## 快速开始

```bash
# 1) 后端
cd backend
python -m venv .venv && .venv/Scripts/pip install fastapi "uvicorn[standard]" "polars>=1.0,<2" pyarrow
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 3022

# 2) 前端
cd frontend
npm install
npm run build          # 产物 frontend/dist，后端会当静态目录托管
```

打开 <http://127.0.0.1:3022/app>。

### 数据从哪来

仓库不带数据。两条路，任选：

1. **通达信本地数据（推荐，离线、最准）**
   在通达信里做一次「系统 → 盘后数据下载」（日线 + 5 分钟，全市场），然后

   ```bash
   python scripts/import_vipdoc.py            # 读 D:\new_tdx\vipdoc → vipdoc_daily.parquet
   python scripts/build_adj_factors.py        # 用 gbbq 算后复权因子
   python scripts/build_store_from_vipdoc.py  # 建代际（原始价 + 后复权价两套列）
   python scripts/add_adj_columns.py          # 补 adj_close / adj_factor
   ```

2. **免费 HTTP 接口**（东财 / 腾讯 / 新浪）
   `scripts/fetch_all_market.py` 拉全市场日线，`scripts/backfill_symbols.py` 补缺票。
   接口会限流，脚本里已做多源回退（东财 → 腾讯 → 新浪）。

### 加自己的策略

```python
# backend/app/strategies/my_strategy.py
import polars as pl

META = {"id": "my_strategy", "name": "我的策略", "tags": ["趋势"]}

def run(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """返回 (symbol, date) 两列，表示该日产生买入信号。"""
    return (
        df.filter(pl.col("close") > pl.col("ma20"))
          .select(["symbol", "date"])
    )
```

放进目录后 `POST /api/strategies/reload`（或重启）即可在「策略库」看到它。

## 数据口径（踩过的坑，写死在这里）

| 列 | 口径 | 用途 |
|---|---|---|
| `close` / `change_pct` | **原始价**（盘面） | 涨停、连板、涨跌幅 —— 和同花顺/通达信一致 |
| `adj_close` / `adj_factor` | **后复权**（gbbq 算的） | 回测收益 —— 除权日不能凭空跌 30% |

两条硬规则：

- **发布前必须体检**：`python scripts/verify_data.py`（唯一性 / 覆盖率 ≥90% / 中位涨跌幅 / 成交额量级 + 双源比对），不通过不要发布
- **单源自检证明不了正确**：必须有第二个源比当天涨跌幅（`scripts/cross_check.py`）

## 目录结构

```
backend/app/
  api.py                 # 行情/回测/选股接口
  store.py               # 代际仓（publish / require / scan）
  indicators.py          # 派生列（均线、量能、新高新低…）
  engine.py              # 策略加载与执行
  backtest/              # 账户回测（broker / portfolio / metrics）
  modules/monitor/       # 看板、涨停梯队、板块
  modules/risk/          # 风险库
  infrastructure/        # 数据源插件（eastmoney / tencent / sina / tdx / vipdoc）
  strategies/            # ← 你的策略（本仓库为空）

frontend/src/
  pages/                 # 业务页面
  tick/                  # 看板（Dashboard 网格、组件、主题）
  services/api.ts        # 所有后端接口的唯一出口

scripts/                 # 数据导入 / 校验 / 回测批跑
docs/                    # 排版规范、数据契约、风险库说明
```

## 技术栈

- 后端：FastAPI + polars（无 pandas 依赖）+ parquet
- 前端：React 18 + Vite + Tailwind + react-query + lightweight-charts + react-grid-layout
- 桌面：PyInstaller + pywebview（内嵌 WebView2），窗口关闭即退出

## 许可

仅供学习研究，不构成投资建议。数据请自行确认来源合规性。
