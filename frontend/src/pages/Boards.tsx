import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { RefreshCw, Search } from 'lucide-react'
import { api, type Board, type BoardMember } from '../services/api'
import { Card } from '../components/ui'
import KLineChart from '../components/KLineChart'

const PERIODS = [
  { key: 'day', label: '日线' },
  { key: 'week', label: '周线' },
  { key: 'month', label: '月线' },
  { key: 'quarter', label: '季线' },
]

const pct = (v: number | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)

/** 6 位代码补市场后缀（和同花顺口径一致：6/9/5 沪，4/8 北，其余深） */
const toSymbol = (code: string) => {
  if (!code || code.length !== 6) return code
  const mkt = /^[695]/.test(code) ? 'SH' : /^[48]/.test(code) ? 'BJ' : 'SZ'
  return `${code}.${mkt}`
}
const cls = (v: number | undefined) =>
  v == null ? 'text-fg3' : v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-fg2'

export default function Boards() {
  const [params, setParams] = useSearchParams()
  const kind = ((params.get('kind') as 'industry' | 'concept') || 'industry') as 'industry' | 'concept'
  const selected = params.get('code') || ''
  const navigate = useNavigate()
  const [sort, setSort] = useState<{ key: string; desc: boolean }>({ key: 'change_pct', desc: true })
  // 板块指数周期：和个股 K 线同一套口径（日/周/月/季）
  const [period, setPeriod] = useState('day')
  const [q, setQ] = useState('')

  const setKind = (k: 'industry' | 'concept') => setParams({ kind: k })
  const select = (code: string) => setParams({ kind, code })

  const list = useQuery({
    queryKey: ['boards', kind],
    queryFn: () => api.boards(kind, 80),
    refetchInterval: 30_000,
    retry: 0,
  })

  const items = useMemo(() => {
    const arr = [...(list.data?.items ?? [])] as (Board & { created?: string; members?: number })[]
    const k = sort.key as keyof (typeof arr)[number]
    arr.sort((a, b) => {
      const av = a[k]
      const bv = b[k]
      if (typeof av === 'string' || typeof bv === 'string') {
        const as = String(av ?? '')
        const bs = String(bv ?? '')
        return sort.desc ? bs.localeCompare(as, 'zh') : as.localeCompare(bs, 'zh')
      }
      const an = Number(av ?? 0)
      const bn = Number(bv ?? 0)
      return sort.desc ? bn - an : an - bn
    })
    return arr
  }, [list.data, sort])

  const cur = items.find((b) => b.code === selected)

  // 板块搜索：名称 / 代码 / 龙头股都能搜
  const filtered = useMemo(() => {
    const k = q.trim()
    if (!k) return items
    return items.filter(
      (b) =>
        b.name.includes(k) ||
        b.code.includes(k) ||
        (b.leader ?? '').includes(k),
    )
  }, [items, q])

  const detail = useQuery({
    queryKey: ['boardDetail', selected],
    queryFn: () => api.boardDetail(selected, 40),
    enabled: !!selected,
    refetchInterval: 30_000,
    retry: 0,
  })

  const kline = useQuery({
    queryKey: ['boardKline', selected, kind, cur?.name ?? '', period],
    queryFn: () => api.boardKline(selected, kind, cur?.name ?? '', period, 2000),
    enabled: !!selected,
    retry: 0,
    staleTime: 120_000,
  })

  // 打开页面就选中第一个板块，右侧不留空
  useEffect(() => {
    if (!selected && items.length > 0) setParams({ kind, code: items[0].code }, { replace: true })
  }, [selected, items, kind, setParams])

  const members = [...(detail.data?.items ?? [])].sort((a, b) => (b.change_pct ?? 0) - (a.change_pct ?? 0))

  return (
    <>
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">板块</h2>
        <span className="font-mono text-[11px] text-fg3">
          {list.data
            ? `${list.data.count} 个 · ${kind === 'industry' ? '行业' : '概念'} · 点一行看板块K线与成分`
            : '加载中…'}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as 'industry' | 'concept')}
            className="rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm2 outline-none focus:border-brand"
          >
            <option value="industry">行业板块</option>
            <option value="concept">概念板块</option>
          </select>
          <button
            onClick={() => list.refetch()}
            disabled={list.isFetching}
            className="flex items-center gap-1 rounded border border-line2 bg-card2 px-2.5 py-1 text-sm2 text-fg2 hover:text-fg disabled:opacity-50"
          >
            <RefreshCw size={11} className={list.isFetching ? 'animate-spin' : ''} />
            刷新
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-12 items-start gap-3">
          {/* 左：板块列表 */}
          <Card
            className="col-span-7"
            title="板块涨跌"
            subtitle={`${filtered.length} / ${items.length} 个 · 点一行看右侧板块指数`}
            right={
              <div className="flex items-center gap-1 rounded border border-line2 bg-card2 px-1.5">
                <Search size={11} className="text-fg3" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="搜索板块 / 代码"
                  className="w-[130px] bg-transparent py-0.5 text-[11px] outline-none placeholder:text-fg3"
                />
              </div>
            }
            padded={false}
          >
            {list.isError ? (
              <div className="px-3 py-6 text-center text-sm2 text-up">
                {(list.error as Error).message}
              </div>
            ) : (
              <div className="max-h-[calc(100vh-132px)] overflow-y-auto">
              <table className="w-full border-collapse whitespace-nowrap text-sm2">
                <thead className="sticky top-0 z-10 bg-card">
                  <tr>
                    {(
                      (kind === 'industry'
                        ? [
                            ['name', '板块', 'text-left'],
                            ['change_pct', '涨跌幅', 'text-right'],
                            ['up_count', '涨/跌', 'text-right'],
                            ['leader', '龙头', 'text-right'],
                            ['leader_pct', '龙头涨幅', 'text-right'],
                            ['amount', '成交额', 'text-right'],
                          ]
                        : [
                            ['name', '概念', 'text-left'],
                            ['created', '创建日', 'text-right'],
                            ['members', '成分数', 'text-right'],
                            ['', '', 'text-right'],
                            ['', '', 'text-right'],
                            ['', '', 'text-right'],
                          ]) as [string, string, string][]
                    ).map(([key, label, align]) => (
                      <th
                        key={`${key}-${label}`}
                        onClick={() => {
                          if (!key) return
                          setSort((s) =>
                            s.key === key ? { key, desc: !s.desc } : { key, desc: key !== 'name' },
                          )
                        }}
                        className={`border-b border-line px-2.5 py-1 text-xs2 font-medium ${align} ${
                          key ? 'cursor-pointer select-none hover:text-fg' : ''
                        } ${sort.key === key ? 'text-brand' : 'text-fg3'}`}
                      >
                        {label}
                        {sort.key === key && <span className="ml-0.5">{sort.desc ? '▼' : '▲'}</span>}
                      </th>
                    ))}
                    <th className="w-full border-b border-line" />
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((b) => {
                    const active = b.code === selected
                    const rowCls = `cursor-pointer ${active ? 'bg-brand/[0.12]' : 'hover:bg-brand/[0.06]'}`
                    const nameCls = `border-b border-line/50 px-2.5 py-[3px] text-left ${
                      active ? 'font-medium text-brand' : 'text-fg'
                    }`
                    return kind === 'industry' ? (
                      <tr key={b.code} onClick={() => select(b.code)} className={rowCls}>
                        <td className={nameCls}>{b.name}</td>
                        <td className={`num border-b border-line/50 px-2.5 py-[3px] text-right ${cls(b.change_pct)}`}>
                          {pct(b.change_pct)}
                        </td>
                        <td className="num border-b border-line/50 px-2.5 py-[3px] text-right">
                          <span className="text-up">{b.up_count}</span>
                          <span className="mx-0.5 text-fg3">/</span>
                          <span className="text-down">{b.down_count}</span>
                        </td>
                        <td className="border-b border-line/50 px-2.5 py-[3px] text-right">
                          {b.leader ? (
                            b.leader_symbol ? (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation()
                                  navigate(`/analysis?symbol=${toSymbol(b.leader_symbol!)}`)
                                }}
                                title={`看 ${b.leader} 个股分析`}
                                className="text-fg2 underline decoration-line2 underline-offset-2 hover:text-cyan hover:decoration-cyan"
                              >
                                {b.leader}
                              </button>
                            ) : (
                              <span className="text-fg2">{b.leader}</span>
                            )
                          ) : (
                            <span className="text-fg3">—</span>
                          )}
                        </td>
                        <td className={`num border-b border-line/50 px-2.5 py-[3px] text-right ${cls(b.leader_pct)}`}>
                          {b.leader_pct ? pct(b.leader_pct) : '—'}
                        </td>
                        <td className="num border-b border-line/50 px-2.5 py-[3px] text-right text-fg3">
                          {b.amount_yi ? `${b.amount_yi.toFixed(0)} 亿` : '—'}
                        </td>
                        <td className="w-full border-b border-line/50" />
                      </tr>
                    ) : (
                      <tr key={b.code} onClick={() => select(b.code)} className={rowCls}>
                        <td className={nameCls}>{b.name}</td>
                        <td className="num border-b border-line/50 px-2.5 py-[3px] text-right text-fg3">
                          {(b as Board & { created?: string }).created || '—'}
                        </td>
                        <td className="num border-b border-line/50 px-2.5 py-[3px] text-right text-gold">
                          {(b as Board & { members?: number }).members ?? '—'}
                        </td>
                        <td className="border-b border-line/50 px-2.5 py-[3px]" />
                        <td className="border-b border-line/50 px-2.5 py-[3px]" />
                        <td className="border-b border-line/50 px-2.5 py-[3px]" />
                        <td className="w-full border-b border-line/50" />
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              </div>
            )}
          </Card>

          {/* 右：板块指数 K 线 + 成分股 */}
          <div className="col-span-5 space-y-3">
            <Card
              title={cur ? `${cur.name} · 板块指数` : '板块指数'}
              subtitle={
                cur
                  ? `等权合成 · ${
                      kline.data?.source === 'local' ? '本地成分' : '同花顺成分'
                    } · 行情至 ${kline.data?.rows?.[kline.data.rows.length - 1]?.date ?? '—'}`
                  : '点左侧板块'
              }
              right={
                cur && kind === 'industry' && cur.change_pct != null ? (
                  <span className={`num text-[12px] font-semibold ${cls(cur.change_pct)}`}>
                    {pct(cur.change_pct)}
                  </span>
                ) : null
              }
              padded={false}
            >
              {cur && kind === 'industry' && (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line/60 px-3 py-1 text-[11px] text-fg3">
                  <span>
                    涨 <span className="num text-up">{cur.up_count}</span> / 跌{' '}
                    <span className="num text-down">{cur.down_count}</span>
                  </span>
                  <span>
                    成交额 <span className="num text-gold">{cur.amount_yi ? `${cur.amount_yi.toFixed(0)} 亿` : '—'}</span>
                  </span>
                  {cur.leader && (
                    <span>
                      龙头 <span className="text-fg2">{cur.leader}</span>{' '}
                      <span className={`num ${cls(cur.leader_pct)}`}>{pct(cur.leader_pct)}</span>
                    </span>
                  )}
                  <span className="ml-auto font-mono">
                    {kline.data ? `${kline.data.count} 根 · ${kline.data.members} 只成分` : '—'}
                  </span>
                </div>
              )}
              <div className="px-1 pt-1">
                {kline.isError ? (
                  <div className="px-2 py-8 text-center text-[11px] text-up">
                    板块K线获取失败（{(kline.error as Error).message}）
                  </div>
                ) : kline.data?.rows?.length ? (
                  <KLineChart
                    bars={kline.data.rows}
                    height={250}
                    periods={PERIODS}
                    period={period}
                    onPeriod={setPeriod}
                    loading={kline.isFetching}
                  />
                ) : (
                  <div className="px-2 py-8 text-center text-[11px] text-fg3">
                    {kline.isLoading ? '加载板块K线…' : '暂无K线'}
                  </div>
                )}
              </div>
            </Card>

            <Card
              title="成分股"
              subtitle={members.length ? `共 ${members.length} 只 · 按涨幅排序` : '点左侧板块'}
              right={
                detail.isFetching ? (
                  <span className="text-xs2 text-fg3">刷新中…</span>
                ) : (
                  <span className="text-xs2 text-fg3">点股票进个股分析</span>
                )
              }
              padded={false}
            >
              <div className="max-h-[300px] overflow-y-auto">
                <table className="w-full border-collapse whitespace-nowrap text-sm2">
                  <thead className="sticky top-0 bg-card">
                    <tr>
                      <th className="border-b border-line px-2.5 py-1 text-left text-xs2 font-medium text-fg3">名称</th>
                      <th className="border-b border-line px-2.5 py-1 text-left text-xs2 font-medium text-fg3">代码</th>
                      <th className="border-b border-line px-2.5 py-1 text-right text-xs2 font-medium text-fg3">现价</th>
                      <th className="border-b border-line px-2.5 py-1 text-right text-xs2 font-medium text-fg3">涨跌幅</th>
                      <th className="border-b border-line px-2.5 py-1 text-right text-xs2 font-medium text-fg3">成交额</th>
                      <th className="w-full border-b border-line" />
                    </tr>
                  </thead>
                  <tbody>
                    {members.map((m: BoardMember) => (
                      <tr
                        key={m.symbol}
                        onClick={() => navigate(`/analysis?symbol=${m.symbol}`)}
                        className="cursor-pointer hover:bg-brand/[0.06]"
                      >
                        <td className="border-b border-line/50 px-2.5 py-[3px] text-left text-fg">{m.name || m.symbol}</td>
                        <td className="num border-b border-line/50 px-2.5 py-[3px] text-left text-xs2 text-fg3">
                          {m.symbol.split('.')[0]}
                        </td>
                        <td className="num border-b border-line/50 px-2.5 py-[3px] text-right">
                          {m.price?.toFixed(2)}
                        </td>
                        <td className={`num border-b border-line/50 px-2.5 py-[3px] text-right ${cls(m.change_pct)}`}>
                          {pct(m.change_pct)}
                        </td>
                        <td className="num border-b border-line/50 px-2.5 py-[3px] text-right text-fg3">
                          {m.amount ? `${(m.amount / 1e8).toFixed(2)}亿` : '—'}
                        </td>
                        <td className="w-full border-b border-line/50" />
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
        </div>
      </div>
    </>
  )
}

