import { ArrowUpRight, Construction } from 'lucide-react'

/**
 * 迁移中的占位页 —— 明确写出「还没做」和「旧版在哪」，不做假按钮。
 */
export default function Placeholder({
  title,
  note,
  migration,
  fallbackHref,
  fallbackLabel,
}: {
  title: string
  note: string
  migration: string
  fallbackHref?: string
  fallbackLabel?: string
}) {
  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">{title}</h2>
        <span className="rounded border border-gold/40 bg-gold/10 px-2 py-0.5 text-xs2 text-gold">
          {migration}
        </span>
      </header>
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-line bg-card p-8 text-center">
          <Construction size={30} className="mx-auto mb-4 text-fg3" strokeWidth={1.5} />
          <h3 className="card-title">{title} · 尚未迁移</h3>
          <p className="mt-2 text-xs leading-relaxed text-fg2">{note}</p>
          <p className="mt-3 text-sm2 leading-relaxed text-fg3">
            旧版静态页面已废弃，这一页还没在 V2 里实现，暂时没有可跳转的去处。
          </p>
          {fallbackHref && (
            <a
              href={fallbackHref}
              className="mt-5 inline-flex items-center gap-1.5 rounded-lg border border-line2 bg-card2 px-4 py-2 text-xs text-fg2 transition-colors hover:border-brand hover:text-fg"
            >
              {fallbackLabel ?? '打开旧版'}
              <ArrowUpRight size={13} />
            </a>
          )}
        </div>
      </div>
    </>
  )
}
