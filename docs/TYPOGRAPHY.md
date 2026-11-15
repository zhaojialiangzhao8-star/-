# 文字规范（Typography）

> 基准是**交易首页（市场看板）**。全站除首页本身外，字号由 `src/index.css` 里的
> `.app-page` 统一收口；首页走 Tick 组件自己的 `.tick-scope`，是参照系，不改。

## 一、档位

只允许这 6 档。新增字号 = 破坏层次，先改这里再说。

| 角色 | 字号 | 字重 | 行高 | 字体 | 颜色 |
|---|---|---|---|---|---|
| 页面标题 | **20px** | 600 | 28px | Inter（尺寸对齐 Tick PageHeader，纯白不渐变） | `text-fg` |
| 个股现价（个股分析头部） | **18px** | 700 | 1 | JetBrains Mono + tabular（Tick StockInfoBar 口径，按涨跌着色） | `text-up` / `text-down` |
| 区块标题（卡片头） | **12px** | 600 | 1.4 | Inter | `text-fg` |
| 数据 / 表格正文 | **12px** | 400 | 1.45 | 数字 JetBrains Mono，文字 Inter | `text-fg` |
| 正文 / 说明 | **11px** | 400 | 1.45 | Inter | `text-fg2` |
| 标签 / 表头 / 单位 | **10px** | 400 | 1.35 | 数字/代码 Mono，标签 Inter | `text-fg3` |
| 极次要（脚注、角标） | **9px** | 400 | 1.3 | Mono | `text-fg3` |

页面标题 20px 对齐 Tick 的 `PageHeader`（`text-xl`），颜色用纯白；首页的 **18 / 24px** 是看板里的「大数」（指数点位、情绪分），属于首页专有的英雄数字，
其它页面不出现；其它页面的英雄数字用 15px（`.text-lg`）。

## 二、数字与字体

- **所有数字等宽**：`font-mono` + `tabular-nums`（`JetBrains Mono`），涨跌不抖动。
  表格里的价格、涨跌幅、金额、比率、代码，一律 `.num` 或 `font-mono tabular-nums`。
- 中文正文字体：`Inter` → 回退 `HarmonyOS Sans SC` / `PingFang SC` / `Microsoft YaHei`。
- 不使用 `text-base` / `text-2xl` 这类默认档；确需大数用 `.text-lg`（映射 15px）。

## 三、实现位置

| 位置 | 作用 |
|---|---|
| `src/index.css` → `.app-page` | 非首页路由的字号收口（`text-xs`→11、`text-sm`→12、`text-lg`→15、基准 11） |
| `src/index.css` → `.page-title` / `.card-title` | 页面标题 20（渐变）/ 区块标题 12 |
| `src/layouts/AppShell.tsx` | 只有非首页路由挂 `.app-page`（`/` 与 `/market` 不挂） |
| `src/components/ui.tsx` | `Card`（accent 竖条 + 12px 标题）、`Metric`（10px 标签 + 12px 等宽数值）、`DataTable`（10px 表头 / 12px 单元） |

## 四、表格约定

- 表头 10px `text-fg3`，正常字重，不加大写/字距
- 单元格 12px，行内边距 3px（紧凑），斑马纹 `odd:bg-white/[0.015]`，悬停 `bg-brand/[0.10]`
- 数字列右对齐；文本列左对齐；列宽固定 + 末尾弹性列吸收剩余宽度（不要用弹性名称列吃掉整行）
- 涨跌语义色：`text-up`（红，涨）/ `text-down`（绿，跌）/ `text-fg2`（平）

## 五、自检

审计脚本在 `bt_compare/audit_fonts2.mjs`（工作区根目录下），逐路由统计「按文字长度加权」的
字号分布，并报出不在规范档位里的字号：

```bash
# 先起源码服务（3022），再用带调试端口的 headless Edge 跑审计
node bt_compare/audit_fonts2.mjs 9228 /app /app/screener /app/watchlist /app/plan /app/strategies
```

输出里出现 `!!! 非规范字号` 就说明有页面漏改。
