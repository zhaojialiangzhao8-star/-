import * as React from 'react'
import GridLayout, { useContainerWidth, type Layout } from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import type { ReactNode } from 'react'

/**
 * 看板网格 — 照 Tick Stock Panel 的 DashboardGrid 写的 react-grid-layout v2 封装。
 *
 * 关键点（都是 TSP 源码里踩过的坑）：
 * 1. **onLayoutChange 只在编辑态传**。RGL 挂载时会回传一份「规范化」布局，
 *    非编辑态如果照收并持久化，会把用户布局写成塌陷版（我第一版就是死在这）。
 * 2. children 用 items.map 动态生成，key 直接等于 layout 的 i，避免 React
 *    给静态 children 的 key 加前缀后匹配不上。
 * 3. containerPadding 用 null（跟 TSP 一致），让卡片贴边、间距靠 margin 控制。
 */

export const GRID_COLS = 12
export const GRID_ROW_H = 26
export const GRID_MARGIN = 8

export interface DashItem {
  i: string
  x: number
  y: number
  w: number
  h: number
  /** 最小宽高，防止缩没了 */
  minW?: number
  minH?: number
}

/** 默认布局 —— 坐标照 TSP 的 DEFAULT_LAYOUT（行高 28） */
export const DEFAULT_LAYOUT: DashItem[] = [
  { i: 'indices', x: 0, y: 0, w: 12, h: 2, minW: 3, minH: 2 },
  { i: 'kpi', x: 0, y: 2, w: 12, h: 3, minW: 3, minH: 3 },
  { i: 'dist', x: 0, y: 5, w: 3, h: 9, minW: 2, minH: 6 },
  { i: 'radar', x: 3, y: 5, w: 3, h: 9, minW: 2, minH: 6 },
  { i: 'trend', x: 6, y: 5, w: 3, h: 9, minW: 2, minH: 6 },
  { i: 'ladder', x: 9, y: 5, w: 3, h: 9, minW: 2, minH: 6 },
  { i: 'industry', x: 0, y: 14, w: 4, h: 7, minW: 2, minH: 5 },
  { i: 'concept', x: 4, y: 14, w: 4, h: 7, minW: 2, minH: 5 },
  { i: 'monitor', x: 8, y: 14, w: 4, h: 7, minW: 2, minH: 5 },
  { i: 'ranks', x: 0, y: 21, w: 12, h: 9, minW: 4, minH: 7 },
  { i: 'signals', x: 0, y: 30, w: 8, h: 10, minW: 3, minH: 5 },
  { i: 'strategies', x: 8, y: 30, w: 4, h: 10, minW: 2, minH: 5 },
  { i: 'tasks', x: 0, y: 40, w: 7, h: 7, minW: 3, minH: 4 },
  { i: 'limitup', x: 7, y: 40, w: 5, h: 7, minW: 3, minH: 4 },
  { i: 'rules', x: 0, y: 47, w: 12, h: 4, minW: 4, minH: 3 },
  { i: 'health', x: 0, y: 51, w: 12, h: 4, minW: 4, minH: 3 },
]

const LS_KEY = 'facet-dash-grid-v2'

export function loadLayout(): DashItem[] {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return DEFAULT_LAYOUT
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed) || parsed.length === 0) return DEFAULT_LAYOUT
    const byId = new Map(parsed.map((x: DashItem) => [x.i, x]))
    // 只认默认里有的 key，坐标异常就用默认值
    return DEFAULT_LAYOUT.map((d) => {
      const hit = byId.get(d.i)
      if (!hit) return d
      const num = (v: unknown, fb: number) => (typeof v === 'number' && Number.isFinite(v) ? Math.round(v) : fb)
      return {
        ...d,
        x: Math.min(Math.max(num(hit.x, d.x), 0), GRID_COLS - 1),
        y: Math.max(num(hit.y, d.y), 0),
        w: Math.min(Math.max(num(hit.w, d.w), d.minW ?? 1), GRID_COLS),
        h: Math.max(num(hit.h, d.h), d.minH ?? 1),
      }
    })
  } catch {
    return DEFAULT_LAYOUT
  }
}

