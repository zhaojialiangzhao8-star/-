import { useQuery } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Calendar, Check, RefreshCw, RotateCcw, LayoutGrid } from 'lucide-react'
import { api, type DailySignalRow, type LimitUpBucket } from '../services/api'
import { Card, DataTable, Metric, Tag } from '../components/ui'
import KLineChart from '../components/KLineChart'
import { DistBar, EmotionRadar, MiniBar, RankList, StatCell } from '../components/dashboard/DashWidgets'
import { DashGrid, DEFAULT_LAYOUT, loadLayout, resetLayout, saveLayout } from '../components/dashboard/DashGrid'
import { clock, fmtInt, fmtPct, moneyClass, pctClass } from '../lib/format'

/** 策略体检结论（来自四关验证，硬编码是因为它是研究结论而非实时数据） */
const HEALTH = [
  { name: '急跌反弹', verdict: 'pass' as const, out: 0.27, uw: 511, dd: -24.6 },
  { name: '52周新高', verdict: 'hold' as const, out: 0.22, uw: null, dd: -51.5 },
  { name: '合成 IC6', verdict: 'fail' as const, out: 0.24, uw: 1286, dd: -45.0 },
  { name: 'RSI超卖', verdict: 'fail' as const, out: -0.06, uw: null, dd: -38.0 },
  { name: '均值回归', verdict: 'fail' as const, out: 0.06, uw: null, dd: -43.0 },
  { name: '高窄旗形', verdict: 'fail' as const, out: 0.12, uw: null, dd: -27.7 },
]

