import { useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { api } from '../services/api'
import KLineChart from './KLineChart'

/** 点首页指数格子弹出的 K 线弹窗：指数没有独立详情页，就地看走势最省事 */
export default function IndexKlineDialog({
  symbol,
  name,
  onClose,
}: {
  symbol: string
  name?: string
  onClose: () => void
}) {
  const q = useQuery({
    queryKey: ['index-kline', symbol],
    queryFn: () => api.indexKline(symbol, 250),
    enabled: !!symbol,
    retry: 0,
  })
  const rows = q.data?.rows ?? []

  // 必须走 portal：看板格子的 react-grid-item 带 transform，会让 fixed 相对它定位
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/55 backdrop-blur-sm" onClick={onClose} />
      <div className="relative flex h-[520px] w-[900px] max-w-[92vw] flex-col overflow-hidden rounded-lg border border-line bg-card shadow-2xl">
        <div className="flex shrink-0 items-center gap-2 border-b border-line px-4 py-2">
          <span className="card-title">{name || symbol}</span>
          <span className="font-mono text-xs2 text-fg3">{symbol}</span>
          <span className="font-mono text-xs2 text-fg3">
            {rows.length ? `近 ${rows.length} 个交易日` : ''}
          </span>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="ml-auto rounded px-2 py-0.5 text-fg3 hover:bg-white/5 hover:text-fg"
          >
            <X size={14} />
          </button>
        </div>
        <div className="min-h-0 flex-1 p-1">
          {q.isError ? (
            <div className="py-16 text-center text-sm2 text-up">{(q.error as Error).message}</div>
          ) : rows.length ? (
            <KLineChart bars={rows} height={450} />
          ) : (
            <div className="py-16 text-center text-sm2 text-fg3">{q.isLoading ? '加载中…' : '没有数据'}</div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
