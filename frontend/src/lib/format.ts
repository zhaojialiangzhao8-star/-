/** 数字与颜色格式化 —— 全站统一口径，避免每页各写一遍。 */

export function fmtInt(n: number | null | undefined): string {
  if (n == null) return '—'
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + ' 亿'
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(0) + ' 万'
  return n.toLocaleString('zh-CN')
}

export function fmtNum(n: number | null | undefined, d = 2): string {
  if (n == null || Number.isNaN(n)) return '—'
  return n.toFixed(d)
}

export function fmtPct(n: number | null | undefined, d = 2, sign = true): string {
  if (n == null || Number.isNaN(n)) return '—'
  const s = sign && n > 0 ? '+' : ''
  return `${s}${n.toFixed(d)}%`
}

/** 百分数带颜色：A 股习惯红涨绿跌 */
export function pctClass(n: number | null | undefined): string {
  if (n == null || n === 0) return 'text-fg2'
  return n > 0 ? 'text-up' : 'text-down'
}

export function moneyClass(n: number | null | undefined): string {
  if (n == null) return 'text-fg3'
  return n > 0 ? 'text-up' : n < 0 ? 'text-down' : 'text-fg2'
}

export function clock(): string {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false })
}
