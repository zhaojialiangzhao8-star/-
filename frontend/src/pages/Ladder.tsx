import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Flame, RefreshCw, TrendingDown } from 'lucide-react'
import { api } from '../services/api'
import { Card, Metric } from '../components/ui'

const pct = (v: number | null | undefined) =>
  v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
const pctCls = (v: number | null | undefined) =>
  v == null ? 'text-fg3' : v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-fg2'

export default function Ladder() {
  const navigate = useNavigate()
  const q = useQuery({
    queryKey: ['limit-ladder'],
    queryFn: () => api.limitLadder(30),
    refetchInterval: 60_000,
    retry: 0,
  })

  const d = q.data
  const go = (sym: string) => navigate(`/analysis?symbol=${sym}`)

  return (
    <>
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">涨停天梯</h2>
        <span className="font-mono text-[11px] text-fg3">
          {d ? `${d.date} · 按连板高度分档 · 点个股进个股分析` : '加载中…'}
        </span>
        <button
          onClick={() => q.refetch()}
          disabled={q.isFetching}
          className="ml-auto flex items-center gap-1 rounded border border-line2 bg-card2 px-2.5 py-1 text-sm2 text-fg2 hover:text-fg disabled:opacity-50"
        >
          <RefreshCw size={11} className={q.isFetching ? 'animate-spin' : ''} />
          刷新
        </button>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        {q.isError ? (
          <Card title="加载失败">
            <div className="text-sm2 text-up">{(q.error as Error).message}</div>
          </Card>
        ) : (
          <div className="grid grid-cols-12 items-start gap-3">
            {/* 天梯主体 */}
            <Card
              className="col-span-8"
              title="连板天梯"
              subtitle={d ? `${d.stats.ladder_tiers} 档 · 高度 ${d.max_boards} 板` : '加载中…'}
              padded={false}
            >
              {(d?.tiers ?? []).map((t) => (
                <div key={t.boards} className="flex gap-3 border-b border-line/50 px-3 py-2 last:border-b-0">
                  <div className="w-[58px] shrink-0 text-right">
                    <div className={`num text-[15px] font-bold leading-none ${t.boards >= 2 ? 'text-up' : 'text-fg2'}`}>
                      {t.boards}
                    </div>
                    <div className="mt-0.5 text-xs2 text-fg3">
                      板 · {t.count} 只
                    </div>
                  </div>
                  <div className="flex min-w-0 flex-wrap gap-1.5">
                    {t.stocks.map((s) => (
                      <button
                        key={s.symbol}
                        onClick={() => go(s.symbol)}
                        title={`${s.symbol} · ${s.turnover != null ? `换手 ${s.turnover}% · ` : ''}${
                          s.amount_yi != null ? `成交 ${s.amount_yi} 亿` : ''
                        }`}
                        className="group flex items-center gap-1.5 rounded border border-line2 bg-card2 px-1.5 py-[3px] text-[12px] transition-colors hover:border-brand hover:bg-brand/10"
                      >
                        <span className="text-fg group-hover:text-brand">{s.name}</span>
                        <span className={`num text-[11px] ${pctCls(s.change_pct)}`}>{pct(s.change_pct)}</span>
                        <span className="num text-xs2 text-fg3">{s.close?.toFixed(2) ?? '—'}</span>
                      </button>
                    ))}
                    {t.truncated > 0 && (
                      <span className="self-center text-xs2 text-fg3">… 还有 {t.truncated} 只</span>
                    )}
                  </div>
                </div>
              ))}
              {!d && <div className="px-3 py-10 text-center text-sm2 text-fg3">加载涨停天梯…</div>}
            </Card>

            {/* 右侧统计 + 跌停 */}
            <div className="col-span-4 space-y-3">
              <Card
                title="今日统计"
                subtitle={d ? d.date : '—'}
                right={
                  d ? (
                    <span className="flex items-center gap-1 text-[11px] text-up">
                      <Flame size={11} /> {d.counts.up}
                    </span>
                  ) : null
                }
              >
                <div className="grid grid-cols-2 gap-2">
                  <Metric label="涨停" value={String(d?.counts.up ?? '—')} tone="text-up" />
                  <Metric label="跌停" value={String(d?.counts.down ?? '—')} tone="text-down" />
                  <Metric label="最高板" value={d ? `${d.max_boards} 板` : '—'} tone="text-up" />
                  <Metric label="连板（2板+）" value={String(d?.stats.ge2 ?? '—')} />
                  <Metric label="首板" value={String(d?.stats.first_board ?? '—')} />
                  <Metric
                    label="连板占比"
                    value={d?.stats.promotion_rate != null ? `${d.stats.promotion_rate}%` : '—'}
                  />
                </div>
              </Card>

              <Card
                title="跌停"
                subtitle={d ? `${d.counts.down} 只 · 按连续跌停排` : '—'}
                right={
                  d && d.counts.down > 0 ? (
                    <span className="flex items-center gap-1 text-[11px] text-down">
                      <TrendingDown size={11} />
                    </span>
                  ) : null
                }
                padded={false}
              >
                {(d?.downs ?? []).length === 0 ? (
                  <div className="px-3 py-6 text-center text-sm2 text-fg3">今天没有跌停</div>
                ) : (
                  <div className="max-h-[280px] overflow-y-auto">
                    {(d?.downs ?? []).map((s) => (
                      <button
                        key={s.symbol}
                        onClick={() => go(s.symbol)}
                        className="flex w-full items-center gap-2 border-b border-line/50 px-3 py-1 text-left text-[12px] hover:bg-brand/[0.06]"
                      >
                        <span className="text-fg">{s.name}</span>
                        {s.boards > 1 && (
                          <span className="rounded bg-down/15 px-1 text-xs2 text-down">{s.boards}连跌</span>
                        )}
                        <span className="ml-auto num text-fg3">{s.close?.toFixed(2) ?? '—'}</span>
                        <span className={`num w-[54px] text-right ${pctCls(s.change_pct)}`}>
                          {pct(s.change_pct)}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
