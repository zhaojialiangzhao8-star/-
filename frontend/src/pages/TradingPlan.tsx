import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Calculator, Search, TriangleAlert } from 'lucide-react'
import { api } from '../services/api'
import { Card, Metric } from '../components/ui'
import { fmtPct, pctClass } from '../lib/format'

/** 手数与费用规则（A 股） */
const LOT = 100
const FEE = {
  commission: 0.00025, // 佣金万 2.5（双边）
  minCommission: 5, // 最低 5 元
  stampTax: 0.0005, // 印花税，卖出单边
  transfer: 0.00001, // 过户费万 0.1（双边）
}

interface Plan {
  riskBudget: number
  perShareRisk: number
  rawQty: number
  qty: number
  invest: number
  buyFee: number
  sellFee: number
  actualRisk: number
  targetProfit: number
  rr: number
  capitalPct: number
  warnings: string[]
}

function calc(
  capital: number,
  riskAmount: number,
  entry: number,
  stop: number,
  target: number,
): Plan | null {
  if (!capital || !entry || !stop || entry <= stop) return null
  const perShareRisk = entry - stop
  const rawQty = riskAmount / perShareRisk
  let qty = Math.floor(rawQty / LOT) * LOT

  // 现金约束：买入金额 + 买入费用 ≤ capital
  const unitCost = entry * (1 + FEE.commission + FEE.transfer)
  const maxByCash = Math.floor(capital / unitCost / LOT) * LOT
  qty = Math.min(qty, maxByCash)

  if (qty < LOT) return null

  const invest = qty * entry
  const buyFee = Math.max(invest * FEE.commission, FEE.minCommission) + invest * FEE.transfer
  const sellValue = qty * target
  const sellFee =
    Math.max(sellValue * FEE.commission, FEE.minCommission) +
    sellValue * FEE.stampTax +
    sellValue * FEE.transfer
  const actualRisk = qty * perShareRisk + buyFee + sellFee
  const targetProfit = (sellValue - qty * entry) - buyFee - sellFee
  const rr = actualRisk > 0 ? targetProfit / actualRisk : 0
  const capitalPct = (invest + buyFee) / capital

  const warnings: string[] = []
  const byRisk = Math.floor(rawQty / LOT) * LOT
  if (qty < byRisk) {
    warnings.push(`受现金限制：按风险可买 ${byRisk} 股，现金只够 ${qty} 股`)
  }
  if (actualRisk > capital * 0.03) warnings.push(`单笔风险占账户 ${fmtPct((actualRisk / capital) * 100, 1, false)}，超过 3% 建议上限`)
  if (rr < 1.5) warnings.push(`盈亏比 ${rr.toFixed(2)} 低于 1.5，长期期望偏低`)
  if (target <= entry) warnings.push('目标价低于入场价，盈亏比无意义')

  return { riskBudget: riskAmount, perShareRisk, rawQty, qty, invest, buyFee, sellFee, actualRisk, targetProfit, rr, capitalPct, warnings }
}

