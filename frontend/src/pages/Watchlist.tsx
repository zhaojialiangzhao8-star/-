import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { CheckSquare, Plus, RefreshCw, Square, Target, Trash2 } from 'lucide-react'
import { api, type RealtimeQuote, type WatchItem, type WatchPlan } from '../services/api'
import { Card, Modal } from '../components/ui'
import { fmtPct, pctClass } from '../lib/format'

/** note 字段存的是计划 JSON，解析失败就当纯文本备注 */
function parsePlan(note?: string): WatchPlan {
  if (!note) return {}
  try {
    const o = JSON.parse(note)
    if (o && typeof o === 'object') return o as WatchPlan
  } catch {
    /* 不是 JSON，当普通备注 */
  }
  return { note }
}

export default function Watchlist() {
  const nav = useNavigate()
  const q = useQuery({ queryKey: ['watchlist'], queryFn: api.watchlist, refetchInterval: 30_000 })
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [adding, setAdding] = useState(false)
  const [addText, setAddText] = useState('')
  const [planFor, setPlanFor] = useState<WatchItem | null>(null)

  const items = q.data?.items ?? []
  const symbols = useMemo(() => items.map((x) => x.symbol), [items])

  // 实时行情（自选股列表为空时不请求）
  const rt = useQuery({
    queryKey: ['watch-quotes', symbols.join(',')],
    queryFn: () => api.realtime(symbols),
    enabled: symbols.length > 0,
    refetchInterval: 5_000,
  })
  const quoteOf = useMemo(() => {
    const m = new Map<string, RealtimeQuote>()
    for (const x of rt.data?.quotes ?? []) m.set(x.symbol, x)
    return m
  }, [rt.data])

  // 行业 / 几天几板 / 连续涨停天数（要扫历史，单独一次请求）
  const extras = useQuery<{ items: Record<string, { industry?: string; boards_label?: string | null; boards_streak?: number }> }>({
    queryKey: ['watch-extras', symbols.join(',')],
    queryFn: () =>
      fetch(`/api/v1/screener/quote-extras?symbols=${encodeURIComponent(symbols.join(','))}`).then((r) => r.json()),
    enabled: symbols.length > 0,
    staleTime: 5 * 60_000,
  })

  const del = useMutation({
    mutationFn: (syms: string[]) => api.deleteWatchBatch(syms),
    onSuccess: () => {
      setSel(new Set())
      q.refetch()
    },
  })

  const add = useMutation({
    mutationFn: (syms: string[]) => api.addWatchBatch(syms),
    onSuccess: () => {
      setAdding(false)
      setAddText('')
      q.refetch()
    },
  })

  const savePlan = useMutation({
    mutationFn: (v: { symbol: string; plan: WatchPlan }) => api.setWatchPlan(v.symbol, v.plan),
    onSuccess: () => {
      setPlanFor(null)
      q.refetch()
    },
  })

  const allOn = items.length > 0 && sel.size === items.length
  const toggleAll = () => setSel(allOn ? new Set() : new Set(symbols))
  const toggle = (s: string) =>
    setSel((prev) => {
      const n = new Set(prev)
      n.has(s) ? n.delete(s) : n.add(s)
      return n
    })

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">自选股</h2>
        <span className="font-mono text-xs2 text-fg3">
          {items.length} 只 · 每 5 秒刷行情 · 可批量删除 / 挂交易计划
        </span>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setAdding(true)}
            className="flex items-center gap-1.5 rounded border border-brand/40 bg-brand/15 px-2.5 py-1 text-sm2 text-brand hover:bg-brand/25"
          >
            <Plus size={12} /> 添加
          </button>
          <button
            onClick={() => q.refetch()}
            className="flex items-center gap-1.5 rounded border border-line bg-card px-2.5 py-1 text-sm2 text-fg2 hover:text-fg"
          >
            <RefreshCw size={12} className={q.isFetching ? 'animate-spin' : ''} /> 刷新
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <Card
          title="自选清单"
          subtitle={rt.data?.trading ? '盘中 · 行情实时' : '非交易时段 · 行情为最近快照'}
          right={
            sel.size > 0 ? (
              <button
                onClick={() => {
                  if (confirm(`确定删除选中的 ${sel.size} 只自选股？`)) del.mutate([...sel])
                }}
                disabled={del.isPending}
                className="flex items-center gap-1 rounded border border-up/50 bg-up/15 px-2.5 py-1 text-sm2 text-up hover:bg-up/25 disabled:opacity-40"
              >
                <Trash2 size={11} /> 删除选中 {sel.size} 只
              </button>
            ) : null
          }
        >
          {q.isLoading && <div className="px-3 py-12 text-center text-sm2 text-fg3">加载中…</div>}
          {!q.isLoading && items.length === 0 && (
            <div className="px-3 py-16 text-center">
              <div className="text-xs text-fg2">还没有自选股</div>
              <div className="mt-1 text-sm2 text-fg3">
                点右上角「添加」，或去「智能选股」用「一键加自选」批量导入
              </div>
            </div>
          )}
          {items.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse whitespace-nowrap text-[12px]">
                <thead>
                  <tr className="bg-[#111726] text-xs2 font-normal text-fg3">
                    <th className="w-[26px] px-2 py-1 text-left font-normal">
                      <button onClick={toggleAll} className="text-fg3 hover:text-fg">
                        {allOn ? <CheckSquare size={12} /> : <Square size={12} />}
                      </button>
                    </th>
                    <th className="w-[30px] px-1 py-1 text-right font-normal">#</th>
                    <th className="w-[70px] px-2.5 py-1 text-left font-normal">代码</th>
                    <th className="w-[96px] px-2.5 py-1 text-left font-normal">名称</th>
                    <th className="w-[62px] px-2.5 py-1 text-right font-normal">现价</th>
                    <th className="w-[66px] px-2.5 py-1 text-right font-normal">涨幅</th>
                    <th className="w-[56px] px-2.5 py-1 text-right font-normal">涨跌</th>
                    <th className="w-[72px] px-2.5 py-1 text-left font-normal">几天几板</th>
                    <th className="w-[44px] px-2.5 py-1 text-right font-normal">连板</th>
                    <th className="w-[48px] px-2.5 py-1 text-right font-normal">量比</th>
                    <th className="w-[74px] px-2.5 py-1 text-right font-normal">总金额</th>
                    <th className="w-[62px] px-2.5 py-1 text-right font-normal">现均差</th>
                    <th className="w-[92px] px-2.5 py-1 text-left font-normal">所属行业</th>
                    <th className="w-[104px] px-2.5 py-1 text-left font-normal">交易计划</th>
                    <th className="w-[120px] px-2.5 py-1 text-left font-normal">备注</th>
                    <th className="w-[52px] px-2.5 py-1 text-right font-normal">操作</th>
                    <th className="w-full" />
                  </tr>
                </thead>
                <tbody>
                  {items.map((it, idx) => {
                    const qt = quoteOf.get(it.symbol)
                    const ex = extras.data?.items?.[it.symbol]
                    const plan = parsePlan(it.note)
                    const hasPlan = plan.entry != null || plan.stop != null || plan.target != null
                    const pct = qt?.change_pct
                    const price = qt?.price && qt.price > 0 ? qt.price : null
                    const chg =
                      qt?.change != null
                        ? qt.change
                        : price != null && qt?.prev_close
                          ? price - qt.prev_close
                          : null
                    const amountYi = qt?.amount ? qt.amount / 1e8 : null
                    const vwap = qt?.amount && qt?.volume ? qt.amount / (qt.volume * 100) : null
                    const devPct = vwap && price != null ? ((price - vwap) / vwap) * 100 : null
                    const tone = pct == null || pct === 0 ? 'text-fg2' : pct > 0 ? 'text-up' : 'text-down'
                    return (
                      <tr
                        key={it.symbol}
                        className="cursor-pointer border-b border-line/30 transition-colors odd:bg-white/[0.015] hover:bg-brand/[0.10]"
                        onClick={() => nav(`/analysis?symbol=${it.symbol}`)}
                      >
                        <td className="px-2 py-[3px]" onClick={(e) => e.stopPropagation()}>
                          <button onClick={() => toggle(it.symbol)} className="text-fg3 hover:text-fg">
                            {sel.has(it.symbol) ? <CheckSquare size={12} /> : <Square size={12} />}
                          </button>
                        </td>
                        <td className="px-1 py-[3px] text-right font-mono text-[11px] text-fg3">{idx + 1}</td>
                        <td className="px-2.5 py-[3px] text-left font-mono text-[12px] text-fg3">
                          {it.symbol.split('.')[0]}
                        </td>
                        <td className="max-w-[96px] px-2.5 py-[3px] text-left text-fg">
                          <span className="truncate">{it.name || '—'}</span>
                        </td>
                        <td className={`px-2.5 py-[3px] text-right font-mono tabular-nums ${tone}`}>
                          {price != null ? price.toFixed(2) : '—'}
                        </td>
                        <td className={`px-2.5 py-[3px] text-right font-mono tabular-nums ${tone}`}>
                          {pct == null ? '—' : `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`}
                        </td>
                        <td className={`px-2.5 py-[3px] text-right font-mono tabular-nums ${tone}`}>
                          {chg == null ? '—' : `${chg > 0 ? '+' : ''}${chg.toFixed(2)}`}
                        </td>
                        <td className="px-2.5 py-[3px] text-left text-fg3">{ex?.boards_label || '—'}</td>
                        <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                          {ex?.boards_streak ? ex.boards_streak : '—'}
                        </td>
                        <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                          {qt?.vol_ratio ? qt.vol_ratio.toFixed(2) : '—'}
                        </td>
                        <td className="px-2.5 py-[3px] text-right font-mono tabular-nums text-fg2">
                          {amountYi == null
                            ? '—'
                            : amountYi >= 1
                              ? `${amountYi.toFixed(2)}亿`
                              : `${Math.round(amountYi * 10000)}万`}
                        </td>
                        <td className={`px-2.5 py-[3px] text-right font-mono tabular-nums ${devPct == null ? 'text-fg3' : devPct > 0 ? 'text-up' : 'text-down'}`}>
                          {devPct == null ? '—' : `${devPct > 0 ? '+' : ''}${devPct.toFixed(2)}%`}
                        </td>
                        <td className="max-w-[92px] truncate px-2.5 py-[3px] text-left text-fg3" title={ex?.industry || ''}>
                          {ex?.industry || '—'}
                        </td>
                        <td className="px-2.5 py-[3px] text-left">
                          {hasPlan ? (
                            <span className="font-mono text-[11px] text-fg2">
                              {plan.entry != null && `入${plan.entry}`}
                              {plan.stop != null && ` 损${plan.stop}`}
                              {plan.target != null && ` 标${plan.target}`}
                            </span>
                          ) : (
                            <span className="text-[11px] text-fg3">—</span>
                          )}
                        </td>
                        <td className="max-w-[120px] truncate px-2.5 py-[3px] text-left text-[11px] text-fg3">
                          {hasPlan ? plan.note || '—' : it.note || '—'}
                        </td>
                        <td className="px-2.5 py-[3px] text-right" onClick={(e) => e.stopPropagation()}>
                          <div className="flex items-center justify-end gap-1">
                            <button
                              onClick={() => setPlanFor(it)}
                              title="设置交易计划"
                              className="rounded border border-line bg-card p-1 text-fg2 hover:border-brand/50 hover:text-brand"
                            >
                              <Target size={11} />
                            </button>
                            <button
                              onClick={() => {
                                if (confirm(`删除 ${it.symbol}？`)) del.mutate([it.symbol])
                              }}
                              title="删除"
                              className="rounded border border-line bg-card p-1 text-fg3 hover:border-up/50 hover:text-up"
                            >
                              <Trash2 size={11} />
                            </button>
                          </div>
                        </td>
                        <td className="px-2.5 py-[3px]" />
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {adding && (
        <Modal title="添加自选股" subtitle="支持逗号/空格/换行分隔，一次加多只" onClose={() => setAdding(false)} width="max-w-[480px]">
          <div className="space-y-3 p-4">
            <textarea
              value={addText}
              onChange={(e) => setAddText(e.target.value)}
              rows={5}
              placeholder="600519.SH, 000001.SZ&#10;300750.SZ"
              className="w-full resize-none rounded-md border border-line bg-card2 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-brand/60"
            />
            {add.isError && (
              <div className="rounded border border-up/40 bg-up/10 px-3 py-2 text-sm2 text-up">
                {(add.error as Error)?.message}
              </div>
            )}
            <div className="flex gap-2">
              <button
                onClick={() => {
                  const syms = addText
                    .split(/[\s,，;；]+/)
                    .map((s) => s.trim().toUpperCase())
                    .filter(Boolean)
                  if (syms.length) add.mutate(syms)
                }}
                disabled={!addText.trim() || add.isPending}
                className="rounded bg-brand px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
              >
                {add.isPending ? '添加中…' : '添加'}
              </button>
              <button
                onClick={() => setAdding(false)}
                className="rounded border border-line bg-card px-4 py-1.5 text-xs text-fg2 hover:text-fg"
              >
                取消
              </button>
            </div>
          </div>
        </Modal>
      )}

      {planFor && <PlanModal item={planFor} onClose={() => setPlanFor(null)} onSave={(p) => savePlan.mutate({ symbol: planFor.symbol, plan: p })} busy={savePlan.isPending} />}
    </>
  )
}

function PlanModal({
  item,
  onClose,
  onSave,
  busy,
}: {
  item: WatchItem
  onClose: () => void
  onSave: (p: WatchPlan) => void
  busy: boolean
}) {
  const init = parsePlan(item.note)
  const [entry, setEntry] = useState(init.entry != null ? String(init.entry) : '')
  const [stop, setStop] = useState(init.stop != null ? String(init.stop) : '')
  const [target, setTarget] = useState(init.target != null ? String(init.target) : '')
  const [note, setNote] = useState(init.note ?? '')

  const num = (s: string) => {
    const v = Number(s)
    return s.trim() === '' || Number.isNaN(v) ? null : v
  }
  const e = num(entry)
  const s = num(stop)
  const t = num(target)
  const rr = e != null && s != null && t != null && e > s ? (t - e) / (e - s) : null

  return (
    <Modal title="交易计划" subtitle={`${item.symbol} ${item.name ?? ''}`} onClose={onClose} width="max-w-[460px]">
      <div className="space-y-3 p-4">
        <div className="grid grid-cols-3 gap-2">
          <Field label="入场价" value={entry} onChange={setEntry} />
          <Field label="止损价" value={stop} onChange={setStop} />
          <Field label="目标价" value={target} onChange={setTarget} />
        </div>
        {rr != null && (
          <div className={`rounded border px-3 py-1.5 text-sm2 ${rr >= 2 ? 'border-down/40 bg-down/10 text-down' : rr >= 1.5 ? 'border-gold/40 bg-gold/10 text-gold' : 'border-up/40 bg-up/10 text-up'}`}>
            盈亏比 <b>{rr.toFixed(2)}</b>
            {rr < 1.5 && ' · 低于 1.5，长期期望偏低'}
            {rr >= 2 && ' · 不错'}
          </div>
        )}
        <Field label="备注" value={note} onChange={setNote} />
        <div className="flex gap-2 pt-1">
          <button
            onClick={() => onSave({ entry: e, stop: s, target: t, note })}
            disabled={busy}
            className="rounded bg-brand px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
          >
            {busy ? '保存中…' : '保存计划'}
          </button>
          <button
            onClick={onClose}
            className="rounded border border-line bg-card px-4 py-1.5 text-xs text-fg2 hover:text-fg"
          >
            取消
          </button>
        </div>
      </div>
    </Modal>
  )
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs2 text-fg3">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded border border-line bg-card2 px-2 py-1 font-mono text-xs outline-none focus:border-brand/60"
      />
    </label>
  )
}
