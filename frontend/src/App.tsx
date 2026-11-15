import { Route, Routes } from 'react-router-dom'
import AppShell from './layouts/AppShell'
import Dashboard from './pages/Dashboard'
import Placeholder from './pages/Placeholder'
import StrategyBacktest from './pages/StrategyBacktest'
import StockAnalysis from './pages/StockAnalysis'
import TradingPlan from './pages/TradingPlan'
import Watchlist from './pages/Watchlist'
import Screener from './pages/Screener'
import Tasks from './pages/Tasks'
import DataManage from './pages/DataManage'
import Monitor from './pages/Monitor'
import Boards from './pages/Boards'
import Ladder from './pages/Ladder'
import Risk from './pages/Risk'
import StrategyLib from './pages/StrategyLib'
import { Dashboard as TickDashboard } from './tick/pages/Dashboard'

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<div className="tick-scope h-full overflow-y-auto"><TickDashboard /></div>} />
        {/* 旧版交易首页，保留一版备查 */}
        <Route path="legacy" element={<Dashboard />} />
        <Route path="screener" element={<Screener />} />
        <Route path="analysis" element={<StockAnalysis />} />
        <Route path="plan" element={<TradingPlan />} />
                    <Route path="watchlist" element={<Watchlist />} />
        <Route path="strategies" element={<StrategyLib />} />
        <Route path="backtest" element={<StrategyBacktest />} />
        <Route path="boards" element={<Boards />} />
            <Route path="ladder" element={<Ladder />} />
            <Route path="risk" element={<Risk />} />
            <Route path="monitor" element={<Monitor />} />
        <Route path="tasks" element={<Tasks />} />
        <Route path="data" element={<DataManage />} />
        {/* /market 与首页同一个看板，保留路径兼容工具链接 */}
        <Route path="market" element={<div className="tick-scope h-full overflow-y-auto"><TickDashboard /></div>} />
      </Route>
    </Routes>
  )
}
