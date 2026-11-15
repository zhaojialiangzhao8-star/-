import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Search } from 'lucide-react'
import { api, type Bar } from '../services/api'
import { Card, DataTable, Metric } from '../components/ui'
import KLineChart, { type PickMarker, type TradeMarker } from '../components/KLineChart'
import { fmtInt, fmtPct, pctClass } from '../lib/format'

const cls = (v: number | undefined) =>
  v == null ? 'text-fg3' : v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-fg2'

/**
 * 归一化股票代码：用户手打经常只写 6 位数字（002463），
 * 而数据里是 002463.SZ。不补后缀会导致「没有 002463 的数据」。
 *
 *   6 开头      → .SH（沪市主板）
 *   688/689    → .SH（科创板）
 *   0 / 3 开头  → .SZ（深市主板 / 创业板）
 *   4 / 8 / 92 → .BJ（北交所）
 */
function normalizeSymbol(raw: string): string {
  const s = raw.trim().toUpperCase()
  if (!s) return s
  if (s.includes('.')) return s
  const code = s.replace(/^(SH|SZ|BJ)/, '')
  if (!/^\d{6}$/.test(code)) return s
  if (code.startsWith(('6'))) return `${code}.SH`
  if (code.startsWith('688') || code.startsWith('689')) return `${code}.SH`
  if (code.startsWith(('4')) || code.startsWith('8') || code.startsWith('92')) return `${code}.BJ`
  return `${code}.SZ`
}

/** 从 K 线序列算出来的派生指标（前端算，避免为展示再加一轮后端往返） */
function derive(bars: Bar[]) {
  if (bars.length < 2) return null
  const last = bars[bars.length - 1]
  const prev = bars[bars.length - 2]
  const chg = prev.close ? (last.close / prev.close - 1) * 100 : 0
  const amp = prev.close ? ((last.high - last.low) / prev.close) * 100 : 0

  // 真实波幅 ATR14
  let trSum = 0
  const n = Math.min(14, bars.length - 1)
  for (let i = bars.length - n; i < bars.length; i++) {
    const b = bars[i]
    const pc = bars[i - 1].close
    trSum += Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc))
  }
  const atr = n > 0 ? trSum / n : 0

  // RSI14
  let gain = 0
  let loss = 0
  const m = Math.min(14, bars.length - 1)
  for (let i = bars.length - m; i < bars.length; i++) {
    const d = bars[i].close - bars[i - 1].close
    if (d >= 0) gain += d
    else loss -= d
  }
  const rsi = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)

  // 3 日 / 20 日涨跌幅
  const r3 = bars.length > 3 ? (last.close / bars[bars.length - 4].close - 1) * 100 : null
  const r20 = bars.length > 20 ? (last.close / bars[bars.length - 21].close - 1) * 100 : null

  // 量比（当日量 / 前 5 日均量）
  const tail = bars.slice(-6, -1)
  const avgVol = tail.reduce((s, b) => s + b.volume, 0) / (tail.length || 1)
  const vr = avgVol ? last.volume / avgVol : 0

  // 60 日高低（支撑/压力参考）
  const win = bars.slice(-60)
  const hi = Math.max(...win.map((b) => b.high))
  const lo = Math.min(...win.map((b) => b.low))

  return { last, chg, amp, atr, rsi, r3, r20, vr, hi, lo }
}

/** 最近 N 根里跌幅最深的几天（对应「急跌反弹」的触发条件） */
function dropDays(bars: Bar[], n = 10) {
  const out: { date: string; drop3: number; close: number; volRatio: number }[] = []
  for (let i = 3; i < bars.length; i++) {
    const r3 = (bars[i].close / bars[i - 3].close - 1) * 100
    if (r3 < -8) {
      const prev5 = bars.slice(Math.max(0, i - 5), i)
      const avg = prev5.reduce((s, b) => s + b.volume, 0) / (prev5.length || 1)
      out.push({ date: bars[i].date, drop3: r3, close: bars[i].close, volRatio: avg ? bars[i].volume / avg : 0 })
    }
  }
  return out.slice(-n).reverse()
}

const PERIODS = [
  { key: 'trend', label: '分时' },
  { key: 'day', label: '日线' },
  { key: 'week', label: '周线' },
  { key: 'month', label: '月线' },
  { key: 'quarter', label: '季线' },
  { key: 'year', label: '年线' },
  { key: '60m', label: '60分' },
  { key: '120m', label: '2小时' },
  { key: '240m', label: '4小时' },
]

