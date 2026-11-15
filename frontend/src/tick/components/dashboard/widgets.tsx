/**
 * 看板展示组件 — 从 pages/Dashboard.tsx 原样搬移, 行为与视觉不变。
 *
 * 组件均为"props 进, 展示出"的纯展示件; 数据切片与交互回调由 Dashboard 页
 * 提供(overview 主查询仍集中在页面层, 避免 widget 各自重复请求)。
 */
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ArrowDownRight, ArrowUpRight, Flame } from 'lucide-react'
import { api, type MarketSnapshotRow, type OverviewDimensionRankItem, type OverviewMarket, type AlertEvent } from '@/lib/api'
import { fmtBigNum, fmtPct } from '@/lib/format'
import { dimensionKindForSourceField, type DimensionMembersTarget } from '@/components/DimensionMembersDialog'
import { cn } from '@/lib/cn'
import { cnSignal } from '@/lib/signals'
import { useCustomSignalNames } from '@/lib/useCustomSignalNames'
import { strategyEventMeta, strategyName } from '@/lib/strategyMonitorEvents'
import { boardTag } from '@/components/stock-table/primitives'
import { toNavItems, type NavItem } from '@/lib/listNav'
import { fmtPrice, fmtIndexPct, fmtStockPct, pctClass, scoreColor as scoreColorValue, SectionTitle } from './shared'

// 切股导航列表构建 (与列表展示行一致: StockList 只显示前 8)
export function stockListNav(rows: MarketSnapshotRow[]): NavItem[] {
  return toNavItems(rows.slice(0, 8))
}
export function rankNav(rank?: OverviewMarket['concept_rank']): NavItem[] {
  const leaders = [...(rank?.leading ?? []), ...(rank?.lagging ?? [])]
    .map(r => r.leader)
    .filter((l): l is NonNullable<typeof l> & { symbol: string } => !!l?.symbol)
  return toNavItems(leaders)
}

