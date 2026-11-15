# 重启后恢复指南

> 所有成果都在磁盘上，重启不会丢。重启后按下面三步恢复即可。

---

## 1. 起服务（一条命令）

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\wang1\Documents\deepseek-harness\default-workspace\facet\serve.ps1"
```

已在跑会跳过。看到 `facet started: http://127.0.0.1:3021` 就成了。

或者双击桌面的 `Facet回测台`（它会自己起服务并开浏览器，端口从 3022 起）。

## 2. 打开界面

| 页面 | 地址 | 用途 |
|---|---|---|
| **交易首页（市场看板）** | http://127.0.0.1:3021/app | 涨跌分布 / 情绪雷达 / 热点 |
| **策略回测** | http://127.0.0.1:3021/app/backtest | 账户级回测、净值与成交 |
| **策略库** | http://127.0.0.1:3021/app/strategies | 启用 / 淘汰 / 审计 |

> 旧版静态页（`/workbench`、`/account`、`/backtest`、`/trading`、`/dashboard`）已废弃，
> 访问会被 307 到 `/app`。

## 3. 验证数据在

```powershell
cd C:\Users\wang1\Documents\deepseek-harness\default-workspace\facet
python scripts\oos_reversal.py    # 四关验证，跑通说明引擎正常
```

---

## 什么会丢、什么不会丢

| 项 | 重启后 |
|---|---|
| 代码（81 个 py） | ✅ 在 |
| 数据（524 MB，30 年日线 + 4 个基准） | ✅ 在 |
| 报告（13 个文档） | ✅ 在 |
| 桌面 exe（248 MB） | ✅ 在 |
| 8 万条测试/验证脚本 | ✅ 在 |
| **3021 的服务进程** | ❌ 会停，用 §1 重启 |
| **桌面 exe 的实例** | ❌ 会停，双击重启 |
| **本对话的上下文** | ⚠️ 取决于 DSH，但工作成果不依赖它 |
| **每 2 小时的定时汇报** | ✅ 存在 DSH 里，重启后恢复（下次 09:00） |

---

## 一句话

**没有任何"内存里没保存"的东西**——我所有的工作都是写文件，重启只是要重新起个服务。
