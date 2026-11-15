import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, ChevronRight, RefreshCw, Search, Star } from 'lucide-react'
import { Card } from '../components/ui'
import { api } from '../services/api'
import { boardTag } from '@/components/stock-table/primitives'

interface Row {
  symbol: string
  name?: string
  date?: string
  close?: number
  score?: number
  note?: string
  /** 后端补的当日行情 */
  change_pct?: number | null
  amount_yi?: number | null
  vol_ratio?: number | null
  industry?: string | null
  boards_label?: string | null
  amplitude?: number | null
}

interface StrategyResult {
  id: string
  name: string
  hit_total?: number
  signal_date?: string | null
  rows: Row[]
  rank?: number | null
  win_rate?: number | null
  return?: number | null
  sharpe?: number | null
  error?: string
}

interface BatchResponse {
  generation: string
  cached: boolean
  state: { status?: string; done?: number; total?: number; elapsed?: number }
  strategy_count: number
  total_signals_today: number
  items: StrategyResult[]
  updated: string
}

const pct = (v: number | null | undefined) =>
  v == null ? '—' : `${(v * 100).toFixed(1)}%`
const tone = (v: number | null | undefined) =>
  v == null ? 'text-fg3' : v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-fg2'

export default function Screener() {
  const nav = useNavigate()
  const [kw, setKw] = useState('')
  const [onlySignal, setOnlySignal] = useState(true)
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [scope, setScope] = useState<'ranked' | 'all'>('ranked')
  /** 手动刷新中：后台重算要 1~2 分钟，期间轮询 state，算完再拉数据 */
  const [rebuilding, setRebuilding] = useState(false)
  /** 一键加自选：正在加的策略 id（null=没在加） */
  const [addingId, setAddingId] = useState<string | null>(null)
  /** 最近一次加自选的结果提示 */
  const [addMsg, setAddMsg] = useState('')

  const addWatch = useMutation({
    mutationFn: (v: { symbols: string[] }) => api.addWatchBatch(v.symbols),
    onMutate: (v) => setAddingId(v.symbols[0] ?? null),
    onSuccess: (r) => setAddMsg(`已加 ${r.added} 只到自选股`),
    onError: (e) => setAddMsg(`加自选失败：${(e as Error).message}`),
    onSettled: () => setAddingId(null),
  })

  const q = useQuery<BatchResponse>({
    queryKey: ['screenerAll', scope],
    queryFn: () =>
      fetch(`/api/v1/screener/all?limit_per_strategy=20&scope=${scope}`).then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      }),
    staleTime: 5 * 60_000,
    retry: 0,
  })

  const refresh = async () => {
    setRebuilding(true)
    try {
      // refresh=true 才会让后端重新扫描；之后轮询 /state，building 变 false 再拉数据
      await fetch(`/api/v1/screener/all?refresh=true&limit_per_strategy=20&scope=${scope}`)
      for (let i = 0; i < 80; i++) {
        await new Promise((r) => setTimeout(r, 3000))
        try {
          const st = await fetch('/api/v1/screener/all/state').then((r) => r.json())
          if (!st?.building) break
        } catch {
          /* 网络抖动继续等 */
        }
      }
      await q.refetch()
    } finally {
      setRebuilding(false)
    }
  }

  const items = q.data?.items ?? []

  /** 屏幕上出现的所有个股 → 一次性拉实时行情（现价/涨跌幅/成交额/量比） */
  const symbols = useMemo(() => {
    const set = new Set<string>()
    for (const s of items) for (const r of s.rows ?? []) set.add(r.symbol)
    return [...set].slice(0, 150)
  }, [items])

  const quotesQ = useQuery<{
    quotes: {
      symbol: string
      available?: boolean
      price?: number
      change_pct?: number
      amount?: number
      vol_ratio?: number
      high?: number
      low?: number
      prev_close?: number
      market_cap?: number
      pe_ttm?: number
    }[]
  }>({
    queryKey: ['screenerQuotes', symbols.join(',')],
    enabled: symbols.length > 0,
    staleTime: 10_000,
    retry: 0,
    queryFn: () =>
      fetch(`/api/v1/realtime?symbols=${encodeURIComponent(symbols.join(','))}`).then((r) => r.json()),
  })

  const qmap = useMemo(() => {
    const m = new Map<
      string,
      {
        price?: number
        change_pct?: number
        amount?: number
        vol_ratio?: number
        high?: number
        low?: number
        prev_close?: number
        market_cap?: number
        pe_ttm?: number
      }
    >()
    for (const x of quotesQ.data?.quotes ?? []) if (x.available) m.set(x.symbol, x)
    return m
  }, [quotesQ.data])

  const filtered = useMemo(() => {
    let list = items
    if (onlySignal) list = list.filter((x) => (x.rows?.length ?? 0) > 0)
    if (kw.trim()) {
      const k = kw.trim().toLowerCase()
      list = list.filter((x) => x.name.toLowerCase().includes(k) || x.id.toLowerCase().includes(k))
    }
    return list
  }, [items, onlySignal, kw])

  const withSignal = items.filter((x) => (x.rows?.length ?? 0) > 0).length

  const toggle = (id: string) => setOpen((s) => ({ ...s, [id]: !s[id] }))

  return (
    <>
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">智能选股</h2>
        <span className="font-mono text-xs2 text-fg3">
          {q.data
            ? `${q.data.strategy_count} 策略 · 今日 ${q.data.total_signals_today} 信号 · 更新 ${q.data.updated}`
            : q.isLoading
              ? '加载中（首次约 5 分钟，之后走缓存）…'
              : ''}
        </span>

        <div className="ml-auto flex items-center gap-2">
          <div className="flex items-center gap-1.5 rounded border border-line2 bg-card px-2 py-1">
            <Search size={12} className="text-fg3" />
            <input
              value={kw}
              onChange={(e) => setKw(e.target.value)}
              placeholder="搜索策略"
              className="w-28 bg-transparent text-sm2 outline-none placeholder:text-fg3"
            />
          </div>
          <label className="flex items-center gap-1.5 font-mono text-xs2 text-fg3">
            <input
              type="checkbox"
              checked={onlySignal}
              onChange={(e) => setOnlySignal(e.target.checked)}
              className="accent-[#7c5cff]"
            />
            只显示有信号
          </label>
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as 'ranked' | 'all')}
            className="rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm2 outline-none focus:border-brand"
          >
            <option value="ranked">已启用策略</option>
            <option value="all">全部（含未回测）</option>
          </select>
          <button
            onClick={refresh}
            disabled={q.isFetching || rebuilding}
            className="flex items-center gap-1 rounded-lg border border-line2 bg-card2 px-2.5 py-1 text-sm2 text-fg2 hover:text-fg disabled:opacity-50"
          >
            <RefreshCw size={11} className={q.isFetching || rebuilding ? 'animate-spin' : ''} />
            {rebuilding ? '重算中…' : '刷新'}
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        {q.isError && (
          <Card title="加载失败">
            <div className="text-xs text-up">{(q.error as Error).message}</div>
            <div className="mt-2 text-sm2 text-fg3">
              批量选股需要把全表灌进内存跑一遍，首次约 5 分钟。如果服务刚重启，再点一次「刷新」。
            </div>
          </Card>
        )}

        {q.isLoading && (
          <Card title="正在扫描全市场">
            <div className="py-6 text-center">
              <div className="mx-auto mb-3 h-1 w-48 overflow-hidden rounded bg-card2">
                <div className="h-full w-1/3 animate-pulse bg-brand" />
              </div>
              <div className="text-sm2 text-fg2">12 个策略 × 全市场 1,095 万行，约 5 分钟</div>
              <div className="mt-1 text-xs2 text-fg3">完成后会缓存，切页面不用重跑</div>
            </div>
          </Card>
        )}

        {!q.isLoading && !q.isError && (
          <>
            <div className="mb-3 flex gap-4 text-sm2 text-fg3">
              <span>
                有信号 <b className="num text-gold">{withSignal}</b> / {items.length} 个策略
              </span>
              <span>
                合计 <b className="num text-fg2">{q.data?.total_signals_today ?? 0}</b> 个信号
              </span>
              <span>代际 {q.data?.generation}</span>
            </div>

            {addMsg && (
              <div className="mb-2 rounded border border-brand/30 bg-brand/10 px-3 py-1.5 text-sm2 text-brand">
                {addMsg}
              </div>
            )}
            <div className="space-y-2">
              {filtered.map((s) => {
                const n = s.rows?.length ?? 0
                const isOpen = open[s.id] ?? n > 0
                return (
                  <div key={s.id} className="overflow-hidden rounded-[10px] border border-line bg-card">
                    <button
                      onClick={() => toggle(s.id)}
                      className="flex w-full items-center gap-3 px-3.5 py-2 text-left hover:bg-brand/[0.04]"
                    >
                      {isOpen ? (
                        <ChevronDown size={13} className="text-fg3" />
                      ) : (
                        <ChevronRight size={13} className="text-fg3" />
                      )}
                      <span className="w-10 shrink-0 font-mono text-sm2 text-fg3">
                        {s.rank ? `#${s.rank}` : '—'}
                      </span>
                      <span className="min-w-[140px] text-xs font-medium">{s.name}</span>
                      <span className="font-mono text-xs2 text-fg3">{s.id}</span>
                      <span className="ml-auto flex items-center gap-4 font-mono text-sm2">
                        <span className={tone(s.return)}>{s.return != null ? pct(s.return) : '—'}</span>
                        <span className="text-fg3">胜率 {s.win_rate != null ? pct(s.win_rate) : '—'}</span>
                        <span className={n > 0 ? 'text-gold' : 'text-fg3'}>今日 {n}</span>
                        <span className="text-fg3">累计 {s.hit_total?.toLocaleString() ?? 0}</span>
                        {n > 0 && (
                          <span
                            role="button"
                            tabIndex={0}
                            onClick={(e) => {
                              e.stopPropagation()
                              addWatch.mutate({ symbols: (s.rows ?? []).map((r) => r.symbol) })
                            }}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' || e.key === ' ') {
                                e.stopPropagation()
                                addWatch.mutate({ symbols: (s.rows ?? []).map((r) => r.symbol) })
                              }
                            }}
                            className="flex items-center gap-1 rounded border border-brand/40 bg-brand/15 px-2 py-0.5 text-xs2 text-brand hover:bg-brand/25"
                            title={`把 ${s.name} 今天的 ${n} 个信号全部加入自选股`}
                          >
                            <Star size={10} />
                            {addWatch.isPending ? '加中…' : '加自选'}
                          </span>
                        )}
                      </span>
                    </button>

                    {isOpen && (
                      <div className="border-t border-line">
                        {s.error ? (
                          <div className="px-4 py-2 text-sm2 text-up">{s.error}</div>
                        ) : n === 0 ? (
                          <div className="px-4 py-2 text-sm2 text-fg3">
                            今日无信号
                            {s.signal_date ? `（最近一次 ${s.signal_date}）` : ''}
                          </div>
                        ) : (
                          <div className="overflow-x-auto">
                            <table className="w-full border-collapse whitespace-nowrap text-[12px]">
                              <thead>
                                <tr className="bg-[#111726] text-xs2 font-normal text-fg3">
                                  <th className="w-[74px] px-2.5 py-1 text-left font-normal">代码</th>
                                  <th className="w-[132px] px-2.5 py-1 text-left font-normal">名称</th>
                                  <th className="w-[76px] px-2.5 py-1 text-right font-normal">现价</th>
                                  <th className="w-[76px] px-2.5 py-1 text-right font-normal">涨幅%</th>
                                  <th className="w-[72px] px-2.5 py-1 text-right font-normal">振幅</th>
                                  <th className="w-[82px] px-2.5 py-1 text-left font-normal">几天几板</th>
                                  <th className="w-[60px] px-2.5 py-1 text-right font-normal">量比</th>
                                  <th className="w-[82px] px-2.5 py-1 text-right font-normal">成交额</th>
                                  <th className="w-[86px] px-2.5 py-1 text-right font-normal">市值</th>
                                  <th className="w-[72px] px-2.5 py-1 text-right font-normal">市盈率</th>
                                  <th className="w-[104px] px-2.5 py-1 text-left font-normal">所属行业</th>
                                  <th className="w-[58px] px-2.5 py-1 text-right font-normal">强度</th>
                                  <th className="w-full" />
                                </tr>
                              </thead>
                              <tbody>
                                {s.rows.map((r, i) => {
                                  const b = boardTag(r.symbol)
                                  const live = qmap.get(r.symbol)
                                  const pct = live?.change_pct ?? r.change_pct
                                  const price = live?.price && live.price > 0 ? live.price : null
                                  const amountYi = live?.amount ? live.amount / 1e8 : r.amount_yi
                                  const volRatio = live?.vol_ratio && live.vol_ratio > 0 ? live.vol_ratio : r.vol_ratio
                                  const amplitude =
                                    live?.high && live?.low && live?.prev_close
                                      ? ((live.high - live.low) / live.prev_close) * 100
                                      : r.amplitude
                                  const cap = live?.market_cap && live.market_cap > 0 ? live.market_cap : null
                                  const pe = live?.pe_ttm && live.pe_ttm > 0 ? live.pe_ttm : null
                                  const tone =
                                    pct == null || pct === 0 ? 'text-fg2' : pct > 0 ? 'text-up' : 'text-down'
                                  return (
                                    <tr
                                      key={`${r.symbol}-${i}`}
                                      className="cursor-pointer border-b border-line/30 transition-colors odd:bg-white/[0.015] hover:bg-brand/[0.10]"
                                      onClick={() => nav(`/analysis?symbol=${r.symbol}&strategy=${s.id}`)}
                                    >
                                      <td className="px-2.5 py-[3px] text-left font-mono text-[12px] text-fg3">
                                        {r.symbol.split('.')[0]}
                                      </td>
                                      <td className="max-w-[132px] px-2.5 py-[3px] text-left text-fg">
                                        <div className="flex min-w-0 items-center gap-1">
                                          <span className="truncate">{r.name || r.symbol}</span>
                                          {b && (
                                            <span className={`inline-flex h-3 shrink-0 items-center justify-center rounded border px-1 text-[8px] font-bold leading-none ${b.color}`}>
                                              {b.label}
                                            </span>
                                          )}
                                        </div>
                                      </td>
                                      <td className={`px-2.5 py-[3px] text-right font-mono tabular-nums ${tone}`}>
                                        {price != null ? price.toFixed(2) : '—'}
                                      </td>
                                      <td className={`px-2.5 py-[3px] text-right font-mono tabular-nums ${tone}`}>
                                        {pct == null ? '—' : `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`}
                                      </td>
                                      <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                                        {amplitude != null ? `${amplitude.toFixed(2)}%` : '—'}
                                      </td>
                                      <td className="px-2.5 py-[3px] text-left text-fg3">{r.boards_label || '—'}</td>
                                      <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                                        {volRatio != null ? volRatio.toFixed(2) : '—'}
                                      </td>
                                      <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                                        {amountYi == null
                                          ? '—'
                                          : amountYi >= 1
                                            ? `${amountYi.toFixed(2)}亿`
                                            : `${Math.round(amountYi * 10000)}万`}
                                      </td>
                                      <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                                        {cap == null
                                          ? '—'
                                          : cap >= 1e12
                                            ? `${(cap / 1e12).toFixed(2)}万亿`
                                            : `${(cap / 1e8).toFixed(cap >= 1e11 ? 0 : 1)}亿`}
                                      </td>
                                      <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                                        {pe != null ? pe.toFixed(1) : '—'}
                                      </td>
                                      <td className="truncate px-2.5 py-[3px] text-left text-fg3" title={r.industry || ''}>
                                        {r.industry || '—'}
                                      </td>
                                      <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-gold">
                                        {r.score != null ? Number(r.score).toFixed(2) : '—'}
                                      </td>
                                      <td className="px-2.5 py-[3px]" />
                                    </tr>
                                  )
                                })}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            {filtered.length === 0 && (
              <div className="rounded-lg border border-line bg-card px-4 py-10 text-center text-sm2 text-fg3">
                {onlySignal ? '今天所有策略都没有信号' : '没有匹配的策略'}
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}
