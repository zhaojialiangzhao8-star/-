import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Download, History, ListChecks, Play, RotateCcw, Trash2 } from 'lucide-react'
import { api, type BacktestResult, type BtParams, type Trade } from '../services/api'
import { Card, DataTable, Metric, Modal, Tag } from '../components/ui'
import EquityChart from '../components/EquityChart'
import KLineChart, { type TradeMarker } from '../components/KLineChart'
import { fmtInt, fmtPct, moneyClass, pctClass } from '../lib/format'

const DEFAULTS: BtParams = {
  // 不写死策略：策略库会变（删/加），写死会指向已删除的策略导致「策略不存在」。
  // 空串由下面的 useEffect 自动填成第一个可用策略。
  strategy: '',
  init_cash: 100_000,
  risk_amount: 3000,
  max_positions: 5,
  max_position_pct: 0.5,
  stop_loss_pct: 0.12,
  max_hold_days: 30,
  start: '2021-11-01',   // 默认近 5 年（用户指定）；更长区间走 CLI 脚本
  end: null,
}

/** 策略体检结论：哪些通过四关验证（对应策略库里已启用的三个） */
const VERDICT: Record<string, 'pass' | 'hold' | 'fail'> = {
  ma_cross_regime: 'pass',
  breakout_p1: 'pass',
  reversal_regime: 'pass',
}

