import type { DashBucket, DashStock } from '../../services/api'
import { fmtPct, pctClass } from '../../lib/format'

/** 涨跌分布柱状图：8 个区间，右边四个红（涨）左边四个绿（跌）；底部一条渐变条 */
export function DistBar({ data, height = 100 }: { data: DashBucket[]; height?: number }) {
  const max = Math.max(...data.map((d) => d.count), 1)
  const downTotal = data.slice(0, 4).reduce((a, b) => a + b.count, 0)
  const upTotal = data.slice(4).reduce((a, b) => a + b.count, 0)
  const sum = downTotal + upTotal || 1
  const downPct = (downTotal / sum) * 100
  return (
    <div>
      <div className="flex items-end gap-1 px-0.5" style={{ height }}>
        {data.map((d, i) => {
          const isUp = i >= 4
          const h = Math.max((d.count / max) * 100, 1.5)
          return (
            <div key={d.label} className="flex h-full flex-1 flex-col items-center justify-end gap-0.5">
              <span className="num text-2xs text-fg3">{d.count}</span>
              <div
                className="w-full rounded-sm transition-all"
                style={{
                  height: `${h}%`,
                  background: isUp ? 'rgba(246,70,93,0.78)' : 'rgba(14,203,129,0.78)',
                }}
              />
              <span className="whitespace-nowrap text-2xs text-fg3">{d.label}</span>
            </div>
          )
        })}
      </div>
      {/* 绿(跌) → 红(涨) 渐变比例条 */}
      <div className="mt-1.5 flex h-1.5 w-full overflow-hidden rounded-full">
        <div style={{ width: `${downPct}%`, background: 'rgba(14,203,129,0.85)' }} />
        <div style={{ width: `${100 - downPct}%`, background: 'rgba(246,70,93,0.85)' }} />
      </div>
    </div>
  )
}

/** 一个小数据块（TSP 那种带边框的 stat cell） */
export function StatCell({
  label,
  value,
  tone = '',
}: {
  label: string
  value: string
  tone?: string
}) {
  return (
    <div className="rounded-[6px] bg-card2 px-2 py-1.5">
      <div className="text-2xs text-fg3">{label}</div>
      <div className={`num mt-0.5 text-sm font-semibold leading-none ${tone || 'text-fg'}`}>{value}</div>
    </div>
  )
}

/** 情绪雷达：六边形，值 0~100 映射到半径 */
export function EmotionRadar({
  dims,
  score,
  size = 190,
}: {
  dims: { key: string; label: string; value: number }[]
  score: number
  size?: number
}) {
  const n = Math.max(dims.length, 3)
  const cx = size / 2
  const cy = size / 2
  const r = size / 2 - 26

  const pt = (i: number, ratio: number) => {
    const a = (Math.PI * 2 * i) / n - Math.PI / 2
    return [cx + r * ratio * Math.cos(a), cy + r * ratio * Math.sin(a)] as const
  }
  // 背景网格（3 圈）
  const rings = [0.33, 0.66, 1].map((ratio) =>
    dims
      .map((_, i) => {
        const [x, y] = pt(i, ratio)
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' '),
  )
  // 数据多边形
  const poly = dims
    .map((d, i) => {
      const [x, y] = pt(i, Math.max(0.02, Math.min(1, d.value / 100)))
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <div className="relative flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="absolute inset-0">
        {rings.map((p, i) => (
          <polygon key={i} points={p} fill="none" stroke="rgba(148,163,184,0.18)" strokeWidth="1" />
        ))}
        {dims.map((_, i) => {
          const [x, y] = pt(i, 1)
          return <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke="rgba(148,163,184,0.14)" strokeWidth="1" />
        })}
        <polygon points={poly} fill="rgba(34,197,94,0.30)" stroke="#4ade80" strokeWidth="2" />
        {dims.map((d, i) => {
          const [x, y] = pt(i, Math.max(0.02, Math.min(1, d.value / 100)))
          return <circle key={d.key} cx={x} cy={y} r="2.5" fill="#86efac" />
        })}
        {dims.map((d, i) => {
          const [x, y] = pt(i, 1.26)
          return (
            <text
              key={`t-${d.key}`}
              x={x}
              y={y}
              textAnchor="middle"
              dominantBaseline="middle"
              fill="#5b6779"
              fontSize="9"
            >
              {d.label}
            </text>
          )
        })}
      </svg>
      <div className="relative text-center">
        <div className="num text-[15px] font-bold leading-none text-fg">{score.toFixed(0)}</div>
        <div className="mt-0.5 text-2xs text-fg3">情绪评分</div>
      </div>
    </div>
  )
}

/** 一条横向进度条（趋势强度 / 涨停梯队用） */
export function MiniBar({
  label,
  value,
  max = 100,
  suffix = '%',
  color = '#7c5cff',
}: {
  label: string
  value: number | null | undefined
  max?: number
  suffix?: string
  color?: string
}) {
  const v = value ?? 0
  const w = Math.min(100, Math.max(0, (v / max) * 100))
  return (
    <div className="flex items-center gap-1.5">
      <span className="w-14 shrink-0 text-2xs text-fg3">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-line">
        <div className="h-full rounded-full" style={{ width: `${w}%`, background: color }} />
      </div>
      <span className="num w-9 shrink-0 text-right text-2xs text-fg2">
        {value == null ? '—' : `${v.toFixed(0)}${suffix}`}
      </span>
    </div>
  )
}

/** 榜单：涨幅/跌幅/成交额/换手 */
export function RankList({
  title,
  items,
  mode,
}: {
  title: string
  items: DashStock[]
  mode: 'pct' | 'amount' | 'turnover'
}) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-xs2 font-medium text-fg2">{title}</div>
      <div className="space-y-0.5">
        {items.length === 0 && <div className="py-2 text-center text-2xs text-fg3">无数据</div>}
        {items.map((s, i) => {
          let right = ''
          let tone = 'text-fg2'
          if (mode === 'pct') {
            right = s.change_pct != null ? fmtPct(s.change_pct) : '—'
            tone = pctClass(s.change_pct)
          } else if (mode === 'amount') {
            right = s.amount_yi != null ? `${s.amount_yi.toFixed(1)}亿` : '—'
            tone = 'text-gold'
          } else {
            right = s.turnover != null ? `${s.turnover.toFixed(2)}%` : '—'
            tone = 'text-cyan'
          }
          return (
            <div key={s.symbol} className="flex items-baseline gap-1.5 py-0.5">
              <span className="num w-3 shrink-0 text-2xs text-fg3">{i + 1}</span>
              <span className="min-w-0 flex-1">
                <div className="flex items-baseline gap-1">
                  <span className="truncate text-sm2 text-fg">{s.name || s.symbol}</span>
                  {s.symbol.endsWith('.BJ') && <span className="rounded bg-gold/25 px-0.5 text-2xs text-gold">北</span>}
                  {s.symbol.startsWith('688') && <span className="rounded bg-cyan/25 px-0.5 text-2xs text-cyan">科</span>}
                  {s.symbol.startsWith('300') && <span className="rounded bg-brand/25 px-0.5 text-2xs text-brand">创</span>}
                </div>
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-mono text-2xs text-fg3">{s.symbol}</span>
                  <span className="num text-2xs text-fg3">{s.close != null ? s.close.toFixed(2) : ''}</span>
                </div>
              </span>
              <span className={`num shrink-0 text-xs2 ${tone}`}>{right}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
