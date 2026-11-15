/**
 * API 客户端 —— 统一入口，全部走 /api/v1（后端双挂载，旧 /api 也通）。
 * 每个响应都带类型，前端不再靠猜字段。
 */

const BASE = '/api/v1'

async function post<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, { method: 'POST', body: JSON.stringify(body) })
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      const d = body?.error?.message || body?.detail
      // FastAPI 422 的 detail 是 [{loc, msg, ...}]，直接拼会变成 [object Object]
      detail = Array.isArray(d)
        ? d.map((x: { msg?: string; loc?: unknown[] }) => x?.msg ?? JSON.stringify(x)).join('; ')
        : d || detail
    } catch {
      /* 保持原错误 */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

/* ---------------- 类型 ---------------- */

export interface Health {
  /** 数据是否落后（后端按交易日算） */
  stale?: boolean
  stale_days?: number
  generation: string
  rows: number
  symbols?: number
  days?: number
  date_start?: string
  date_end?: string
  strategies: number
  errors: Record<string, string>
  updated_at?: string
}

export interface Overview {
  date: string
  symbols: number
  advancers: number
  decliners: number
  unchanged?: number
  limit_ups: number
  limit_downs: number
  amount_yi: number
  /** true = 来自真实成交额快照（未复权）；false/缺省 = 复权口径（绝对值偏大，只可比不可当真） */
  amount_real?: boolean
  median_ret_pct: number
  breadth_pct: number
}

export interface DailySignalRow {
  symbol: string
  board: string
  drop3: number
  vol_ratio: number
  close: number
  buy_low: number
  buy_high: number
  stop: number
  qty: number
  amount: number
  risk_amount: number
  change_pct: number
}

export interface DailySignal {
  date: string
  risk_per_trade: number
  count: number
  rows: DailySignalRow[]
  total_amount: number
  total_risk: number
  rule: string
}

export interface LimitUpBucket {
  label: string
  n: number
  promote: number
  next_chg: number
  oc_return: number
  win_rate: number
}

export interface LimitUpStats {
  buckets: LimitUpBucket[]
  total_limit_up_days: number
  sample: string
}

export interface TaskItem {
  id: string
  type: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  progress: number
  params?: Record<string, unknown>
  result?: Record<string, unknown>
  error?: string | null
  log?: string | null
  created_at: string
  started_at?: string | null
  finished_at?: string | null
}

export interface Bar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount?: number
  change_pct?: number
  ma5?: number
  ma20?: number
}

export interface Trade {
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  price: number
  dt: string
  signal_dt: string
  hold_days: number
  reason: string
  fees: Record<string, number>
  pnl: number
  /** 相对成本的收益率：0.12 = +12%，-0.083 = -8.3%（卖出才有，买入为 0） */
  pnl_pct?: number
  /** 卖出时的持仓成本价（含买入费用摊薄）；买入为成交价 */
  entry_price?: number
  source: string
}

export interface BacktestMetrics {
  start?: string
  end?: string
  days?: number
  init_cash?: number
  final_equity?: number
  total_return?: number
  annual_return?: number
  excess_return?: number
  max_drawdown?: number
  max_drawdown_days?: number
  sharpe?: number
  sortino?: number
  calmar?: number
  volatility?: number
  trades?: number
  wins?: number
  losses?: number
  win_rate?: number
  avg_win?: number
  avg_loss?: number
  payoff?: number
  [k: string]: number | string | undefined
}

export interface BacktestResult {
  metrics: BacktestMetrics
  nav: [string, number][]
  trades: Trade[]
  rejected: Record<string, number>
  warnings: string[]
  config: Record<string, number | string | null>
}

export interface RunMeta {
  id: string
  kind: string
  title: string
  created_at: string
  params: Record<string, unknown>
  metrics: Partial<BacktestMetrics>
  trade_count: number
}

export interface RunDetail extends RunMeta {
  result: BacktestResult
}

export interface BtParams {
  strategy: string
  params?: Record<string, unknown>
  init_cash: number
  risk_amount?: number | null
  risk_pct?: number
  max_positions: number
  max_position_pct: number
  stop_loss_pct: number
  take_profit_pct?: number | null
  max_hold_days?: number | null
  start?: string | null
  end?: string | null
  commission_rate?: number
  stamp_tax_rate?: number
  slippage_bps?: number
}

