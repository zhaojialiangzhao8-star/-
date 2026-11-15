import { useEffect, useRef, useState } from 'react'
import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { Bar } from '../services/api'

const toTime = (s: string) => {
  // 支持 "2026-11-14"（日线）和 "2026-11-14 09:30"（分时）两种
  const iso = s.length > 10 ? `${s.slice(0, 10)}T${s.slice(11, 16)}:00Z` : `${s.slice(0, 10)}T00:00:00Z`
  return (Date.parse(iso) / 1000) as UTCTimestamp
}

export interface TradeMarker {
  /** 成交日 YYYY-MM-DD */
  date: string
  side: 'buy' | 'sell'
  price: number
  /** 可选：在图上显示的短标签，如 "B 24.5" */
  label?: string
}

interface PriceLineSpec {
  price: number
  color: string
  title: string
  style?: number
}

interface Props {
  bars: Bar[]
  height?: number
  /** 买卖点标记 */
  trades?: TradeMarker[]
  /** 支撑 / 压力 / 目标位水平线 */
  priceLines?: PriceLineSpec[]
  /** 把视图滚到这一天附近（左侧买卖点列表点击时用） */
  focusDate?: string | null
  /** 预设的智能选股信号点（图上用小旗标出「今天/明天买」） */
  picks?: PickMarker[]
  /** 周期切换：给了才显示切换栏 */
  periods?: { key: string; label: string }[]
  period?: string
  onPeriod?: (key: string) => void
  /** 周期切换时数据在加载 */
  loading?: boolean
  /** 分时用折线，日线用蜡烛 */
  mode?: 'candle' | 'line'
}

/** 智能选股信号：图上标出这只票哪天被哪个策略选中 */
export interface PickMarker {
  date: string
  /** 策略名（短） */
  strategy?: string
  /** 'today' = 当日收盘买（次日开盘成交），'next' = 明日 */
  timing?: 'today' | 'next'
}

const WEEK = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六']

/** 2026/08/19 星期三 */
function fmtDateFull(s: string): string {
  const d = new Date(s.slice(0, 10) + 'T00:00:00Z')
  if (Number.isNaN(d.getTime())) return s
  const y = d.getUTCFullYear()
  const m = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  return `${y}/${m}/${dd} ${WEEK[d.getUTCDay()]}`
}

/**
 * 日 K + 成交量 + 均线（MA5 / MA20）+ 买卖点标记。
 *
 * 视觉对齐同花顺的习惯：
 *   · 蜡烛更宽、去边框、网格更淡（institutional 看盘那种「干净」感）
 *   · 十字光标跟随的悬浮数据窗（同花顺那个小方框）
 *   · 顶部信息栏日期带星期
 */
