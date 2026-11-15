import type { ReactElement } from 'react'

/** 能力标签 —— 占位实现：看板只用到 MissingCapChip 这个提示小标签。 */
export function MissingCapChip({ label }: { label?: string }): ReactElement | null {
  if (!label) return null
  return <span className="rounded bg-warning/15 px-1 text-[10px] text-warning">{label}</span>
}