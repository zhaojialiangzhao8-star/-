import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, RefreshCw, ShieldAlert } from 'lucide-react'
import { api } from '../services/api'
import { Card, Metric } from '../components/ui'

const LEVEL_STYLE: Record<string, { text: string; bg: string; label: string }> = {
  star: { text: 'text-up', bg: 'bg-up/12 border-up/35', label: '*ST 退市风险' },
  st: { text: 'text-gold', bg: 'bg-gold/12 border-gold/35', label: 'ST 风险警示' },
  latent: { text: 'text-cyan', bg: 'bg-cyan/12 border-cyan/35', label: '隐性预雷' },
}

/** 规则行左侧只显示「编号」，英文 id 放悬停——和策略库同一个取舍 */
const ruleNo = (id: string): string => {
  if (id === 'name_star') return '*ST'
  if (id === 'name_st') return 'ST'
  const m = id.match(/_(\d+)$/)
  return m ? m[1].padStart(2, '0') : id
}

export default function Risk() {
  const navigate = useNavigate()
  const [level, setLevel] = useState('')
  const [rule, setRule] = useState('')

  const ov = useQuery({ queryKey: ['risk-overview'], queryFn: () => api.riskOverview(), refetchInterval: 30_000 })
  const list = useQuery({
    queryKey: ['risk-list', level, rule],
    queryFn: () => api.riskList({ level, rule, limit: 500 }),
    refetchInterval: 30_000,
  })

  const rules = ov.data?.rules ?? []
  const items = list.data?.items ?? []
  const stats = ov.data?.stats
  const gaps = useMemo(
    () => rules.filter((r) => r.status === 'no-data'),
    [rules],
  )

  return (
    <>
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">风险库</h2>
        <span className="font-mono text-[11px] text-fg3">
          {stats
            ? `扫描 ${stats.scanned} 只 · 命中 ${stats.flagged} 只 · 未更新 ${stats.updated_at ?? '—'}`
            : '加载中…'}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={() => {
              ov.refetch()
              list.refetch()
            }}
            className="flex items-center gap-1 rounded border border-line2 bg-card2 px-2.5 py-1 text-sm2 text-fg2 hover:text-fg"
          >
            <RefreshCw size={11} className={ov.isFetching ? 'animate-spin' : ''} />
            刷新
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-12 items-start gap-3">
          {/* 左：规则清单 */}
          <Card
            className="col-span-7"
            title="规则清单"
            subtitle={`${rules.length} 条 · 点一条只看该规则命中的票`}
            padded={false}
          >
            <div className="max-h-[calc(100vh-200px)] overflow-y-auto">
              {['star', 'st', 'latent'].map((lv) => (
                <div key={lv}>
                  <div className={`flex items-center gap-2 border-b border-line/60 px-3 py-1 text-xs2 ${LEVEL_STYLE[lv].text}`}>
                    <span className="font-semibold">{LEVEL_STYLE[lv].label}</span>
                    <span className="text-fg3">命中即拉黑</span>
                  </div>
                  {rules
                    .filter((r) => r.level === lv)
                    .map((r) => {
                      const active = rule === r.id
                      return (
                        <button
                          key={r.id}
                          onClick={() => setRule(active ? '' : r.id)}
                          title={`${r.id} · ${r.desc}`}
                          className={`flex w-full items-center gap-2 border-b border-line/40 px-3 py-[5px] text-left text-[12px] transition-colors ${
                            active ? 'bg-brand/12' : 'hover:bg-brand/[0.06]'
                          }`}
                        >
                          <span className={`w-[34px] shrink-0 font-mono text-[11px] ${r.hits > 0 ? 'text-up' : 'text-fg3'}`}>
                            {ruleNo(r.id)}
                          </span>
                          <span className={`min-w-0 flex-1 truncate text-[12px] ${active ? 'text-brand' : 'text-fg'}`}>{r.title}</span>
                          <span className={`num w-[38px] shrink-0 text-right ${r.hits > 0 ? 'text-up' : 'text-fg3'}`}>
                            {r.hits}
                          </span>
                          <span
                            className={`w-[52px] shrink-0 text-right text-xs2 ${
                              r.status === 'no-data' ? 'text-gold' : r.auto ? 'text-cyan' : 'text-fg3'
                            }`}
                          >
                            {r.status === 'no-data' ? '缺数据' : r.auto ? '自动' : '待接'}
                          </span>
                        </button>
                      )
                    })}
                </div>
              ))}
            </div>
          </Card>

          {/* 右：统计 + 命中标的 */}
          <div className="col-span-5 space-y-3">
            <Card title="概况" subtitle={stats?.latest_snapshot ? `快照 ${stats.latest_snapshot}` : ''}>
              <div className="grid grid-cols-2 gap-2">
                <Metric label="扫描标的" value={String(stats?.scanned ?? '—')} />
                <Metric label="命中 / 拉黑" value={String(stats?.flagged ?? '—')} tone="text-up" />
                <Metric label="人工拉黑" value={String(stats?.manual ?? '—')} />
                <Metric label="规则缺口" value={String(gaps.length)} tone="text-gold" />
              </div>
              {gaps.length > 0 && (
                <div className="mt-2 rounded-md border border-gold/35 bg-gold/10 px-2.5 py-1.5 text-[11px] text-gold">
                  <AlertTriangle size={11} className="mr-1 inline" />
                  {gaps.length} 条规则需要财报字段（扣非净利、营收、审计意见、占用、担保、分红、商誉、质押）。
                  把数据写进 <code className="rounded bg-black/30 px-1 font-mono text-fg2">data/risk/fundamentals.json</code>{' '}
                  后会自动生效，缺数据时不会当成"通过"。
                </div>
              )}
            </Card>

            <Card
              title="命中标的"
              subtitle={rule ? `规则 ${rule}` : level ? LEVEL_STYLE[level]?.label : '全部档位'}
              right={
                <div className="flex items-center gap-1">
                  {[
                    ['', '全部'],
                    ['star', '*ST'],
                    ['st', 'ST'],
                    ['latent', '潜雷'],
                  ].map(([k, label]) => (
                    <button
                      key={k}
                      onClick={() => {
                        setLevel(k)
                        setRule('')
                      }}
                      className={`rounded px-1.5 py-0.5 text-xs2 ${
                        level === k ? 'bg-brand/20 text-brand' : 'text-fg3 hover:text-fg'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              }
              padded={false}
            >
              <div className="max-h-[calc(100vh-420px)] overflow-y-auto">
                {items.length === 0 && (
                  <div className="px-3 py-8 text-center text-sm2 text-fg3">没有命中的标的</div>
                )}
                {items.map((it) => {
                  const st = LEVEL_STYLE[it.level] ?? LEVEL_STYLE.latent
                  return (
                    <button
                      key={it.symbol}
                      onClick={() => navigate(`/analysis?symbol=${it.symbol}`)}
                      className="flex w-full items-center gap-2 border-b border-line/40 px-3 py-[4px] text-left text-[12px] hover:bg-brand/[0.06]"
                    >
                      <span
                        className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${st.bg} ${st.text}`}
                      >
                        <ShieldAlert size={9} />
                      </span>
                      <span className={`w-[86px] shrink-0 truncate ${it.level === 'star' ? 'font-semibold text-up' : 'text-fg'}`}>
                        {it.name || it.symbol}
                      </span>
                      <span className="num shrink-0 font-mono text-[11px] text-fg3">{it.symbol.split('.')[0]}</span>
                      <span className={`shrink-0 text-xs2 ${st.text}`}>
                        {it.manual ? '人工' : it.level === 'star' ? '*ST' : it.level === 'st' ? 'ST' : '潜雷'}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[11px] text-fg3">
                        {(it.rules[0]?.title ?? '') + (it.rules.length > 1 ? ` 等 ${it.rules.length} 条` : '')}
                      </span>
                    </button>
                  )
                })}
              </div>
            </Card>
          </div>
        </div>
      </div>
    </>
  )
}
