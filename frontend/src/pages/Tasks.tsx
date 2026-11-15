import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Play, RefreshCw, XCircle } from 'lucide-react'
import { api, type TaskItem } from '../services/api'
import { Card, DataTable, Metric } from '../components/ui'

const TYPE_LABEL: Record<string, string> = {
  backtest: '账户回测',
  scan: '全市场扫描',
  grid: '参数网格',
}

const STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: '排队', cls: 'text-fg3' },
  running: { label: '运行中', cls: 'text-gold' },
  done: { label: '完成', cls: 'text-down' },
  failed: { label: '失败', cls: 'text-up' },
  cancelled: { label: '已取消', cls: 'text-fg3' },
}

export default function Tasks() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [type, setType] = useState<'scan' | 'backtest' | 'grid'>('scan')

  const list = useQuery({
    queryKey: ['tasks', 'full'],
    queryFn: () => api.tasks(50),
    refetchInterval: 2_000,
  })

  const detail = useQuery({
    queryKey: ['task', selected],
    queryFn: () => api.task(selected!),
    enabled: !!selected,
    refetchInterval: (q) => {
      const s = q.state.data?.status
      return s === 'running' || s === 'pending' ? 1_500 : false
    },
  })

  const submit = useMutation({
    mutationFn: () => {
      const params =
        type === 'scan'
          ? { strategy: 'classic_week52_high', limit: 20 }
          : type === 'backtest'
            ? {
                strategy: 'classic_reversal',
                risk_amount: 3000,
                max_positions: 10,
                max_position_pct: 0.25,
                stop_loss_pct: 0.12,
                max_hold_days: 5,
                start: '2020-01-01',
              }
            : { strategy: 'classic_reversal', risk_amount: 3000, stops: [0.08, 0.12], holds: [5, 10], caps: [0.25, 0.5] }
      return api.submit(type, params)
    },
    onSuccess: (d) => {
      setSelected(d.task_id)
      qc.invalidateQueries({ queryKey: ['tasks'] })
    },
  })

  const items: TaskItem[] = list.data?.items ?? []
  const counts = items.reduce<Record<string, number>>((acc, t) => {
    acc[t.status] = (acc[t.status] ?? 0) + 1
    return acc
  }, {})

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">任务队列</h2>
        <span className="font-mono text-xs2 text-fg3">子进程执行 · 不阻塞页面 · SQLite 持久化</span>
        <div className="ml-auto flex items-center gap-2">
          <select
            value={type}
            onChange={(e) => setType(e.target.value as typeof type)}
            className="rounded border border-line2 bg-card2 px-2 py-1.5 font-mono text-sm2 outline-none focus:border-brand"
          >
            <option value="scan">全市场扫描</option>
            <option value="backtest">账户回测</option>
            <option value="grid">参数网格</option>
          </select>
          <button
            onClick={() => submit.mutate()}
            disabled={submit.isPending}
            className="flex items-center gap-1.5 rounded-lg bg-brand px-3.5 py-1.5 text-sm2 font-medium text-white disabled:opacity-50"
          >
            <Play size={12} /> 提交任务
          </button>
          <button
            onClick={() => list.refetch()}
            className="rounded-lg border border-line2 bg-card2 p-1.5 text-fg2 hover:text-fg"
            title="刷新"
          >
            <RefreshCw size={13} />
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-12 gap-3">
          <Card className="col-span-12" title="概览" subtitle="最近 50 条">
            <div className="grid grid-cols-5 gap-2">
              <Metric label="总数" value={String(items.length)} />
              <Metric label="运行中" value={String(counts.running ?? 0)} tone="text-gold" />
              <Metric label="完成" value={String(counts.done ?? 0)} tone="text-down" />
              <Metric label="失败" value={String(counts.failed ?? 0)} tone="text-up" />
              <Metric label="排队" value={String(counts.pending ?? 0)} />
            </div>
          </Card>

          <Card className="col-span-12" title="任务列表" subtitle="点击任意一行看详情" padded={false}>
            <div className="max-h-[calc(100vh-300px)] overflow-y-auto">
              <DataTable<TaskItem>
                rows={items}
                rowKey={(r) => r.id}
                onRowClick={(r) => setSelected(r.id)}
                empty={list.isLoading ? '加载中…' : '还没有任务，点右上角「提交任务」'}
                columns={[
                  {
                    key: 'id',
                    label: 'ID',
                    render: (r) => (
                      <span className={`font-mono ${selected === r.id ? 'text-brand' : 'text-fg2'}`}>
                        {r.id.slice(0, 8)}
                      </span>
                    ),
                  },
                  { key: 'type', label: '类型', render: (r) => <span className="text-fg3">{TYPE_LABEL[r.type] ?? r.type}</span> },
                  {
                    key: 'progress',
                    label: '进度',
                    render: (r) => (
                      <div className="flex items-center gap-1.5">
                        <div className="h-[5px] w-16 overflow-hidden rounded bg-card2">
                          <div
                            className={`h-full ${r.status === 'failed' ? 'bg-up' : r.status === 'done' ? 'bg-down' : 'bg-brand'}`}
                            style={{ width: `${Math.round(r.progress * 100)}%` }}
                          />
                        </div>
                        <span className="w-8 text-right text-xs2 text-fg3">{Math.round(r.progress * 100)}%</span>
                      </div>
                    ),
                  },
                  {
                    key: 'status',
                    label: '状态',
                    render: (r) => (
                      <span className={STATUS[r.status]?.cls}>{STATUS[r.status]?.label ?? r.status}</span>
                    ),
                  },
                  { key: 'created', label: '创建', render: (r) => <span className="text-fg3">{r.created_at.slice(5)}</span> },
                ]}
              />
            </div>
          </Card>

          {/* 详情抽屉：只在选中任务时出现，不再常驻一张空卡 */}
          {selected && (
            <div className="fixed inset-0 z-50">
              <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={() => setSelected(null)} />
              <aside className="absolute inset-y-0 right-0 flex w-[560px] max-w-full flex-col border-l border-line bg-card shadow-2xl">
                <div className="flex shrink-0 items-center gap-2 border-b border-line px-4 py-2.5">
                  <span className="card-title">任务详情</span>
                  <span className="font-mono text-xs2 text-fg3">{selected.slice(0, 8)}</span>
                  {detail.data && (
                    <span className={`text-xs2 ${STATUS[detail.data.status]?.cls}`}>
                      {STATUS[detail.data.status]?.label}
                    </span>
                  )}
                  <button
                    onClick={() => setSelected(null)}
                    className="ml-auto rounded px-2 py-0.5 text-fg3 hover:bg-white/5 hover:text-fg"
                    aria-label="关闭"
                  >
                    ×
                  </button>
                </div>
                <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
                  {!detail.data ? (
                    <div className="py-10 text-center text-sm2 text-fg3">加载中…</div>
                  ) : (
                    <>
                      <div className="space-y-2 text-sm2">
                        <Row label="类型" value={TYPE_LABEL[detail.data.type] ?? detail.data.type} />
                        <Row label="进度" value={`${Math.round((detail.data.progress ?? 0) * 100)}%`} />
                        <Row label="创建" value={detail.data.created_at} />
                        <Row label="开始" value={detail.data.started_at ?? '—'} />
                        <Row label="结束" value={detail.data.finished_at ?? '—'} />
                      </div>
                      {detail.data.error && (
                        <div className="rounded-md border border-up/30 bg-up/10 px-2.5 py-2 text-sm2 text-up">
                          <XCircle size={11} className="mr-1 inline" />
                          {detail.data.error}
                        </div>
                      )}
                      {detail.data.log && (
                        <div>
                          <div className="mb-1 text-xs2 text-fg3">执行日志（最后 200 行）</div>
                          <pre className="max-h-[320px] overflow-auto whitespace-pre-wrap rounded-md bg-card2 p-3 font-mono text-[11px] leading-relaxed text-fg2">
                            {detail.data.log}
                          </pre>
                        </div>
                      )}
                      {detail.data.result && (
                        <div>
                          <div className="mb-1 text-xs2 text-fg3">结果</div>
                          <pre className="max-h-[320px] overflow-auto whitespace-pre-wrap rounded-md bg-card2 p-3 font-mono text-[11px] leading-relaxed text-fg2">
                            {JSON.stringify(detail.data.result, null, 2)}
                          </pre>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </aside>
            </div>
          )}
        </div>
      </div>
    </>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-fg3">{label}</span>
      <span className="num text-fg2">{value}</span>
    </div>
  )
}