// ===== 指数行情条 =====
export function IndexTicker({ item, onClick }: { item: OverviewMarket['indices'][number]; onClick?: () => void }) {
  const pct = item.change_pct
  const isUp = (pct ?? 0) >= 0
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${item.name || item.symbol} · 看指数K线`}
      className="grid w-full min-w-0 grid-cols-[1fr_auto] items-center gap-x-2 gap-y-0.5 rounded-lg border border-border bg-elevated/45 px-1.5 py-1 text-left shadow-[0_1px_1px_hsl(var(--border)/0.3)] backdrop-blur-sm transition-all hover:border-accent/40 hover:bg-elevated hover:shadow-[0_2px_6px_hsl(var(--accent)/0.15)]"
    >
      <div className="truncate text-xs font-medium text-foreground">{item.name || item.symbol}</div>
      <div className={`font-mono text-xs font-semibold ${pctClass(pct)}`}>{fmtIndexPct(pct)}</div>
      <div className="font-mono text-[10px] text-muted">{item.symbol}</div>
      <div className={`flex items-center gap-1 font-mono text-[11px] ${pctClass(pct)}`}>
        {isUp ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
        {fmtPrice(item.last_price)}
      </div>
    </button>
  )
}

// ===== 涨跌广度 =====
export function BreadthBar({ data }: { data: OverviewMarket['breadth'] }) {
  const denom = Math.max(data.total, 1)
  const upW = data.up / denom * 100
  const downW = data.down / denom * 100
  const flatW = Math.max(0, 100 - upW - downW)
  return (
    <div className="space-y-2">
      {/* 与上方涨跌分布同向: 左跌(绿) 右涨(红) */}
      <div className="flex h-2.5 overflow-hidden rounded-full bg-elevated">
        <div className="bg-bear/85" style={{ width: `${downW}%` }} />
        <div className="bg-muted/45" style={{ width: `${flatW}%` }} />
        <div className="bg-bull/85" style={{ width: `${upW}%` }} />
      </div>
      <div className="grid grid-cols-3 gap-1.5 text-[11px]">
        <div className="rounded bg-bear/8 px-2 py-1 text-bear">跌 <span className="font-mono">{data.down}</span></div>
        <div className="rounded bg-elevated/70 px-2 py-1 text-muted">平 <span className="font-mono">{data.flat}</span></div>
        <div className="rounded bg-bull/8 px-2 py-1 text-bull">涨 <span className="font-mono">{data.up}</span></div>
      </div>
    </div>
  )
}

// ===== 涨跌分布直方 =====
export function DistributionBars({ rows }: { rows: OverviewMarket['distribution'] }) {
  const maxCount = Math.max(...rows.map(r => r.count), 1)
  return (
    <div className="grid h-24 grid-cols-8 items-end gap-1 pt-1">
      {rows.map((r, i) => {
        const positive = i >= 4
        return (
          <div key={r.label} className="flex h-full min-w-0 flex-col items-center justify-end gap-0.5">
            <div className="font-mono text-[9px] text-muted">{r.count || ''}</div>
            <div
              className={`w-2 rounded-full ${positive ? 'bg-gradient-to-t from-bull/45 to-bull/90' : 'bg-gradient-to-t from-bear/45 to-bear/90'}`}
              style={{ height: `${Math.max(4, r.count / maxCount * 86)}%` }}
              title={`${r.label}: ${r.count}只`}
            />
            <div className="truncate text-[9px] text-muted">{r.label}</div>
          </div>
        )
      })}
    </div>
  )
}

// ===== 情绪雷达 (手写 SVG) =====
export function EmotionRadar({ radar, score }: { radar: OverviewMarket['radar']; score: number }) {
  const size = 240
  const cx = size / 2
  const cy = size / 2
  const maxR = 78
  const color = scoreColorValue(score)
  if (!radar.length) return <div className="flex h-52 items-center justify-center text-xs text-muted">暂无雷达数据</div>
  const points = radar.map((r, i) => {
    const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
    const radius = maxR * Math.max(0, Math.min(100, r.value)) / 100
    return {
      ...r,
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
      lx: cx + Math.cos(angle) * (maxR + 27),
      ly: cy + Math.sin(angle) * (maxR + 27),
      gx: cx + Math.cos(angle) * maxR,
      gy: cy + Math.sin(angle) * maxR,
    }
  })
  const polygon = points.map(p => `${p.x},${p.y}`).join(' ')
  const gridPolygons = [1, 0.66, 0.33].map((level, idx) => ({
    level,
    idx,
    points: radar.map((_, i) => {
      const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
      return `${cx + Math.cos(angle) * maxR * level},${cy + Math.sin(angle) * maxR * level}`
    }).join(' '),
  }))
  return (
    <div className="flex justify-center">
      <svg viewBox={`0 0 ${size} ${size}`} className="h-56 w-full">
        <defs>
          <radialGradient id="emotionRadarFill" cx="50%" cy="45%" r="70%">
            <stop offset="0%" stopColor={`${color}57`} />
            <stop offset="100%" stopColor={`${color}1f`} />
          </radialGradient>
          {/* 中心/网格用 CSS 变量取色, 亮暗主题自动切换 (SVG 属性支持 hsl(var(--x))) */}
          <radialGradient id="emotionRadarCenter" cx="50%" cy="50%" r="55%">
            <stop offset="0%" stopColor="hsl(var(--surface) / 0.92)" />
            <stop offset="68%" stopColor="hsl(var(--surface) / 0.70)" />
            <stop offset="100%" stopColor="hsl(var(--surface) / 0)" />
          </radialGradient>
        </defs>
        {gridPolygons.map(g => (
          <polygon
            key={g.level}
            points={g.points}
            fill={g.idx % 2 === 0 ? 'hsl(var(--elevated) / 0.55)' : 'hsl(var(--elevated) / 0.3)'}
            stroke={g.level === 1 ? 'hsl(var(--border) / 0.9)' : 'hsl(var(--border) / 0.5)'}
            strokeWidth={g.level === 1 ? 1.2 : 0.8}
          />
        ))}
        {points.map(p => <line key={p.key} x1={cx} y1={cy} x2={p.gx} y2={p.gy} stroke="hsl(var(--border) / 0.4)" />)}
        <polygon points={polygon} fill="url(#emotionRadarFill)" stroke={color} strokeWidth="2" />
        {points.map(p => <circle key={p.key} cx={p.x} cy={p.y} r="2.8" fill={color} stroke="hsl(var(--surface) / 0.9)" strokeWidth="1" />)}
        <circle cx={cx} cy={cy} r="29" fill="url(#emotionRadarCenter)" />
        <text x={cx} y={cy + 7} textAnchor="middle" className="fill-foreground font-mono text-[24px] font-bold">{score}</text>
        {points.map(p => (
          <text key={`${p.key}-label`} x={p.lx} y={p.ly + 4} textAnchor="middle" className="fill-secondary text-[10px] font-medium">{p.label}</text>
        ))}
      </svg>
    </div>
  )
}

// ===== 涨停梯队 =====
export function LadderMini({ limit }: { limit: OverviewMarket['limit'] }) {
  const navigate = useNavigate()
  const tiers = limit.tiers.filter(t => t.boards >= 2).slice(0, 6)
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between rounded bg-elevated/55 px-2 py-1.5 text-[11px]">
        <span className="text-muted">封板率</span>
        <span className="font-mono text-accent">{(limit.seal_rate ?? 0).toFixed(0)}%</span>
      </div>
      {tiers.length === 0 && <div className="rounded border border-dashed border-border py-5 text-center text-xs text-muted">暂无 2 板以上</div>}
      {tiers.map(t => {
        const stocks = t.stocks ?? []
        // 超过 3 只也要显示名字（原来 <=3 才显示，2 板 4 只时一片空白，看着像没数据）
        const showStocks = stocks.length > 0
        return (
          <div key={t.boards} className="rounded bg-elevated/35 px-2 py-1.5">
            <div className="grid grid-cols-[42px_1fr_auto] items-center gap-2">
              <span className={`font-mono text-sm font-bold ${t.boards >= 5 ? 'text-bull' : t.boards >= 3 ? 'text-accent' : 'text-secondary'}`}>{t.boards}板</span>
              <div className="h-1.5 overflow-hidden rounded-full bg-base">
                <div className="h-full rounded-full bg-bull/70" style={{ width: `${Math.min(100, t.count * 12)}%` }} />
              </div>
              <span className="font-mono text-xs text-foreground">{t.count}</span>
            </div>
            {showStocks && (
              <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 pl-[50px]">
                {stocks.map(s => (
                  <button
                    key={s.symbol}
                    type="button"
                    onClick={() => navigate(`/analysis?symbol=${s.symbol}`)}
                    title={`${s.name || s.symbol} · 看个股详情`}
                    className="inline-flex items-center gap-0.5 text-[11px] text-secondary underline decoration-border/60 underline-offset-2 transition-colors hover:text-accent hover:decoration-accent"
                  >
                    {s.name || s.symbol}
                  </button>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ===== 四大榜单 =====
export function StockList({ title, rows, mode, onStockClick, activeSymbol }: {
  title: string; rows: MarketSnapshotRow[]; mode: 'gain' | 'loss' | 'amount' | 'active';
  onStockClick?: (symbol: string, name?: string) => void;
  activeSymbol?: string;
}) {
  return (
    <div className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-xs font-semibold text-foreground">{title}</h3>
        <span className="text-[9px] text-muted">TOP {Math.min(rows.length, 8)}</span>
      </div>
      <div className="space-y-1">
        {rows.slice(0, 8).map((r, idx) => (
          <div
            key={`${r.symbol}-${idx}`}
            className={`grid grid-cols-[18px_1fr_auto] items-center gap-1.5 rounded-md px-1.5 py-1 cursor-pointer transition-colors border ${r.symbol === activeSymbol ? 'bg-accent/10 border-accent/30' : 'bg-elevated/40 border-transparent hover:bg-elevated hover:brightness-110 hover:border-border/60'}`}
            onClick={() => onStockClick?.(r.symbol, r.name ?? undefined)}
          >
            <span className="text-center font-mono text-[10px] text-muted">{idx + 1}</span>
            <div className="min-w-0">
              <div className="flex items-center gap-1">
                <span className="truncate text-[11px] text-foreground" title={r.name || r.symbol}>{r.name || r.symbol}</span>
                {(() => {
                  const board = boardTag(r.symbol)
                  return board ? (
                    <span className={`shrink-0 inline-flex items-center justify-center h-3 px-1 rounded text-[8px] font-bold leading-none border ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
              </div>
              <span className="font-mono text-[9px] text-muted">{r.symbol}</span>
            </div>
            <div className="text-right">
              {mode === 'amount' ? (
                <>
                  <div className="font-mono text-[11px] text-foreground">{fmtBigNum(r.amount)}</div>
                  <div className={`font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : mode === 'active' ? (
                <>
                  <div className="font-mono text-[11px] text-accent">{fmtPrice(r.turnover_rate, 1)}%</div>
                  <div className={`font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : (
                <>
                  <div className={`font-mono text-[11px] font-semibold ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                  <div className="font-mono text-[9px] text-muted">{fmtPrice(r.close)}</div>
                </>
              )}
            </div>
          </div>
        ))}
        {rows.length === 0 && <div className="py-5 text-center text-xs text-muted">暂无数据</div>}
      </div>
    </div>
  )
}

// ===== 概念/行业热度列 =====
export function RankColumn({ title, rows, tone, onStockClick, onDimensionClick, activeSymbol }: {
  title: string; rows: OverviewDimensionRankItem[]; tone: 'bull' | 'bear';
  onStockClick?: (symbol: string, name?: string) => void;
  onDimensionClick?: (target: DimensionMembersTarget) => void;
  activeSymbol?: string;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <div className={`text-[10px] font-medium ${tone === 'bull' ? 'text-bull' : 'text-bear'}`}>{title}</div>
      {rows.slice(0, 5).map((r, idx) => {
        const kind = r.source_field ? dimensionKindForSourceField(r.source_field) : null
        const clickable = !!(r.source_field && kind && onDimensionClick)
        const isActive = r.leader?.symbol != null && r.leader.symbol === activeSymbol
        return (
        <div
          key={`${title}-${r.name}-${idx}`}
          onClick={() => clickable && onDimensionClick!({
            kind: kind!,
            value: r.name,
            sourceField: r.source_field!,
          })}
          title={clickable ? `查看「${r.name}」成分股` : undefined}
          className={`grid grid-cols-[14px_1fr_auto] items-center gap-1 rounded-md px-1.5 py-1 border transition-colors ${
            isActive ? 'border-accent/30 bg-accent/10' : 'border-transparent bg-elevated/40'
          } ${clickable ? 'cursor-pointer hover:border-accent/40 hover:bg-elevated/70' : 'hover:border-border/60'}`}
        >
          <span className="text-center font-mono text-[9px] text-muted">{idx + 1}</span>
          <div className="min-w-0">
            <div className="truncate text-[11px] text-foreground" title={r.name}>
              {r.name}
              {clickable && <span className="ml-1 text-[8px] text-muted/50">↗</span>}
            </div>
            <div className="mt-0.5 flex items-center gap-1">
              <span className="shrink-0 font-mono text-[9px] text-muted">{r.count}只</span>
              <span className="text-muted">·</span>
              {r.leader?.symbol ? (
                <button
                  onClick={(e) => { e.stopPropagation(); onStockClick?.(r.leader!.symbol!, r.leader!.name ?? undefined) }}
                  className="truncate text-[10px] font-medium text-secondary hover:text-accent cursor-pointer transition-colors"
                  title={r.leader?.symbol ?? undefined}
                >{r.leader?.name ?? '—'}</button>
              ) : (
                <span className="truncate text-[10px] text-muted">{r.leader?.name ?? '—'}</span>
              )}
              {r.leader?.change_pct != null && (
                <span className={`shrink-0 font-mono text-[9px] tabular-nums ${pctClass(r.leader.change_pct)}`}>
                  {fmtStockPct(r.leader.change_pct)}
                </span>
              )}
              {r.leader?.symbol && (() => {
                const board = boardTag(r.leader!.symbol!)
                return board ? (
                  <span className={`shrink-0 inline-flex items-center justify-center h-3 px-1 rounded text-[8px] font-bold leading-none border ${board.color}`}>
                    {board.label}
                  </span>
                ) : null
              })()}
            </div>
          </div>
          <div className={`font-mono text-[10px] font-semibold ${pctClass(r.avg_pct)}`}>{fmtStockPct(r.avg_pct)}</div>
        </div>
        )
      })}
      {rows.length === 0 && <div className="rounded border border-dashed border-border py-4 text-center text-xs text-muted">暂无数据</div>}
    </div>
  )
}

// ===== 概念/行业热度卡 =====
export function HotRankCard({ title, rank, configUrl, to, onStockClick, onDimensionClick, activeSymbol }: {
  title: string; rank?: OverviewMarket['concept_rank']; configUrl: string; to?: string;
  onStockClick?: (symbol: string, name?: string) => void;
  onDimensionClick?: (target: DimensionMembersTarget) => void;
  activeSymbol?: string;
}) {
  const hasData = (rank?.leading?.length ?? 0) > 0 || (rank?.lagging?.length ?? 0) > 0
  return (
    <section className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <SectionTitle icon={Flame} title={title} hint="领涨/领跌 · 点击板块看成分股" to={to} />
      {hasData ? (
        <div className="grid grid-cols-2 gap-2">
          <RankColumn title="领涨" rows={rank?.leading ?? []} tone="bull" onStockClick={onStockClick} onDimensionClick={onDimensionClick} activeSymbol={activeSymbol} />
          <RankColumn title="领跌" rows={rank?.lagging ?? []} tone="bear" onStockClick={onStockClick} onDimensionClick={onDimensionClick} activeSymbol={activeSymbol} />
        </div>
      ) : (
        <div className="py-4 text-center">
          <p className="text-[11px] text-muted">未配置扩展数据源</p>
          <Link
            to={configUrl}
            className="mt-1.5 inline-block text-[11px] text-accent hover:text-accent/80 transition-colors"
          >
            前往配置 →
          </Link>
        </div>
      )}
    </section>
  )
}

// ===== 监控中心小组件 — 前 10 条触发记录 =====
const _SOURCE_BADGE: Record<string, string> = {
  strategy: 'bg-amber-400/10 text-amber-400',
  signal: 'bg-accent/10 text-accent',
  price: 'bg-emerald-400/10 text-emerald-400',
  market: 'bg-purple-500/10 text-purple-400',
  sector: 'bg-cyan-500/10 text-cyan-700 dark:text-cyan-300',
}
const _SOURCE_LABEL: Record<string, string> = {
  strategy: '策略', signal: '信号', price: '价格', market: '异动', sector: '板块',
}
const _SEVERITY_BAR: Record<string, string> = {
  info: 'bg-accent/40', warn: 'bg-warning', critical: 'bg-danger',
}

export function MonitorWidget({ onStockClick, activeSymbol }: {
  onStockClick: (event: AlertEvent, navList?: NavItem[]) => void
  activeSymbol?: string
}) {
  const navigate = useNavigate()
  const customNames = useCustomSignalNames()
  const alerts = useQuery({
    queryKey: ['alerts', ''],
    queryFn: () => api.alertsList({ days: 7, limit: 10 }),
    refetchInterval: 10000,
  })
  const events: AlertEvent[] = alerts.data?.alerts ?? []
  // 切股导航列表: 有 symbol 的触发记录
  const alertNav = toNavItems(events.filter((ev): ev is AlertEvent & { symbol: string } => !!ev.symbol))

  if (events.length === 0) {
    return (
      <div className="mt-1 py-6 text-center text-[11px] text-muted">暂无触发记录</div>
    )
  }

  return (
    <>
      <div className="mt-1 divide-y divide-border/40">
        {events.map((ev, i) => {
          const sev = _SEVERITY_BAR[ev.severity ?? 'info'] ?? _SEVERITY_BAR.info
          const pct = ev.change_pct
          const isSector = ev.source === 'sector'
          const sname = ev.source === 'strategy' ? strategyName(ev.message ?? '') : ''
          // 相对时间：刚触发看「几分钟前」，老记录看「月-日」，避免 1 月的记录看着像刚发生
          const time = (() => {
            if (!ev.ts) return ''
            const d = new Date(ev.ts)
            const mins = Math.floor((Date.now() - d.getTime()) / 60000)
            if (mins < 1) return '刚刚'
            if (mins < 60) return `${mins} 分钟前`
            if (mins < 24 * 60) return `${Math.floor(mins / 60)} 小时前`
            return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit' })
          })()
          return (
            <button
              key={`${ev.ts}-${i}`}
              onClick={() => (isSector ? navigate('/monitor') : ev.symbol ? onStockClick(ev, alertNav) : navigate('/monitor'))}
              title={ev.symbol ? `${ev.symbol}${ev.name ? ` ${ev.name}` : ''} · 点开日K` : '在监控中心查看'}
              className={`flex w-full items-center gap-2 px-1 py-[5px] text-left transition-colors hover:bg-elevated/60 ${
                ev.symbol && ev.symbol === activeSymbol ? 'bg-accent/5' : ''
              }`}
            >
              <span className={`h-3 w-0.5 shrink-0 rounded-full ${sev}`} />
              <span className="shrink-0 font-mono text-[11px] text-foreground/80">
                {ev.symbol?.replace(/\.(SH|SZ|BJ)$/, '') ?? '—'}
              </span>
              <span className="min-w-0 shrink truncate text-[12px] text-foreground">{ev.name ?? (isSector ? '板块' : '')}</span>
              {sname && (
                <span className="shrink-0 rounded border border-amber-400/40 bg-amber-400/10 px-1 text-[10px] leading-4 text-amber-400">
                  {sname}
                </span>
              )}
              <span className="flex-1" />
              {ev.price != null && (
                <span className="shrink-0 font-mono text-[11px] text-muted">{fmtPrice(ev.price)}</span>
              )}
              {pct != null && (
                <span className={`w-[54px] shrink-0 text-right font-mono text-[12px] font-medium ${pct >= 0 ? 'text-bull' : 'text-bear'}`}>
                  {fmtPct(pct)}
                </span>
              )}
              <span className="w-[76px] shrink-0 text-right font-mono text-[10px] text-muted/60">{time}</span>
            </button>
          )
        })}
      </div>
    </>
  )
}
