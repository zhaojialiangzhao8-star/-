import { useEffect, useRef } from 'react'
import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'

/** 把 'YYYY-MM-DD' 转成 lightweight-charts 要的时间戳 */
const toTime = (s: string) => (Date.parse(s + 'T00:00:00Z') / 1000) as UTCTimestamp

interface Props {
  /** [[日期, 净值], ...]，净值基准 1.0 */
  nav: [string, number][]
  height?: number
}

/**
 * 净值曲线 + 回撤曲线（共享时间轴，上下两栏）。
 * 用 lightweight-charts 而不是手写 SVG：可缩放、可拖拽、有十字光标。
 */
export default function EquityChart({ nav, height = 320 }: Props) {
  const navRef = useRef<HTMLDivElement>(null)
  const ddRef = useRef<HTMLDivElement>(null)
  const charts = useRef<{ nav?: IChartApi; dd?: IChartApi }>({})

  useEffect(() => {
    if (!navRef.current || !ddRef.current || nav.length < 2) return

    const common = {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#5b6779',
        fontSize: 10,
        fontFamily: 'JetBrains Mono, ui-monospace, Consolas, monospace',
      },
      grid: {
        vertLines: { color: '#1c212b' },
        horzLines: { color: '#1c212b' },
      },
      rightPriceScale: { borderColor: '#262d3a', scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: '#262d3a', timeVisible: false, rightOffset: 3 },
      crosshair: {
        mode: 1 as const,
        vertLine: { color: '#7c5cff', width: 1 as const, labelBackgroundColor: '#7c5cff' },
        horzLine: { color: '#7c5cff', width: 1 as const, labelBackgroundColor: '#7c5cff' },
      },
      handleScroll: true,
      handleScale: true,
    }

    const navChart = createChart(navRef.current, { ...common, height, width: navRef.current.clientWidth })
    const navSeries: ISeriesApi<'Area'> = navChart.addAreaSeries({
      lineColor: '#7c5cff',
      topColor: 'rgba(124,92,255,0.35)',
      bottomColor: 'rgba(124,92,255,0.02)',
      lineWidth: 2,
      priceLineVisible: false,
    })
    navSeries.setData(nav.map(([d, v]) => ({ time: toTime(d), value: Number(v.toFixed(4)) })))

    const ddChart = createChart(ddRef.current, { ...common, height: 110, width: ddRef.current.clientWidth })
    const ddSeries: ISeriesApi<'Area'> = ddChart.addAreaSeries({
      lineColor: '#f6465d',
      topColor: 'rgba(246,70,93,0.02)',
      bottomColor: 'rgba(246,70,93,0.35)',
      lineWidth: 1,
      priceLineVisible: false,
    })
    let peak = nav[0][1]
    ddSeries.setData(
      nav.map(([d, v]) => {
        peak = Math.max(peak, v)
        return { time: toTime(d), value: Number(((v / peak - 1) * 100).toFixed(2)) }
      }),
    )

    // 两图时间轴联动
    const sync = (from: IChartApi, to: IChartApi) => {
      from.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (range) to.timeScale().setVisibleLogicalRange(range)
      })
    }
    sync(navChart, ddChart)
    sync(ddChart, navChart)
    navChart.timeScale().fitContent()
    ddChart.timeScale().fitContent()

    charts.current = { nav: navChart, dd: ddChart }

    const onResize = () => {
      if (navRef.current) navChart.applyOptions({ width: navRef.current.clientWidth })
      if (ddRef.current) ddChart.applyOptions({ width: ddRef.current.clientWidth })
    }
    window.addEventListener('resize', onResize)

    return () => {
      window.removeEventListener('resize', onResize)
      navChart.remove()
      ddChart.remove()
      charts.current = {}
    }
  }, [nav, height])

  if (nav.length < 2) {
    return <div className="px-3 py-10 text-center text-sm2 text-fg3">运行回测后显示净值曲线</div>
  }

  return (
    <div>
      <div ref={navRef} className="w-full" />
      <div className="flex items-center gap-2 px-2 pt-1 text-3xs text-fg3">
        <span>回撤（%）</span>
        <span className="text-up">■ 水下</span>
      </div>
      <div ref={ddRef} className="w-full" />
    </div>
  )
}