export default function StrategyBacktest() {
  const [form, setForm] = useState<BtParams>(DEFAULTS)

  // 指标模式：选一个技术指标 + 条件 + 阈值，直接回测
  const [sp0] = useSearchParams()
  const [mode, setMode] = useState<'strategy' | 'indicator'>(
    sp0.get('mode') === 'indicator' ? 'indicator' : 'strategy',
  )
  const [ind, setInd] = useState({
    indicator: 'rsi',
    op: 'lt',
    value: 30,
    value2: 60,
    period: 14,
  })
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [sort, setSort] = useState<{ key: keyof Trade; desc: boolean }>({ key: 'dt', desc: true })

  const strategies = useQuery({ queryKey: ['strategies'], queryFn: api.strategies, staleTime: 300_000 })

  /** 当前选中策略的完整信息（描述 / 标签），用于选择器下面的说明行 */
  const selectedStrategy = (strategies.data?.items ?? []).find(
    (s) => (s as { id: string }).id === form.strategy,
  ) as { id: string; name?: string; description?: string; tags?: string[] } | undefined

  // 策略列表加载后，如果当前选中的策略不在列表里（首次进入是空串，
  // 或之前选的策略已被删除），自动落到第一个可用策略。
  // 不写死默认策略的原因：策略库会变，写死就会像 classic_reversal 那样报「策略不存在」。
  useEffect(() => {
    const ids = ((strategies.data?.items ?? []) as { id: string }[]).map((s) => s.id)
    if (!ids.length) return
    setForm((f) => (ids.includes(f.strategy) ? f : { ...f, strategy: ids[0] }))
  }, [strategies.data])

  // 代码 → 中文名：成交明细、个股弹窗标题都用它（拉一次全量，之后不再请求）
  const names = useQuery({
    queryKey: ['symbol-names'],
    queryFn: api.symbolNames,
    staleTime: Infinity,
    gcTime: Infinity,
  })
  const nameOf = (sym: string): string => names.data?.items?.[sym] ?? ''

  const [sp, setSearchParams] = useSearchParams()
  const [runFilter, setRunFilter] = useState<string>('')
  const runs = useQuery({
    queryKey: ['runs', runFilter],
    queryFn: () => api.runs(100, runFilter),
    staleTime: 10_000,
  })

  /** 打开一条历史：把参数还原到表单，结果直接铺到结果区 */
  const openRun = async (id: string) => {
    try {
      const d = await api.runDetail(id)
      const prm = (d.params || {}) as Record<string, unknown>
      setForm((f) => ({
        ...f,
        ...(typeof prm.strategy === 'string' && prm.strategy ? { strategy: prm.strategy as string } : {}),
        ...(typeof prm.start === 'string' ? { start: prm.start } : {}),
        ...(typeof prm.end === 'string' ? { end: prm.end as string } : {}),
        ...(typeof prm.max_positions === 'number' ? { max_positions: prm.max_positions } : {}),
        ...(typeof prm.risk_amount === 'number' ? { risk_amount: prm.risk_amount } : {}),
        ...(typeof prm.init_cash === 'number' ? { init_cash: prm.init_cash } : {}),
        ...(typeof prm.max_position_pct === 'number' ? { max_position_pct: prm.max_position_pct } : {}),
        ...(typeof prm.stop_loss_pct === 'number' ? { stop_loss_pct: prm.stop_loss_pct } : {}),
        ...(typeof prm.max_hold_days === 'number' ? { max_hold_days: prm.max_hold_days } : {}),
      }))
      if (prm.params && typeof prm.params === 'object') {
        const ip = prm.params as Record<string, unknown>
        if (typeof ip.indicator === 'string') {
          setMode('indicator')
          setInd((cur) => ({
            ...cur,
            indicator: ip.indicator as string,
            op: (ip.op as string) || cur.op,
            value: typeof ip.value === 'number' ? ip.value : cur.value,
            value2: typeof ip.value2 === 'number' ? ip.value2 : cur.value2,
            period: typeof ip.period === 'number' ? ip.period : cur.period,
          }))
        }
      }
      if (d.result) setResult(d.result)
    } catch {
      /* 打不开就什么都不做，列表还在 */
    }
  }

  const delRun = async (id: string) => {
    try {
      await api.deleteRun(id)
    } finally {
      runs.refetch()
    }
  }

  const run = useMutation({
    mutationFn: (p: BtParams) => api.btRun(p),
    onSuccess: (d) => {
      setResult(d)
      // 参数写进 URL：切页面回来能还原，也能直接分享链接
      const next = new URLSearchParams(sp)
      next.set('mode', mode)
      if (form.strategy) next.set('strategy', form.strategy)
      if (form.start) next.set('start', form.start)
      if (form.end) next.set('end', form.end)
      next.set('max_positions', String(form.max_positions ?? 5))
      setSearchParams(next, { replace: true })
      runs.refetch()
    },
  })

  // ?autorun=1 自动跑一次（便于分享链接 / 自动截图验证）
  // 可带 &start=2026-06-01&end=2026-11-12&strategy=classic_reversal 覆盖回测区间
  useEffect(() => {
    if (sp.get('autorun') !== '1') return
    const q = sp.get('start')
    const e = sp.get('end')
    const st = sp.get('strategy')
    const p: BtParams = {
      ...DEFAULTS,
      ...(q ? { start: q } : {}),
      ...(e ? { end: e } : {}),
      ...(st ? { strategy: st } : {}),
    }
    setForm(p)
    run.mutate(p)
    // 只在挂载时判断一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const set = <K extends keyof BtParams>(k: K, v: BtParams[K]) => setForm((f) => ({ ...f, [k]: v }))

  const trades = useMemo(() => {
    const list = [...(result?.trades ?? [])]
    list.sort((a, b) => {
      const av = a[sort.key]
      const bv = b[sort.key]
      const an = typeof av === 'number' ? av : String(av)
      const bn = typeof bv === 'number' ? bv : String(bv)
      if (typeof an === 'number' && typeof bn === 'number') return sort.desc ? bn - an : an - bn
      return sort.desc ? String(bn).localeCompare(String(an)) : String(an).localeCompare(String(bn))
    })
    return list
  }, [result, sort])

  const m = result?.metrics

  // 收益率口径：后端给了 entry_price / pnl_pct 就直接用；
  // 旧后端（打包版没有这两个字段）时按成交顺序把买卖配对，本地算成本价与收益率。
  const tradeCalc = useMemo(() => {
    const raw = result?.trades ?? []
    const pos = new Map<string, { qty: number; cost: number }>()
    const out = new Map<Trade, { entry: number; ret: number }>()
    for (const t of raw) {
      if (t.side === 'buy') {
        const cur = pos.get(t.symbol)
        pos.set(t.symbol, {
          qty: (cur?.qty ?? 0) + t.qty,
          cost: (cur?.cost ?? 0) + t.price * t.qty,
        })
        continue
      }
      const cur = pos.get(t.symbol)
      if (!cur || cur.qty <= 0) continue
      const unit = cur.cost / cur.qty
      const basis = unit * t.qty
      out.set(t, { entry: unit, ret: basis > 0 ? (t.pnl ?? 0) / basis : 0 })
      const left = cur.qty - t.qty
      if (left <= 0) pos.delete(t.symbol)
      else pos.set(t.symbol, { qty: left, cost: unit * left })
    }
    return out
  }, [result])

  const entryOf = (t: Trade): number | null =>
    t.side === 'sell' ? (t.entry_price ?? tradeCalc.get(t)?.entry ?? null) : null
  const retOf = (t: Trade): number | null =>
    t.side === 'sell' ? (t.pnl_pct ?? tradeCalc.get(t)?.ret ?? null) : null

  // 已平仓成交的收益率统计：亏损百分比一眼可见（止损 -12% 会显示成 -12.x%，已扣费用）
  const closed = useMemo(() => trades.filter((t) => t.side === 'sell'), [trades])
  const stats = useMemo(() => {
    const pcts = closed.map((t) => retOf(t) ?? 0)
    const neg = pcts.filter((p) => p < 0)
    const pos = pcts.filter((p) => p > 0)
    const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0)
    return {
      n: pcts.length,
      wins: pos.length,
      losses: neg.length,
      realized: closed.reduce((a, t) => a + (t.pnl ?? 0), 0),
      avg: avg(pcts),
      avgWin: avg(pos),
      avgLoss: avg(neg),
      worst: neg.length ? Math.min(...neg) : 0,
      best: pos.length ? Math.max(...pos) : 0,
    }
  }, [closed, tradeCalc])

  // 个股弹窗：K 线起点取该票首次成交前 60 天，保证买卖点全在图上
  const [picked, setPicked] = useState<string | null>(null)
  /** 左栏点某笔成交后，把日K滚到那一天 */
  const [focusDate, setFocusDate] = useState<string | null>(null)
  const pickedTrades = useMemo(
    () => (picked ? trades.filter((t) => t.symbol === picked) : []),
    [trades, picked],
  )
  const klineParams = useMemo(() => {
    if (!pickedTrades.length) return null
    const first = pickedTrades.map((t) => t.dt).sort()[0]
    const d = new Date(`${first}T00:00:00Z`)
    d.setUTCDate(d.getUTCDate() - 60)
    return { symbol: pickedTrades[0].symbol, start: d.toISOString().slice(0, 10) }
  }, [pickedTrades, tradeCalc])

  const kline = useQuery({
    queryKey: ['bt-kline', klineParams?.symbol, klineParams?.start],
    queryFn: () => api.kline(klineParams!.symbol, 1000, klineParams!.start),
    enabled: !!klineParams,
    staleTime: 300_000,
  })

  const markers = useMemo<TradeMarker[]>(
    () =>
      pickedTrades.map((t) => ({
        date: t.dt,
        side: t.side,
        price: t.price,
        label:
          t.side === 'buy'
            ? `${t.dt.slice(5)} 买 ${t.price.toFixed(2)}`
            : `${t.dt.slice(5)} 卖 ${t.price.toFixed(2)} ${
                retOf(t) != null ? `${(retOf(t) as number) > 0 ? '+' : ''}${((retOf(t) as number) * 100).toFixed(1)}%` : ''
              }`,
      })),
    [pickedTrades, tradeCalc],
  )

  // 一键回测全部：走独立脚本，服务里只负责展示结果
  const [scope, setScope] = useState<'ranked' | 'all'>('ranked')
  const ranking = useQuery<{
    items: {
      id: string
      name: string
      rank: number | null
      return: number | null
      mdd: number | null
      sharpe: number | null
      win_rate: number | null
      trades: number | null
    }[]
  }>({
    queryKey: ['strategyRanking', 'bt'],
    queryFn: () => fetch('/api/v1/strategy-ranking').then((r) => r.json()),
    staleTime: 60_000,
  })

  // 策略下拉：中文名 + 排名 + 胜率，按排名排序（没有排名的排后面）
  const strategyOptions = useMemo(() => {
    const rankMap = new Map((ranking.data?.items ?? []).map((x) => [x.id, x]))
    return ((strategies.data?.items ?? []) as { id: string; name?: string }[])
      .map((s) => {
        const r = rankMap.get(s.id)
        return {
          id: s.id,
          name: s.name || s.id,
          rank: r?.rank ?? null,
          win_rate: r?.win_rate ?? null,
          verdict: VERDICT[s.id] as string | undefined,
        }
      })
      .sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999))
  }, [strategies.data, ranking.data])

  const batchResult = useMemo(() => {
    const items = (ranking.data?.items ?? []).filter((x) => x.rank !== null)
    if (!items.length) return undefined
    return {
      strategies: ranking.data?.items.length ?? 0,
      ok: items.length,
      elapsed: 0,
      ranking: items.map((x) => ({
        strategy: x.name,
        rank: x.rank!,
        return: x.return ?? 0,
        annual: undefined as number | undefined,
        mdd: x.mdd ?? 0,
        sharpe: x.sharpe ?? 0,
        win_rate: x.win_rate ?? 0,
        trades: x.trades ?? 0,
      })),
    }
  }, [ranking.data])

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">策略回测</h2>
        <span className="font-mono text-xs2 text-fg3">账户级 · T+1 · 次日开盘成交 · 涨跌停/停牌拒绝</span>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={() => ranking.refetch()}
            disabled={ranking.isFetching}
            className="flex items-center gap-1 rounded-lg border border-brand/50 bg-brand/15 px-3 py-1.5 text-sm2 text-brand hover:bg-brand/25 disabled:opacity-50"
            title="批量回测要独占内存，请在命令行跑 scripts/batch_backtest_all.py；这里读它的结果"
          >
            <ListChecks size={12} />
            {ranking.isFetching ? '刷新中…' : '全部策略排名'}
          </button>
          <button
            onClick={() => setForm(DEFAULTS)}
            className="flex items-center gap-1 rounded-lg border border-line2 bg-card2 px-3 py-1.5 text-sm2 text-fg2 hover:text-fg"
          >
            <RotateCcw size={12} /> 重置
          </button>
          <button
            onClick={() => run.mutate(mode === 'indicator' ? { ...form, strategy: 'indicator_single', params: { ...ind } } : form)}
            disabled={run.isPending}
            className="flex items-center gap-1.5 rounded-lg bg-brand px-4 py-1.5 text-sm2 font-medium text-white disabled:opacity-50"
          >
            <Play size={12} /> {run.isPending ? '回测中…' : '运行回测'}
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-12 gap-3">
          {/* 参数区 */}
          <Card className="col-span-12 xl:col-span-4" title="参数" subtitle="单笔固定止损金额">
            <div className="grid grid-cols-2 gap-x-2.5 gap-y-2 xl:grid-cols-2">
              {/* 模式切换：选现成策略 / 单指标自选条件 */}
              <div className="col-span-2 flex gap-1 rounded-lg bg-card2 p-1">
                {(['strategy', 'indicator'] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setMode(m)}
                    className={`flex-1 rounded px-2 py-1 text-sm2 transition ${
                      mode === m ? 'bg-brand text-white' : 'text-fg3 hover:text-fg'
                    }`}
                  >
                    {m === 'strategy' ? '策略' : '单指标'}
                  </button>
                ))}
              </div>

              {mode === 'strategy' ? (
                <Field label="策略" className="col-span-2">
                  <select
                    value={form.strategy}
                    onChange={(e) => set('strategy', e.target.value)}
                    className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-sm2 outline-none focus:border-brand"
                  >
                    {strategyOptions.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                        {s.rank ? ` · 排名#${s.rank}` : ''}
                        {s.win_rate != null ? ` · 胜率${Math.round(s.win_rate * 100)}%` : ''}
                        {s.verdict === 'pass' ? ' ✅' : s.verdict === 'hold' ? ' ⚠️' : ''}
                      </option>
                    ))}
                    {strategyOptions.length === 0 && <option value={form.strategy}>{form.strategy}</option>}
                  </select>
                </Field>
              ) : (
                <>
                  <Field label="指标" className="col-span-2">
                    <select
                      value={ind.indicator}
                      onChange={(e) => {
                        const v = e.target.value
                        const preset: Record<string, { op: string; value: number; period: number }> = {
                          rsi: { op: 'lt', value: 30, period: 14 },
                          macd: { op: 'cross_up', value: 0, period: 9 },
                          kdj: { op: 'lt', value: 0, period: 9 },
                          boll: { op: 'lt', value: -2, period: 20 },
                          ma: { op: 'gt', value: 0.05, period: 20 },
                          atr: { op: 'gt', value: 0.03, period: 14 },
                          volume_ratio: { op: 'gt', value: 2, period: 20 },
                          change_pct: { op: 'gt', value: 9.8, period: 1 },
                          turnover: { op: 'gt', value: 5, period: 1 },
                          amplitude: { op: 'gt', value: 8, period: 1 },
                          new_high: { op: 'gt', value: 0.98, period: 60 },
                          new_low: { op: 'lt', value: 1.02, period: 60 },
                        }
                        const p = preset[v] || { op: 'gt', value: 0, period: 14 }
                        setInd({ indicator: v, op: p.op, value: p.value, value2: 60, period: p.period })
                      }}
                      className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-sm2 outline-none focus:border-brand"
                    >
                      {[
                        ['rsi', 'RSI'],
                        ['macd', 'MACD 柱'],
                        ['kdj', 'KDJ-J'],
                        ['boll', '布林带位置（σ）'],
                        ['ma', '偏离均线'],
                        ['atr', 'ATR / 价格'],
                        ['volume_ratio', '量比'],
                        ['change_pct', '当日涨跌幅 %'],
                        ['turnover', '换手率 %'],
                        ['amplitude', '振幅 %'],
                        ['new_high', '距 N 日新高'],
                        ['new_low', '距 N 日新低'],
                      ].map(([v, t]) => (
                        <option key={v} value={v}>
                          {t}
                        </option>
                      ))}
                    </select>
                  </Field>

                  <Field label="条件">
                    <select
                      value={ind.op}
                      onChange={(e) => setInd({ ...ind, op: e.target.value })}
                      className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-sm2 outline-none focus:border-brand"
                    >
                      {[
                        ['lt', '小于'],
                        ['gt', '大于'],
                        ['cross_up', '上穿'],
                        ['cross_down', '下穿'],
                        ['between', '介于'],
                      ].map(([v, t]) => (
                        <option key={v} value={v}>
                          {t}
                        </option>
                      ))}
                    </select>
                  </Field>

                  <div className="grid grid-cols-2 gap-2">
                    <Field label="阈值">
                      <input
                        type="number"
                        step="0.1"
                        value={ind.value}
                        onChange={(e) => setInd({ ...ind, value: Number(e.target.value) })}
                        className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-right font-mono text-sm2 outline-none focus:border-brand"
                      />
                    </Field>
                    <Field label={ind.op === 'between' ? '上界' : '周期'}>
                      <input
                        type="number"
                        step="1"
                        value={ind.op === 'between' ? ind.value2 : ind.period}
                        onChange={(e) =>
                          setInd(
                            ind.op === 'between'
                              ? { ...ind, value2: Number(e.target.value) }
                              : { ...ind, period: Number(e.target.value) },
                          )
                        }
                        className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-right font-mono text-sm2 outline-none focus:border-brand"
                      />
                    </Field>
                  </div>
                  {ind.op === 'between' && (
                    <Field label="周期">
                      <input
                        type="number"
                        value={ind.period}
                        onChange={(e) => setInd({ ...ind, period: Number(e.target.value) })}
                        className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-right font-mono text-sm2 outline-none focus:border-brand"
                      />
                    </Field>
                  )}
                  <div className="rounded border border-line2 bg-card2/50 px-2 py-1.5 font-mono text-xs2 text-fg3">
                    {ind.indicator === 'rsi' && `RSI(${ind.period}) ${ind.op === 'lt' ? '<' : ind.op === 'gt' ? '>' : ind.op === 'cross_up' ? '上穿' : ind.op === 'cross_down' ? '下穿' : '介于'} ${ind.value}`}
                    {ind.indicator === 'macd' && `MACD柱 ${ind.op === 'cross_up' ? '上穿' : ind.op === 'cross_down' ? '下穿' : ind.op === 'lt' ? '<' : '>'} ${ind.value}`}
                    {ind.indicator === 'kdj' && `KDJ-J(${ind.period}) ${ind.op === 'lt' ? '<' : '>'} ${ind.value}`}
                    {ind.indicator === 'boll' && `BOLL位置(${ind.period}) ${ind.op === 'lt' ? '<' : '>'} ${ind.value}σ`}
                    {ind.indicator === 'ma' && `close/MA${ind.period}-1 ${ind.op === 'lt' ? '<' : '>'} ${(ind.value * 100).toFixed(1)}%`}
                    {ind.indicator === 'atr' && `ATR(${ind.period})/价格 ${ind.op === 'lt' ? '<' : '>'} ${(ind.value * 100).toFixed(1)}%`}
                    {ind.indicator === 'volume_ratio' && `量比 ${ind.op === 'lt' ? '<' : '>'} ${ind.value}`}
                    {ind.indicator === 'change_pct' && `当日涨跌幅 ${ind.op === 'lt' ? '<' : '>'} ${ind.value}%`}
                    {ind.indicator === 'turnover' && `换手率 ${ind.op === 'lt' ? '<' : '>'} ${ind.value}%`}
                    {ind.indicator === 'amplitude' && `振幅 ${ind.op === 'lt' ? '<' : ind.op === 'gt' ? '>' : '∈'} ${ind.value}${ind.op === 'between' ? `~${ind.value2}` : ''}%`}
                    {ind.indicator === 'new_high' && `close/近${ind.period}日高 ${ind.op === 'lt' ? '<' : '>'} ${(ind.value * 100).toFixed(0)}%`}
                    {ind.indicator === 'new_low' && `close/近${ind.period}日低 ${ind.op === 'lt' ? '<' : '>'} ${(ind.value * 100).toFixed(0)}%`}
                  </div>
                </>
              )}

              {mode === 'strategy' && (
                <div className="col-span-2 -mt-0.5">
                  {selectedStrategy?.description ? (
                    <div
                      className="rounded border border-bull/25 bg-bull/[0.07] px-2 py-1.5 text-[11px] leading-relaxed text-bull/90"
                      title={selectedStrategy.description}
                    >
                      {selectedStrategy.description}
                      {selectedStrategy.tags && selectedStrategy.tags.length > 0 && (
                        <span className="ml-1.5 text-[10px] text-bull/60">
                          [{selectedStrategy.tags.join(' · ')}]
                        </span>
                      )}
                    </div>
                  ) : (
                    <div className="rounded border border-dashed border-line px-2 py-1.5 text-[11px] text-fg3">
                      这个策略没写描述（策略文件里的 META.description）
                    </div>
                  )}
                </div>
              )}

              <Field label="初始资金（元）">
                <Num value={form.init_cash} onChange={(v) => set('init_cash', v)} />
              </Field>

              <Field label="单笔止损金额（元）">
                <Num value={form.risk_amount ?? 3000} onChange={(v) => set('risk_amount', v)} />
              </Field>

              <Field label="最多持仓（只）">
                <Num value={form.max_positions} onChange={(v) => set('max_positions', v)} />
              </Field>

              <Field label="单票上限（%）">
                <Num value={form.max_position_pct * 100} onChange={(v) => set('max_position_pct', v / 100)} />
              </Field>

              <Field label="止损（%）">
                <Num value={form.stop_loss_pct * 100} onChange={(v) => set('stop_loss_pct', v / 100)} />
              </Field>

              <Field label="最大持仓天数">
                <Num value={form.max_hold_days ?? 5} onChange={(v) => set('max_hold_days', v)} />
              </Field>

              <Field label="起始日期" className="col-span-2">
                <input
                  type="date"
                  value={form.start ?? ''}
                  onChange={(e) => set('start', e.target.value || null)}
                  className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 font-mono text-sm2 outline-none focus:border-brand"
                />
              </Field>
            </div>

            <div className="col-span-2 mt-1 rounded-md bg-card2 px-2.5 py-2 text-xs2 leading-relaxed text-fg3">
              仓位 = 止损金额 ÷（买入价 − 止损价），向下取整 100 股。
              实际止损额会因取整略小于设定值。
            </div>

            {/* 回测历史：后端落盘 data/runs/*.json，重启后还在 */}
            <div className="col-span-2 mt-1 border-t border-line pt-2">
              <div className="mb-1 flex items-center gap-1.5">
                <History size={12} className="text-fg3" />
                <span className="text-sm2 font-medium text-fg2">回测历史</span>
                <span className="font-mono text-xs2 text-fg3">{runs.data?.count ?? 0} 条</span>
                <select
                  value={runFilter}
                  onChange={(e) => setRunFilter(e.target.value)}
                  className="ml-1 rounded border border-line2 bg-card2 px-1 py-0.5 text-[10px] text-fg2 outline-none focus:border-brand"
                  title="按策略筛选历史"
                >
                  <option value="">全部策略</option>
                  {[...new Set((runs.data?.items ?? []).map((r) => r.strategy || r.title))].filter(Boolean).map((sid) => (
                    <option key={sid} value={sid}>
                      {sid}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => {
                    if (!runs.data?.count) return
                    window.open(api.runsExportUrl(), '_blank')
                  }}
                  disabled={!runs.data?.count}
                  className="ml-auto flex items-center gap-0.5 text-xs2 text-fg3 hover:text-accent disabled:opacity-40"
                  title="把全部回测记录导出成 Excel（汇总 / 分状态 / 逐笔）"
                >
                  <Download size={11} /> 导出
                </button>
                <button
                  onClick={() => runs.refetch()}
                  className="text-xs2 text-fg3 hover:text-fg2"
                  title="刷新历史列表"
                >
                  刷新
                </button>
              </div>
              <div className="max-h-[184px] overflow-y-auto">
                {(runs.data?.items ?? []).length === 0 && (
                  <div className="rounded border border-dashed border-line py-4 text-center text-xs2 text-fg3">
                    还没有记录，跑一次回测就会存下来
                  </div>
                )}
                {(runs.data?.items ?? []).map((it) => (
                  <div
                    key={it.id}
                    className="group flex items-center gap-1.5 border-b border-line/60 px-1 py-[5px] hover:bg-card2"
                  >
                    <button
                      onClick={() => openRun(it.id)}
                      className="flex min-w-0 flex-1 items-center gap-2 text-left"
                      title={`${it.title || it.kind} · ${it.created_at}`}
                    >
                      <span className="w-[92px] shrink-0 font-mono text-xs2 text-fg3">
                        {(it.created_at || '').slice(5, 16)}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-xs2 text-fg2">
                        {it.title || it.kind}
                      </span>
                      {(() => {
                        // 徽章只放一个字：强 / 横 / 弱；全称和占比放 tooltip
                        const d = it.regime?.dominant
                        const short = d === '强市' ? '强' : d === '弱市' ? '弱' : d === '震荡' ? '横' : '—'
                        const cls =
                          d === '强市'
                            ? 'bg-bull/15 text-bull'
                            : d === '弱市'
                              ? 'bg-bear/15 text-bear'
                              : d === '震荡'
                                ? 'bg-card2 text-fg2'
                                : 'bg-card2 text-fg3'
                        const tip = d
                          ? `区间大盘状态：${d}（${Object.entries(it.regime?.dist || {})
                              .map(([k, v]) => `${k} ${v}%`)
                              .join(' / ')}）${
                              it.regime?.index_return != null ? ` · 指数 ${it.regime.index_return}%` : ''
                            }`
                          : '这条老记录没有起止日期，算不了区间大盘状态'
                        return (
                          <span
                            className={`shrink-0 rounded px-1.5 text-center font-mono text-[10px] leading-[16px] ${cls}`}
                            title={tip}
                          >
                            {short}
                          </span>
                        )
                      })()}
                      {it.extras?.avg_hold_days != null && (
                        <span className="shrink-0 font-mono text-[10px] text-fg3" title="平均持仓天数（自然日）">
                          {it.extras.avg_hold_days}天
                        </span>
                      )}
                      <span
                        className={`w-[54px] shrink-0 text-right font-mono text-xs2 ${pctClass(
                          (it.metrics?.total_return ?? 0) as number,
                        )}`}
                      >
                        {fmtPct((it.metrics?.total_return ?? 0) as number)}
                      </span>
                    </button>
                    <button
                      onClick={() => window.open(api.runsExportUrl([it.id]), '_blank')}
                      className="shrink-0 rounded p-0.5 text-fg3 opacity-0 hover:text-accent group-hover:opacity-100"
                      title="导出这条记录（含逐笔成交）"
                    >
                      <Download size={11} />
                    </button>
                    <button
                      onClick={() => delRun(it.id)}
                      className="shrink-0 rounded p-0.5 text-fg3 opacity-0 hover:text-bear group-hover:opacity-100"
                      title="删除这条记录"
                    >
                      <Trash2 size={11} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </Card>

          {/* 结果区 */}
          <div className="col-span-12 space-y-3 xl:col-span-8">
            {/* 全策略排名（来自 batch_backtest_all.py 的结果） */}
            {batchResult && (
              <Card
                title="全部策略排名"
                subtitle={`${batchResult.ok}/${batchResult.strategies} 个有回测结果 · 读自 reports/account_compare_fullmarket.md`}
                right={
                  <span className="text-3xs">
                    重跑：<code className="rounded bg-white/5 px-1">python scripts/batch_backtest_all.py</code>
                  </span>
                }
                padded={false}
              >
                <div className="max-h-[300px] overflow-y-auto">
                  <table className="w-full border-collapse text-sm2">
                    <thead className="sticky top-0 bg-card">
                      <tr>
                        {['#', '策略', '收益', '回撤', '夏普', '胜率', '交易'].map((h, i) => (
                          <th
                            key={h}
                            className={`border-b border-line px-2 py-1.5 text-3xs font-medium text-fg3 ${
                              i <= 1 ? 'text-left' : 'text-right'
                            }`}
                          >
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {batchResult.ranking.map((r) => (
                        <tr key={r.strategy} className="hover:bg-brand/[0.06]">
                          <td className="border-b border-line/50 px-2 py-1 text-left font-mono text-fg3">{r.rank}</td>
                          <td className="border-b border-line/50 px-2 py-1 text-left">{r.strategy}</td>
                          <td
                            className={`num border-b border-line/50 px-2 py-1 text-right ${
                              r.return > 0 ? 'text-up' : r.return < 0 ? 'text-down' : 'text-fg3'
                            }`}
                          >
                            {(r.return * 100).toFixed(1)}%
                          </td>
                          <td className="num border-b border-line/50 px-2 py-1 text-right text-down">
                            {(r.mdd * 100).toFixed(1)}%
                          </td>
                          <td className="num border-b border-line/50 px-2 py-1 text-right text-gold">
                            {r.sharpe.toFixed(2)}
                          </td>
                          <td className="num border-b border-line/50 px-2 py-1 text-right text-fg3">
                            {(r.win_rate * 100).toFixed(0)}%
                          </td>
                          <td className="num border-b border-line/50 px-2 py-1 text-right text-fg3">{r.trades}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}

            {run.isError && (
              <Card title="回测失败">
                <div className="text-xs text-up">{(run.error as Error).message}</div>
              </Card>
            )}

            <Card
              title="净值与回撤"
              subtitle={result ? `${result.metrics.start} ~ ${result.metrics.end} · ${result.nav.length} 个交易日` : '未运行'}
              right={result ? `期末 ${Math.round(result.metrics.final_equity ?? 0).toLocaleString()} 元` : ''}
              padded={false}
            >
              <EquityChart nav={result?.nav ?? []} height={300} />
            </Card>

            <Card title="绩效指标" subtitle="账户口径" padded={false}>
              <div className="grid grid-cols-6 gap-2 p-3">
                <Metric
                  label="总收益"
                  value={m ? fmtPct((m.total_return ?? 0) * 100) : '—'}
                  tone={m ? pctClass((m.total_return ?? 0) * 100) : ''}
                />
                <Metric
                  label="年化"
                  value={m ? fmtPct((m.annual_return ?? 0) * 100) : '—'}
                  tone={m ? pctClass((m.annual_return ?? 0) * 100) : ''}
                />
                <Metric
                  label="最大回撤"
                  value={m ? fmtPct((m.max_drawdown ?? 0) * 100) : '—'}
                  tone="text-down"
                />
                <Metric label="夏普" value={m?.sharpe?.toFixed(2) ?? '—'} />
                <Metric label="卡玛" value={m?.calmar?.toFixed(2) ?? '—'} />
                <Metric label="胜率" value={m?.win_rate != null ? `${(m.win_rate * 100).toFixed(1)}%` : '—'} />
                <Metric label="交易笔数" value={String(m?.trades ?? '—')} />
                <Metric label="盈利/亏损" value={m ? `${m.wins}/${m.losses}` : '—'} />
                <Metric label="盈亏比" value={m?.payoff?.toFixed(2) ?? '—'} />
                <Metric label="波动率" value={m ? fmtPct((m.volatility ?? 0) * 100, 1, false) : '—'} />
                <Metric label="回撤天数" value={String(m?.max_drawdown_days ?? '—')} />
                <Metric label="期末权益" value={m ? fmtInt(m.final_equity ?? 0) : '—'} />
              </div>
              {result && Object.keys(result.rejected).length > 0 && (
                <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-line px-3 py-2 text-xs2 text-fg3">
                  <span>拒单：</span>
                  {Object.entries(result.rejected).map(([k, v]) => (
                    <span key={k}>
                      {REJECT_LABEL[k] ?? k} <b className="num text-fg2">{v}</b>
                    </span>
                  ))}
                </div>
              )}
            </Card>

            <Card
              title="成交明细"
              subtitle={result ? `${result.trades.length} 笔 · 点任意一行看日K与买卖点` : '未运行'}
              right={
                result ? (
                  <button
                    onClick={() => setSort({ key: 'dt', desc: !sort.desc })}
                    className="text-fg3 hover:text-fg2"
                  >
                    日期 {sort.key === 'dt' ? (sort.desc ? '↓' : '↑') : ''}
                  </button>
                ) : null
              }
              padded={false}
            >
              {/* 已平仓收益分布：亏损百分比（单笔 mean / 最差） */}
              {closed.length > 0 && (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-3 py-2 text-xs2 text-fg3">
                  <span className="text-fg2">
                    已实现 <b className={`num ${moneyClass(stats.realized)}`}>{fmtInt(stats.realized)}</b>
                  </span>
                  <span>
                    平均收益 <b className={`num ${pctClass(stats.avg * 100)}`}>{fmtPct(stats.avg * 100)}</b>
                  </span>
                  <span>
                    平均亏损 <b className={`num ${pctClass(stats.avgLoss * 100)}`}>{stats.losses ? fmtPct(stats.avgLoss * 100) : '—'}</b>
                  </span>
                  <span>
                    平均盈利 <b className={`num ${pctClass(stats.avgWin * 100)}`}>{stats.wins ? fmtPct(stats.avgWin * 100) : '—'}</b>
                  </span>
                  <span>
                    单笔最差 <b className={`num ${pctClass(stats.worst * 100)}`}>{stats.losses ? fmtPct(stats.worst * 100) : '—'}</b>
                  </span>
                  <span>
                    单笔最好 <b className={`num ${pctClass(stats.best * 100)}`}>{stats.wins ? fmtPct(stats.best * 100) : '—'}</b>
                  </span>
                  <span className="ml-auto">
                    平仓 {stats.n} 笔 · 盈 {stats.wins} / 亏 {stats.losses}
                  </span>
                </div>
              )}
              <div className="max-h-[340px] overflow-y-auto">
                <DataTable<Trade>
                  rows={trades}
                  rowKey={(r, i) => `${r.symbol}-${r.dt}-${r.side}-${i}`}
                  empty={run.isPending ? '回测中…' : '尚无成交'}
                  onRowClick={(r) => setPicked(r.symbol)}
                  columns={[
                    { key: 'dt', label: '成交日', render: (r) => <span className="text-fg2">{r.dt}</span> },
                    {
                      key: 'symbol',
                      label: '代码 / 名称',
                      render: (r) => (
                        <span
                          className="cursor-pointer whitespace-nowrap text-brand hover:underline"
                          title="点击看该股日K与买卖点"
                        >
                          {r.symbol}
                          {nameOf(r.symbol) ? (
                            <span className="ml-1 text-3xs text-fg3">{nameOf(r.symbol)}</span>
                          ) : null}
                        </span>
                      ),
                    },
                    {
                      key: 'side',
                      label: '方向',
                      render: (r) => (
                        <span className={r.side === 'buy' ? 'text-up' : 'text-down'}>
                          {r.side === 'buy' ? '买' : '卖'}
                        </span>
                      ),
                    },
                    { key: 'qty', label: '数量', render: (r) => r.qty },
                    {
                      key: 'price',
                      label: '价格',
                      render: (r) => (
                        <span>
                          {r.price.toFixed(2)}
                          {r.side === 'sell' && entryOf(r) ? (
                            <span className="ml-1 text-3xs text-fg3">← {entryOf(r)!.toFixed(2)}</span>
                          ) : null}
                        </span>
                      ),
                    },
                    {
                      key: 'amount',
                      label: '金额',
                      render: (r) => fmtInt(r.qty * r.price),
                    },
                    {
                      key: 'pnl',
                      label: '盈亏 / %',
                      render: (r) =>
                        r.side === 'sell' ? (
                          <div className="leading-[1.25]">
                            <div className={moneyClass(r.pnl)}>{r.pnl ? r.pnl.toFixed(0) : '—'}</div>
                            <div className={`text-3xs ${pctClass((retOf(r) ?? 0) * 100)}`}>
                              {retOf(r) != null ? fmtPct(retOf(r)! * 100) : '—'}
                            </div>
                          </div>
                        ) : (
                          <span className="text-fg3">—</span>
                        ),
                    },
                    {
                      key: 'fees',
                      label: '费用',
                      render: (r) => <span className="text-fg3">{(r.fees?.total ?? 0).toFixed(1)}</span>,
                    },
                    { key: 'reason', label: '原因', render: (r) => <span className="text-fg3">{REASON[r.reason] ?? r.reason}</span> },
                    { key: 'hold', label: '持有', render: (r) => <span className="text-fg3">{r.hold_days}天</span> },
                  ]}
                />
              </div>
            </Card>
          </div>
        </div>
      </div>

      {picked && (
        <Modal
          title={
            <span>
              {picked}
              {nameOf(picked) ? <span className="ml-1.5 font-normal text-fg2">{nameOf(picked)}</span> : null}
              <span className="ml-2 text-xs2 font-normal text-fg3">日K与买卖点</span>
            </span>
          }
          subtitle={`${pickedTrades.filter((t) => t.side === 'buy').length} 买 / ${
            pickedTrades.filter((t) => t.side === 'sell').length
          } 卖`}
          right={
            <span>
              实现盈亏{' '}
              <b className={moneyClass(pickedTrades.reduce((a, t) => a + (t.pnl ?? 0), 0))}>
                {fmtInt(pickedTrades.reduce((a, t) => a + (t.pnl ?? 0), 0))}
              </b>
            </span>
          }
          onClose={() => {
            setPicked(null)
            setFocusDate(null)
          }}
          width="max-w-[1260px]"
        >
          <div className="flex gap-3 p-3">
            {/* 左栏：整张成交明细（本次回测全部成交），点一笔 → 右侧切到那只票并定位到那一天 */}
            <div className="w-[268px] shrink-0">              <div className="mb-1 flex items-center justify-between px-1 text-3xs text-fg3">
                <span>成交明细（点一笔看图）</span>
                <span className="num">{trades.length} 笔</span>
              </div>
              <div className="max-h-[548px] overflow-y-auto rounded border border-line">
                {trades.length === 0 && (
                  <div className="px-2 py-6 text-center text-xs2 text-fg3">尚无成交</div>
                )}
                {trades.map((t, i) => {
                  const active = t.symbol === picked && focusDate === t.dt
                  const ret = retOf(t)
                  return (
                    <button
                      key={`${t.symbol}-${t.dt}-${t.side}-${i}`}
                      onClick={() => {
                        if (t.symbol !== picked) setPicked(t.symbol)
                        setFocusDate(t.dt)
                      }}
                      title="点击看这只票这一步的日K"
                      className={`flex w-full flex-col gap-[1px] border-b border-line/50 px-2 py-[5px] text-left font-mono transition last:border-b-0 ${
                        active ? 'bg-brand/20' : 'hover:bg-white/[0.05]'
                      }`}
                    >
                      <div className="flex items-baseline gap-1.5">
                        <span className="text-xs2 text-fg3">{t.dt.slice(5)}</span>
                        <span
                          className={`text-xs2 ${t.side === 'buy' ? 'text-up' : 'text-down'}`}
                        >
                          {t.side === 'buy' ? '买' : '卖'}
                        </span>
                        <span className="text-xs2 text-brand">{t.symbol}</span>
                        <span className={`num ml-auto text-3xs ${pctClass((ret ?? 0) * 100)}`}>
                          {ret != null ? fmtPct(ret * 100) : ''}
                        </span>
                      </div>
                      <div className="flex items-baseline gap-1.5 text-3xs text-fg3">
                        <span className="truncate">{nameOf(t.symbol) || '—'}</span>
                        <span className="num ml-auto whitespace-nowrap">
                          {t.qty}股 @{t.price.toFixed(2)}
                        </span>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>

            {/* 右侧：日K + 买卖点 */}
            <div className="min-w-0 flex-1">
              {kline.isLoading && (
                <div className="py-16 text-center text-sm2 text-fg3">日K加载中…</div>
              )}
              {kline.isError && (
                <div className="py-16 text-center text-sm2 text-up">
                  日K加载失败：{(kline.error as Error).message}
                </div>
              )}
              {!kline.isLoading && !kline.isError && (
                <>
                  <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs2 text-fg3">
                    <span className="flex items-center gap-1">
                      <i className="inline-block h-2 w-2 rotate-45 bg-up" /> 买入（下方：日期 买价）
                    </span>
                    <span className="flex items-center gap-1">
                      <i className="inline-block h-2 w-2 rotate-45 bg-down" /> 卖出（上方：日期 卖价 收益率）
                    </span>
                    <span>{kline.data?.rows.length ?? 0} 根日线</span>
                    {klineParams?.start && <span>起 {klineParams.start}</span>}
                    <span className="ml-auto">滚轮缩放 · 拖拽平移</span>
                  </div>
                  <KLineChart
                    bars={kline.data?.rows ?? []}
                    trades={markers}
                    height={500}
                    focusDate={focusDate}
                  />
                </>
              )}
            </div>
          </div>
        </Modal>
      )}
    </>
  )
}

const REJECT_LABEL: Record<string, string> = {
  limit_up: '涨停开盘',
  limit_down: '跌停开盘',
  suspended: '停牌',
  lot: '不足一手',
  max_positions: '仓位已满',
  budget: '预算用尽',
  cash: '现金不足',
  no_bar: '无行情',
}

const REASON: Record<string, string> = {
  entry: '建仓',
  stop: '止损',
  stop_loss: '止损',
  target: '止盈',
  take_profit: '止盈',
  max_hold: '到期',
  eod: '收盘平仓',
  signal_exit: '策略平仓',
  fail_exit: '打板失败·次日开盘卖',
  break_ma: '跌破均线·次日开盘卖',
  break_board: '断板·次日开盘卖',
}

function Field({
  label,
  children,
  className = '',
}: {
  label: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1 block text-xs2 text-fg3">{label}</span>
      {children}
    </label>
  )
}

function Num({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <input
      type="number"
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-right font-mono text-sm2 outline-none focus:border-brand"
    />
  )
}
