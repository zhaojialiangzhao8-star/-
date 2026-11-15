/**
 * 板块成分股弹窗 —— 点看板「概念热度 / 行业热度」里的板块行时打开。
 *
 * 数据来自 /api/v1/dashboard/board-members：概念走 symbol_industry_em.json，
 * 行业走 industry_members.json，涨跌幅取最新一代快照。
 * 列表按当日涨幅降序，点任意一只是打开个股弹窗（与榜单里的个股点击同一套）。
 */
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, X } from 'lucide-react'
import { api } from '@/lib/api'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

export interface DimensionMembersTarget {
  kind: string
  value: string
  sourceField?: string | null
}

/** widgets.tsx 用它把 source_field 映射成板块类型 */
export function dimensionKindForSourceField(field?: string | null): string | null {
  if (!field) return null
  return field.includes('concept') ? 'concept' : 'industry'
}

function pctClass(v: number | null | undefined) {
  if (v == null || v === 0) return 'text-muted'
  return v > 0 ? 'text-bull' : 'text-bear'
}

export function DimensionMembersDialog({
  target,
  onClose,
  onStockClick,
}: {
  target: DimensionMembersTarget | null
  onClose: () => void
  onStockClick?: (symbol: string, name?: string) => void
}) {
  const backdrop = useDialogBackdrop(onClose)
  const open = !!target?.value
  const kind = (target?.kind === 'industry' ? 'industry' : 'concept') as 'concept' | 'industry'
  const q = useQuery({
    queryKey: ['board-members', kind, target?.value],
    queryFn: () => api.boardMembers(kind, target!.value),
    enabled: open,
    staleTime: 30_000,
  })

  if (!open) return null
  const rows = q.data?.items ?? []

  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" {...backdrop} />
      <div className="relative mx-4 flex max-h-[80vh] w-full max-w-xl flex-col overflow-hidden rounded-dialog border border-border bg-surface shadow-2xl">
        <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
          <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-medium leading-none text-accent">
            {kind === 'concept' ? '概念' : '行业'}
          </span>
          <h3 className="text-sm font-medium text-foreground">{target!.value}</h3>
          <span className="font-mono text-[10px] text-muted">
            {q.data ? `${q.data.count} 只 · ${q.data.date ?? '—'}` : '加载中…'}
          </span>
          <button
            onClick={onClose}
            className="ml-auto rounded p-0.5 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {q.isLoading ? (
          <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载成分股…
          </div>
        ) : rows.length === 0 ? (
          <div className="px-4 py-14 text-center text-xs text-muted">
            这个板块没有可用成分股映射
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <table className="w-full border-collapse text-[11px]">
              <thead>
                <tr>
                  <th className="sticky top-0 border-b border-border bg-surface px-3 py-1.5 text-left font-medium text-muted">代码</th>
                  <th className="sticky top-0 border-b border-border bg-surface px-3 py-1.5 text-left font-medium text-muted">名称</th>
                  <th className="sticky top-0 border-b border-border bg-surface px-3 py-1.5 text-right font-medium text-muted">现价</th>
                  <th className="sticky top-0 border-b border-border bg-surface px-3 py-1.5 text-right font-medium text-muted">涨跌幅</th>
                  <th className="sticky top-0 border-b border-border bg-surface px-3 py-1.5 text-right font-medium text-muted">成交额(亿)</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.symbol}
                    onClick={() => onStockClick?.(r.symbol, r.name)}
                    className="cursor-pointer transition-colors hover:bg-brand/[0.08]"
                  >
                    <td className="border-b border-border/50 px-3 py-1 font-mono text-[10px] text-secondary">{r.symbol}</td>
                    <td className="border-b border-border/50 px-3 py-1 text-foreground">{r.name}</td>
                    <td className="border-b border-border/50 px-3 py-1 text-right font-mono tabular-nums text-foreground">
                      {r.close ?? '—'}
                    </td>
                    <td className={`border-b border-border/50 px-3 py-1 text-right font-mono tabular-nums ${pctClass(r.change_pct)}`}>
                      {r.change_pct == null ? '—' : `${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(2)}%`}
                    </td>
                    <td className="border-b border-border/50 px-3 py-1 text-right font-mono tabular-nums text-secondary">
                      {r.amount_yi ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