export default function Dashboard() {
  const [risk, setRisk] = useState(3000)
  /** 点开的指数（弹 K 线）；null = 没开 */
  const [indexSym, setIndexSym] = useState<string | null>(null)
  /** 看板编辑态：只有编辑态才允许拖动/缩放，也才接收布局回调 */
  const [editing, setEditing] = useState(false)
  const [layout, setLayout] = useState(() => loadLayout())

  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  // 看板数据：整屏一次拿全（广度/分布/情绪/趋势/四榜）
  const dash = useQuery({ queryKey: ['dashboard'], queryFn: () => api.dashboard(8), refetchInterval: 60_000 })
  const overview = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: 60_000 })
  const signal = useQuery({ queryKey: ['signal', risk], queryFn: () => api.dailySignal(risk) })
  const limitUp = useQuery({ queryKey: ['limitUp'], queryFn: api.limitUp, staleTime: 600_000 })
  const tasks = useQuery({ queryKey: ['tasks'], queryFn: () => api.tasks(5), refetchInterval: 5_000 })

  // 全策略当日信号（与智能选股页共用缓存，进页面秒回）
  const allSig = useQuery<{
    strategy_count: number
    total_signals_today: number
    items: {
      id: string
      name: string
      rows: { symbol: string; name?: string; close?: number; score?: number; note?: string }[]
      rank?: number | null
      win_rate?: number | null
      return?: number | null
      signal_date?: string | null
    }[]
    updated: string
  }>({
    queryKey: ['screenerAll', 'ranked'],
    queryFn: () =>
      fetch('/api/v1/screener/all?limit_per_strategy=6&scope=ranked').then((r) => r.json()),
    staleTime: 5 * 60_000,
    retry: 0,
  })

  // 完整策略排名（58 个，可滚动）
  const ranking = useQuery<{
    count: number
    ranked: number
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
    queryKey: ['strategyRanking'],
    queryFn: () => fetch('/api/v1/strategy-ranking').then((r) => r.json()),
    staleTime: 5 * 60_000,
  })

  const ov = overview.data

  const sigItems = (allSig.data?.items ?? []).filter((x) => (x.rows?.length ?? 0) > 0)

  // 指数条（5 秒刷新）
  const idx = useQuery({
    queryKey: ['indices'],
    queryFn: () => api.indices(),
    refetchInterval: 5_000,
    retry: 0,
  })

  // 点右侧排名 → 左侧今日信号高亮并滚动过去
  const [highlight, setHighlight] = useState<string | null>(null)
  const [rebuilding, setRebuilding] = useState(false)

  // 批量加自选
  const [adding, setAdding] = useState<string | null>(null)
  const addAll = async (rows: { symbol: string; name?: string }[]) => {
    setAdding('batch')
    try {
      for (const r of rows) {
        await fetch('/api/v1/watchlist', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: r.symbol, name: r.name ?? '' }),
        })
      }
    } finally {
      setAdding(null)
    }
  }

  return (
    <>
      {/* 顶部工具栏 —— 对齐 Tick：标题 + 情绪徽章 + 日期 + 状态 + 操作 */}
      <header className="flex flex-wrap items-center gap-2 border-b border-line bg-bg2 px-4 py-1.5">
        <span className="text-[15px] font-medium tracking-wide text-fg">市场看板</span>
        {dash.data && (
          <span
            className={`rounded border px-1.5 py-0.5 text-[11px] ${
              (dash.data.emotion.score ?? 0) >= 60
                ? 'border-up/40 bg-up/15 text-up'
                : (dash.data.emotion.score ?? 0) >= 40
                  ? 'border-gold/40 bg-gold/15 text-gold'
                  : 'border-down/40 bg-down/15 text-down'
            }`}
            title="综合情绪评分（0~100）"
          >
            {(dash.data.emotion.score ?? 0) >= 60 ? '偏热' : (dash.data.emotion.score ?? 0) >= 40 ? '中性' : '偏冷'}
            · {dash.data.emotion.score?.toFixed(0) ?? '—'}
          </span>
        )}
        <span className="ml-3 flex items-center gap-1.5 rounded border border-brand/50 bg-brand/10 px-2 py-1 text-[12px] text-fg">
          <Calendar size={13} className="text-brand" />
          {dash.data?.date ?? '—'}
        </span>
        <div className="flex-1" />
        {editing && (
          <button
            onClick={() => { setLayout(resetLayout()); setEditing(false) }}
            className="flex items-center gap-1.5 rounded border border-line bg-card px-2 py-1 text-[12px] text-fg3 hover:text-fg"
            title="恢复默认布局"
          >
            <RotateCcw size={12} /> 恢复默认
          </button>
        )}
        <button
          onClick={() => setEditing((v) => !v)}
          className={`flex items-center gap-1.5 rounded border px-2 py-1 text-[12px] transition-colors ${
            editing ? 'border-brand bg-brand/20 text-brand' : 'border-line bg-card text-fg2 hover:border-brand/50 hover:text-brand'
          }`}
          title={editing ? '完成编辑' : '拖动 / 缩放卡片'}
        >
          {editing ? <><Check size={12} /> 完成</> : <><LayoutGrid size={12} /> 自定义布局</>}
        </button>
        <span className="rounded-full border border-line bg-card px-2.5 py-1 font-mono text-xs2 text-fg2">
          <span className={health.isError ? 'text-gold' : 'text-down'}>●</span>{' '}
          {health.isError ? '未连接' : '引擎在线'}
        </span>
        <span className="rounded-full border border-line bg-card px-2.5 py-1 font-mono text-xs2 text-fg2">
          {clock()}
        </span>
      </header>

      <div className="flex-1 overflow-y-auto p-3">
        <DashGrid editing={editing} layout={layout} onLayoutChange={(l) => { setLayout(l); saveLayout(l) }}>
          {/* 指数卡：4 等分，参照 TSP 的看板第一行 */}
          <div key="indices" className="grid grid-cols-4 gap-2">
          {(idx.data?.items ?? [])
            .filter((x) => x.available)
            .slice(0, 4)
            .map((x) => (
              <button
                key={x.symbol}
                onClick={() => setIndexSym(x.symbol)}
                className="group rounded-lg border border-line bg-card px-2.5 py-1.5 text-left transition-colors hover:border-brand/60 hover:bg-card2"
                title={`${x.name} · 点击看 K 线`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate text-[14px] text-fg2 group-hover:text-brand">{x.name}</span>
                  <span className={`num shrink-0 text-[12px] font-medium ${pctClass(x.change_pct)}`}>
                    {fmtPct(x.change_pct)}
                  </span>
                </div>
                <div className="mt-1 flex items-baseline justify-between gap-2">
                  <span className="font-mono text-[11px] text-fg3">{x.symbol}</span>
                  {/* Tick 的点位：跟着涨跌变色（跌绿涨红），字重 normal，18px */}
                  <span className={`num flex items-baseline gap-1 text-[15px] font-normal leading-none ${pctClass(x.change_pct)}`}>
                    <span className="text-[12px]">
                      {x.change_pct != null && x.change_pct > 0 ? '↗' : x.change_pct != null && x.change_pct < 0 ? '↘' : ''}
                    </span>
                    {x.price?.toFixed(2)}
                  </span>
                </div>
              </button>
            ))}
          </div>

          {/* 市场广度：7 个**独立卡片**（Tick 里每项都是自己的卡片，不是一条大卡 + 竖线分隔） */}
          <div key="kpi" className="grid grid-cols-7 gap-2">
              <Kpi
                label="个股涨 / 平 / 跌"
                value={
                  dash.data ? (
                    <>
                      <span className="text-up">{dash.data.breadth.up}</span>
                      <span className="text-fg3"> / </span>
                      <span className="text-fg2">{dash.data.breadth.flat}</span>
                      <span className="text-fg3"> / </span>
                      <span className="text-down">{dash.data.breadth.down}</span>
                    </>
                  ) : (
                    '—'
                  )
                }
                sub={dash.data ? `上涨率 ${dash.data.breadth.up_pct}%` : ''}
              />
              <Kpi
                label="强势 / 弱势"
                value={
                  dash.data ? (
                    <>
                      <span className="text-up">{dash.data.breadth.strong}</span>
                      <span className="text-fg3"> / </span>
                      <span className="text-down">{dash.data.breadth.weak}</span>
                    </>
                  ) : (
                    '—'
                  )
                }
                sub="涨跌 ≥3%"
              />
              <Kpi
                label="涨停 / 跌停"
                value={
                  dash.data ? (
                    <>
                      <span className="text-up">{dash.data.breadth.limit_up}</span>
                      <span className="text-fg3"> / </span>
                      <span className="text-down">{dash.data.breadth.limit_down}</span>
                    </>
                  ) : (
                    '—'
                  )
                }
                sub={dash.data ? `封板率 ${dash.data.breadth.limit_up + dash.data.breadth.limit_down > 0 ? Math.round((dash.data.breadth.limit_up / (dash.data.breadth.limit_up + dash.data.breadth.limit_down)) * 100) : 0}%` : ''}
              />
              <Kpi
                label="最高连板"
                value={
                  dash.data?.ladder.top ? (
                    <span className="text-cyan">{dash.data.ladder.top.boards} 板</span>
                  ) : (
                    '—'
                  )
                }
                sub={dash.data?.ladder.top?.name || (dash.data ? `共 ${dash.data.ladder.total} 只连板` : '')}
              />
              <Kpi
                label="涨跌中位"
                value={dash.data ? `${dash.data.breadth.median_pct > 0 ? '+' : ''}${dash.data.breadth.median_pct}%` : '—'}
                sub={dash.data ? `均值 ${dash.data.breadth.avg_pct}%` : ''}
                tone={dash.data ? (dash.data.breadth.median_pct >= 0 ? 'text-up' : 'text-down') : ''}
              />
              <Kpi
                label={dash.data?.amount.real ? '成交额（真实）' : '成交额'}
                value={dash.data ? `${(dash.data.amount.total_yi / 10000).toFixed(2)} 万亿` : '—'}
                sub={dash.data ? `均额 ${dash.data.amount.avg_yi} 亿` : ''}
              />
              <Kpi
                label="换手 / 量比"
                value={
                  dash.data ? (
                    <>
                      <span className="text-cyan">{dash.data.turnover.avg ?? '—'}%</span>
                      <span className="text-fg3"> / </span>
                      <span className="text-cyan">{dash.data.turnover.vol_ratio ?? '—'}</span>
                    </>
                  ) : (
                    '—'
                  )
                }
                sub={
                  dash.data
                    ? `高换手 ${dash.data.turnover.high_count ?? '—'} · 放量占比 ${dash.data.turnover.expand_ratio ?? '—'}%`
                    : ''
                }
              />
            </div>

          {/* 三栏：涨跌分布 | 情绪雷达 | 趋势强度 */}
          <Card key="dist" title="涨跌分布" subtitle={dash.data ? `${dash.data.total} 只 · ${dash.data.date}` : ''}>
            {dash.data ? (
              <div>
                <DistBar data={dash.data.breadth.distribution} />
                <div className="mt-2 flex items-center justify-between border-t border-line pt-1.5 text-xs2">
                  <span className="text-down">跌 {dash.data.breadth.down}</span>
                  <span className="text-fg3">平 {dash.data.breadth.flat}</span>
                  <span className="text-up">涨 {dash.data.breadth.up}</span>
                </div>
                <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                  <StatCell
                    label="平均涨跌"
                    value={`${dash.data.breadth.avg_pct > 0 ? '+' : ''}${dash.data.breadth.avg_pct}%`}
                    tone={dash.data.breadth.avg_pct >= 0 ? 'text-up' : 'text-down'}
                  />
                  <StatCell
                    label="中位涨跌"
                    value={`${dash.data.breadth.median_pct > 0 ? '+' : ''}${dash.data.breadth.median_pct}%`}
                    tone={dash.data.breadth.median_pct >= 0 ? 'text-up' : 'text-down'}
                  />
                </div>
              </div>
            ) : (
              <Empty />
            )}
          </Card>

          <Card key="radar" title="情绪评分" subtitle="六维雷达">
            {dash.data ? (
              <div className="flex justify-center py-1">
                <EmotionRadar dims={dash.data.emotion.dims} score={dash.data.emotion.score} size={186} />
              </div>
            ) : (
              <Empty />
            )}
          </Card>

          <Card key="trend" title="趋势强度" subtitle="站上均线 · 60 日新高新低">
            {dash.data ? (
              <div className="space-y-2 py-1">
                <div className="grid grid-cols-3 gap-1.5">
                  <StatCell label="站上MA5" value={`${dash.data.trend.above_ma5 ?? '—'}%`} />
                  <StatCell label="站上MA20" value={`${dash.data.trend.above_ma20 ?? '—'}%`} />
                  <StatCell label="站上MA60" value={`${dash.data.trend.above_ma60 ?? '—'}%`} />
                  <StatCell label="60日新高" value={String(dash.data.trend.new_high_60 ?? '—')} tone="text-up" />
                  <StatCell label="60日新低" value={String(dash.data.trend.new_low_60 ?? '—')} tone="text-down" />
                  <StatCell label="高低比" value={`${dash.data.trend.hl_ratio ?? '—'}%`} />
                </div>
                {/* 实用监控（Tick 同名模块，在趋势强度卡片下半部分） */}
                <div className="border-t border-line pt-2">
                  <div className="mb-1 text-2xs text-fg3">实用监控</div>
                  <div className="grid grid-cols-3 gap-1.5">
                    <StatCell label="炸板" value={String(dash.data.monitor?.broken_board ?? '—')} tone="text-gold" />
                    <StatCell label="跌停" value={String(dash.data.monitor?.limit_down ?? '—')} tone="text-down" />
                    <StatCell label="站上MA60" value={`${dash.data.monitor?.above_ma60 ?? '—'}%`} />
                    <StatCell
                      label="新高/新低"
                      value={`${dash.data.monitor?.new_high_60 ?? '—'}/${dash.data.monitor?.new_low_60 ?? '—'}`}
                    />
                    <StatCell label="高换手" value={String(dash.data.monitor?.high_turnover ?? '—')} tone="text-cyan" />
                    <StatCell label="放量占比" value={`${dash.data.monitor?.expand_ratio ?? '—'}%`} tone="text-gold" />
                  </div>
                </div>
              </div>
            ) : (
              <Empty />
            )}
          </Card>

          {/* 涨停梯队：独立卡片（TSP 里是 x:9 w:3 的独立 widget，不塞进趋势里） */}
          <Card
            key="ladder"
            title="涨停梯队"
            subtitle={dash.data ? `共 ${dash.data.ladder.total} 只连板` : ''}
          >
            {dash.data ? (
              <div className="space-y-1.5 py-1">
                <div className="flex items-baseline justify-between border-b border-line pb-1">
                  <span className="text-2xs text-fg3">封板率</span>
                  <span className="num text-xs2 text-fg2">
                    {dash.data.breadth.limit_up + dash.data.breadth.limit_down > 0
                      ? Math.round((dash.data.breadth.limit_up / (dash.data.breadth.limit_up + dash.data.breadth.limit_down)) * 100)
                      : 0}
                    %
                  </span>
                </div>
                {dash.data.ladder.items.map((l) => {
                  const maxC = Math.max(...dash.data!.ladder.items.map((x) => x.count), 1)
                  return (
                    <div key={l.label} className="space-y-0.5">
                      <div className="flex items-center gap-1.5">
                        <span className="w-8 shrink-0 text-xs2 text-fg2">{l.label}</span>
                        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-line">
                          <div
                            className="h-full rounded-full bg-up"
                            style={{ width: `${Math.max((l.count / maxC) * 100, l.count > 0 ? 4 : 0)}%` }}
                          />
                        </div>
                        <span className="num w-5 shrink-0 text-right text-xs2 text-fg2">{l.count}</span>
                      </div>
                      {(l.names ?? []).length > 0 && (
                        <div className="pl-9 text-2xs leading-tight text-fg3">{l.names!.join('  ')}</div>
                      )}
                    </div>
                  )
                })}

              </div>
            ) : (
              <Empty />
            )}
          </Card>

          {/* 行业热度（TSP: industry-rank, x:4 w:4） */}
          <Card
            key="industry"
            title="行业热度"
            subtitle={dash.data?.boards.industry.count ? `${dash.data.boards.industry.count} 个行业` : ''}
          >
            {dash.data ? (
              <BoardHot up={dash.data.boards.industry.up} down={dash.data.boards.industry.down} />
            ) : (
              <Empty />
            )}
          </Card>

          {/* 概念热度（TSP: concept-rank, x:0 w:4） */}
          <Card
            key="concept"
            title="概念热度"
            subtitle={dash.data?.boards.concept.count ? `${dash.data.boards.concept.count} 个概念` : ''}
          >
            {dash.data ? (
              <BoardHot up={dash.data.boards.concept.up} down={dash.data.boards.concept.down} />
            ) : (
              <Empty />
            )}
          </Card>

          {/* 监控中心（TSP: monitor, x:8 w:4 h:22） */}
          <Card
            key="monitor"
            title="监控中心"
            subtitle={allSig.data ? `${allSig.data.total_signals_today} 条信号 · ${allSig.data.updated}` : '实时信号'}
            right={<span className="flex items-center gap-1"><span className="text-down">●</span>在线</span>}
          >
            <MonitorFeed items={allSig.data?.items ?? []} />
          </Card>

          {/* 四榜：TSP 里各占 w:2（12 列里的 2 列） */}
          <Card key="ranks" title="市场榜单" subtitle="Top 8">
            {dash.data ? (
              <div className="grid grid-cols-8 gap-3">
                <div className="col-span-2">
                  <RankList title="涨幅榜" items={dash.data.ranks.gain} mode="pct" />
                </div>
                <div className="col-span-2">
                  <RankList title="跌幅榜" items={dash.data.ranks.loss} mode="pct" />
                </div>
                <div className="col-span-2">
                  <RankList title="成交额榜" items={dash.data.ranks.amount} mode="amount" />
                </div>
                <div className="col-span-2">
                  <RankList title="换手榜" items={dash.data.ranks.turnover} mode="turnover" />
                </div>
              </div>
            ) : (
              <Empty />
            )}
          </Card>

          {/* 今日信号 · 全部策略 */}
          <Card
            key="signals"
            title="今日信号 · 全部策略"
            subtitle={
              allSig.data
                ? `${allSig.data.strategy_count} 个策略 · 合计 ${allSig.data.total_signals_today} 个信号 · 更新 ${allSig.data.updated}`
                : allSig.isLoading
                  ? '首次加载约 5 分钟（之后走缓存）'
                  : ''
            }
            right={
              <button
                onClick={async () => {
                  // 光 refetch() 没用 —— 后端读到的是同一个磁盘缓存，看起来「没反应」。
                  // 必须带 refresh=true 触发重算，然后轮询 state 直到算完再拉数据。
                  setRebuilding(true)
                  try {
                    await fetch('/api/v1/screener/all?refresh=true&limit_per_strategy=6&scope=ranked')
                    for (let i = 0; i < 80; i++) {
                      await new Promise((r) => setTimeout(r, 3000))
                      try {
                        const st = await fetch('/api/v1/screener/all/state').then((r) => r.json())
                        if (!st?.building) break
                      } catch {
                        /* 网络抖动继续等 */
                      }
                    }
                    await allSig.refetch()
                  } finally {
                    setRebuilding(false)
                  }
                }}
                disabled={allSig.isFetching || rebuilding}
                className="rounded border border-line2 bg-card2 px-2 py-0.5 text-xs2 text-fg2 hover:text-fg disabled:opacity-50"
                title="重新扫描全部策略（后台跑，约 1~2 分钟）"
              >
                {rebuilding || allSig.isFetching ? '扫描中…' : '刷新'}
              </button>
            }
            padded={false}
          >
            {allSig.isError ? (
              <div className="px-3 py-6 text-center text-sm2 text-up">
                批量选股失败（{(allSig.error as Error).message}）
              </div>
            ) : allSig.isLoading ? (
              <div className="px-3 py-10 text-center">
                <div className="mx-auto mb-3 h-1 w-48 overflow-hidden rounded bg-card2">
                  <div className="h-full w-1/3 animate-pulse bg-brand" />
                </div>
                <div className="text-sm2 text-fg2">正在扫描 12 个策略 × 全市场 1,095 万行</div>
                <div className="mt-1 text-xs2 text-fg3">完成后缓存，切页面不用重跑</div>
              </div>
            ) : sigItems.length === 0 ? (
              <div className="px-3 py-8 text-center text-sm2 text-fg3">
                {allSig.data?.strategy_count ?? 0} 个策略今天都没有信号
              </div>
            ) : (
              <div className="max-h-[420px] overflow-y-auto">
                {sigItems.map((s) => (
                  <div
                    key={s.id}
                    id={`sig-${s.id}`}
                    className={`border-b border-line/50 transition last:border-0 ${
                      highlight === s.id ? 'bg-brand/15 ring-1 ring-inset ring-brand/40' : ''
                    }`}
                  >
                    <div className="flex items-center gap-2 bg-card2/40 px-3 py-1.5">
                      <span className="w-8 font-mono text-xs2 text-fg3">
                        {s.rank ? `#${s.rank}` : '—'}
                      </span>
                      <span className="text-xs font-medium">{s.name}</span>
                      <span className="font-mono text-3xs text-fg3">{s.id}</span>
                      <span className="ml-auto flex gap-3 font-mono text-xs2">
                        <span
                          className={
                            (s.return ?? 0) > 0 ? 'text-up' : (s.return ?? 0) < 0 ? 'text-down' : 'text-fg3'
                          }
                        >
                          {s.return != null ? `${(s.return * 100).toFixed(1)}%` : '—'}
                        </span>
                        <span className="text-fg3">
                          胜率 {s.win_rate != null ? `${(s.win_rate * 100).toFixed(0)}%` : '—'}
                        </span>
                        <span className="text-gold">{s.rows.length} 只</span>
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-x-3 gap-y-1 px-3 py-1.5">
                      {s.rows.map((r) => (
                        <span key={r.symbol} className="inline-flex items-baseline gap-1 text-xs2">
                          <Link
                            to={`/analysis?symbol=${r.symbol}&strategy=${s.id}`}
                            className="text-sm2 text-fg hover:text-cyan hover:underline"
                            title={r.symbol}
                          >
                            {r.name || r.symbol}
                          </Link>
                          <span className="font-mono text-3xs text-fg3">{r.symbol}</span>
                          <span className="font-mono text-fg3">
                            {r.close != null ? Number(r.close).toFixed(2) : ''}
                          </span>
                          {r.score != null && (
                            <span className="font-mono text-gold">{Number(r.score).toFixed(1)}</span>
                          )}
                        </span>
                      ))}
                      <button
                        onClick={() => addAll(s.rows)}
                        className="ml-auto rounded border border-line2 px-1.5 py-0.5 text-3xs text-fg3 hover:border-brand hover:text-brand"
                        title="把这一组全部加进自选"
                      >
                        全加自选
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {/* 策略排名（可滚动） */}
          <Card
            key="strategies"
            title="策略排名"
            subtitle={
              ranking.data ? `${ranking.data.ranked} / ${ranking.data.count} 个有回测排名` : '加载中…'
            }
            padded={false}
          >
            <div className="max-h-[420px] overflow-y-auto">
              <table className="w-full border-collapse text-sm2">
                <thead className="sticky top-0 bg-card">
                  <tr>
                    {['#', '策略', '收益', '夏普', '胜率'].map((h, i) => (
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
                  {(ranking.data?.items ?? []).map((x) => {
                    // 左边今日信号里有没有这个策略 —— 没有就置灰，点了也不滚动
                    const hasSignals = sigItems.some((s) => s.id === x.id)
                    return (
                      <tr
                        key={x.id}
                        onClick={() => {
                          if (!hasSignals) return
                          setHighlight(x.id)
                          const el = document.getElementById(`sig-${x.id}`)
                          el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                          // 3 秒后取消高亮
                          window.setTimeout(() => setHighlight(null), 3000)
                        }}
                        className={`transition ${
                          hasSignals ? 'cursor-pointer hover:bg-brand/10' : 'opacity-45'
                        } ${highlight === x.id ? 'bg-brand/20' : ''}`}
                        title={hasSignals ? '跳到左侧今日信号' : '今日无信号'}
                      >
                        <td className="w-8 border-b border-line/50 px-2 py-1 text-left font-mono text-fg3">
                          {x.rank ?? '—'}
                        </td>
                        <td className="border-b border-line/50 px-2 py-1 text-left">{x.name}</td>
                        <td
                          className={`num border-b border-line/50 px-2 py-1 text-right ${
                            (x.return ?? 0) > 0 ? 'text-up' : (x.return ?? 0) < 0 ? 'text-down' : 'text-fg3'
                          }`}
                        >
                          {x.return != null ? `${(x.return * 100).toFixed(1)}%` : '—'}
                        </td>
                        <td className="num border-b border-line/50 px-2 py-1 text-right text-fg2">
                          {x.sharpe != null ? x.sharpe.toFixed(2) : '—'}
                        </td>
                        <td className="num border-b border-line/50 px-2 py-1 text-right text-fg3">
                          {x.win_rate != null ? `${(x.win_rate * 100).toFixed(0)}%` : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div className="border-t border-line px-3 py-1.5 text-3xs leading-relaxed text-fg3">
              排名来自最近一次账户回测（10 万本金 / 固定止损 3000 / 3 年）。未回测的策略显示 —。
            </div>
          </Card>

          {/* 连板晋级率 */}
          <Card key="limitup" title="连板晋级率" subtitle={limitUp.data?.sample} padded={false}>
            <DataTable<LimitUpBucket>
              rows={limitUp.data?.buckets ?? []}
              rowKey={(r) => r.label}
              empty={limitUp.isError ? '连板统计不可用' : '加载中…'}
              columns={[
                { key: 'label', label: '档位', render: (r) => r.label },
                { key: 'n', label: '样本', render: (r) => <span className="text-fg3">{r.n.toLocaleString()}</span> },
                { key: 'promote', label: '晋级率', render: (r) => <span className="text-gold">{r.promote}%</span> },
                {
                  key: 'oc',
                  label: '开盘买收盘卖',
                  render: (r) => <span className={moneyClass(r.oc_return)}>{fmtPct(r.oc_return)}</span>,
                },
                { key: 'win', label: '胜率', render: (r) => <span className="text-fg3">{r.win_rate}%</span> },
              ]}
            />
            <div className="border-t border-line px-3.5 py-2 text-xs2 leading-relaxed text-fg3">
              晋级率越高 ≠ 越赚钱：2 板以上「开盘买收盘卖」全部为负，溢价在开盘一次性兑现。
            </div>
          </Card>

          {/* 任务队列 */}
          <Card
            key="tasks"
            title="任务队列"
            subtitle="异步执行 · 不阻塞页面"
            right={`${tasks.data?.items.filter((t) => t.status === 'running').length ?? 0} 运行中`}
            padded={false}
          >
            <DataTable
              rows={tasks.data?.items ?? []}
              rowKey={(r) => r.id}
              empty={tasks.isError ? '任务接口不可用' : '暂无任务'}
              columns={[
                { key: 'id', label: '任务', render: (r) => <span className="text-fg2">{r.id.slice(0, 8)}</span> },
                { key: 'type', label: '类型', render: (r) => <span className="text-fg3">{r.type}</span> },
                {
                  key: 'progress',
                  label: '进度',
                  render: (r) => (
                    <div className="h-[5px] w-24 overflow-hidden rounded bg-card2">
                      <div
                        className={`h-full rounded ${r.status === 'failed' ? 'bg-up' : r.status === 'done' ? 'bg-down' : 'bg-brand'}`}
                        style={{ width: `${Math.round(r.progress * 100)}%` }}
                      />
                    </div>
                  ),
                },
                {
                  key: 'status',
                  label: '状态',
                  render: (r) => (
                    <span
                      className={
                        r.status === 'done' ? 'text-down' : r.status === 'failed' ? 'text-up' : 'text-gold'
                      }
                    >
                      {r.status === 'done' ? '完成' : r.status === 'failed' ? '失败' : r.status === 'running' ? '运行中' : '排队'}
                    </span>
                  ),
                },
                { key: 'created', label: '创建', render: (r) => <span className="text-fg3">{r.created_at.slice(11, 19)}</span> },
              ]}
            />
          </Card>

          {/* 实盘规则 */}
          <Card key="rules" title="当前实盘规则" subtitle="唯一通过四关验证的策略">
            <div className="text-xs leading-[1.95] text-fg2">
              <b className="text-fg">触发</b>　3 日累计跌幅 &lt; −15% + 当日收阳 + 量比 &gt; 1.2　
              <b className="text-fg">买入</b>　次日开盘（超买入区上沿放弃）　
              <b className="text-fg">止损</b>　买入价 × 0.88　
              <b className="text-fg">离场</b>　持有 5 个交易日或触发止损　
              <b className="text-fg">仓位</b>　单笔止损固定 {fmtInt(risk)} 元 →
              股数 = {risk} ÷ (买入价 − 止损价)，向下取整 100 股
              <br />
              <span className="text-xs2 text-fg3">
                依据：18 组参数样本外 78% 为正，持仓 5 天档位 6/6 全正，最长水下 511 天，11 年 +196.5%（年化 9.6%）。
              </span>
            </div>
          </Card>

          {/* 引擎信息 */}
          <Card key="health" title="引擎与数据" subtitle="健康检查">
            <div className="grid grid-cols-6 gap-2">
              <Metric label="数据行数" value={health.data ? fmtInt(health.data.rows) : '—'} />
              <Metric label="策略插件" value={String(health.data?.strategies ?? '—')} />
              <Metric label="覆盖标的" value={ov ? fmtInt(ov.symbols) : '—'} />
              <Metric label="数据代际" value={<span className="text-xs">{health.data?.generation.slice(0, 8) ?? '—'}</span>} />
              <Metric label="任务总数" value={String(tasks.data?.count ?? '—')} />
              <Metric label="接口版本" value="v1" />
            </div>
          </Card>
        </DashGrid>
      </div>

      {indexSym && <IndexKlineModal symbol={indexSym} onClose={() => setIndexSym(null)} />}
    </>
  )
}

/** 点指数弹出的 K 线窗 */
function IndexKlineModal({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const q = useQuery({
    queryKey: ['index-kline', symbol],
    queryFn: () => api.indexKline(symbol, 400),
    staleTime: 60_000,
  })
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-6"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[1100px] rounded-lg border border-line2 bg-bg shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-4 py-2">
          <div className="flex items-baseline gap-2">
            <span className="text-xs font-semibold text-fg">{q.data?.name ?? symbol}</span>
            <span className="font-mono text-xs2 text-fg3">{symbol}</span>
            {q.data && (
              <span className="num text-xs text-fg2">
                收 {q.data.rows[q.data.rows.length - 1]?.close.toFixed(2)}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="rounded border border-line bg-card px-2.5 py-0.5 text-sm2 text-fg2 hover:text-fg"
          >
            关闭
          </button>
        </div>
        <div className="px-3 py-2">
          {q.isLoading && <div className="px-3 py-16 text-center text-sm2 text-fg3">加载中…</div>}
          {q.isError && (
            <div className="px-3 py-16 text-center text-sm2 text-up">
              {(q.error as Error)?.message}
            </div>
          )}
          {q.data && <KLineChart bars={q.data.rows} height={430} />}
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, sub, tone = '' }: { label: string; value: string; sub: string; tone?: string }) {
  return (
    <div className="min-w-[112px] rounded-[7px] border border-line bg-card px-3 py-1.5">
      <div className="text-3xs text-fg3">{label}</div>
      <div className={`num text-xs font-semibold ${tone}`}>{value}</div>
      <div className="font-mono text-3xs text-fg3">{sub}</div>
    </div>
  )
}

function Big({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs2 text-fg3">{label}</div>
      <div className="num mt-0.5 text-lg font-semibold">{value}</div>
    </div>
  )
}

/** 指标卡：一个数字 + 一行说明 */
function Kpi({ label, value, sub, tone = '' }: { label: string; value: ReactNode; sub?: string; tone?: string }) {
  // Tick 里每个指标是独立卡片：自带边框、圆角、内边距
  // 字号对齐 Tick：label 12px / value 20px / sub 11px（sub 之前 8px 太小看不清）
  return (
    <div className="rounded-lg border border-line bg-card px-2.5 py-1.5">
      <div className="truncate text-[12px] text-fg3">{label}</div>
      <div className={`num mt-0.5 whitespace-nowrap text-[15px] font-semibold leading-tight ${tone || 'text-fg'}`}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-[11px] text-fg3">{sub}</div>}
    </div>
  )
}

function Empty() {
  return <div className="py-8 text-center text-xs2 text-fg3">加载中…</div>
}

/** 板块热度：领涨/领跌左右两列对照（TSP 的形式），每行带序号 */
function BoardHot({
  up,
  down,
  upLabel = '领涨',
  downLabel = '领跌',
}: {
  up: { board: string; change_pct: number; members: number }[]
  down: { board: string; change_pct: number; members: number }[]
  upLabel?: string
  downLabel?: string
}) {
  const col = (
    items: { board: string; change_pct: number; members: number }[],
    title: string,
    tone: string,
  ) => (
    <div className="min-w-0">
      <div className={`mb-1 text-2xs font-medium ${tone}`}>{title}</div>
      <div className="space-y-0.5">
        {items.length === 0 && <div className="py-1 text-2xs text-fg3">—</div>}
        {items.map((b, i) => (
          <div key={b.board} className="flex items-baseline gap-1 rounded px-0.5 py-0.5 hover:bg-white/[0.03]">
            <span className="w-3 shrink-0 text-2xs text-fg3">{i + 1}</span>
            <span className="min-w-0 flex-1 truncate text-sm2 text-fg">{b.board}</span>
            <span className="num shrink-0 text-2xs text-fg3">{b.members ? `${b.members}只` : ''}</span>
            <span className={`num w-11 shrink-0 text-right text-xs2 ${b.change_pct >= 0 ? 'text-up' : 'text-down'}`}>
              {b.change_pct >= 0 ? '+' : ''}
              {b.change_pct.toFixed(2)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  )
  return (
    <div className="grid grid-cols-2 gap-3">
      {col(up, upLabel, 'text-up')}
      {col(down, downLabel, 'text-down')}
    </div>
  )
}

/** 监控中心：把各策略今日信号摊平成时间流（TSP 那块的形式） */
function MonitorFeed({
  items,
}: {
  items: {
    id: string
    name: string
    rows: { symbol: string; name?: string; close?: number; score?: number; note?: string; date?: string }[]
  }[]
}) {
  const flat = items.flatMap((s) =>
    (s.rows ?? []).map((r) => ({
      strategy: s.name,
      sid: s.id,
      symbol: r.symbol,
      name: r.name,
      close: r.close,
      score: r.score,
      note: r.note,
      date: r.date,
    })),
  )
  if (flat.length === 0) {
    return <div className="py-6 text-center text-xs2 text-fg3">今日暂无信号</div>
  }
  return (
    <div className="max-h-[420px] space-y-1 overflow-y-auto">
      {flat.map((x, i) => (
        <div key={`${x.symbol}-${i}`} className="flex items-baseline gap-1.5 border-b border-line/30 pb-1">
          <span className="min-w-0 flex-1 truncate text-sm2 text-fg">
            <span className="font-mono text-2xs text-fg3">{x.symbol.split('.')[0]} </span>
            {x.name || ''}
          </span>
          <span className="num shrink-0 text-xs2 text-gold">{x.close != null ? Number(x.close).toFixed(2) : '—'}</span>
          <span className="shrink-0 truncate text-2xs text-cyan" title={x.strategy}>
            {x.strategy.slice(0, 6)}
          </span>
        </div>
      ))}
    </div>
  )
}
