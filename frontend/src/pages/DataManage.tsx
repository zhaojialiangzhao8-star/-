import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Database, HardDrive, RefreshCw } from 'lucide-react'
import { api } from '../services/api'
import { Card, DataTable, Metric } from '../components/ui'
import { fmtInt } from '../lib/format'

interface Generation {
  generation: string
  rows: number
  updated_at?: string
  updated_text?: string
  path?: string
  is_current?: boolean
}

const CAP_LABEL: Record<string, string> = {
  daily: '日线',
  minute: '分钟线',
  snapshot: '快照',
  index: '指数',
}

export default function DataManage() {
  // AI 设置：Key 写进 SQLite，下次请求立刻生效
  const [aiKey, setAiKey] = useState('')
  const [savingKey, setSavingKey] = useState(false)
  const aiStatus = useQuery({
    queryKey: ['aiStatus'],
    queryFn: api.aiStatus,
    refetchInterval: 30_000,
    retry: 0,
  })

  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  const audit = useQuery({ queryKey: ['data-audit'], queryFn: api.dataAudit, staleTime: 120_000, retry: 0 })
  const gens = useQuery({
    queryKey: ['generations'],
    queryFn: () => fetch('/api/v1/generations').then((r) => r.json()),
    refetchInterval: 60_000,
  })
  const sources = useQuery({
    queryKey: ['data-sources'],
    queryFn: () =>
      fetch('/api/v1/data-sources').then((r) => r.json()) as Promise<{
        providers: Record<
          string,
          { capabilities: string[]; max_daily_bars: number; max_minute_bars: number; adjust_support: string[]; note: string }
        >
        routing: Record<string, { providers: string[]; preferred: string | null }>
      }>,
    staleTime: 300_000,
  })

  const items: Generation[] = Array.isArray(gens.data) ? gens.data : (gens.data?.items ?? [])
  const current = health.data?.generation
  const src = sources.data

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">数据管理</h2>
        <span className="font-mono text-xs2 text-fg3">Parquet 代际 · 原子切换 · fail-closed 读取</span>
        <div className="ml-auto">
          <button
            onClick={() => gens.refetch()}
            className="flex items-center gap-1.5 rounded-lg border border-line2 bg-card2 px-3 py-1.5 text-sm2 text-fg2 hover:text-fg"
          >
            <RefreshCw size={12} /> 刷新
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-12 gap-3">
          <Card className="col-span-12" title="数据完整性" subtitle="个股库 / 名称表 / 板块成分 对不对得齐">
            <div className="grid grid-cols-6 gap-2">
              <Metric label="个股库" value={audit.data ? String(audit.data.stocks.in_store) : '—'} />
              <Metric label="名称表" value={audit.data ? String(audit.data.stocks.in_names) : '—'} />
              <Metric
                label="库内缺失"
                value={audit.data ? String(audit.data.stocks.missing_in_store) : '—'}
                tone={(audit.data?.stocks.missing_in_store ?? 0) > 0 ? 'text-up' : ''}
              />
              <Metric label="板块数" value={audit.data ? String(audit.data.boards.total) : '—'} />
              <Metric
                label="板块成分缺失"
                value={audit.data ? String(audit.data.boards.members_missing) : '—'}
                tone={(audit.data?.boards.members_missing ?? 0) > 0 ? 'text-gold' : ''}
              />
              <Metric
                label="成分≤2 的板块"
                value={audit.data ? String(audit.data.boards.thin_count) : '—'}
                tone={(audit.data?.boards.thin_count ?? 0) > 0 ? 'text-gold' : ''}
              />
            </div>
            {(audit.data?.stocks.missing_in_store ?? 0) > 0 && (
              <div className="mt-2 text-[11px] leading-relaxed text-fg3">
                <span className="text-up">名称表里有、个股库里没有：</span>
                {(audit.data?.stocks.missing_preview ?? []).slice(0, 12).map((m) => (
                  <span key={m.symbol} className="mr-2 font-mono">
                    {m.name || m.symbol}({m.symbol.split('.')[0]})
                  </span>
                ))}
                {(audit.data?.stocks.missing_in_store ?? 0) > 12 && <span>… 共 {audit.data?.stocks.missing_in_store} 只</span>}
                <div className="mt-0.5">
                  K 线根数：最少 {audit.data?.stocks.bars_min} · 中位 {audit.data?.stocks.bars_median} · 最新交易日{' '}
                  {audit.data?.stocks.latest_date}（滞后 {audit.data?.stocks.stale_today} 只）
                </div>
              </div>
            )}
            {(audit.data?.boards.thin_count ?? 0) > 0 && (
              <div className="mt-1.5 text-[11px] leading-relaxed text-fg3">
                <span className="text-gold">成分不足 3 只的板块（指数参考性弱）：</span>
                {(audit.data?.boards.thin_preview ?? []).map((t) => (
                  <span key={t.board} className="mr-2">
                    {t.board}({t.in_store}/{t.total})
                  </span>
                ))}
              </div>
            )}
          </Card>

          <Card className="col-span-12" title="当前数据" subtitle="来自 /api/v1/health">
            <div className="grid grid-cols-5 gap-2">
              <Metric label="总行数" value={health.data ? fmtInt(health.data.rows) : '—'} />
              <Metric label="覆盖标的" value={health.data?.symbols ? health.data.symbols.toLocaleString() : '—'} />
              <Metric label="交易日" value={health.data?.days ? health.data.days.toLocaleString() : '—'} />
              <Metric label="策略插件" value={String(health.data?.strategies ?? '—')} />
              <Metric
                label="当前代际"
                value={<span className="text-xs">{current?.slice(0, 8) ?? '—'}</span>}
              />
            </div>
            {/* 数据过期提醒：所有 parquet 派生的数字都停在 date_end，必须显式告警 */}
            {health.data?.date_end && (
              <div
                className={`mt-2 flex flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 text-[11px] ${
                  health.data.stale
                    ? 'border-up/40 bg-up/10 text-up'
                    : 'border-line2 bg-card2 text-fg2'
                }`}
              >
                <span className="font-medium">
                  {health.data.stale
                    ? `本地数据已落后 ${health.data.stale_days} 天`
                    : '本地数据新鲜'}
                </span>
                <span className="font-mono text-fg3">
                  最新交易日 {health.data.date_end} · 代际 {current?.slice(0, 8) ?? '—'}
                </span>
                <span className="text-fg3">
                  K 线尾巴 / 涨跌分布 / 情绪 / 连板梯队 / 回测 / 板块指数都基于这天的快照；
                  只有「现价」走实时行情。
                </span>
                {health.data.stale && (
                  <code className="ml-auto rounded bg-black/30 px-1.5 py-0.5 font-mono text-fg2">
                    python scripts/gen_data.py
                  </code>
                )}
              </div>
            )}
          </Card>

            {/* AI 设置 */}
            <Card
              className="col-span-12"
              title="AI 设置"
              subtitle="DeepSeek · 用于个股分析与回测解读"
              right={
                <span className={`font-mono text-xs2 ${aiStatus.data?.configured ? 'text-down' : 'text-gold'}`}>
                  {aiStatus.data?.configured ? '● 已配置' : '○ 未配置'}
                </span>
              }
            >
              <div className="space-y-2">
                <div className="text-sm2 leading-relaxed text-fg3">
                  在{' '}
                  <a
                    href="https://platform.deepseek.com/api_keys"
                    target="_blank"
                    rel="noreferrer"
                    className="text-cyan hover:underline"
                  >
                    platform.deepseek.com/api_keys
                  </a>{' '}
                  申请，`sk-` 开头。写进本地 SQLite，不落日志、不外传。
                  {aiStatus.data?.key_preview ? (
                    <span className="ml-1 text-fg2">当前：{aiStatus.data.key_preview}</span>
                  ) : null}
                </div>
                <div className="flex gap-1.5">
                  <input
                    type="password"
                    value={aiKey}
                    onChange={(e) => setAiKey(e.target.value)}
                    placeholder="sk-xxxxxxxxxxxxxxxxxxxxxxxx"
                    className="flex-1 rounded border border-line2 bg-card2 px-2 py-1.5 font-mono text-sm2 outline-none placeholder:text-fg3 focus:border-brand"
                  />
                  <button
                    onClick={async () => {
                      setSavingKey(true)
                      try {
                        await api.aiSetKey(aiKey.trim())
                        setAiKey('')
                        await aiStatus.refetch()
                      } finally {
                        setSavingKey(false)
                      }
                    }}
                    disabled={savingKey || !aiKey.trim()}
                    className="rounded bg-brand px-3 py-1.5 text-sm2 font-medium text-white disabled:opacity-40"
                  >
                    {savingKey ? '保存中…' : '保存'}
                  </button>
                  {aiStatus.data?.configured && (
                    <button
                      onClick={async () => {
                        await api.aiSetKey('')
                        await aiStatus.refetch()
                      }}
                      className="rounded border border-line2 bg-card2 px-2.5 py-1.5 text-sm2 text-fg3 hover:text-up"
                    >
                      清除
                    </button>
                  )}
                </div>
                <div className="text-xs2 text-fg3">
                  保存后立刻生效，不用重启。也可以设环境变量{' '}
                  <code className="rounded bg-white/5 px-1">FACET_DEEPSEEK_KEY</code>（需重启）。
                </div>
              </div>
            </Card>

          <Card className="col-span-12" title="数据源能力矩阵" subtitle="按能力路由，策略不直接依赖供应商">
            {src ? (
              <>
                <div className="grid grid-cols-2 gap-3">
                  {Object.entries(src.providers).map(([name, p]) => (
                    <div key={name} className="rounded-md bg-card2 px-3 py-2.5">
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-xs text-fg">{name}</span>
                        <span className="flex gap-1">
                          {p.capabilities.map((c) => (
                            <span key={c} className="rounded bg-brand/15 px-1.5 py-px text-3xs text-brand">
                              {CAP_LABEL[c] ?? c}
                            </span>
                          ))}
                        </span>
                      </div>
                      <div className="mt-1.5 flex gap-4 font-mono text-xs2 text-fg3">
                        <span>日线 ≤ {p.max_daily_bars || '—'}</span>
                        <span>分钟 ≤ {p.max_minute_bars || '—'}</span>
                        <span>复权 {p.adjust_support.join('/')}</span>
                      </div>
                      <div className="mt-1 text-xs2 leading-relaxed text-fg3">{p.note}</div>
                    </div>
                  ))}
                </div>
                <div className="mt-3 rounded-md bg-card2 px-3 py-2 text-sm2">
                  <span className="text-fg3">路由：</span>
                  {Object.entries(src.routing).map(([cap, r]) => (
                    <span key={cap} className="mr-4 inline-block">
                      <span className="text-fg3">{CAP_LABEL[cap] ?? cap}</span> →{' '}
                      <span className="text-cyan">{r.preferred ?? '无'}</span>
                    </span>
                  ))}
                </div>
              </>
            ) : (
              <div className="py-6 text-center text-sm2 text-fg3">加载中…</div>
            )}
          </Card>

          <Card className="col-span-8" title="代际列表" subtitle={`${items.length} 个`} padded={false}>
            <DataTable<Generation>
              rows={items}
              rowKey={(r) => r.generation ?? r.path ?? String(Math.random())}
              empty={gens.isLoading ? '加载中…' : '暂无代际'}
              columns={[
                {
                  key: 'gen',
                  label: '代际 ID',
                  render: (r) => (
                    <span className="flex items-center gap-1.5">
                      <span className="font-mono text-fg2">{(r.generation ?? r.path ?? '').slice(0, 8)}</span>
                      {(r.generation ?? '').startsWith(current ?? '___') && (
                        <span className="rounded bg-down/15 px-1 text-3xs text-down">当前</span>
                      )}
                    </span>
                  ),
                },
                { key: 'rows', label: '行数', render: (r) => (r.rows != null ? fmtInt(r.rows) : '—') },
                {
                  key: 'updated',
                  label: '更新时间',
                  render: (r) => <span className="text-fg3">{r.updated_text ?? r.updated_at ?? '—'}</span>,
                },
              ]}
            />
          </Card>

          <div className="col-span-4 space-y-3">
            <Card title="数据来源" subtitle="采集脚本">
              <div className="space-y-2 text-sm2 leading-relaxed">
                <Line k="日线" v="东方财富 push2his" />
                <Line k="复权" v="后复权 fqt=2（前复权会产生负价格）" />
                <Line k="基准" v="沪深300 / 上证 / 中证500 / 创业板指" />
                <Line k="范围" v={health.data?.date_start ? `${health.data.date_start} ~ ${health.data.date_end}` : '—'} />
                <Line k="标的" v={health.data?.symbols ? `${health.data.symbols.toLocaleString()} 只（可交易池）` : '—'} />
              </div>
            </Card>

            <Card title="采集与重建" subtitle="命令行">
              <pre className="overflow-x-auto rounded bg-card2 p-2.5 font-mono text-xs2 leading-relaxed text-fg2">{`# 全量拉取（约 20 秒）
python scripts/fetch_history.py \\
  --limit 700 --bars 5000 --fqt 2

# 基准指数
python scripts/fetch_benchmark.py

# 重建派生列（发布新代际）
curl -X POST localhost:3021/api/v1/strategies/reload`}</pre>
              <div className="mt-2 text-xs2 leading-relaxed text-fg3">
                代际切换是原子的：写 <code className="rounded bg-white/5 px-1">gens/&lt;uuid&gt;/</code> +
                <code className="rounded bg-white/5 px-1">_SUCCESS</code> → 切
                <code className="rounded bg-white/5 px-1">CURRENT.json</code>。
                读者拿不到稳定代际会报 503，不会返回空数据。
              </div>
            </Card>

            <Card title="清理策略">
              <div className="text-sm2 leading-relaxed text-fg2">
                每代约 200 MB。<code className="rounded bg-white/5 px-1">KEEP_GENERATIONS = 1</code>
                <br />
                保留当前 + 1 个旧代际（用于回滚），其余在发布时自动删除。
              </div>
            </Card>
          </div>
        </div>
      </div>
    </>
  )
}

function Line({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="shrink-0 text-fg3">{k}</span>
      <span className="text-right text-fg2">{v}</span>
    </div>
  )
}