export function saveLayout(layout: DashItem[]) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(layout))
  } catch {
    /* 隐私模式写不了 */
  }
}

export function resetLayout(): DashItem[] {
  try {
    localStorage.removeItem(LS_KEY)
  } catch {
    /* ignore */
  }
  return DEFAULT_LAYOUT
}

/** 把 children 的 key 归一化（React 会给 key 加 `.$` 前缀） */
function normKey(k: React.Key | null | undefined): string {
  if (k == null) return ''
  return String(k).replace(/^\.\$/, '')
}

export function DashGrid({
  editing,
  layout,
  onLayoutChange,
  children,
}: {
  editing: boolean
  layout: DashItem[]
  onLayoutChange: (l: DashItem[]) => void
  children: ReactNode
}) {
  const { width, containerRef, mounted } = useContainerWidth({ measureBeforeMount: true })

  const rglLayout = React.useMemo(
    () => layout.map((it) => ({ i: it.i, x: it.x, y: it.y, w: it.w, h: it.h, minW: it.minW, minH: it.minH })),
    [layout],
  )

  // children → 数组，key 与 layout 的 i 对齐（关键：动态 map，不传静态 JSX）
  const childMap = React.useMemo(() => {
    const m = new Map<string, ReactNode>()
    React.Children.forEach(children, (c) => {
      if (React.isValidElement(c)) m.set(normKey(c.key), c)
    })
    return m
  }, [children])

  const handleLayoutChange = React.useCallback(
    (l: Layout) => {
      const byId = new Map(l.map((x) => [x.i, x]))
      onLayoutChange(
        layout.map((it) => {
          const hit = byId.get(it.i)
          return hit ? { ...it, x: hit.x, y: hit.y, w: hit.w, h: hit.h } : it
        }),
      )
    },
    [layout, onLayoutChange],
  )

  const isMobile = mounted && width < 768

  return (
    <div ref={containerRef as unknown as React.RefObject<HTMLDivElement>} className="relative">
      {!mounted ? (
        <div className="h-32" />
      ) : isMobile ? (
        /* 窄屏不挂网格，按顺序纵排（RGL 只服务桌面宽度） */
        <div className="space-y-2">
          {layout.map((it) => (
            <div key={it.i}>{childMap.get(it.i)}</div>
          ))}
        </div>
      ) : (
        <GridLayout
          width={width}
          layout={rglLayout}
          gridConfig={{
            cols: GRID_COLS,
            rowHeight: GRID_ROW_H,
            margin: [GRID_MARGIN, GRID_MARGIN],
            containerPadding: null,
            maxRows: Infinity,
          }}
          dragConfig={{
            enabled: editing,
            // 整卡可拖（不限把手），内部按钮/链接/输入框不触发拖拽
            cancel: 'button, a, input, select, textarea, .no-drag',
            threshold: 3,
          }}
          resizeConfig={{
            enabled: editing,
            handles: ['n', 'e', 's', 'w', 'ne', 'nw', 'se', 'sw'],
          }}
          // 关键：非编辑态不接收回调，避免 RGL 挂载时的规范化布局覆盖已存布局
          onLayoutChange={editing ? handleLayoutChange : undefined}
        >
          {layout.map((it) => (
            <div
              key={it.i}
              className={editing ? 'cursor-grab select-none ring-1 ring-brand/25 hover:ring-brand/60 active:cursor-grabbing' : ''}
              style={{ overflow: 'hidden', borderRadius: 8 }}
              data-dash-item={it.i}
            >
              {editing && (
                <div className="absolute inset-x-0 top-0 z-10 flex h-5 items-center rounded-t-[8px] bg-card2/95 px-2 text-2xs text-fg3">
                  ⠿ {it.i}
                </div>
              )}
              {/* 编辑态阻断卡片内交互，拖拽由网格项接管；卡片强制撑满格子并裁切溢出 */}
              <div className={`h-full [&>section]:h-full [&>section]:overflow-hidden ${editing ? 'pointer-events-none' : ''}`}>
                {childMap.get(it.i)}
              </div>
            </div>
          ))}
        </GridLayout>
      )}
    </div>
  )
}
