import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, Bell, Radio, Wifi, WifiOff } from 'lucide-react'
import { api } from '../services/api'
import { Card, Metric } from '../components/ui'
import { fmtInt } from '../lib/format'

interface Snapshot {
  ts: string
  health: { ok: boolean; generation?: string; rows?: number; strategies?: number; errors?: number; error?: string }
  running: { id: string; type: string; progress: number; log: string[] }[]
  recent: { id: string; type: string; status: string; progress: number; finished_at: string; error: string }[]
}

interface Quote {
  symbol: string
  name?: string
  price?: number
  change_pct?: number
  volume?: number
  vol_ratio?: number
  turnover?: number
  market_cap?: number
  pe_ttm?: number
  pb?: number
  limit_up?: number
  limit_down?: number
  age_ms?: number
  available: boolean
  /** trading | closed —— 收盘后不再显示「N 秒前」 */
  session?: string
}

const TYPE_LABEL: Record<string, string> = { backtest: '账户回测', scan: '全市场扫描', grid: '参数网格' }

export default function Monitor() {
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState<{ ts: string; text: string; kind: 'info' | 'warn' | 'ok' }[]>([])
  const [retry, setRetry] = useState(0)

  const signal = useQuery({ queryKey: ['signal', 3000], queryFn: () => api.dailySignal(3000) })
  const limitUp = useQuery({ queryKey: ['limitUp'], queryFn: api.limitUp, staleTime: 600_000 })

  // 实时行情：3 秒轮询自选股
  const rt = useQuery({
    queryKey: ['realtime'],
    queryFn: () =>
      fetch('/api/v1/realtime').then((r) => r.json()) as Promise<{
        count: number
        trading: boolean
        quotes: Quote[]
      }>,
    refetchInterval: 3_000,
  })
  const quotes = rt.data?.quotes ?? []
  const trading = rt.data?.trading ?? false

  // 自选股管理
  const [addSym, setAddSym] = useState('')
  const [adding, setAdding] = useState(false)
  const watch = useQuery({
    queryKey: ['watchlist'],
    queryFn: () => fetch('/api/v1/watchlist').then((r) => r.json()) as Promise<{ count: number; items: { symbol: string; name?: string }[] }>,
    refetchInterval: 30_000,
  })

  const addWatch = async () => {
    const sym = addSym.trim().toUpperCase()
    if (!sym) return
    setAdding(true)
    try {
      const r = await fetch('/api/v1/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, name: '' }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setAddSym('')
      await watch.refetch()
      await rt.refetch()
    } catch (e) {
      // 失败时把输入框标红由下面的提示承担
      console.warn('add watch failed', e)
    } finally {
      setAdding(false)
    }
  }

  const delWatch = async (sym: string) => {
    await fetch(`/api/v1/watchlist/${sym}`, { method: 'DELETE' })
    await watch.refetch()
    await rt.refetch()
  }

  // SSE 订阅：断了 3 秒后重连。?static=1 只拉一次快照（截图 / 降级用）
  useEffect(() => {
    let es: EventSource | null = null
    let timer: number | undefined
    const staticMode = new URLSearchParams(window.location.search).get('static') === '1'

    const push = (text: string, kind: 'info' | 'warn' | 'ok') => {
      setEvents((prev) => {
        const next = [{ ts: new Date().toLocaleTimeString('zh-CN', { hour12: false }), text, kind }, ...prev]
        return next.slice(0, 60)
      })
    }

    const applySnap = (d: Snapshot) => {
      setSnap(d)
      setConnected(true)
      if (!d.health.ok) push('数据代际不可用：' + (d.health.error ?? ''), 'warn')
      d.running.forEach((t) =>
        push(`${TYPE_LABEL[t.type] ?? t.type} ${t.id.slice(0, 8)} 进度 ${Math.round(t.progress * 100)}%`, 'info'),
      )
    }

    if (staticMode) {
      fetch('/api/v1/events/snapshot')
        .then((r) => r.json())
        .then((d: Snapshot) => {
          applySnap(d)
          push('静态快照模式（?static=1），不建立长连接', 'ok')
        })
        .catch(() => push('快照获取失败', 'warn'))
      return
    }

    const connect = () => {
      es = new EventSource('/api/v1/events')
      es.addEventListener('open', () => {
        setConnected(true)
        push('已连接到事件流', 'ok')
      })
      es.addEventListener('snapshot', (e) => {
        applySnap(JSON.parse((e as MessageEvent).data))
      })
      es.addEventListener('error', () => {
        setConnected(false)
        es?.close()
        timer = window.setTimeout(() => {
          setRetry((r) => r + 1)
          connect()
        }, 3000)
      })
    }

    connect()
    return () => {
      es?.close()
      if (timer) window.clearTimeout(timer)
    }
  }, [retry])

  const h = snap?.health

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">实时监控</h2>
        <span className="font-mono text-xs2 text-fg3">
          SSE 推送 · 任务/引擎/数据状态{snap ? ` · 最后更新 ${snap.ts}` : ''}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <span
            className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-xs2 ${
              connected ? 'border-down/40 bg-down/10 text-down' : 'border-gold/40 bg-gold/10 text-gold'
            }`}
          >
            {connected ? <Wifi size={11} /> : <WifiOff size={11} />}
            {connected ? '已连接' : '重连中'}
          </span>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-12 gap-3">
          {/* 自选股实时报价 */}
          <Card
            className="col-span-12"
            title="自选股实时"
            subtitle={trading ? '交易时段 · 3 秒刷新' : '非交易时段 · 显示最后快照'}
            right={
              <span className={trading ? 'text-down' : 'text-fg3'}>
                {trading ? '● 盘中' : '○ 收盘'}
              </span>
            }
            padded={false}
          >
            {/* 添加自选 */}
            <div className="flex flex-wrap items-center gap-2 border-b border-line bg-card2/40 px-3 py-2">
              <input
                value={addSym}
                onChange={(e) => setAddSym(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') addWatch()
                }}
                placeholder="输入代码，如 600519.SH"
                className="w-52 rounded border border-line2 bg-card px-2 py-1 font-mono text-sm2 outline-none placeholder:text-fg3 focus:border-brand"
              />
              <button
                onClick={addWatch}
                disabled={adding || !addSym.trim()}
                className="rounded-lg bg-brand px-3 py-1 text-sm2 font-medium text-white disabled:opacity-40"
              >
                {adding ? '添加中…' : '添加自选'}
              </button>
              <span className="font-mono text-xs2 text-fg3">
                已有 {watch.data?.count ?? 0} 只 · 盘中每 3 秒刷新
              </span>
            </div>

            {quotes.length === 0 ? (
              <div className="px-3 py-8 text-center text-sm2 text-fg3">
                {rt.isLoading ? '加载中…' : '还没有自选股 —— 在上面输入代码添加'}
              </div>
            ) : (
              <table className="w-full border-collapse whitespace-nowrap text-sm2">
                <thead>
                  <tr>
                    {['代码', '名称', '现价', '涨跌幅', 'PE', 'PB', '市值', '量比', '涨停/跌停', '更新', '操作'].map(
                      (h, i) => (
                        <th
                          key={`${h}-${i}`}
                          className={`border-b border-line px-2.5 py-1 text-xs2 font-medium text-fg3 ${
                            i < 2 ? 'text-left' : 'text-right'
                          }`}
                        >
                          {h}
                        </th>
                      ),
                    )}
                    <th className="w-full border-b border-line" />
                  </tr>
                </thead>
                <tbody>
                  {quotes.map((q) => (
                    <tr key={q.symbol} className="hover:bg-brand/[0.06]">
                      <td className="num border-b border-line/60 px-2.5 py-1 text-left">{q.symbol}</td>
                      <td className="border-b border-line/60 px-2.5 py-1 text-left text-fg2">{q.name || '—'}</td>
                      <td className="num border-b border-line/60 px-2.5 py-1 text-right">
                        {q.available ? q.price?.toFixed(2) : '—'}
                      </td>
                      <td
                        className={`num border-b border-line/60 px-2.5 py-1 text-right ${
                          (q.change_pct ?? 0) > 0 ? 'text-up' : (q.change_pct ?? 0) < 0 ? 'text-down' : 'text-fg2'
                        }`}
                      >
                        {q.available ? `${(q.change_pct ?? 0) > 0 ? '+' : ''}${q.change_pct?.toFixed(2)}%` : '—'}
                      </td>
                      <td className="num border-b border-line/60 px-2.5 py-1 text-right text-fg2">
                        {q.pe_ttm ? q.pe_ttm.toFixed(1) : '—'}
                      </td>
                      <td className="num border-b border-line/60 px-2.5 py-1 text-right text-fg2">
                        {q.pb ? q.pb.toFixed(2) : '—'}
                      </td>
                      <td className="num border-b border-line/60 px-2.5 py-1 text-right text-fg3">
                        {q.market_cap ? `${(q.market_cap / 1e8).toFixed(0)} 亿` : '—'}
                      </td>
                      <td className="num border-b border-line/60 px-2.5 py-1 text-right text-gold">
                        {q.vol_ratio ? q.vol_ratio.toFixed(2) : '—'}
                      </td>
                      <td className="num border-b border-line/60 px-2.5 py-1 text-right text-xs2">
                        <span className="text-up">{q.limit_up ? q.limit_up.toFixed(2) : '—'}</span>
                        <span className="mx-0.5 text-fg3">/</span>
                        <span className="text-down">{q.limit_down ? q.limit_down.toFixed(2) : '—'}</span>
                      </td>
                      <td className="num border-b border-line/60 px-2.5 py-1 text-right text-fg3">
                        {q.available ? (q.session === 'closed' ? '收盘' : `${((q.age_ms ?? 0) / 1000).toFixed(1)}s`) : '—'}
                      </td>
                      <td className="border-b border-line/60 px-2.5 py-1 text-right">
                        <button
                          onClick={() => delWatch(q.symbol)}
                          className="text-xs2 text-fg3 hover:text-up"
                          title="删除"
                        >
                          移除
                        </button>
                      </td>
                      <td className="w-full border-b border-line/60" />
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          {/* 引擎状态 */}
          <Card className="col-span-12" title="引擎状态" subtitle="来自 SSE 快照">
            <div className="grid grid-cols-5 gap-2">
              <Metric
                label="数据通道"
                value={h?.ok ? '正常' : '异常'}
                tone={h?.ok ? 'text-down' : 'text-up'}
              />
              <Metric label="数据行数" value={h?.rows != null ? fmtInt(h.rows) : '—'} />
              <Metric label="代际" value={<span className="text-xs">{h?.generation ?? '—'}</span>} />
              <Metric label="策略" value={String(h?.strategies ?? '—')} />
              <Metric
                label="运行中任务"
                value={String(snap?.running.length ?? 0)}
                tone={(snap?.running.length ?? 0) > 0 ? 'text-gold' : ''}
              />
            </div>
            {h && !h.ok && (
              <div className="mt-2 rounded-md border border-up/30 bg-up/10 px-2.5 py-2 text-sm2 text-up">
                数据不可用：{h.error}
              </div>
            )}
          </Card>

          {/* 事件流 */}
          <Card
            className="col-span-7"
            title="事件流"
            subtitle="实时推送"
            right={`${events.length} 条`}
            padded={false}
          >
            <div className="max-h-[420px] overflow-y-auto">
              {events.length === 0 ? (
                <div className="px-3 py-14 text-center text-sm2 text-fg3">
                  <Radio size={22} className="mx-auto mb-2 opacity-50" />
                  等待事件…
                </div>
              ) : (
                events.map((e, i) => (
                  <div
                    key={`${e.ts}-${i}`}
                    className="flex items-start gap-2 border-b border-line/50 px-3 py-1.5 text-sm2 last:border-0"
                  >
                    <span className="shrink-0 font-mono text-xs2 text-fg3">{e.ts}</span>
                    <span
                      className={
                        e.kind === 'warn' ? 'text-up' : e.kind === 'ok' ? 'text-down' : 'text-fg2'
                      }
                    >
                      {e.text}
                    </span>
                  </div>
                ))
              )}
            </div>
          </Card>

          {/* 任务 */}
          <Card className="col-span-5" title="任务进度" subtitle="运行中与最近完成" padded={false}>
            <div className="max-h-[420px] overflow-y-auto">
              {(snap?.running ?? []).map((t) => (
                <div key={t.id} className="border-b border-line/50 px-3 py-2">
                  <div className="flex items-center justify-between text-sm2">
                    <span className="text-fg2">
                      {TYPE_LABEL[t.type] ?? t.type}{' '}
                      <span className="font-mono text-fg3">{t.id.slice(0, 8)}</span>
                    </span>
                    <span className="num text-xs2 text-gold">{Math.round(t.progress * 100)}%</span>
                  </div>
                  <div className="mt-1 h-[4px] overflow-hidden rounded bg-card2">
                    <div className="h-full bg-brand" style={{ width: `${t.progress * 100}%` }} />
                  </div>
                  {t.log[0] && <div className="mt-1 truncate font-mono text-xs2 text-fg3">{t.log[0]}</div>}
                </div>
              ))}
              {(snap?.running.length ?? 0) === 0 && (
                <div className="px-3 py-8 text-center text-sm2 text-fg3">
                  <Activity size={18} className="mx-auto mb-1.5 opacity-50" />
                  当前没有运行中的任务
                </div>
              )}
              <div className="border-t border-line px-3 py-1.5 text-3xs tracking-wide text-fg3">最近完成</div>
              {(snap?.recent ?? []).slice(0, 5).map((t) => (
                <div key={t.id} className="flex items-center justify-between border-b border-line/40 px-3 py-1.5 text-xs2">
                  <span className="text-fg3">
                    {TYPE_LABEL[t.type] ?? t.type} <span className="font-mono">{t.id.slice(0, 8)}</span>
                  </span>
                  <span className={t.status === 'done' ? 'text-down' : t.status === 'failed' ? 'text-up' : 'text-fg3'}>
                    {t.status === 'done' ? '完成' : t.status === 'failed' ? '失败' : t.status}
                  </span>
                </div>
              ))}
            </div>
          </Card>

          {/* 今日信号 */}
          <Card className="col-span-6" title="今日信号" subtitle={signal.data ? `${signal.data.date} · ${signal.data.count} 条` : ''} padded={false}>
            {(signal.data?.rows.length ?? 0) === 0 ? (
              <div className="px-3 py-8 text-center text-sm2 text-fg3">
                {signal.isLoading ? '加载中…' : '今日无信号'}
              </div>
            ) : (
              signal.data!.rows.map((r) => (
                <div key={r.symbol} className="flex items-center justify-between border-b border-line/50 px-3 py-1.5 text-sm2">
                  <span className="text-cyan">{r.symbol}</span>
                  <span className="num text-fg3">
                    {r.buy_low}~{r.buy_high}
                  </span>
                  <span className="num text-down">止损 {r.stop}</span>
                  <span className="num text-gold">{r.qty} 股</span>
                </div>
              ))
            )}
          </Card>

          {/* 连板 */}
          <Card className="col-span-6" title="连板梯队" subtitle={limitUp.data?.sample ?? ''} padded={false}>
            <div className="grid grid-cols-[1fr_88px_72px_88px] items-center border-b border-line px-3 py-1 text-xs2 text-fg3">
              <span>档位</span>
              <span className="text-right">样本</span>
              <span className="text-right">晋级率</span>
              <span className="text-right">次日开盘买</span>
            </div>
            {(limitUp.data?.buckets ?? []).map((b) => (
              <div
                key={b.label}
                className="grid grid-cols-[1fr_88px_72px_88px] items-center border-b border-line/50 px-3 py-1 text-sm2"
              >
                <span className="text-fg2">{b.label}</span>
                <span className="num text-right text-fg3">{b.n.toLocaleString()}</span>
                <span className="num text-right text-gold">{b.promote}%</span>
                <span className={`num text-right ${b.oc_return > 0 ? 'text-up' : 'text-down'}`}>
                  {b.oc_return > 0 ? '+' : ''}
                  {b.oc_return}%
                </span>
              </div>
            ))}
          </Card>

          <Card className="col-span-12" title="监控范围说明" subtitle="哪些能做，哪些做不了">
            <div className="grid grid-cols-2 gap-4 text-sm2 leading-relaxed">
              <div>
                <div className="mb-1.5 flex items-center gap-1.5 font-medium text-down">
                  <Bell size={12} /> 已实现
                </div>
                <ul className="space-y-1 text-fg2">
                  <li>· <b>自选股 3 秒实时报价</b>（东财批量接口，50 只/次，盘中才轮询）</li>
                  <li>· 现价 / 涨跌幅 / 成交量 / 量比 / 数据新鲜度</li>
                  <li>· 任务进度实时推送（SSE，2 秒轮询，只在变化时推）</li>
                  <li>· 引擎健康 / 数据代际状态</li>
                  <li>· 今日信号与连板梯队</li>
                  <li>· 断线自动重连（3 秒）</li>
                </ul>
              </div>
              <div>
                <div className="mb-1.5 flex items-center gap-1.5 font-medium text-gold">
                  <WifiOff size={12} /> 还没做（需要更高等级的数据）
                </div>
                <ul className="space-y-1 text-fg3">
                  <li>· 逐笔成交、五档以上盘口（现在只有快照，无委托队列）</li>
                  <li>· 封板瞬间 / 撤单墙 / 大单流入（需 Level-2 付费源）</li>
                  <li>· 全市场秒级扫描（免费源 50 只/次，全市场要 96 次请求）</li>
                  <li>· 盘中异动预警规则引擎（可基于现有快照做，尚未实现）</li>
                </ul>
              </div>
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}
