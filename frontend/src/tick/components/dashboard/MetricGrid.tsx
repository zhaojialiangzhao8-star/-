import { useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { MiniMetric } from './shared'
import { api } from '../../../services/api'

export interface MetricCell {
  label: string
  value: string
  cls?: string
  /** 传了就能点：弹出该指标命中的个股 */
  metric?: string
}

/**
 * 趋势强度 / 实用监控的指标格子：每个格子可点，弹出命中个股 → 点个股进分析。
 * 弹窗走 portal：看板格子是 react-grid-item（带 transform），fixed 会相对它定位。
 */
export function MetricGrid({ items }: { items: MetricCell[] }) {
  const [metric, setMetric] = useState<{ key: string; label: string } | null>(null)
  const navigate = useNavigate()

  const q = useQuery({
    queryKey: ['metric-members', metric?.key],
    queryFn: () => api.metricMembers(metric!.key, 200),
    enabled: !!metric,
    retry: 0,
    staleTime: 30_000,
  })

  return (
    <>
      <div className="grid grid-cols-3 gap-1.5">
        {items.map((it) =>
          it.metric ? (
            <button
              key={it.label}
              type="button"
              onClick={() => setMetric({ key: it.metric!, label: it.label })}
              title={`看「${it.label}」命中的个股`}
              className="rounded-md text-left transition-colors hover:bg-elevated/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
            >
              <MiniMetric label={it.label} value={it.value} cls={it.cls} />
            </button>
          ) : (
            <MiniMetric key={it.label} label={it.label} value={it.value} cls={it.cls} />
          ),
        )}
      </div>

      {metric &&
        createPortal(
          <div className="fixed inset-0 z-[70] flex items-center justify-center">
            <div className="absolute inset-0 bg-black/55 backdrop-blur-sm" onClick={() => setMetric(null)} />
            <div className="relative flex h-[560px] w-[560px] max-w-[92vw] flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-2xl">
              <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
                <span className="text-xs font-semibold text-foreground">{metric.label}</span>
                <span className="font-mono text-[10px] text-muted">
                  {q.data ? `${q.data.count} 只 · ${q.data.date}` : '加载中…'}
                </span>
                <button
                  onClick={() => setMetric(null)}
                  aria-label="关闭"
                  className="ml-auto rounded p-1 text-muted hover:bg-elevated hover:text-foreground"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">
                {q.isError && <div className="px-3 py-8 text-center text-[11px] text-bear">{(q.error as Error).message}</div>}
                {!q.isError && (q.data?.items ?? []).length === 0 && (
                  <div className="px-3 py-8 text-center text-[11px] text-muted">{q.isLoading ? '加载中…' : '没有命中的个股'}</div>
                )}
                {(q.data?.items ?? []).map((r) => (
                  <button
                    key={r.symbol}
                    onClick={() => navigate(`/analysis?symbol=${r.symbol}`)}
                    className="flex w-full items-center gap-2 border-b border-border/40 px-3 py-[5px] text-left hover:bg-elevated/60"
                  >
                    <span className="w-[76px] shrink-0 truncate text-[12px] text-foreground">{r.name || r.symbol}</span>
                    <span className="shrink-0 font-mono text-[10px] text-muted">{r.symbol.split('.')[0]}</span>
                    <span className="flex-1" />
                    <span className="w-[52px] shrink-0 text-right font-mono text-[11px] text-muted">
                      {r.close?.toFixed(2) ?? '—'}
                    </span>
                    <span
                      className={`w-[56px] shrink-0 text-right font-mono text-[12px] font-medium ${
                        (r.change_pct ?? 0) >= 0 ? 'text-bull' : 'text-bear'
                      }`}
                    >
                      {r.change_pct == null ? '—' : `${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(2)}%`}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