export default function StockAnalysis() {
  const [params, setParams] = useSearchParams()
  const initialSymbol = normalizeSymbol(params.get('symbol') || '605258.SH')
  const [symbol, setSymbol] = useState(initialSymbol)
  const [input, setInput] = useState(initialSymbol)

  // 搜索联想：代码 / 名称都能搜，模糊匹配（防抖 250ms）
  const [sug, setSug] = useState<{ symbol: string; name: string }[]>([])
  const [showSug, setShowSug] = useState(false)
  useEffect(() => {
    const kw = input.trim()
    if (!kw || /^\d{6}(\.(SH|SZ|BJ))?$/i.test(kw)) {
      setSug([])
      return
    }
    const t = window.setTimeout(() => {
      api
        .symbolSearch(kw, 12)
        .then((r) => setSug(r.items ?? []))
        .catch(() => setSug([]))
    }, 250)
    return () => window.clearTimeout(t)
  }, [input])
  const pickSug = (s: { symbol: string; name: string }) => {
    setInput(s.symbol)
    setSymbol(s.symbol)
    setShowSug(false)
  }

  // 从别的页面点进来带了 ?symbol=xxxxxx.SH，跟着变
  useEffect(() => {
    const s = params.get('symbol')
    if (s) {
      const n = normalizeSymbol(s)
      if (n !== symbol) {
        setSymbol(n)
        setInput(n)
      }
    }
  }, [params])
  const [strategy, setStrategy] = useState('')
  /** 左侧栏：同策略 / 自选股 共用一个位置 */
  const [leftTab, setLeftTab] = useState<'peers' | 'watch'>('peers')
  /** K 线周期：day/week/month/quarter/year/60m/120m/240m */
  const [period, setPeriod] = useState('day')
  const [markers, setMarkers] = useState<TradeMarker[]>([])
  const [btInfo, setBtInfo] = useState<{ trades: number; win: number; total: number } | null>(null)

  const q = useQuery({
    queryKey: ['kline', symbol, period],
    queryFn: () =>
      period === 'trend'
        ? api.klineTrend(symbol, 1)
        : period.endsWith('m')
          ? api.klineMin(symbol, Number(period.replace('m', '')), 600)
          // 传 start 拿全量历史（后端见到 start 会把上限放到 3000 根），否则只有 250 根 = 只看得到 1 年
          : api.kline(symbol, period === 'day' ? 250 : 300, '2020-01-01', period),
    retry: 0,
    refetchInterval: period === 'trend' ? 30_000 : false,
  })

  const strategies = useQuery({ queryKey: ['strategies'], queryFn: api.strategies, staleTime: 300_000 })

  // 策略列表加载后自动选第一个（默认不写死策略名：策略库会增删，写死就会指向已删除的）
  useEffect(() => {
    const ids = ((strategies.data?.items ?? []) as { id: string }[]).map((s) => s.id)
    if (ids.length && !ids.includes(strategy)) setStrategy(ids[0])
  }, [strategies.data, strategy])

  // 个股档案（名称 / 五档 / 关键数据 / 板块）
  const pf = useQuery({
    queryKey: ['profile', symbol],
    queryFn: () => api.profile(symbol),
    retry: 0,
    refetchInterval: 5_000,
  })

  // 同伴列表：从 ?strategy= 或 ?peers= 拿。首页某个策略里点进来时，
  // 同策略的其他股票会显示在左侧，方便横向比较。
  const peerStrategy = params.get('strategy') || ''
  const peerSymbolsParam = params.get('peers') || ''

  const peers = useMemo<{ symbol: string; name?: string; close?: number }[]>(() => {
    if (peerSymbolsParam) {
      return peerSymbolsParam
        .split(',')
        .filter(Boolean)
        .map((s) => ({ symbol: s.trim().toUpperCase() }))
    }
    return []
  }, [peerSymbolsParam])

  const allSig = useQuery({
    queryKey: ['screenerAll', 'ranked'],
    queryFn: () => fetch('/api/v1/screener/all?limit_per_strategy=30&scope=ranked').then((r) => r.json()),
    staleTime: 5 * 60_000,
    retry: 0,
    enabled: !!peerStrategy,
  })

  const groupRows = useMemo(() => {
    if (!peerStrategy) return peers
    const items =
      ((allSig.data as { items?: { id: string; rows: { symbol: string; name?: string; close?: number }[] }[] })?.items) ?? []
    const hit = items.find((x) => x.id === peerStrategy)
    return (hit?.rows ?? []).map((r) => ({ symbol: r.symbol, name: r.name, close: r.close }))
  }, [peerStrategy, peers, allSig.data])

  const spark = useQuery({
    queryKey: ['spark', groupRows.map((r) => r.symbol).join(',')],
    queryFn: () => api.sparklines(groupRows.map((r) => r.symbol), 20),
    enabled: groupRows.length > 0,
    staleTime: 120_000,
    retry: 0,
  })

  // 支撑 / 压力 / 目标位
  const lv = useQuery({
    queryKey: ['levels', symbol],
    queryFn: () => api.levels(symbol),
    retry: 0,
    staleTime: 60_000,
  })

  // 拼成 K 线图上的水平线：止损 1 条 + 支撑 1 条 + 压力 1 条 + 目标 1 条
  // 标签上显示**真实价**（后复权价 ÷ 复权因子），图上的线仍画在后复权坐标。
  const priceLines = useMemo(() => {
    const d = lv.data
    if (!d) return []
    const out: { price: number; color: string; title: string; style?: number }[] = []
    const seen = new Set<number>()
    const real = (it?: { price: number; real_price?: number }) =>
      it?.real_price != null ? it.real_price : it?.price
    const push = (item: { price: number; real_price?: number } | undefined, color: string, label: string, style: number) => {
      if (!item?.price || !Number.isFinite(item.price) || item.price <= 0) return
      const key = Math.round(item.price * 100)
      if (seen.has(key)) return
      seen.add(key)
      out.push({ price: item.price, color, title: `${label} ${real(item)?.toFixed(2)}`, style })
    }
    push(d.stop, '#0ecb81', '止损', 0)     // 实线
    push(d.supports?.[0], 'rgba(14,203,129,0.5)', '', 2)
    push(d.resistances?.[0], '#f0b90b', '压力', 2)
    // 目标优先用盈亏比 2:1（技术目标常常和压力位重合，画出来只有一条）
    const rrTarget = (d.targets ?? []).find((t) => t.kind === 'rr') ?? d.targets?.[0]
    if (rrTarget) push(rrTarget, 'rgba(34,211,238,0.75)', '目标', 2)
    return out
  }, [lv.data])

  // 跑一次账户回测，把这只票的买卖点抽出来标在 K 线上
  const runBt = useMutation({
    mutationFn: () =>
      api.btRun({
        strategy,
        init_cash: 100_000,
        risk_amount: 3000,
        max_positions: 5,        // 5 仓才能精选（10 仓会被信号占满、分散过度）
        max_position_pct: 0.5,
        stop_loss_pct: 0.12,
        max_hold_days: 30,
        start: '2024-01-01',
        end: null,
      }),
    onSuccess: (d) => {
      const mine = d.trades.filter((t) => t.symbol === symbol)
      const marks: TradeMarker[] = mine.map((t) => ({
        date: t.dt,
        side: t.side,
        price: t.price,
        label:
          t.side === 'buy'
            ? `B ${t.price.toFixed(1)}`
            : `S ${t.price.toFixed(1)}${t.pnl ? ` ${t.pnl > 0 ? '+' : ''}${Math.round(t.pnl)}` : ''}`,
      }))
      setMarkers(marks)
      const buys = mine.filter((t) => t.side === 'buy').length
      const wins = mine.filter((t) => t.side === 'sell' && t.pnl > 0).length
      setBtInfo({ trades: buys, win: wins, total: d.metrics.trades ?? 0 })
    },
  })

  const bars = q.data?.rows ?? []
  const d = useMemo(() => derive(bars), [bars])
  const drops = useMemo(() => dropDays(bars), [bars])

  // 智能选股信号 → K 线上的买点标记
  // 规则：策略在今天收盘后选中 → 明天开盘买入（T+1），所以标在最后一根 K 线上、文字是「明买」
  const picks = useMemo<PickMarker[]>(() => {
    const items = ((allSig.data as { items?: { id: string; name?: string; rows?: { symbol: string }[] }[] })?.items) ?? []
    const hit = items.find((it) => (it.rows ?? []).some((r) => r.symbol === symbol))
    if (!hit || !bars.length) return []
    return [{ date: bars[bars.length - 1].date, strategy: hit.name || hit.id, timing: 'next' }]
  }, [allSig.data, symbol, bars])

  // ---- AI 分析（DeepSeek）----
  const [aiQ, setAiQ] = useState('')
  const [lossLimit, setLossLimit] = useState(3000)
  const ai = useMutation({
    mutationFn: () => api.aiAnalyze(symbol, aiQ),
  })

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">个股分析</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            // 输入的是名称（或模糊词）时，直接取第一条匹配，省得再点一次
            if (!/^\d{6}(\.(SH|SZ|BJ))?$/i.test(input.trim()) && sug.length > 0) {
              pickSug(sug[0])
            } else {
              setSymbol(normalizeSymbol(input))
            }
          }}
          className="relative flex items-center gap-1.5"
        >
          <div className="flex items-center gap-1.5 rounded-lg border border-line2 bg-card px-2.5 py-1.5">
            <Search size={13} className="text-fg3" />
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onFocus={() => setShowSug(true)}
              onBlur={() => window.setTimeout(() => setShowSug(false), 150)}
              placeholder="代码 / 名称，如 600519 或 茅台"
              className="w-44 bg-transparent font-mono text-xs outline-none placeholder:text-fg3"
            />
          </div>
          <button type="submit" className="rounded-lg bg-brand px-3 py-1.5 text-sm2 font-medium text-white">
            查询
          </button>
          {showSug && sug.length > 0 && (
            <div className="absolute left-0 top-full z-50 mt-1 w-[280px] overflow-hidden rounded-lg border border-line2 bg-card shadow-xl">
              {sug.map((s) => (
                <button
                  key={s.symbol}
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pickSug(s)}
                  className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-brand/10"
                >
                  <span className="min-w-0 flex-1 truncate text-[12px] text-fg">{s.name || s.symbol}</span>
                  <span className="shrink-0 font-mono text-[11px] text-fg3">{s.symbol}</span>
                </button>
              ))}
            </div>
          )}
        </form>
        {/* 股票名称 + 代码（对齐 Tick 的 StockInfoBar：名称 14 粗体、代码 10 等宽） */}
        {pf.data?.quote && (
          <>
            <span className="truncate text-[14px] font-semibold tracking-wide text-fg">{pf.data.quote.name}</span>
            <span className="font-mono text-xs2 text-fg3">{pf.data.quote.symbol}</span>
          </>
        )}
        {/* 现价 18 粗体（Tick 口径：带名称时 text-lg，独立时 text-2xl），涨跌幅 12
            价格优先用实时快照：K 线最后一根是本地代际（可能落后几天），拿它当“现价”会打架 */}
        {d && (
          <>
            <span
              className={`num text-[18px] font-bold leading-none ${pctClass(
                pf.data?.quote?.change_pct ?? d.chg,
              )}`}
            >
              {(pf.data?.quote?.price ?? d.last.close).toFixed(2)}
            </span>
            <span
              className={`num text-[12px] font-semibold ${pctClass(
                pf.data?.quote?.change_pct ?? d.chg,
              )}`}
            >
              {fmtPct(pf.data?.quote?.change_pct ?? d.chg)}
            </span>
          </>
        )}
        <span className="ml-auto flex items-center gap-1.5 font-mono text-xs2 text-fg3">
          <button
            onClick={() => ai.mutate()}
            disabled={ai.isPending}
            className="rounded border border-brand/50 bg-brand/15 px-2 py-1 text-xs2 text-brand hover:bg-brand/25 disabled:opacity-50"
            title="把这只票的结构化数据交给 DeepSeek 分析"
          >
            {ai.isPending ? 'AI 分析中…' : 'AI 分析'}
          </button>
          日线 · {bars.length} 根 · {bars[0]?.date} ~ {bars[bars.length - 1]?.date}
        </span>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        {q.isError && (
          <Card title="查询失败">
            <div className="text-xs text-up">{(q.error as Error).message}</div>
          </Card>
        )}

        <div className="grid grid-cols-12 gap-3">
          {/* 左侧栏：同策略 / 自选股 共用一个位置（没有同伴时只剩自选股） */}
          {(
            <Card className="col-span-2 flex flex-col" padded={false}>
              <div className="flex items-center border-b border-line bg-bg2 text-xs2">
                {groupRows.length > 1 && (
                  <button
                    onClick={() => setLeftTab('peers')}
                    className={`flex-1 px-2 py-1.5 ${leftTab === 'peers' ? 'border-b-2 border-brand text-brand' : 'text-fg3 hover:text-fg2'}`}
                  >
                    同策略 {groupRows.length}
                  </button>
                )}
                <button
                  onClick={() => setLeftTab('watch')}
                  className={`flex-1 px-2 py-1.5 ${leftTab === 'watch' || groupRows.length <= 1 ? 'border-b-2 border-brand text-brand' : 'text-fg3 hover:text-fg2'}`}
                >
                  自选股
                </button>
              </div>
              {leftTab === 'watch' || groupRows.length <= 1 ? (
                <WatchSide current={symbol} onPick={(sym) => { setSymbol(sym); setInput(sym) }} />
              ) : (
              <div className="max-h-[520px] overflow-y-auto">
                {groupRows.map((r) => {
                  const sp = spark.data?.items?.[r.symbol]
                  const chg = sp?.change_pct ?? 0
                  const active = r.symbol === symbol
                  const pts = sp?.closes ?? []
                  // 迷你 sparkline：20 个点缩到 60x18
                  let path = ''
                  if (pts.length > 1) {
                    const lo = Math.min(...pts)
                    const hi = Math.max(...pts)
                    const span = hi - lo || 1
                    path = pts
                      .map((p, i) => {
                        const x = (i / (pts.length - 1)) * 60
                        const y = 16 - ((p - lo) / span) * 14
                        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
                      })
                      .join(' ')
                  }
                  return (
                    <button
                      key={r.symbol}
                      onClick={() => {
                        setSymbol(normalizeSymbol(r.symbol))
                        setInput(r.symbol)
                        setParams({ symbol: r.symbol, ...(peerStrategy ? { strategy: peerStrategy } : {}) })
                      }}
                      className={`flex w-full flex-col gap-0.5 border-b border-line/40 px-2 py-1.5 text-left transition ${
                        active ? 'bg-brand/15' : 'hover:bg-brand/[0.07]'
                      }`}
                    >
                      <div className="flex items-baseline justify-between gap-1">
                        <span className={`truncate text-sm2 ${active ? 'font-semibold text-brand' : 'text-fg'}`}>
                          {r.name || r.symbol}
                        </span>
                        <span className={`num text-xs2 ${cls(chg)}`}>
                          {chg > 0 ? '+' : ''}
                          {chg.toFixed(1)}%
                        </span>
                      </div>
                      <div className="flex items-center justify-between gap-1">
                        <span className="font-mono text-3xs text-fg3">{r.symbol}</span>
                        {path ? (
                          <svg width="60" height="18" className="shrink-0">
                            <path
                              d={path}
                              fill="none"
                              stroke={chg >= 0 ? '#f6465d' : '#0ecb81'}
                              strokeWidth="1"
                            />
                          </svg>
                        ) : (
                          <span className="text-3xs text-fg3">—</span>
                        )}
                      </div>
                    </button>
                  )
                })}
              </div>
              )}
            </Card>
          )}

          <Card
            className="col-span-8"
            title="日 K"
            subtitle={
              markers.length > 0
                ? `已标 ${markers.length} 个买卖点（${strategy}）· 红色▲买入 / 绿色▼卖出`
                : '红涨绿跌 · MA5 青 / MA20 金 · 下方成交量'
            }
            right={
              <div className="flex items-center gap-1.5">
                <select
                  value={strategy}
                  onChange={(e) => {
                    setStrategy(e.target.value)
                    setMarkers([])
                    setBtInfo(null)
                  }}
                  className="max-w-[170px] rounded border border-line2 bg-card2 px-1.5 py-0.5 font-mono text-xs2 outline-none focus:border-brand"
                >
                  {(strategies.data?.items ?? []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name || s.id}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => runBt.mutate()}
                  disabled={runBt.isPending}
                  className="rounded border border-line2 bg-card2 px-2 py-0.5 text-xs2 text-fg2 hover:text-fg disabled:opacity-50"
                >
                  {runBt.isPending ? '回测中…' : '标买卖点'}
                </button>
                {markers.length > 0 && (
                  <button
                    onClick={() => {
                      setMarkers([])
                      setBtInfo(null)
                    }}
                    className="rounded border border-line2 bg-card2 px-2 py-0.5 text-xs2 text-fg3 hover:text-fg"
                  >
                    清除
                  </button>
                )}
              </div>
            }
            padded={false}
          >
            {/* K 线（指标改由十字光标的浮动小窗显示，不再叠固定面板） */}
            <div className="relative">
              <KLineChart bars={bars} height={440} trades={period === 'trend' ? [] : markers} priceLines={period === 'trend' ? [] : priceLines} picks={period === 'trend' ? [] : picks} periods={PERIODS} period={period} onPeriod={setPeriod} loading={q.isFetching} mode={period === 'trend' ? 'line' : 'candle'} />
            </div>
            {btInfo && (
              <div className="flex flex-wrap gap-5 border-t border-line px-3 py-1.5 text-sm2 text-fg3">
                <span>
                  该股买入 <b className="num text-up">{btInfo.trades}</b> 次
                </span>
                <span>
                  盈利卖出 <b className="num text-down">{btInfo.win}</b> 次
                </span>
                <span>
                  策略全程 <b className="num text-fg2">{btInfo.total}</b> 笔
                </span>
                <span className="ml-auto text-xs2">回测 2024-01-01 至今</span>
              </div>
            )}
          </Card>

          {/* 固定亏损计算器：止损取近期底部，目标取近期高位 */}


          <Card
            className="col-span-2"
            title="仓位"
          >
            {(() => {
              const q = pf.data?.quote
              const price = q?.price ?? lv.data?.real_close ?? d?.last.close
              const stopP = lv.data?.stop?.real_price ?? lv.data?.stop?.price
              const tgt =
                (lv.data?.targets ?? []).find((t) => t.kind === 'rr')?.real_price ??
                lv.data?.targets?.[0]?.real_price ??
                lv.data?.resistances?.[0]?.real_price ??
                lv.data?.resistances?.[0]?.price
              if (price == null || stopP == null) {
                return <div className="py-8 text-center text-sm2 text-fg3">加载中…</div>
              }
              const risk = price - stopP
              const reward = tgt != null ? tgt - price : null
              const shares = risk > 0 ? Math.floor(lossLimit / risk / 100) * 100 : 0
              const capital = shares * price
              return (
                <div className="space-y-2 whitespace-nowrap text-xs2">
                  {/* 入场 / 止损 / 目标 */}
                  <div className="space-y-1 rounded-lg bg-card2/60 px-2 py-1.5 text-sm2">
                    <div className="flex justify-between">
                      <span className="text-fg3">现价</span>
                      <span className="num font-medium">{price.toFixed(2)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-down">止损</span>
                      <span className="num text-down">{stopP.toFixed(2)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-up">目标</span>
                      <span className="num text-up">{tgt != null ? tgt.toFixed(2) : '—'}</span>
                    </div>
                  </div>

                  {/* 亏损上限 */}
                  <div>
                    <div className="mb-0.5 text-3xs text-fg3">亏损上限</div>
                    <input
                      type="number"
                      step="100"
                      value={lossLimit}
                      onChange={(e) => setLossLimit(Math.max(0, Number(e.target.value) || 0))}
                      className="w-full rounded border border-line2 bg-card2 px-2 py-1 text-right font-mono text-sm2 outline-none focus:border-brand"
                    />
                  </div>

                  {/* 计算结果 */}
                  <div className="space-y-1 border-t border-line pt-2 text-sm2">
                    <div className="flex justify-between">
                      <span className="text-fg3">每股险</span>
                      <span className="num text-down">{risk > 0 ? risk.toFixed(2) : '—'}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-fg3">幅度</span>
                      <span className="num text-down">{risk > 0 ? `${((risk / price) * 100).toFixed(2)}%` : '—'}</span>
                    </div>
                    <div className="flex justify-between border-t border-line/50 pt-1">
                      <span className="text-fg2">股数</span>
                      <span className="num text-xs font-semibold text-brand">{shares.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-fg3">占用</span>
                      <span className="num text-gold">{capital.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
                    </div>
                    <div className="flex justify-between border-t border-line/50 pt-1">
                      <span className="text-fg2">盈亏比</span>
                      <span
                        className={`num font-semibold ${
                          reward != null && risk > 0 && reward / risk >= 2 ? 'text-up' : 'text-gold'
                        }`}
                      >
                        {reward != null && risk > 0 ? `${(reward / risk).toFixed(2)} : 1` : '—'}
                      </span>
                    </div>
                    {reward != null && reward > 0 && (
                      <div className="flex justify-between">
                        <span className="text-fg3">到目标盈利</span>
                        <span className="num text-up">
                          +{Math.round(shares * reward).toLocaleString()}
                        </span>
                      </div>
                    )}
                  </div>

                  <div className="text-3xs leading-relaxed text-fg3">
                    股数按 100 股取整。止损来自近 10 日最低下移 0.5%；
                    目标取 2:1 盈亏比位，无数据时用上方最近压力位。
                  </div>
                </div>
              )
            })()}
          </Card>

          {/* 个股档案：五档 / 关键数据 / 板块 */}
          <Card
            className="col-span-12"
            title="个股档案"
            subtitle={pf.data?.quote ? `${pf.data.quote.name} ${pf.data.quote.symbol} · 3 秒快照 · 真实价（K 线同为真实价）` : '加载中…'}
            padded={false}
          >
            {pf.isError ? (
              <div className="px-3 py-4 text-center text-sm2 text-up">
                档案获取失败（{(pf.error as Error).message}）
              </div>
            ) : !pf.data?.quote ? (
              <div className="px-3 py-4 text-center text-sm2 text-fg3">加载中…</div>
            ) : (
              <div className="grid grid-cols-12 gap-3 px-3 py-2">
                {/* 五档 */}
                <div className="col-span-3">
                  <div className="mb-1 text-xs2 text-fg3">卖五档（价 / 量 / 金额）</div>
                  <table className="w-full border-collapse text-sm2">
                    <tbody>
                      {[...pf.data.quote.asks].reverse().map((a) => (
                        <tr key={`a${a.level}`}>
                          <td className="py-[1px] text-left text-fg3">卖{a.level}</td>
                          <td className="num py-[1px] text-right text-up">{a.price.toFixed(2)}</td>
                          <td className="num py-[1px] text-right text-fg3">{(a.volume / 1e2).toFixed(0)}手</td>
                          <td className="num py-[1px] text-right text-gold">{(a.amount / 1e4).toFixed(0)}万</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="my-1 border-t border-dashed border-line" />
                  <table className="w-full border-collapse text-sm2">
                    <tbody>
                      {pf.data.quote.bids.map((b) => (
                        <tr key={`b${b.level}`}>
                          <td className="py-[1px] text-left text-fg3">买{b.level}</td>
                          <td className="num py-[1px] text-right text-down">{b.price.toFixed(2)}</td>
                          <td className="num py-[1px] text-right text-fg3">{(b.volume / 1e2).toFixed(0)}手</td>
                          <td className="num py-[1px] text-right text-gold">{(b.amount / 1e4).toFixed(0)}万</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* 关键数据 */}
                <div className="col-span-6">
                  <div className="mb-1 text-xs2 text-fg3">关键数据</div>
                  <div className="grid grid-cols-4 gap-x-3 gap-y-0.5 text-sm2">
                    {[
                      ['最高', pf.data.quote.high.toFixed(2), 'text-up'],
                      ['开盘', pf.data.quote.open.toFixed(2), 'text-fg'],
                      ['昨收', pf.data.quote.prev_close.toFixed(2), 'text-fg2'],
                      ['最低', pf.data.quote.low.toFixed(2), 'text-down'],
                      ['成交量', `${(pf.data.quote.volume / 1e4).toFixed(1)}万手`, 'text-fg2'],
                      ['成交额', `${(pf.data.quote.amount / 1e4).toFixed(2)}亿`, 'text-gold'],
                      ['量比', pf.data.quote.vol_ratio.toFixed(2), 'text-gold'],
                      ['振幅', `${pf.data.quote.amplitude.toFixed(2)}%`, 'text-fg2'],
                      ['换手', `${pf.data.quote.turnover.toFixed(2)}%`, 'text-fg2'],
                      ['涨停', pf.data.quote.limit_up.toFixed(2), 'text-up'],
                      ['跌停', pf.data.quote.limit_down.toFixed(2), 'text-down'],
                      ['PE(TTM)', pf.data.quote.pe_ttm ? pf.data.quote.pe_ttm.toFixed(1) : '—', 'text-fg3'],
                      ['PB', pf.data.quote.pb ? pf.data.quote.pb.toFixed(2) : '—', 'text-fg3'],
                      ['总市值', `${pf.data.quote.total_cap.toFixed(0)}亿`, 'text-fg2'],
                      ['流通市值', `${pf.data.quote.float_cap.toFixed(0)}亿`, 'text-fg3'],
                    ].map(([k, v, cls]) => (
                      <div key={k} className="flex justify-between gap-2">
                        <span className="text-fg3">{k}</span>
                        <span className={`num ${cls}`}>{v}</span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* 板块 */}
                <div className="col-span-3">
                  <div className="mb-1 text-xs2 text-fg3">所属板块</div>
                  <div className="mb-1.5 flex flex-wrap gap-1">
                    {(pf.data.industry ?? []).map((b) => (
                      <Link
                        key={b.code}
                        to={`/boards?kind=industry`}
                        className="rounded border border-brand/40 bg-brand/10 px-1.5 py-0.5 text-xs2 text-brand hover:bg-brand/20"
                      >
                        {b.name}
                      </Link>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {(pf.data.concept ?? []).map((b) => (
                      <Link
                        key={b.code}
                        to={`/boards?kind=concept`}
                        className="rounded border border-line2 bg-card2 px-1.5 py-0.5 text-xs2 text-fg2 hover:border-brand hover:text-brand"
                      >
                        {b.name}
                      </Link>
                    ))}
                    {(pf.data.industry ?? []).length === 0 && (pf.data.concept ?? []).length === 0 && (
                      <span className="text-xs2 text-fg3">无板块数据</span>
                    )}
                  </div>
                </div>
              </div>
            )}
          </Card>

          {/* AI 分析结果 */}
          {(ai.data || ai.isError) && (
            <Card
              className="col-span-12"
              title="AI 分析 · DeepSeek"
              subtitle={ai.data ? `${ai.data.model} · ${ai.data.usage.total_tokens} tokens` : '失败'}
              right={
                <button onClick={() => ai.reset()} className="text-xs2 text-fg3 hover:text-fg">
                  关闭
                </button>
              }
            >
              {ai.isError ? (
                <div className="whitespace-pre-wrap text-sm2 text-up">
                  {(ai.error as Error).message}
                </div>
              ) : (
                <div className="whitespace-pre-wrap text-sm2 leading-relaxed text-fg2">
                  {ai.data?.content}
                </div>
              )}
            </Card>
          )}

          <Card
            className="col-span-7"
            title="急跌信号回溯"
            subtitle="3 日跌幅 < −8% 的日子（策略触发条件是 −15%）"
            padded={false}
          >
            <DataTable
              rows={drops}
              rowKey={(r) => r.date}
              empty="近 160 根内无 3 日跌幅超 8% 的交易日"
              columns={[
                { key: 'date', label: '日期', render: (r) => <span className="text-fg2">{r.date}</span> },
                { key: 'drop3', label: '3日跌幅', render: (r) => <span className="text-down">{r.drop3.toFixed(1)}%</span> },
                { key: 'close', label: '收盘', render: (r) => r.close.toFixed(2) },
                {
                  key: 'vr',
                  label: '量比',
                  render: (r) => <span className={r.volRatio > 1.2 ? 'text-gold' : 'text-fg3'}>{r.volRatio.toFixed(2)}</span>,
                },
                {
                  key: 'hit',
                  label: '是否触发',
                  render: (r) => (
                    <span className={r.drop3 < -15 && r.volRatio > 1.2 ? 'text-up' : 'text-fg3'}>
                      {r.drop3 < -15 && r.volRatio > 1.2 ? '是' : '否'}
                    </span>
                  ),
                },
              ]}
            />
          </Card>

          <Card className="col-span-5" title="交易计划速算" subtitle="固定止损金额">
            {d ? (
              <div className="space-y-2 text-sm2">
                <Row label="参考买入价" value={d.last.close.toFixed(2)} />
                <Row label="止损价（−12%）" value={(d.last.close * 0.88).toFixed(2)} tone="text-down" />
                <Row label="每股风险" value={(d.last.close * 0.12).toFixed(2)} />
                <div className="my-2 border-t border-line" />
                <Row label="止损 3,000 元 → 股数" value={`${Math.floor(3000 / (d.last.close * 0.12) / 100) * 100} 股`} tone="text-gold" />
                <Row
                  label="占用资金"
                  value={`${Math.round(Math.floor(3000 / (d.last.close * 0.12) / 100) * 100 * d.last.close).toLocaleString()} 元`}
                />
                <Row
                  label="实际止损额"
                  value={`${Math.round(Math.floor(3000 / (d.last.close * 0.12) / 100) * 100 * d.last.close * 0.12).toLocaleString()} 元`}
                />
                <div className="mt-2 rounded bg-card2 px-2.5 py-2 text-xs2 leading-relaxed text-fg3">
                  完整参数（止损比例、盈亏比、目标价）在「交易计划」页设置。
                </div>
              </div>
            ) : (
              <div className="py-8 text-center text-sm2 text-fg3">加载中…</div>
            )}
          </Card>
        </div>
      </div>
    </>
  )
}

function Row({ label, value, tone = '' }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-fg3">{label}</span>
      <span className={`num font-semibold ${tone}`}>{value}</span>
    </div>
  )
}

/** 左侧自选股列表：同花顺那种「名称/代码 | 走势 | 价格/涨跌幅」的行 */
function WatchSide({ current, onPick }: { current: string; onPick: (sym: string) => void }) {
  const q = useQuery({ queryKey: ['watchlist'], queryFn: api.watchlist, refetchInterval: 30_000 })
  const items = q.data?.items ?? []
  const syms = useMemo(() => items.map((x) => x.symbol), [items])
  // 现价/涨跌幅走实时行情：store 里的 close 是后复权，直接显示会把「平安银行 12 元」显示成 2340
  const rt = useQuery({
    queryKey: ['watch-rt', syms.join(',')],
    queryFn: () => api.realtime(syms),
    enabled: syms.length > 0,
    refetchInterval: 5_000,
    retry: 0,
  })

  if (q.isLoading) {
    return <div className="px-3 py-6 text-center text-sm2 text-fg3">加载中…</div>
  }
  if (items.length === 0) {
    return (
      <div className="px-3 py-6 text-center text-sm2 text-fg3">
        还没有自选股
        <div className="mt-1 text-xs2">去「智能选股」用「加自选」，或在「自选股」页添加</div>
      </div>
    )
  }
  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
      {items.map((it) => {
        const sp = rt.data?.quotes?.find((x) => x.symbol === it.symbol)
        const chg = sp?.change_pct ?? null
        const active = it.symbol === current
        const price = sp?.available && sp.price != null ? sp.price : null
        const tone = chg == null ? 'text-fg2' : chg > 0 ? 'text-up' : chg < 0 ? 'text-down' : 'text-fg2'
        // 整行背景：涨淡红、跌淡绿、选中深蓝（同花顺的视觉习惯）
        const rowBg = active
          ? 'bg-brand/15'
          : chg == null
            ? 'hover:bg-white/[0.03]'
            : chg > 0
              ? 'bg-up/[0.055] hover:bg-up/[0.09]'
              : chg < 0
                ? 'bg-down/[0.055] hover:bg-down/[0.09]'
                : 'hover:bg-white/[0.03]'
        return (
          <button
            key={it.symbol}
            onClick={() => onPick(it.symbol)}
            className={`grid w-full grid-cols-[1fr_auto] items-center gap-2 border-b border-line/30 px-1.5 py-1 text-left transition ${rowBg}`}
          >
            {/* 左：名称 + 代码 */}
            <div className="min-w-0">
              <div title={it.name || it.symbol} className={`truncate text-sm2 leading-tight ${active ? 'font-semibold text-brand' : 'text-fg'}`}>
                {it.name || it.symbol}
              </div>
              <div className="font-mono text-3xs leading-tight text-fg3">{it.symbol.split('.')[0]}</div>
            </div>
            {/* 右：价格 + 涨跌幅 */}
            <div className="text-right">
              <div className={`num text-xs font-semibold leading-tight ${tone}`}>
                {price != null ? price.toFixed(2) : '—'}
              </div>
              <div className={`num text-xs2 leading-tight ${tone}`}>
                {chg == null ? '—' : `${chg > 0 ? '+' : ''}${chg.toFixed(2)}%`}
              </div>
            </div>
          </button>
        )
      })}
      </div>
      <div className="shrink-0 border-t border-line/50 px-2 py-1 text-center text-3xs text-fg3">
        共 {items.length} 只 · 每 5 秒刷新
      </div>
    </div>
  )
}