export default function KLineChart({ bars, height = 420, trades, priceLines, focusDate, picks, periods, period, onPeriod, loading, mode = 'candle' }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  /** 十字光标落在第几根 K 线；null = 没悬停（显示最新一根） */
  const [hoverIdx, setHoverIdx] = useState<number | null>(null)
  /** 悬浮小窗的位置（相对图表容器） */
  const [tip, setTip] = useState<{ x: number; y: number } | null>(null)

  useEffect(() => {
    if (!ref.current || bars.length < 2) return

    const chart: IChartApi = createChart(ref.current, {
      height,
      width: ref.current.clientWidth,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#6b7684',
        fontSize: 10,
        fontFamily: 'JetBrains Mono, ui-monospace, Consolas, monospace',
      },
      // 十字光标那条竖线的标签（默认是「14 8月 '26」）→ 改成 2026/08/18 星期二
      localization: {
        timeFormatter: (time: Time) => {
          const d = new Date((time as number) * 1000)
          if (Number.isNaN(d.getTime())) return ''
          const y = d.getUTCFullYear()
          const m = String(d.getUTCMonth() + 1).padStart(2, '0')
          const dd = String(d.getUTCDate()).padStart(2, '0')
          return `${y}/${m}/${dd} ${WEEK[d.getUTCDay()]}`
        },
      },
      grid: {
        // 网格淡到几乎看不见，同花顺默认是没有竖线的
        vertLines: { visible: false },
        horzLines: { color: '#171c24' },
      },
      rightPriceScale: { borderColor: '#20262f', scaleMargins: { top: 0.06, bottom: 0.26 } },
      timeScale: {
        borderColor: '#20262f',
        rightOffset: 3,
        barSpacing: 8,          // 比原来 6 宽，蜡烛更饱满
        minBarSpacing: 2,
        timeVisible: false,
        secondsVisible: false,
        // 底部时间轴：同花顺那种 2026/03。
        // 刻度是按「日期」生成的，同一个月里可能有 2 个刻度 → 只在前 7 天标月份，
        // 其余返回空串把刻度隐藏，避免出现「2026/06 2026/06」这种重复。
        // 1 月额外只标年份，作为跨年分界。
        tickMarkFormatter: (time: Time) => {
          const d = new Date((time as number) * 1000)
          if (Number.isNaN(d.getTime())) return ''
          const y = d.getUTCFullYear()
          const m = d.getUTCMonth() + 1
          const day = d.getUTCDate()
          if (day > 7) return ''
          return m === 1 ? `${y}` : `${y}/${String(m).padStart(2, '0')}`
        },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: 'rgba(124,92,255,0.55)', width: 1, labelBackgroundColor: '#5b4bd6', style: 3 },
        horzLine: { color: 'rgba(124,92,255,0.55)', width: 1, labelBackgroundColor: '#5b4bd6', style: 3 },
      },
    })

    const isLine = mode === 'line'
    let candles: ISeriesApi<'Candlestick'>
    if (isLine) {
      // 分时：面积图，一根线走完当日
      const area = chart.addAreaSeries({
        lineColor: '#22d3ee',
        topColor: 'rgba(34,211,238,0.25)',
        bottomColor: 'rgba(34,211,238,0.01)',
        lineWidth: 1,
        priceLineVisible: false,
      })
      area.setData(bars.map((b) => ({ time: toTime(b.date), value: b.close })))
      candles = area as unknown as ISeriesApi<'Candlestick'>
    } else {
      candles = chart.addCandlestickSeries({
        upColor: '#ef4444',
        downColor: '#10b981',
        borderVisible: false,
        wickUpColor: '#ef4444',
        wickDownColor: '#10b981',
        priceLineVisible: false,
      })
      candles.setData(
        bars.map((b) => ({
          time: toTime(b.date),
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
        })),
      )
    }

    // 水平线：止损/支撑/压力/目标。
    // 用 LineSeries 画**短线段**（最近 45 根），而不是 createPriceLine 的全宽横线——
    // 同花顺的价位线就是「标签 + 一小段虚线」，全宽横线会把 K 线切得乱七八糟。
    if (priceLines && priceLines.length > 0) {
      const from = Math.max(0, bars.length - 45)
      const t0 = toTime(bars[from].date)
      const t1 = toTime(bars[bars.length - 1].date)
      for (const pl of priceLines) {
        if (!Number.isFinite(pl.price) || pl.price <= 0) continue
        const ls = chart.addLineSeries({
          color: pl.color,
          lineWidth: 1,
          lineStyle: (pl.style ?? 2) as never,
          priceLineVisible: false,
          lastValueVisible: true,      // 右侧价格轴上标出价位
          crosshairMarkerVisible: false,
          title: pl.title,             // 左上角图例文字
        })
        ls.setData([
          { time: t0, value: pl.price },
          { time: t1, value: pl.price },
        ])
      }
    }

    // 买卖点标记：买 → 下方红色上箭头；卖 → 上方绿色下箭头
    if (trades && trades.length > 0) {
      const barDates = new Set(bars.map((b) => b.date.slice(0, 10)))
      const markers: SeriesMarker<Time>[] = trades
        .filter((t) => barDates.has(t.date.slice(0, 10)))
        .sort((a, b) => a.date.localeCompare(b.date))
        .map((t) => ({
          time: toTime(t.date),
          position: t.side === 'buy' ? 'belowBar' : 'aboveBar',
          color: t.side === 'buy' ? '#ef4444' : '#10b981',
          shape: t.side === 'buy' ? 'arrowUp' : 'arrowDown',
          text: t.label ?? (t.side === 'buy' ? 'B' : 'S'),
          size: 1,
        }))
      if (markers.length) candles.setMarkers(markers)
    }

    // 智能选股信号：绿色小旗 + 「今/明」
    if (picks && picks.length > 0) {
      const barDates = new Set(bars.map((b) => b.date.slice(0, 10)))
      const pm: SeriesMarker<Time>[] = picks
        .filter((p) => barDates.has(p.date.slice(0, 10)))
        .sort((a, b) => a.date.localeCompare(b.date))
        .map((p) => ({
          time: toTime(p.date),
          position: 'belowBar',
          color: p.timing === 'next' ? '#f59e0b' : '#22d3ee',
          shape: 'circle',
          text: p.timing === 'next' ? '明买' : '今买',
          size: 1,
        }))
      if (pm.length) {
        const exist = (trades ?? []).length > 0
        if (!exist) candles.setMarkers(pm)
      }
    }

    // 成交量放到底部 24%
    const vol = chart.addHistogramSeries({
      priceScaleId: 'vol',
      priceFormat: { type: 'volume' },
      priceLineVisible: false,
      lastValueVisible: false,
    })
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } })
    vol.setData(
      bars.map((b) => ({
        time: toTime(b.date),
        value: b.volume,
        color: b.close >= b.open ? 'rgba(239,68,68,0.38)' : 'rgba(16,185,129,0.38)',
      })),
    )

    if (bars.some((b) => b.ma5 != null)) {
      const ma5: ISeriesApi<'Line'> = chart.addLineSeries({
        color: '#38bdf8',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      })
      ma5.setData(bars.filter((b) => b.ma5 != null).map((b) => ({ time: toTime(b.date), value: b.ma5! })))
    }
    if (bars.some((b) => b.ma20 != null)) {
      const ma20: ISeriesApi<'Line'> = chart.addLineSeries({
        color: '#eab308',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      })
      ma20.setData(bars.filter((b) => b.ma20 != null).map((b) => ({ time: toTime(b.date), value: b.ma20! })))
    }

    // 默认只显示最近 120 根（同花顺 PC 版日 K 的习惯：约半年），
    // 而不是 fitContent 把 250 根全塞进去（那样蜡烛太细、看不出形态）。
    const SHOW_BARS = 120
    if (bars.length > SHOW_BARS) {
      chart.timeScale().setVisibleLogicalRange({
        from: bars.length - SHOW_BARS,
        to: bars.length - 1 + 3,
      })
    } else {
      chart.timeScale().fitContent()
    }

    // 十字光标 → 顶部信息栏 + 悬浮小窗
    const timeToIdx = new Map<number, number>()
    bars.forEach((b, i) => timeToIdx.set(toTime(b.date) as unknown as number, i))
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point) {
        setHoverIdx(null)
        setTip(null)
        return
      }
      const i = timeToIdx.get(param.time as unknown as number)
      setHoverIdx(i == null ? null : i)
      setTip(i == null ? null : { x: param.point.x, y: param.point.y })
    })

    chartRef.current = chart

    const onResize = () => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth })
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chartRef.current = null
      chart.remove()
    }
  }, [bars, height, trades, priceLines, picks, mode])

  // 切换股票时重置悬停状态，避免残留上一只票的小窗
  useEffect(() => {
    setHoverIdx(null)
    setTip(null)
  }, [bars])

  // 左侧列表点某笔成交 → 把视图滚到那根 K 线附近（不重建图表）
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !focusDate) return
    const target = focusDate.slice(0, 10)
    const idx = bars.findIndex((b) => b.date.slice(0, 10) === target)
    if (idx < 0) return
    const half = 22
    chart.timeScale().setVisibleLogicalRange({
      from: Math.max(0, idx - half),
      to: idx + half,
    })
  }, [focusDate, bars])

  if (bars.length < 2) {
    return <div className="px-3 py-16 text-center text-sm2 text-fg3">无行情数据</div>
  }

  // 光标悬停的那根；没悬停就显示最新一根
  const idx = hoverIdx != null && hoverIdx >= 0 && hoverIdx < bars.length ? hoverIdx : bars.length - 1
  const bar = bars[idx]
  const prev = idx > 0 ? bars[idx - 1] : null
  const chg = prev && prev.close > 0 ? (bar.close / prev.close - 1) * 100 : null
  const chgAbs = prev ? bar.close - prev.close : null
  const amp = prev && prev.close > 0 ? ((bar.high - bar.low) / prev.close) * 100 : null
  const tone = chg == null || chg === 0 ? 'text-fg2' : chg > 0 ? 'text-up' : 'text-down'

  // 至今涨幅：光标那根收盘 → 最新收盘
  const lastBar = bars[bars.length - 1]
  const lastIdx = bars.length - 1
  const toNow = idx < lastIdx && bar.close > 0 ? (lastBar.close / bar.close - 1) * 100 : null
  const toNowBars = lastIdx - idx
  const rangeChg = bars[0].close > 0 ? (lastBar.close / bars[0].close - 1) * 100 : null
  const toneOf = (v: number | null) => (v == null || v === 0 ? 'text-fg2' : v > 0 ? 'text-up' : 'text-down')

  // 小窗：贴着光标，右侧放不下就翻到左边
  const tipW = 132
  const wrapW = wrapRef.current?.clientWidth ?? 800
  const flip = tip != null && tip.x + tipW + 24 > wrapW

  return (
    <div className="relative w-full" ref={wrapRef}>
      {/* 周期切换栏：日线 / 周线 / 月线 / 季线 / 年线 / 60分 / 2小时 / 4小时 */}
      {periods && periods.length > 0 && (
        <div className="mb-1 flex flex-wrap items-center gap-1 border-b border-line/60 px-1 pb-1">
          {periods.map((p) => (
            <button
              key={p.key}
              onClick={() => onPeriod?.(p.key)}
              className={
                'rounded px-2 py-0.5 text-sm2 transition-colors ' +
                (period === p.key ? 'bg-brand/20 text-brand' : 'text-fg3 hover:bg-white/5 hover:text-fg2')
              }
            >
              {p.label}
            </button>
          ))}
          {loading && <span className="ml-1 text-sm2 text-fg3">加载中…</span>}
        </div>
      )}
      {/* 顶部信息栏：完整日期（带星期）+ 开高低收 + 涨跌幅 + 振幅 + 量 + 至今/区间涨幅 */}
      <div className="flex min-h-[20px] flex-wrap items-center gap-x-2.5 gap-y-0.5 overflow-hidden whitespace-nowrap px-1 font-mono text-sm2 leading-[1.15]">
        <span className="text-fg2">{fmtDateFull(bar.date)}</span>
        <span className={tone}>开 {bar.open.toFixed(2)}</span>
        <span className={tone}>高 {bar.high.toFixed(2)}</span>
        <span className={tone}>低 {bar.low.toFixed(2)}</span>
        <span className={`font-semibold ${tone}`}>收 {bar.close.toFixed(2)}</span>
        {chg != null && chgAbs != null && (
          <span className={`font-semibold ${tone}`}>
            {chg > 0 ? '+' : ''}
            {chgAbs.toFixed(2)} ({fmtSigned(chg)})
          </span>
        )}
        {amp != null && <span className="text-fg3">振幅 {amp.toFixed(2)}%</span>}
        <span className="text-fg3">量 {fmtVol(bar.volume)}</span>
        {bar.ma5 != null && <span className="text-xs2 text-sky-400/80">MA5 {bar.ma5.toFixed(2)}</span>}
        {bar.ma20 != null && <span className="text-xs2 text-gold/80">MA20 {bar.ma20.toFixed(2)}</span>}
        {toNow != null && (
          <span className={`ml-1 font-semibold ${toneOf(toNow)}`}>
            至今 {fmtSigned(toNow)}
            <span className="ml-1 text-xs2 font-normal text-fg3">{toNowBars}日</span>
          </span>
        )}
        <span className={`font-semibold ${toneOf(rangeChg)}`} title={`自 ${bars[0].date}`}>
          区间 {rangeChg != null ? fmtSigned(rangeChg) : '—'}
        </span>
        {hoverIdx == null && <span className="ml-auto text-3xs font-normal text-fg3">悬停看当日</span>}
      </div>
      <div ref={ref} className="w-full" />

      {/* 悬浮数据窗：同花顺那个跟随十字光标的小方框 */}
      {tip && hoverIdx != null && (
        <div
          className="pointer-events-none absolute z-20 rounded-md border border-line2/50 bg-[#0d1117]/70 px-2 py-1.5 font-mono text-xs2 shadow-lg backdrop-blur-[1.5px]"
          style={{
            left: flip ? Math.max(4, tip.x - tipW - 16) : tip.x + 16,
            top: Math.min(Math.max(4, tip.y - 46), height - 96),
            width: tipW,
          }}
        >
          <div className="mb-1 border-b border-line/60 pb-1 text-3xs text-fg3">{fmtDateFull(bar.date)}</div>
          <TipRow label="开" value={bar.open.toFixed(2)} />
          <TipRow label="高" value={bar.high.toFixed(2)} />
          <TipRow label="低" value={bar.low.toFixed(2)} />
          <TipRow label="收" value={bar.close.toFixed(2)} strong tone={tone} />
          {chg != null && <TipRow label="涨跌" value={fmtSigned(chg)} tone={tone} />}
          {amp != null && <TipRow label="振幅" value={`${amp.toFixed(2)}%`} />}
          <TipRow label="成交量" value={fmtVol(bar.volume)} />
          {bar.ma5 != null && <TipRow label="MA5" value={bar.ma5.toFixed(2)} color="text-sky-400" />}
          {bar.ma20 != null && <TipRow label="MA20" value={bar.ma20.toFixed(2)} color="text-gold" />}
        </div>
      )}
    </div>
  )
}

function TipRow({
  label,
  value,
  tone,
  strong,
  color,
}: {
  label: string
  value: string
  tone?: string
  strong?: boolean
  color?: string
}) {
  return (
    <div className="flex items-baseline justify-between gap-2 leading-[1.5]">
      <span className="text-fg3">{label}</span>
      <span className={`${color ?? tone ?? 'text-fg'} ${strong ? 'font-semibold' : ''}`}>{value}</span>
    </div>
  )
}

/** 带符号百分比：+12.34% / -5.00% */
function fmtSigned(v: number): string {
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

/** 成交量简写（数据口径是股） */
function fmtVol(v: number | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(1)}万`
  return v.toFixed(0)
}
