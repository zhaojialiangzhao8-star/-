/**
 * 股票预览弹窗 —— 轻量占位版。
 *
 * Dashboard 的实际调用是 props 平铺（symbol/name/onNavigate/onClose），不是 target 对象。
 * TSP 原版开多标签面板（分时/K线/财务/规则），依赖链太长；这里点击即跳个股分析页。
 */
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'

export interface StockPreviewTriggerInfo {
  price?: number | null
  changePct?: number | null
  ts?: number
  signals?: string[]
  message?: string
}

export function StockPreviewDialog({
  symbol,
  name,
  onClose,
}: {
  symbol: string | null
  name?: string
  triggerInfo?: StockPreviewTriggerInfo | null
  navList?: unknown
  onNavigate?: (symbol: string, name?: string) => void
  onClose: () => void
}) {
  const navigate = useNavigate()
  useEffect(() => {
    if (symbol) {
      navigate(`/analysis?symbol=${encodeURIComponent(symbol)}`)
      onClose()
    }
  }, [symbol, navigate, onClose])
  // name 仅用于类型占位，避免 noUnusedParameters 报错
  void name
  return null
}
