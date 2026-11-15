# 数据层契约（P1/P2 收口）

本文件记录「同一个东西在不同接口里必须长得一样」的几条硬约定。改接口前先看这里。

## 1. 金额单位：`amount` 一律「元」，`amount_yi` 才是「亿」

| 字段 | 单位 | 出现位置 |
|---|---|---|
| `amount` | **元** | `/boards`、`/boards/{code}`、`/sparklines` 之外的行情接口 |
| `amount_yi` | 亿 | `/boards`（便利字段）、`/dashboard` 榜单、`/screener` 结果 |

历史坑：`/boards` 曾经把同花顺的「亿」原样塞进 `amount`，而 `/boards/{code}` 的 `amount`
是元 —— 同名不同量纲，换个页面就错 1e8。现在 `/boards` 两个字段都给，前端一律读 `amount_yi`。

## 2. 交易日历：从指数日线反推，不手抄节假日

`app/market_calendar.py`：

- `is_trading_day(date)` —— 指数日线（`data/index_daily.parquet`，000001.SH）里有这根 K 线就是交易日；
  在数据范围内但没 K 线 = 休市（春节/国庆/调休都算）
- 数据范围之外（未来）退回「周一至周五」，等数据更新后自动变准
- `is_trading_time()`（实时行情轮询）现在同时判断「工作日 + 是否交易日 + 在不在交易时段」，
  节假日不会再每 3 秒白拉一次

自查：

```bash
curl "localhost:3022/api/v1/health"          # 看 date_end
python -c "from app.market_calendar import calendar_info; print(calendar_info())"
```

## 3. 板块入口：按「代码」和按「名称」各一个，别再新增

| 用途 | 接口 | 数据来源 |
|---|---|---|
| 按代码取成分 | `GET /boards/{code}/members?name=` | 传 name → 本地 `industry_members.json`（与板块 K 线同源）；不传 → 同花顺 |
| 按代码取板块指数 K 线 | `GET /boards/{code}/kline?name=&period=` | 本地成分等权合成，`period=day/week/month/quarter/year` |
| 按名称取成分（看板热度卡用） | `GET /dashboard/board-members` | 本地映射（保留兼容，新代码走上面第一条） |

板块列表 `GET /boards` 仍是同花顺实时涨跌（60s 缓存）。

## 4. 价格口径：`close` 是后复权，别当现价

- `store` 里的 `close/high/low` 都是**后复权**（平安银行 000001 会是 2340 而不是 12）
- 现价只从 `/realtime`（东财快照）或 `fundamentals_snapshot.parquet` 的 `price` 取
- 涨跌幅用 `change_pct` / `ret_1d`，不要用后复权价相减去还原

## 5. 未做的事（有意为之）

- **`api.py` 不拆**：960 行确实偏大，但里面是 18 个互相依赖的行情接口 + 两个进程内缓存
  （`_BARS_CACHE` / `_FRAME_CACHE`），现在拆文件的收益（可读性）小于风险（缓存生命周期错乱）。
  真要拆，先把缓存抽成独立对象再做。
- **自动跑 gen_data**：打包后的 exe 里没有 Python，刷新数据是「人在场」的操作；
  当前用首页角标 + 数据管理红条把「数据旧了」显式暴露出来，而不是偷偷跑。

## 6. 代际发布（publish）的坑：分片目录 + 行数校验

代际目录长这样：

```
enriched/gens/<gen>/data.parquet/     ← 注意：data.parquet 是「多分片目录」
        ├── data.parquet              ← 一个 750MB 的分片文件（名字也叫 data.parquet）
        └── part_backfill_<ts>.parquet
enriched/gens/<gen>/_SUCCESS
enriched/current.json                 ← 指向当前代际
```

`GenerationStore.publish_file(src_dir)` 的行为是 **把 src_dir 整个 move 成新的 `data.parquet`**。所以：

| 规则 | 原因 |
|---|---|
| 复制旧分片要 `rglob("*.parquet")` 并**摊平重命名**（`data.parquet__data.parquet`） | 旧代际里有嵌套同名文件，平级复制会互相覆盖 |
| 新分片必须用**时间戳命名**（`part_backfill_<ts>.parquet`） | 固定名字会和上一批回补的分片重名 |
| 发布前校验 `staging_rows == 实际旧行数 + 新增行数`，不符就中止 | 曾因为用了 manifest 里的 `rows`（会和实际分片对不上）而静默丢过 14 只股票 |
| 基线用 `pl.scan_parquet(旧分片目录).select(len)` 的**实际值** | manifest 的 `rows` 是发布时写的，可能和磁盘不一致 |

回补脚本：`scripts/backfill_symbols.py`（东财 → 腾讯 → 新浪三级回退，幂等，带上述校验）。

## 7. 股票池覆盖现状（2026-11-15）

| 市场 | 名称表 | 已入库 | 说明 |
|---|---|---|---|
| 沪深主板/创业板/科创板 | 5,224 | **5,215** | 基本齐全 |
| 北交所（920xxx.BJ / 8xxxxx.BJ） | 355 | **0** | 腾讯日线返回 501，新浪日线返回空 —— **两个免费日线源都不支持北交所** |
| 合计 | 5,579 | 5,215 | 缺的 364 = 355 北交所 + 9 只被质量闸门拦下 |

补北交所只有两条路：东财恢复后跑 `fetch_all_market.py`，或用 `tdx_local`（需要本地通达信数据目录）。

回补脚本：`scripts/fetch_symbol_list.py`（新浪全市场清单）+ `scripts/backfill_symbols.py`（三级回退抓日线、幂等、带行数校验）。