import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { BookOpen, RefreshCw, ShieldCheck, ShieldOff, Loader2, Plus, Pencil, Trash2 } from 'lucide-react'
import { api, type StrategyLibItem, type StrategyLibResp } from '../services/api'
import { Card, DataTable, Modal } from '../components/ui'
import { fmtPct } from '../lib/format'
import StrategyEdit from './StrategyEdit'
import StrategyDetail from './StrategyDetail'

/**
 * 策略库 —— 策略启用 / 淘汰的唯一入口。
 *
 * 这里的开关直接改写 `strategies/enabled.json` 并重载引擎：
 * 停用的策略不会被加载，`/api/strategies`、`/api/v1/strategy-ranking`、
 * 今日信号、智能选股全都看不到它。所以这一页是"我让用的才能调用"的落点。
 */

type Tab = 'all' | 'on' | 'retired' | 'unaudited'
/** 单个策略的状态（没有 'all'） */
type Status = Exclude<Tab, 'all'>

const TABS: { key: Tab; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'on', label: '已启用' },
  { key: 'retired', label: '已淘汰' },
  { key: 'unaudited', label: '未审计' },
]

function statusOf(r: StrategyLibItem): Status {
  if (r.enabled) return 'on'
  if (r.disabled_reason) return 'retired'
  return 'unaudited'
}

/** 后端给的是小数（0.147 = 14.7%），fmtPct 收的是百分数，这里统一换算 */
function ratio(v: number | null | undefined, d = 1): string {
  return v == null ? '—' : fmtPct(v * 100, d)
}

