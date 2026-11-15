import { useEffect, type ReactNode } from 'react'

export function Card({
  title,
  subtitle,
  right,
  children,
  padded = true,
  className = '',
}: {
  title?: ReactNode
  subtitle?: ReactNode
  right?: ReactNode
  children: ReactNode
  padded?: boolean
  className?: string
}) {
  const hasHeader = title || subtitle || right
  return (
    // 卡片视觉参照 Tick Stock Panel 的 sectionCard：
    // 半透明底 + 毛玻璃 + 1px 细阴影，悬停时阴影加深——这层是「通透感」的来源。
    <section
      className={`overflow-hidden rounded-[8px] border border-line bg-card/80 shadow-[0_1px_2px_rgba(0,0,0,0.45)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_rgba(0,0,0,0.55)] ${className}`}
    >
      {hasHeader && (
        <header className="flex items-center gap-2 border-b border-line px-3 py-1.5">
          {/* 与首页 SectionTitle 同一套：accent 竖条 + 12px 标题 + 10px 等宽副信息 */}
          <span
            className="h-3 w-0.5 flex-none rounded-full bg-gradient-to-b from-brand to-brand/30"
            aria-hidden
          />
          {title && <h3 className="card-title">{title}</h3>}
          {subtitle && <span className="font-mono text-xs2 text-fg3">{subtitle}</span>}
          {right && <div className="ml-auto font-mono text-xs2 text-fg3">{right}</div>}
        </header>
      )}
      <div className={padded ? 'p-2.5' : ''}>{children}</div>
    </section>
  )
}

export function Metric({ label, value, tone = '' }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="rounded-[7px] bg-card2 px-2.5 py-2">
      <div className="text-xs2 text-fg3">{label}</div>
      <div className={`num mt-0.5 text-sm font-semibold ${tone}`}>{value}</div>
    </div>
  )
}

export function DataTable<T>({
  columns,
  rows,
  empty = '暂无数据',
  rowKey,
  onRowClick,
}: {
  columns: { key: string; label: string; align?: 'left' | 'right'; render: (r: T) => ReactNode }[]
  rows: T[]
  empty?: ReactNode
  rowKey: (r: T, i: number) => string
  /** 给整行加点击（如打开个股日K），行会变成可点的 pointer + 高亮 */
  onRowClick?: (r: T) => void
}) {
  if (!rows.length) {
    return <div className="px-3 py-6 text-center text-sm2 text-fg3">{empty}</div>
  }
  return (
    <table className="w-full border-collapse whitespace-nowrap text-sm2">
      <thead>
        <tr>
          {columns.map((c, i) => (
            <th
              key={c.key}
              className={`whitespace-nowrap border-b border-line px-2.5 py-1 text-xs2 font-medium text-fg3 ${
                c.align === 'left' || i === 0 ? 'text-left' : 'text-right'
              }`}
            >
              {c.label}
            </th>
          ))}
          {/* 末尾弹性列：吃掉多余宽度，数据列保持紧凑（否则 w-full 会把列摊平） */}
          <th className="w-full border-b border-line" />
        </tr>
      </thead>
      <tbody>
        {rows.map((r, ri) => (
          <tr
            key={rowKey(r, ri)}
            onClick={onRowClick ? () => onRowClick(r) : undefined}
            className={`transition-colors hover:bg-brand/[0.06] ${
              onRowClick ? 'cursor-pointer' : ''
            }`}
          >
            {columns.map((c, ci) => (
              <td
                key={c.key}
                className={`num whitespace-nowrap border-b border-line/60 px-2.5 py-1 ${
                  c.align === 'left' || ci === 0 ? 'text-left' : 'text-right'
                }`}
              >
                {c.render(r)}
              </td>
            ))}
            <td className="border-b border-line/60" />
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function Tag({ kind, children }: { kind: 'pass' | 'fail' | 'hold'; children: ReactNode }) {
  const map = {
    pass: 'bg-down/15 text-down',
    fail: 'bg-up/15 text-up',
    hold: 'bg-gold/15 text-gold',
  }
  return <span className={`rounded px-1.5 py-px font-mono text-3xs ${map[kind]}`}>{children}</span>
}

/** 居中弹窗：点遮罩或 Esc 关闭，内容自带滚动。 */
export function Modal({
  title,
  subtitle,
  right,
  onClose,
  children,
  width = 'max-w-[1060px]',
}: {
  title: ReactNode
  subtitle?: ReactNode
  right?: ReactNode
  onClose: () => void
  children: ReactNode
  width?: string
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-6"
      onClick={onClose}
      role="presentation"
    >
      <div
        className={`flex max-h-[86vh] w-full ${width} flex-col overflow-hidden rounded-[12px] border border-line2 bg-card shadow-2xl`}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-line px-4 py-2.5">
          <h3 className="card-title">{title}</h3>
          {subtitle && <span className="font-mono text-xs2 text-fg3">{subtitle}</span>}
          {right && <div className="ml-auto flex items-center gap-3 font-mono text-xs2 text-fg3">{right}</div>}
          <button
            onClick={onClose}
            className={`${right ? 'ml-1' : 'ml-auto'} rounded px-1.5 text-sm leading-none text-fg3 hover:bg-white/5 hover:text-fg`}
            title="关闭（Esc）"
          >
            ×
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  )
}