/* ---------------- 接口 ---------------- */

export interface StrategyMeta {
  id: string
  name: string
  description?: string
  tags?: string[]
  params?: unknown[]
  source?: string
}

/* ---------------- 策略库（启用 / 淘汰治理） ---------------- */

export interface StrategyLibItem extends StrategyMeta {
  file?: string | null
  enabled: boolean
  loaded: boolean
  disabled_reason?: string | null
  rank?: number | null
  return?: number | null
  mdd?: number | null
  sharpe?: number | null
  win_rate?: number | null
  trades?: number | null
  missing?: boolean
}

export interface StrategyLibResp {
  enabled: string[]
  disabled: Record<string, string>
  note?: string
  method?: string
  updated?: string | null
  random_baseline?: Record<string, { mean: number; lo: number; hi: number }> | null
  loaded_count: number
  stats: {
    total: number
    enabled: number
    retired: number
    unaudited: number
    loaded: number
  }
  items: StrategyLibItem[]
}

export const api = {
  indices: () => req<{ count: number; items: IndexQuote[] }>('/indices'),

  /** 首页看板：一次拿整屏（广度/分布/情绪/趋势/四榜） */
  dashboard: (rankN = 8) => req<DashboardData>(`/dashboard?rank_n=${rankN}`),

  /** 单个指数的历史日线（首页点指数弹 K 线用） */
  indexKline: (symbol: string, limit = 250) =>
    req<{
      symbol: string
      name: string
      count: number
      rows: { date: string; open: number; high: number; low: number; close: number; volume: number }[]
    }>(`/index/kline?symbol=${encodeURIComponent(symbol)}&limit=${limit}`),

  /* ---------- 自选股 ---------- */
  watchlist: () =>
    req<{ count: number; items: WatchItem[] }>('/watchlist'),
  addWatchBatch: (symbols: string[], grp = 'default', note = '') =>
    post<{ ok: boolean; added: number; watchlist: string[] }>('/watchlist/batch', { symbols, grp, note }),
  deleteWatchBatch: (symbols: string[]) =>
    post<{ ok: boolean; removed: number; watchlist: string[] }>('/watchlist/batch-delete', { symbols }),
  setWatchPlan: (symbol: string, plan: { entry?: number | null; stop?: number | null; target?: number | null; note?: string }) =>
    req<{ ok: boolean; symbol: string; plan: WatchPlan }>(`/watchlist/${encodeURIComponent(symbol)}/plan`, {
      method: 'PUT',
      body: JSON.stringify(plan),
    }),
  /** 实时快照（自选股刷新用）；symbols 留空则后端用自选股列表 */
  realtime: (symbols: string[] = []) =>
    req<{ count: number; trading: boolean; quotes: RealtimeQuote[]; hint?: string }>(
      `/realtime${symbols.length ? `?symbols=${encodeURIComponent(symbols.join(','))}` : ''}`,
    ),
  levels: (symbol: string) => req<LevelsResp>(`/levels/${symbol}`),
  profile: (symbol: string) => req<StockProfile>(`/stock/${symbol}/profile`),
  aiStatus: () =>
    req<{ configured: boolean; key_preview: string; models: { id: string; name: string }[]; note: string }>(
      '/ai/status',
    ),
  aiSetKey: (key: string) => post<{ ok: boolean; configured: boolean }>('/ai/key', { key }),
  aiAnalyze: (symbol: string, question = '', model = 'deepseek-chat') =>
    post<{ symbol: string; content: string; model: string; usage: { total_tokens: number } }>('/ai/analyze', {
      symbol,
      question,
      model,
    }),
  sparklines: (symbols: string[], days = 20) =>
    req<{ days: number; count: number; items: Record<string, { closes: number[]; change_pct: number; last: number }> }>(
      `/sparklines?symbols=${symbols.join(',')}&days=${days}`,
    ),
  limitLadder: (days = 30) =>
    req<{
      date: string
      max_boards: number
      counts: { up: number; down: number }
      stats: { first_board: number; ge2: number; ladder_tiers: number; promotion_rate: number }
      tiers: {
        boards: number
        label: string
        count: number
        truncated: number
        stocks: {
          symbol: string
          name: string
          close: number | null
          change_pct: number | null
          turnover: number | null
          amount_yi: number | null
          boards: number
        }[]
      }[]
      downs: {
        symbol: string
        name: string
        close: number | null
        change_pct: number | null
        boards: number
      }[]
    }>(`/limit-ladder?days=${days}`),
  riskOverview: () =>
    req<{
      rules: {
        id: string
        level: string
        level_label: string
        title: string
        desc: string
        needs: string[]
        auto: boolean
        hits: number
        status: string
      }[]
      stats: {
        scanned: number
        flagged: number
        manual: number
        snapshot_days: number
        latest_snapshot: string | null
        updated_at: string | null
        unknown_rules: string[]
      }
      levels: Record<string, string>
    }>('/risk/overview'),
  riskList: (p: { level?: string; rule?: string; limit?: number } = {}) =>
    req<{
      count: number
      items: {
        symbol: string
        name: string
        level: string
        level_label: string
        rules: { id: string; title: string; level: string; detail: string }[]
        watching: { id: string; title: string; progress: string }[]
        manual: boolean
        at: string | null
      }[]
    }>(`/risk/list?level=${p.level ?? ''}&rule=${p.rule ?? ''}&limit=${p.limit ?? 300}`),
  riskBlacklist: () => req<{ count: number; items: Record<string, string> }>('/risk/blacklist'),
  boards: (kind: 'industry' | 'concept' = 'industry', limit = 50) =>
    req<{ kind: string; count: number; items: Board[] }>(`/boards?kind=${kind}&limit=${limit}`),
  boardKline: (
    code: string,
    kind: 'industry' | 'concept' = 'industry',
    name = '',
    period = 'day',
    limit = 120,
  ) =>
    req<{
      code: string
      kind: string
      name: string
      source: string
      period: string
      members: number
      count: number
      rows: Bar[]
    }>(
      `/boards/${code}/kline?kind=${kind}&name=${encodeURIComponent(name)}&period=${period}&limit=${limit}`,
    ),
  boardDetail: (code: string, limit = 30) =>
    req<{ code: string; count: number; items: BoardMember[] }>(`/boards/${code}?limit=${limit}`),
  health: () => req<Health>('/health'),
  overview: () => req<Overview>('/overview'),
  dailySignal: (risk = 3000) => req<DailySignal>(`/daily-signal?risk=${risk}`),
  limitUp: () => req<LimitUpStats>('/limit-up-stats'),
  strategies: () => req<{ items: StrategyMeta[]; errors: Record<string, string> }>('/strategies'),
  metricMembers: (metric: string, limit = 200) =>
    req<{
      date: string
      metric: string
      label: string
      count: number
      items: {
        symbol: string
        name: string
        close: number | null
        change_pct: number | null
        turnover: number | null
        amount_yi: number | null
      }[]
    }>(`/market/metric-members?metric=${encodeURIComponent(metric)}&limit=${limit}`),  dataAudit: () =>
    req<{
      generation: string | null
      stocks: {
        in_store: number
        in_names: number
        missing_in_store: number
        missing_preview: { symbol: string; name: string }[]
        extra_in_store: number
        bars_min: number
        bars_median: number
        latest_date: string | null
        stale_today: number
      }
      boards: {
        total: number
        members_in_store: number
        members_missing: number
        thin_preview: { board: string; in_store: number; total: number }[]
        thin_count: number
      }
    }>('/data/audit'),
  symbolSearch: (q: string, limit = 12) =>
    req<{ count: number; items: { symbol: string; name: string }[] }>(
      `/symbols/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  symbolNames: () => req<{ count: number; items: Record<string, string> }>('/symbol-names'),
  kline: (symbol: string, limit = 120, start?: string, period = 'day') =>
    req<{ symbol: string; period?: string; rows: Bar[] }>(
      `/kline/${symbol}?limit=${limit}&period=${period}${start ? `&start=${start}` : ''}`,
    ),
  /** 分钟级 K 线：60 / 120 / 240 分钟（后端现抓东财，120/240 由 60 分钟聚合） */
  klineMin: (symbol: string, period = 60, limit = 600) =>
    req<{ symbol: string; period: string; count: number; rows: Bar[] }>(
      `/kline-min/${symbol}?period=${period}&limit=${limit}`,
    ),
  /** 分时：当日 1 分钟走势（东财 trends2） */
  klineTrend: (symbol: string, days = 1) =>
    req<{
      symbol: string
      name?: string
      pre_close?: number
      count: number
      rows: { date: string; open: number; close: number; high: number; low: number; volume: number; avg?: number | null }[]
    }>(`/kline-trend/${symbol}?days=${days}`),
  tasks: (limit = 20) => req<{ count: number; items: TaskItem[] }>(`/tasks?limit=${limit}`),
  task: (id: string) => req<TaskItem>(`/tasks/${id}`),
  submit: (type: string, params: Record<string, unknown>) =>
    req<{ task_id: string }>('/tasks', { method: 'POST', body: JSON.stringify({ type, params }) }),
  btRun: (body: BtParams) =>
    req<BacktestResult>('/bt/run', { method: 'POST', body: JSON.stringify(body) }),

  /* 回测历史（后端落盘 data/runs/*.json） */
  runs: (limit = 30) => req<{ items: RunMeta[]; count: number }>(`/runs?limit=${limit}`),
  runDetail: (id: string) => req<RunDetail>(`/runs/${encodeURIComponent(id)}`),
  deleteRun: (id: string) => req<{ deleted: string }>(`/runs/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /* 策略库 —— 开关直接改 strategies/enabled.json 并重载引擎 */
  strategyLib: () => req<StrategyLibResp>('/strategy-lib'),
  toggleStrategy: (id: string, enabled: boolean) =>
    post<{ ok: boolean; id: string; enabled: boolean; loaded_count: number }>(
      '/strategy-lib/toggle',
      { id, enabled },
    ),
  reloadStrategies: () =>
    post<{ count: number; errors: Record<string, string> }>('/strategy-lib/reload', {}),
  /** 旧后端兼容：只读策略排名 */
  strategyRanking: () =>
    req<{
      count: number
      ranked: number
      items: {
        id: string
        name: string
        tags?: string[]
        rank: number | null
        return: number | null
        mdd: number | null
        sharpe: number | null
        win_rate: number | null
        trades: number | null
      }[]
      note?: string
    }>('/strategy-ranking'),

  /* 单个策略的增删改 */
  strategyDetail: (id: string, includeSource = true) =>
    req<{
      id: string
      file: string
      meta: Record<string, unknown>
      params: { id?: string; label?: string; type?: string; default?: unknown }[]
      lines: number
      chars: number
      editable: boolean
      enabled: boolean
      disabled_reason?: string | null
      source: string | null
      truncated: boolean
    }>(`/strategy-lib/${id}?include_source=${includeSource ? 'true' : 'false'}`),
  updateStrategy: (
    id: string,
    body: { name?: string; description?: string; tags?: string[]; params?: unknown[] },
  ) =>
    req<{ ok: boolean; id: string; changed: string[] }>(`/strategy-lib/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteStrategy: (id: string) =>
    req<{ ok: boolean; id: string; moved_to: string }>(`/strategy-lib/${id}`, {
      method: 'DELETE',
    }),
  createStrategy: (id: string, name: string) =>
    post<{ ok: boolean; id: string; file: string }>('/strategy-lib/new', { id, name }),
  restoreStrategy: (id: string) =>
    post<{ ok: boolean; id: string; restored_to: string }>(`/strategy-lib/${id}/restore`, {}),
}

/* ---------------- 指数 / 板块 ---------------- */
export interface IndexQuote {
  symbol: string
  name: string
  key: string
  price?: number
  change?: number
  change_pct?: number
  high?: number
  low?: number
  amount?: number
  available: boolean
}

export interface Board {
  code: string
  name: string
  change_pct: number
  price?: number
  market_cap?: number
  up_count: number
  down_count: number
  leader?: string
  /** 龙头股 6 位代码（同花顺给的，用来点龙头直接跳个股分析） */
  leader_symbol?: string
  leader_pct?: number
  /** 行业板块：成交额（亿） */
  amount?: number
  /** 成交额（亿）——后端同时给 amount(元) 与 amount_yi(亿)，显示优先用它 */
  amount_yi?: number
  /** 概念板块：创建日期 / 成分数 */
  created?: string
  members?: number
}

export interface BoardMember {
  symbol: string
  name: string
  price: number
  change_pct: number
  volume?: number
  amount?: number
  turnover?: number
  vol_ratio?: number
}
/* ---------------- 支撑 / 压力 / 目标位 ---------------- */
export interface PriceLevel {
  price: number
  real_price?: number
  label: string
  kind: string
  touches?: number
  rr?: number | null
  distance_pct?: number
}

export interface LevelsResp {
  symbol: string
  date: string
  close: number
  real_close: number
  adjust_factor: number
  ma20: number
  ma60: number
  lookback: number
  swing: number
  supports: PriceLevel[]
  resistances: PriceLevel[]
  stop: PriceLevel
  targets: PriceLevel[]
  note: string
}
/* ---------------- 个股档案 ---------------- */
export interface OrderLevel {
  level: number
  price: number
  volume: number
  amount: number
}

export interface StockQuote {
  symbol: string
  name: string
  price: number
  prev_close: number
  open: number
  high: number
  low: number
  change: number
  change_pct: number
  volume: number
  amount: number
  turnover: number
  pe_ttm: number
  pb: number
  float_cap: number
  total_cap: number
  limit_up: number
  limit_down: number
  vol_ratio: number
  amplitude: number
  bids: OrderLevel[]
  asks: OrderLevel[]
  note?: string
}

export interface StockProfile {
  quote: StockQuote
  industry: { name: string; code: string }[]
  concept: { name: string; code: string }[]
}
/* ---------- 自选股 ---------- */
export interface WatchPlan {
  entry?: number | null
  stop?: number | null
  target?: number | null
  note?: string
}

export interface WatchItem {
  symbol: string
  name?: string
  grp?: string
  /** note 字段存的是交易计划的 JSON 字符串，读的时候要 parse */
  note?: string
  added_at?: string
}

export interface RealtimeQuote {
  /** trading=盘中实时；closed=非交易时段（拿到的是收盘价，age_ms 不代表行情新鲜度） */
  session?: 'trading' | 'closed'
  symbol: string
  name?: string
  price?: number
  change_pct?: number
  available?: boolean
  age_ms?: number
  /** 实时行情扩展字段（腾讯源）：涨跌、昨收、高低、成交额、量比、市值、市盈率 */
  prev_close?: number
  open?: number
  high?: number
  low?: number
  change?: number
  volume?: number
  amount?: number
  turnover?: number
  vol_ratio?: number
  market_cap?: number
  pe_ttm?: number
}
/* ---------- 首页看板 ---------- */
export interface DashBucket {
  label: string
  count: number
}
export interface DashStock {
  symbol: string
  name?: string
  close?: number | null
  change_pct?: number | null
  amount_yi?: number | null
  turnover?: number | null
}
export interface DashboardData {
  date: string
  empty: boolean
  total: number
  breadth: {
    up: number
    flat: number
    down: number
    up_pct: number
    strong: number
    weak: number
    limit_up: number
    limit_down: number
    median_pct: number
    avg_pct: number
    distribution: DashBucket[]
  }
  amount: { total_yi: number; avg_yi: number; real: boolean }
  turnover: {
    avg: number | null
    high_count: number | null
    vol_ratio?: number | null
    expand_ratio?: number | null
  }
  monitor: {
    broken_board: number | null
    limit_down: number
    above_ma60: number | null
    new_high_60: number | null
    new_low_60: number | null
    high_turnover: number | null
    expand_ratio: number | null
  }
  trend: {
    above_ma5: number | null
    above_ma20: number | null
    above_ma60: number | null
    new_high_20: number | null
    new_low_20: number | null
    new_high_60?: number | null
    new_low_60?: number | null
    hl_ratio?: number | null
  }
  emotion: { score: number; dims: { key: string; label: string; value: number }[] }
  ladder: {
    items: { label: string; count: number; names?: string[] }[]
    total: number
    date?: string
    top?: { symbol: string; name: string; boards: number } | null
  }
  boards: {
    date: string | null
    updated?: string | null
    industry: {
      count: number
      up: { board: string; change_pct: number; members: number; up?: number | null; down?: number | null }[]
      down: { board: string; change_pct: number; members: number; up?: number | null; down?: number | null }[]
    }
    concept: {
      count: number
      up: { board: string; change_pct: number; members: number; up?: number | null; down?: number | null }[]
      down: { board: string; change_pct: number; members: number; up?: number | null; down?: number | null }[]
    }
  }
  ranks: { gain: DashStock[]; loss: DashStock[]; amount: DashStock[]; turnover: DashStock[] }
}