export default function StrategyLib() {
  const [tab, setTab] = useState<Tab>('all')
  const [q, setQ] = useState('')
  const [readOnly, setReadOnly] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  // 支持 /app/strategies?open=<id> 直接展开某条策略（分享链接 / 从别处跳进来）
  const [detail, setDetail] = useState<string | null>(() => {
    try {
      return new URLSearchParams(window.location.search).get('open')
    } catch {
      return null
    }
  })
  const [creating, setCreating] = useState(false)
  const [newId, setNewId] = useState('')
  const [newName, setNewName] = useState('')
  const [confirmDel, setConfirmDel] = useState<StrategyLibItem | null>(null)

  /**
   * 优先读 /strategy-lib（新版后端，含 enabled 清单与审计理由）。
   * 旧后端没有这个端点 —— 退回到 strategies + strategy-ranking 拼一份只读视图，
   * 让页面至少能用来看，而不是整页报错。
   */
  const query = useQuery({
    queryKey: ['strategy-lib'],
    refetchOnWindowFocus: false,
    queryFn: async (): Promise<StrategyLibResp> => {
      try {
        const d = await api.strategyLib()
        setReadOnly(false)
        return d
      } catch {
        const [s, r] = await Promise.all([api.strategies(), api.strategyRanking()])
        const rmap = new Map(r.items.map((x) => [x.id, x]))
        const items: StrategyLibItem[] = s.items.map((m) => {
          const x = rmap.get(m.id)
          return {
            ...m,
            enabled: true, // 旧后端只返回已加载的策略，所以能看到的都是已启用
            loaded: true,
            disabled_reason: null,
            rank: x?.rank ?? null,
            return: x?.return ?? null,
            mdd: x?.mdd ?? null,
            sharpe: x?.sharpe ?? null,
            win_rate: x?.win_rate ?? null,
            trades: x?.trades ?? null,
          }
        })
        setReadOnly(true)
        return {
          enabled: items.map((x) => x.id),
          disabled: {},
          note:
            '当前后端是旧版本，只返回已加载的策略（读不到 enabled.json 的淘汰清单）。' +
            '重新打包后端后可看到「已淘汰 / 未审计」与开关。',
          loaded_count: items.length,
          stats: {
            total: items.length,
            enabled: items.length,
            retired: 0,
            unaudited: 0,
            loaded: items.length,
          },
          items,
        }
      }
    },
  })

  const toggle = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.toggleStrategy(v.id, v.enabled),
    onSuccess: () => query.refetch(),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteStrategy(id),
    onSuccess: () => {
      setConfirmDel(null)
      query.refetch()
    },
  })

  const create = useMutation({
    mutationFn: (v: { id: string; name: string }) => api.createStrategy(v.id, v.name),
    onSuccess: () => {
      setCreating(false)
      setNewId('')
      setNewName('')
      query.refetch()
    },
  })

  const data = query.data
  const items = data?.items ?? []

  const counts = useMemo(() => {
    const c = { on: 0, retired: 0, unaudited: 0 }
    for (const r of items) c[statusOf(r)] += 1
    return c
  }, [items])

  const rows = useMemo(() => {
    const kw = q.trim().toLowerCase()
    return items.filter((r) => {
      if (tab !== 'all' && statusOf(r) !== tab) return false
      if (!kw) return true
      return (
        r.id.toLowerCase().includes(kw) ||
        r.name.toLowerCase().includes(kw) ||
        (r.tags ?? []).some((t) => t.toLowerCase().includes(kw))
      )
    })
  }, [items, tab, q])

  if (query.isError) {
    return (
      <>
        <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
          <h2 className="page-title">策略库</h2>
        </header>
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="max-w-lg rounded-xl border border-up/40 bg-card p-7 text-center">
            <ShieldOff size={28} className="mx-auto mb-3 text-up" strokeWidth={1.6} />
            <h3 className="card-title">读不到策略库</h3>
            <p className="mt-2 text-xs leading-relaxed text-fg2">
              {(query.error as Error)?.message}
            </p>
            <p className="mt-3 text-sm2 leading-relaxed text-fg3">
              这个页面依赖后端 <code className="text-fg2">/api/v1/strategy-lib</code>。
              如果后端还是旧版本，需要重新打包（build.ps1）后才会生效。
            </p>
          </div>
        </div>
      </>
    )
  }

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <BookOpen size={15} strokeWidth={1.9} className="text-brand" />
        <h2 className="page-title">策略库</h2>
        <span className="font-mono text-xs2 text-fg3">
          {data ? `启用 ${data.stats.enabled} / 共 ${data.stats.total}` : '加载中'}
        </span>
        {data && (
          <span className="ml-1 rounded border border-line2 px-1.5 py-px font-mono text-xs2 text-fg3">
            引擎已加载 {data.loaded_count}
          </span>
        )}
        {readOnly && (
          <span
            className="rounded border border-gold/40 bg-gold/10 px-2 py-0.5 text-xs2 text-gold"
            title="后端是旧版本，读不到 enabled.json；重新打包后可用"
          >
            只读 · 后端未更新
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索 id / 名称 / 标签"
            className="w-56 rounded-md border border-line bg-card px-2.5 py-1 text-sm2 text-fg outline-none placeholder:text-fg3 focus:border-brand/60"
          />
          <button
            onClick={() => setCreating(true)}
            disabled={readOnly}
            className="flex items-center gap-1.5 rounded-md border border-brand/40 bg-brand/15 px-2.5 py-1 text-sm2 text-brand hover:bg-brand/25 disabled:opacity-40"
          >
            <Plus size={12} />
            新建策略
          </button>
          <button
            onClick={() => query.refetch()}
            className="flex items-center gap-1.5 rounded-md border border-line bg-card px-2.5 py-1 text-sm2 text-fg2 hover:border-line2 hover:text-fg"
          >
            <RefreshCw size={12} className={query.isFetching ? 'animate-spin' : ''} />
            刷新
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-3.5">
        {/* KPI */}
        <div className="mb-3 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
          <Kpi label="已启用（参与下游）" value={counts.on} tone="text-brand" />
          <Kpi label="已淘汰（有审计理由）" value={counts.retired} tone="text-up" />
          <Kpi label="未审计" value={counts.unaudited} tone="text-fg2" />
          <Kpi label="回测有排名" value={items.filter((r) => r.rank != null).length} tone="text-gold" />
        </div>

        <Card
          title="策略清单"
          subtitle={data?.updated ? `清单更新 ${data.updated}` : undefined}
          right={
            <div className="flex items-center gap-1">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={`rounded px-2 py-0.5 font-mono text-xs2 transition-colors ${
                    tab === t.key ? 'bg-brand/20 text-brand' : 'text-fg3 hover:text-fg2'
                  }`}
                >
                  {t.label}
                  {t.key === 'on' && ` ${counts.on}`}
                  {t.key === 'retired' && ` ${counts.retired}`}
                  {t.key === 'unaudited' && ` ${counts.unaudited}`}
                </button>
              ))}
            </div>
          }
          padded={false}
        >
          <DataTable
            rows={rows}
            rowKey={(r) => r.id}
            onRowClick={(r) => setDetail(r.id)}
            empty={query.isLoading ? '加载中…' : '没有匹配的策略'}
            columns={[
              {
                key: 'rank',
                label: '#',
                render: (r) => <span className="text-fg3">{r.rank ?? '—'}</span>,
              },
              {
                key: 'name',
                label: '策略',
                align: 'left',
                render: (r) => (
                  <div className="min-w-[190px]">
                    <div className="flex items-center gap-1.5">
                      {/* 英文 ID 不占行：挪到 title，需要时悬停看 */}
                      <b className="text-xs" title={r.id}>
                        {r.name}
                      </b>
                      <span title={r.loaded ? '引擎已加载' : '引擎未加载'}>
                        {r.loaded ? (
                          <ShieldCheck size={11} className="text-down" />
                        ) : (
                          <ShieldOff size={11} className="text-fg3" />
                        )}
                      </span>
                    </div>
                  </div>
                ),
              },
              {
                key: 'tags',
                label: '标签',
                align: 'left',
                render: (r) => (
                  <div className="flex flex-nowrap gap-1">
                    {(r.tags ?? []).map((t) => (
                      <span
                        key={t}
                        className="rounded border border-line2 px-1.5 py-px text-xs2 text-fg3"
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                ),
              },
              {
                key: 'return',
                label: '收益',
                render: (r) =>
                  r.return == null ? (
                    <span className="text-fg3">—</span>
                  ) : (
                    <span className={r.return >= 0 ? 'text-up' : 'text-down'}>
                      {ratio(r.return)}
                    </span>
                  ),
              },
              {
                key: 'mdd',
                label: '回撤',
                render: (r) =>
                  r.mdd == null ? <span className="text-fg3">—</span> : ratio(r.mdd),
              },
              {
                key: 'sharpe',
                label: '夏普',
                render: (r) => (r.sharpe == null ? '—' : r.sharpe.toFixed(2)),
              },
              {
                key: 'win_rate',
                label: '胜率',
                render: (r) => (r.win_rate == null ? '—' : ratio(r.win_rate, 0)),
              },
              {
                key: 'trades',
                label: '交易',
                render: (r) => <span className="num text-fg2">{r.trades ?? '—'}</span>,
              },
              {
                key: 'status',
                label: '状态',
                align: 'left',
                render: (r) => {
                  const s = statusOf(r)
                  const map = {
                    on: 'bg-brand/15 text-brand',
                    retired: 'bg-up/15 text-up',
                    unaudited: 'bg-white/10 text-fg2',
                  }
                  const label = { on: '已启用', retired: '已淘汰', unaudited: '未审计' }
                  return (
                    <span className={`rounded px-1.5 py-px font-mono text-[11px] ${map[s]}`}>
                      {label[s]}
                    </span>
                  )
                },
              },
              {
                key: 'reason',
                label: '审计结论',
                align: 'left',
                render: (r) =>
                  r.disabled_reason ? (
                    <span className="block max-w-[320px] truncate text-[11px] text-fg2" title={r.disabled_reason}>
                      {r.disabled_reason}
                    </span>
                  ) : r.enabled ? (
                    <span className="text-[11px] text-fg2">参与下游</span>
                  ) : (
                    <span className="text-[11px] text-fg2">未参与审计</span>
                  ),
              },
              {
                key: 'act',
                label: '操作',
                align: 'left',
                render: (r) => {
                  const busy = toggle.isPending && toggle.variables?.id === r.id
                  if (readOnly) {
                    return <span className="text-xs2 text-fg3">—</span>
                  }
                  return (
                    <div className="flex items-center gap-1">
                      <button
                        disabled={busy}
                        onClick={(e) => { e.stopPropagation(); toggle.mutate({ id: r.id, enabled: !r.enabled }) }}
                        className={`flex items-center gap-1 rounded px-2 py-0.5 text-xs2 transition-colors ${
                          r.enabled
                            ? 'border border-up/40 bg-up/10 text-up hover:bg-up/20'
                            : 'border border-brand/40 bg-brand/10 text-brand hover:bg-brand/20'
                        } disabled:opacity-40`}
                      >
                        {busy && <Loader2 size={10} className="animate-spin" />}
                        {r.enabled ? '停用' : '启用'}
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setEditing(r.id) }}
                        title="改名称 / 描述 / 参数"
                        className="rounded border border-line bg-card p-1 text-fg2 hover:border-line2 hover:text-fg"
                      >
                        <Pencil size={11} />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setConfirmDel(r) }}
                        title="删除（移到回收站，可恢复）"
                        className="rounded border border-line bg-card p-1 text-fg3 hover:border-up/50 hover:text-up"
                      >
                        <Trash2 size={11} />
                      </button>
                    </div>
                  )
                },
              },
            ]}
          />
        </Card>

        {toggle.isError && (
          <div className="mt-2 rounded-md border border-up/40 bg-up/10 px-3 py-2 text-sm2 text-up">
            操作失败：{(toggle.error as Error)?.message}
          </div>
        )}

        {/* 审计说明 */}
        {data?.note && (
          <Card title="这份清单怎么来的" className="mt-3">
            <p className="text-sm2 leading-relaxed text-fg2">{data.note}</p>
            {data.method && (
              <p className="mt-2 text-xs2 leading-relaxed text-fg3">方法：{data.method}</p>
            )}
            {data.random_baseline && (
              <div className="mt-3 grid grid-cols-3 gap-2">
                {(['train', 'valid', 'holdout'] as const).map((k) => {
                  const b = data.random_baseline?.[k]
                  if (!b) return null
                  const label = { train: '训练 2021-2023', valid: '验证 2024-2025', holdout: '留出 2026' }[k]
                  return (
                    <div key={k} className="rounded-md bg-card2 px-2.5 py-2">
                      <div className="text-xs2 text-fg3">{label} · 随机对照</div>
                      <div className="num mt-0.5 text-sm2 text-fg2">
                        均值 {ratio(b.mean)} · 区间 [{ratio(b.lo)}, {ratio(b.hi)}]
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </Card>
        )}
      </div>

      {editing && (
        <StrategyEdit
          id={editing}
          onClose={() => setEditing(null)}
          onSaved={() => query.refetch()}
        />
      )}

      {detail && !editing && (
        <StrategyDetail
          id={detail}
          onClose={() => setDetail(null)}
          onEdit={(id) => {
            setDetail(null)
            setEditing(id)
          }}
        />
      )}

      {creating && (
        <Modal title="新建策略" subtitle="生成骨架后可在编辑里改条件" onClose={() => setCreating(false)} width="max-w-[460px]">
          <div className="space-y-3 p-4">
            <label className="block">
              <span className="mb-1 block text-xs2 text-fg3">策略 ID（英文、数字、下划线）</span>
              <input
                value={newId}
                onChange={(e) => setNewId(e.target.value)}
                placeholder="my_strategy"
                className="w-full rounded-md border border-line bg-card2 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-brand/60"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs2 text-fg3">显示名称</span>
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="我的策略"
                className="w-full rounded-md border border-line bg-card2 px-2.5 py-1.5 text-xs outline-none focus:border-brand/60"
              />
            </label>
            {create.isError && (
              <div className="rounded-md border border-up/40 bg-up/10 px-3 py-2 text-sm2 text-up">
                {(create.error as Error)?.message}
              </div>
            )}
            <div className="flex gap-2 pt-1">
              <button
                onClick={() => create.mutate({ id: newId.trim(), name: newName.trim() })}
                disabled={!newId.trim() || !newName.trim() || create.isPending}
                className="rounded-md bg-brand px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
              >
                {create.isPending ? '创建中…' : '创建'}
              </button>
              <button
                onClick={() => setCreating(false)}
                className="rounded-md border border-line bg-card px-4 py-1.5 text-xs text-fg2 hover:text-fg"
              >
                取消
              </button>
            </div>
          </div>
        </Modal>
      )}

      {confirmDel && (
        <Modal title="删除策略" subtitle={confirmDel.id} onClose={() => setConfirmDel(null)} width="max-w-[420px]">
          <div className="p-4">
            <p className="text-xs leading-relaxed text-fg2">
              确定删除 <b className="text-fg">{confirmDel.name}</b> 吗？
            </p>
            <p className="mt-2 text-sm2 leading-relaxed text-fg3">
              文件会移到 <code>_retired/</code> 目录，不是物理删除 —— 需要时可以恢复。
              同时会从启用清单移除。
            </p>
            {remove.isError && (
              <div className="mt-3 rounded-md border border-up/40 bg-up/10 px-3 py-2 text-sm2 text-up">
                {(remove.error as Error)?.message}
              </div>
            )}
            <div className="mt-4 flex gap-2">
              <button
                onClick={() => remove.mutate(confirmDel.id)}
                disabled={remove.isPending}
                className="rounded-md bg-up px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
              >
                {remove.isPending ? '删除中…' : '确认删除'}
              </button>
              <button
                onClick={() => setConfirmDel(null)}
                className="rounded-md border border-line bg-card px-4 py-1.5 text-xs text-fg2 hover:text-fg"
              >
                取消
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
  )
}

function Kpi({ label, value, tone = '' }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-[9px] border border-line bg-card px-3 py-2.5">
      <div className={`num text-lg font-semibold leading-tight ${tone}`}>{value}</div>
      <div className="mt-0.5 text-xs2 text-fg3">{label}</div>
    </div>
  )
}
