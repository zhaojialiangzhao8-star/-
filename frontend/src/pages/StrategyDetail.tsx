import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileCode2, Pencil } from 'lucide-react'
import { api } from '../services/api'
import { Modal } from '../components/ui'

/**
 * 策略详情 —— 点行展开，看这个策略写了什么。
 *
 * 三块：它在盯什么（描述 + 参数）、它怎么算的（源码，注释高亮）、
 * 它被怎么评价的（审计结论）。
 */
export default function StrategyDetail({
  id,
  onClose,
  onEdit,
}: {
  id: string
  onClose: () => void
  onEdit: (id: string) => void
}) {
  const [showCode, setShowCode] = useState(true)
  const q = useQuery({
    queryKey: ['strategy-detail', id],
    queryFn: () => api.strategyDetail(id),
  })

  const d = q.data
  const meta = (d?.meta ?? {}) as {
    name?: string
    description?: string
    tags?: string[]
    order_by?: string | null
    descending?: boolean
    limit?: number
  }
  const params = ((d?.params ?? []) as { id?: string; label?: string; type?: string; default?: unknown }[]) || []

  const srcLines = useMemo(() => {
    if (!d?.source) return []
    return d.source.split('\n')
  }, [d?.source])

  return (
    <Modal
      title={meta.name || id}
      subtitle={d ? `${d.file} · ${d.lines} 行` : id}
      onClose={onClose}
      width="max-w-[1000px]"
      right={
        d?.editable ? (
          <button
            onClick={() => onEdit(id)}
            className="flex items-center gap-1 rounded border border-line bg-card px-2 py-0.5 text-xs2 text-fg2 hover:border-brand/50 hover:text-brand"
          >
            <Pencil size={10} /> 编辑
          </button>
        ) : null
      }
    >
      <div className="p-4">
        {q.isLoading && <div className="text-xs text-fg3">加载中…</div>}
        {q.isError && (
          <div className="rounded-md border border-up/40 bg-up/10 px-3 py-2 text-sm2 text-up">
            {(q.error as Error)?.message}
          </div>
        )}

        {d && (
          <>
            {/* 抬头 */}
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs2 text-fg3">{d.id}</span>
              <span
                className={`rounded px-1.5 py-px font-mono text-3xs ${
                  d.enabled ? 'bg-brand/15 text-brand' : 'bg-white/5 text-fg3'
                }`}
              >
                {d.enabled ? '已启用' : '未启用'}
              </span>
              {(meta.tags ?? []).map((t) => (
                <span key={t} className="rounded border border-line2 px-1.5 py-px text-3xs text-fg3">
                  {t}
                </span>
              ))}
            </div>

            {meta.description && (
              <p className="mt-3 rounded-md bg-card2 px-3 py-2 text-xs leading-relaxed text-fg2">
                {meta.description}
              </p>
            )}

            {d.disabled_reason && (
              <div className="mt-2 rounded-md border-l-[3px] border-up bg-up/10 px-3 py-2 text-sm2 leading-relaxed text-[#f0a8a8]">
                <b>审计结论：</b>
                {d.disabled_reason}
              </div>
            )}

            {/* 参数 */}
            {params.length > 0 && (
              <div className="mt-4">
                <div className="mb-1.5 text-xs2 text-fg3">参数</div>
                <div className="overflow-hidden rounded-md border border-line">
                  <table className="w-full text-sm2">
                    <thead>
                      <tr className="bg-bg2 text-fg3">
                        <th className="px-2.5 py-1 text-left font-medium">id</th>
                        <th className="px-2.5 py-1 text-left font-medium">说明</th>
                        <th className="px-2.5 py-1 text-left font-medium">类型</th>
                        <th className="px-2.5 py-1 text-right font-medium">默认值</th>
                      </tr>
                    </thead>
                    <tbody>
                      {params.map((p, i) => (
                        <tr key={p.id ?? i} className="border-t border-line/60">
                          <td className="px-2.5 py-1 font-mono text-sm2 text-fg">{p.id}</td>
                          <td className="px-2.5 py-1 text-fg2">{p.label ?? '—'}</td>
                          <td className="px-2.5 py-1 text-fg3">{p.type ?? '—'}</td>
                          <td className="px-2.5 py-1 text-right font-mono text-fg2">
                            {typeof p.default === 'object' ? JSON.stringify(p.default) : String(p.default ?? '—')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* 其他 META */}
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs2 text-fg3">
              {meta.order_by && (
                <span>
                  排序 <span className="font-mono text-fg2">{meta.order_by}</span>
                  {meta.descending ? ' ↓' : ' ↑'}
                </span>
              )}
              {!!meta.limit && (
                <span>
                  取前 <span className="font-mono text-fg2">{meta.limit}</span> 条
                </span>
              )}
              {d.truncated && <span className="text-gold">源码过长，已截断显示</span>}
            </div>

            {/* 源码 */}
            <div className="mt-4">
              <button
                onClick={() => setShowCode((v) => !v)}
                className="mb-1.5 flex items-center gap-1.5 text-xs2 text-fg3 hover:text-fg2"
              >
                <FileCode2 size={11} />
                {showCode ? '收起源码' : '展开源码'}
                <span className="text-fg3/60">（{d.lines} 行）</span>
              </button>
              {showCode && (
                <div className="max-h-[420px] overflow-auto rounded-md border border-line bg-[#0a0e14]">
                  <pre className="m-0 p-0 text-sm2 leading-[1.55]">
                    {srcLines.map((line, i) => (
                      <CodeLine key={i} n={i + 1} text={line} />
                    ))}
                  </pre>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}

/** 一行源码：行号 + 内容，# 之后当注释染灰（够用，不做完整语法高亮） */
function CodeLine({ n, text }: { n: number; text: string }) {
  const idx = text.indexOf('#')
  const code = idx >= 0 ? text.slice(0, idx) : text
  const comment = idx >= 0 ? text.slice(idx) : ''
  return (
    <div className="flex hover:bg-white/[0.03]">
      <span className="w-11 flex-none select-none pr-2 text-right font-mono text-xs2 text-[#3d4654]">
        {n}
      </span>
      <span className="flex-1 whitespace-pre pr-3 font-mono text-[#c8d3df]">
        {code}
        {comment && <span className="text-[#5d7185]">{comment}</span>}
      </span>
    </div>
  )
}