export default function TradingPlan() {
  const [capital, setCapital] = useState(100_000)
  const [mode, setMode] = useState<'fixed' | 'pct'>('fixed')
  const [riskAmount, setRiskAmount] = useState(3000)
  const [riskPct, setRiskPct] = useState(1)
  const [symbol, setSymbol] = useState('605258.SH')
  const [entry, setEntry] = useState(38.51)
  const [stop, setStop] = useState(33.89)
  const [target, setTarget] = useState(46)

  const q = useQuery({
    queryKey: ['kline', symbol],
    queryFn: () => api.kline(symbol, 5),
    retry: 0,
    enabled: false,
  })

  const budget = mode === 'fixed' ? riskAmount : (capital * riskPct) / 100
  const plan = useMemo(() => calc(capital, budget, entry, stop, target), [capital, budget, entry, stop, target])
  const priceNow = q.data?.rows?.at(-1)?.close

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line bg-bg2 px-4 py-2.5">
        <h2 className="page-title">交易计划</h2>
        <span className="font-mono text-xs2 text-fg3">固定止损金额 → 反推仓位 · 含费用 · 100 股取整</span>
        <div className="ml-auto font-mono text-xs2 text-fg3">
          佣金 万2.5 · 印花税 0.05%（卖）· 过户费 万0.1 · 最低佣金 5 元
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-12 gap-3">
          {/* 输入 */}
          <Card className="col-span-4" title="输入" subtitle="计划参数">
            <div className="space-y-2.5">
              <Field label="账户总资金（元）">
                <Num value={capital} onChange={setCapital} />
              </Field>

              <div>
                <span className="mb-1 block text-[11px] text-fg3">风险口径</span>
                <div className="flex gap-1.5">
                  {(['fixed', 'pct'] as const).map((m) => (
                    <button
                      key={m}
                      onClick={() => setMode(m)}
                      className={`flex-1 rounded border px-2 py-1.5 text-sm2 transition-colors ${
                        mode === m
                          ? 'border-brand bg-brand/15 text-fg'
                          : 'border-line2 bg-card2 text-fg3 hover:text-fg2'
                      }`}
                    >
                      {m === 'fixed' ? '固定金额' : '按比例'}
                    </button>
                  ))}
                </div>
              </div>

              {mode === 'fixed' ? (
                <Field label="单笔止损金额（元）">
                  <Num value={riskAmount} onChange={setRiskAmount} />
                </Field>
              ) : (
                <Field label="单笔风险（%）">
                  <Num value={riskPct} onChange={setRiskPct} />
                </Field>
              )}

              <div className="border-t border-line pt-3">
                <Field label="股票代码（可选，查现价）">
                  <div className="flex gap-1.5">
                    <input
                      value={symbol}
                      onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                      className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 font-mono text-sm2 outline-none focus:border-brand"
                    />
                    <button
                      onClick={() => q.refetch()}
                      className="rounded border border-line2 bg-card2 px-2 text-fg2 hover:text-fg"
                      title="查现价"
                    >
                      <Search size={13} />
                    </button>
                  </div>
                </Field>
                {priceNow != null && (
                  <div className="mt-1.5 flex items-center gap-2 text-xs2 text-fg3">
                    现价 <b className="num text-fg2">{priceNow.toFixed(2)}</b>
                    <button
                      onClick={() => {
                        setEntry(priceNow)
                        setStop(Number((priceNow * 0.88).toFixed(2)))
                        setTarget(Number((priceNow * 1.2).toFixed(2)))
                      }}
                      className="rounded border border-line2 px-1.5 py-0.5 hover:text-fg"
                    >
                      一键填入（−12% / +20%）
                    </button>
                  </div>
                )}
              </div>

              <div className="border-t border-line pt-2.5 space-y-2.5">
                <Field label="计划入场价">
                  <Num value={entry} onChange={setEntry} step={0.01} />
                </Field>
                <Field label="止损价">
                  <Num value={stop} onChange={setStop} step={0.01} />
                </Field>
                <Field label="目标价">
                  <Num value={target} onChange={setTarget} step={0.01} />
                </Field>
              </div>

              {entry > 0 && stop > 0 && stop < entry && (
                <div className="text-xs2 text-fg3">
                  止损幅度 {fmtPct(((stop - entry) / entry) * 100)} · 目标幅度{' '}
                  <span className={pctClass(((target - entry) / entry) * 100)}>
                    {fmtPct(((target - entry) / entry) * 100)}
                  </span>
                </div>
              )}
            </div>
          </Card>

          {/* 结果 */}
          <div className="col-span-8 space-y-3">
            <Card title="仓位计算" subtitle={`风险预算 ${budget.toLocaleString()} 元`}>
              {plan ? (
                <>
                  <div className="grid grid-cols-4 gap-2">
                    <Metric label="买入股数" value={<span className="text-gold">{plan.qty.toLocaleString()} 股</span>} />
                    <Metric label="投入资金" value={Math.round(plan.invest).toLocaleString()} />
                    <Metric label="占账户" value={fmtPct(plan.capitalPct * 100, 1, false)} />
                    <Metric label="每股风险" value={plan.perShareRisk.toFixed(2)} />
                    <Metric
                      label="止损亏损"
                      value={Math.round(plan.actualRisk).toLocaleString()}
                      tone="text-down"
                    />
                    <Metric
                      label="目标盈利"
                      value={Math.round(plan.targetProfit).toLocaleString()}
                      tone="text-up"
                    />
                    <Metric label="盈亏比" value={plan.rr.toFixed(2)} tone={plan.rr >= 2 ? 'text-up' : plan.rr >= 1.5 ? 'text-gold' : 'text-down'} />
                    <Metric label="买入费用" value={plan.buyFee.toFixed(1)} />
                  </div>

                  <div className="mt-3 grid grid-cols-2 gap-3 text-sm2">
                    <div className="rounded-md bg-card2 px-3 py-2">
                      <div className="mb-1 text-xs2 text-fg3">按风险算出的理论股数</div>
                      <div className="num text-xs">{Math.floor(plan.rawQty / LOT) * LOT} 股</div>
                      <div className="mt-0.5 text-xs2 text-fg3">
                        {budget.toLocaleString()} ÷ {plan.perShareRisk.toFixed(2)} = {plan.rawQty.toFixed(1)} 股
                      </div>
                    </div>
                    <div className="rounded-md bg-card2 px-3 py-2">
                      <div className="mb-1 text-xs2 text-fg3">卖出费用（按目标价）</div>
                      <div className="num text-xs">{plan.sellFee.toFixed(1)} 元</div>
                      <div className="mt-0.5 text-xs2 text-fg3">含佣金 + 印花税 + 过户费</div>
                    </div>
                  </div>

                  {plan.warnings.length > 0 && (
                    <div className="mt-3 space-y-1.5">
                      {plan.warnings.map((w) => (
                        <div
                          key={w}
                          className="flex items-start gap-1.5 rounded-md border border-gold/30 bg-gold/10 px-2.5 py-1.5 text-sm2 text-gold"
                        >
                          <TriangleAlert size={12} className="mt-0.5 shrink-0" />
                          {w}
                        </div>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <div className="py-10 text-center text-sm2 text-fg3">
                  <Calculator size={22} className="mx-auto mb-2 opacity-50" />
                  填写入场价与止损价（止损必须低于入场价）后才能计算
                </div>
              )}
            </Card>

            <Card title="盈亏分布" subtitle="按当前计划">
              {plan ? (
                <div className="space-y-2.5">
                  <BarRow
                    label="止损出局"
                    value={-plan.actualRisk}
                    max={Math.max(plan.actualRisk, plan.targetProfit)}
                    tone="down"
                  />
                  <BarRow
                    label="目标达成"
                    value={plan.targetProfit}
                    max={Math.max(plan.actualRisk, plan.targetProfit)}
                    tone="up"
                  />
                  <div className="mt-3 grid grid-cols-3 gap-2 border-t border-line pt-3 text-xs2 text-fg3">
                    <div>
                      保本价
                      <div className="num mt-0.5 text-xs text-fg2">
                        {((plan.invest + plan.buyFee + plan.qty * entry * FEE.stampTax) / plan.qty).toFixed(2)}
                      </div>
                    </div>
                    <div>
                      需要涨幅保本
                      <div className="num mt-0.5 text-xs text-fg2">
                        {fmtPct((((plan.buyFee + plan.qty * entry * FEE.stampTax) / plan.invest) * 100))}
                      </div>
                    </div>
                    <div>
                      期望收益（按 50% 胜率）
                      <div className="num mt-0.5 text-xs text-fg2">
                        {Math.round((plan.targetProfit - plan.actualRisk) / 2).toLocaleString()} 元
                      </div>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="py-6 text-center text-sm2 text-fg3">无数据</div>
              )}
            </Card>

            <Card title="风控规则" subtitle="来自回测引擎的不可越过边界">
              <ul className="space-y-1 text-sm2 leading-[1.65] text-fg2">
                <li>· 单笔风险固定 → 股数 = 止损金额 ÷（入场价 − 止损价），向下取整 100 股</li>
                <li>· 买入受 T+1 限制：当日买入次日才能卖</li>
                <li>· 涨停开盘无法买入，跌停开盘无法卖出（回测里计为拒单）</li>
                <li>· 停牌期间不能交易，市价用停牌前收盘价</li>
                <li>· 费用：佣金万 2.5（最低 5 元）、印花税 0.05%（仅卖出）、过户费万 0.1</li>
                <li>· 建议单笔风险不超过账户 2%，最多同时 5~10 只</li>
              </ul>
            </Card>
          </div>
        </div>
      </div>
    </>
  )
}

function BarRow({ label, value, max, tone }: { label: string; value: number; max: number; tone: 'up' | 'down' }) {
  const pct = max > 0 ? Math.min(Math.abs(value) / max, 1) * 100 : 0
  const color = tone === 'up' ? 'bg-up' : 'bg-down'
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-sm2">
        <span className="text-fg3">{label}</span>
        <span className={`num font-semibold ${tone === 'up' ? 'text-up' : 'text-down'}`}>
          {value > 0 ? '+' : ''}
          {Math.round(value).toLocaleString()} 元
        </span>
      </div>
      <div className="h-[6px] overflow-hidden rounded bg-card2">
        <div className={`h-full rounded ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] text-fg3">{label}</span>
      {children}
    </label>
  )
}

function Num({ value, onChange, step = 1 }: { value: number; onChange: (v: number) => void; step?: number }) {
  return (
    <input
      type="number"
      step={step}
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      className="w-full rounded border border-line2 bg-card2 px-2 py-1.5 text-right font-mono text-sm2 outline-none focus:border-brand"
    />
  )
}
