import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  Activity, BarChart3, Bell, BookOpen, Boxes, CandlestickChart, ChevronLeft, ChevronRight, Flame, LayoutDashboard, ListChecks, Settings, Target, PieChart, ShieldAlert, Star } from 'lucide-react'
import { api } from '../services/api'
import { fmtInt } from '../lib/format'

const NAV = [
  {
    group: '交易',
    items: [
      { to: '/', label: '交易首页', icon: LayoutDashboard },
      { to: '/screener', label: '智能选股', icon: ListChecks },
      { to: '/analysis', label: '个股分析', icon: CandlestickChart },
      { to: '/plan', label: '交易计划', icon: Target },
      { to: '/watchlist', label: '自选股', icon: Star },
    ],
  },
  {
    group: '研究',
    items: [
      { to: '/strategies', label: '策略库', icon: BookOpen },
      { to: '/backtest', label: '策略回测', icon: BarChart3 },
      { to: '/boards', label: '板块', icon: PieChart },
  { to: '/ladder', label: '涨停天梯', icon: Flame },
  { to: '/risk', label: '风险库', icon: ShieldAlert },
  { to: '/monitor', label: '实时监控', icon: Bell },
    ],
  },
  {
    group: '系统',
    items: [
      { to: '/tasks', label: '任务队列', icon: Activity },
      { to: '/data', label: '数据管理', icon: Boxes },
    ],
  },
]

/** 折叠状态记在 localStorage：键盘 `[` 也能切，刷新后保持 */
const LS_KEY = 'ui.sidebarCollapsed'

export default function AppShell() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  const online = !health.isError
  const { pathname } = useLocation()
  // 首页是 Tick 版市场看板，本来就是目标尺度；其余页面挂 .app-page 收敛字号
  const isHome = pathname === '/' || pathname === '/market'

  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(LS_KEY) === '1'
    } catch {
      return false
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY, collapsed ? '1' : '0')
    } catch {
      /* 隐私模式忽略 */
    }
  }, [collapsed])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === '[' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const el = e.target as HTMLElement | null
        const tag = el?.tagName?.toLowerCase()
        if (tag === 'input' || tag === 'textarea' || el?.isContentEditable) return
        setCollapsed((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="flex h-full">
      <aside
        className={`flex flex-none flex-col border-r border-line bg-side py-3.5 transition-[width] duration-150 ${
          collapsed ? 'w-[52px]' : 'w-[196px]'
        }`}
      >
        <div className={`flex items-center gap-2.5 pb-4 ${collapsed ? 'justify-center px-0' : 'px-4'}`}>
          <div className="grid h-[26px] w-[26px] flex-none place-items-center rounded-[7px] bg-gradient-to-br from-brand via-indigo-600 to-cyan text-xs font-extrabold">
            F
          </div>
          {!collapsed && (
            <div className="min-w-0">
              <b className="text-[12px] font-semibold tracking-wide">文一股票策略</b>
              <em className="block text-[9px] not-italic tracking-[1.4px] text-fg3">STOCK STRATEGY</em>
            </div>
          )}
          {!collapsed && (
            <button
              onClick={() => setCollapsed(true)}
              title="收起侧栏（快捷键 [）"
              className="ml-auto rounded p-0.5 text-fg3 hover:bg-card hover:text-fg2"
            >
              <ChevronLeft size={14} />
            </button>
          )}
        </div>

        {collapsed && (
          <button
            onClick={() => setCollapsed(false)}
            title="展开侧栏（快捷键 [）"
            className="mb-1 self-center rounded p-1 text-fg3 hover:bg-card hover:text-fg2"
          >
            <ChevronRight size={14} />
          </button>
        )}

        <nav className="flex-1 overflow-y-auto overflow-x-hidden">
          {NAV.map((g) => (
            <div key={g.group}>
              {collapsed ? (
                <div className="mx-auto my-1.5 h-px w-5 bg-line" />
              ) : (
                <div className="px-4 pb-1.5 pt-4 text-[11px] tracking-[1px] text-fg3">{g.group}</div>
              )}
              {g.items.map(({ to, label, icon: Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={to === '/'}
                  title={collapsed ? label : undefined}
                  className={({ isActive }) =>
                    `flex items-center border-l-2 transition-colors ${
                      collapsed ? 'justify-center px-0 py-3' : 'gap-2.5 px-4 py-2.5'
                    } text-[12px] ${
                      isActive
                        ? 'border-brand bg-gradient-to-r from-brand/20 to-transparent text-white'
                        : 'border-transparent text-fg2 hover:bg-card hover:text-fg'
                    }`
                  }
                >
                  <Icon size={14} strokeWidth={1.8} className="flex-none opacity-80" />
                  {!collapsed && <span className="truncate">{label}</span>}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        {collapsed ? (
          <div className="flex justify-center border-t border-line pt-2.5" title={health.data ? `${fmtInt(health.data.rows)} 行 · 代际 ${health.data.generation?.slice(0, 8)}` : '数据状态'}>
            <span className={`text-[10px] ${online ? 'text-down' : 'text-gold'}`}>●</span>
          </div>
        ) : (
          <div className="border-t border-line px-4 pt-2.5 text-[10px] leading-relaxed text-fg3">
            <div className="flex items-center gap-1.5">
              数据
              <span className={online ? 'text-down' : 'text-gold'}>●</span>
              {health.data ? fmtInt(health.data.rows) + ' 行' : '—'}
            </div>
            <div>代际 {health.data?.generation?.slice(0, 8) ?? '—'}</div>
            <div>策略 {health.data?.strategies ?? '—'} 个</div>
          </div>
        )}
      </aside>

      <main className={`${isHome ? '' : 'app-page '}flex min-w-0 flex-1 flex-col`}>
        <Outlet />
      </main>
    </div>
  )
}
