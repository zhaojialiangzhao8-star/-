/** 复权因子同步门 —— 占位：Dashboard 用到 guard() 和 dialog 两个成员 */
import type { ReactNode } from 'react'

export function useAdjFactorSyncGate() {
  return {
    needSync: false,
    syncing: false,
    progress: null as number | null,
    /** 包一层前置检查；占位版直接执行 */
    guard: (fn: () => void) => fn(),
    dialog: null as ReactNode,
  }
}

export function AdjFactorSyncGate({ children }: { children?: ReactNode }) {
  return <>{children}</>
}
