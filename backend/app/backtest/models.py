"""账户级回测的数据结构 — 全部不可变值对象，方便测试时逐条比对。

设计原则：
  1. 成交价、费用、拒绝原因全部落到对象里，事后可复算
  2. 不藏状态：Account 只存现金与持仓，权益由 mark-to-market 算出来
  3. 拒绝原因用枚举，测试可以直接断言字符串
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"      # 已生成，等待下一交易日开盘
    FILLED = "filled"
    REJECTED = "rejected"


class RejectReason(str, Enum):
    SUSPENDED = "suspended"          # 停牌：当日无行情
    LIMIT_UP = "limit_up"            # 涨停开盘，买不进
    LIMIT_DOWN = "limit_down"        # 跌停开盘，卖不出
    NO_CASH = "no_cash"              # 现金不足（含费用）
    LOT = "lot"                      # 不足 100 股
    NO_POSITION = "no_position"      # 无持仓可卖
    T1_LOCKED = "t1_locked"          # T+1：当日买入不可卖
    NO_BAR = "no_bar"                # 当日无该标的行情
    NO_TOUCH = "no_touch"            # 打板单：当日没触及涨停价，不成交


@dataclass(frozen=True, slots=True)
class Bar:
    """统一 K 线。D1 的 dt 是当日 00:00，M30 是交易所本地时间（含时分）。

    `slots=True` 是硬需求不是优化：全市场 400 万行回测时，
    带 __dict__ 的 dataclass 每行约 250 字节（1 GB），
    slots 版约 72 字节（290 MB）—— 差出来的 700 MB 就是"回测闪退"的根源。
    """

    symbol: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    pre_close: float
    change_pct: float = 0.0
    turnover: float = 0.0

    @property
    def date(self) -> date:
        return self.dt.date()

    @property
    def is_one_word(self) -> bool:
        """一字板：全天只有一个价格。"""
        return self.high <= self.low + 1e-9


@dataclass
class FeeConfig:
    """A股费用。默认值取 2024 年后的常见券商口径。"""

    commission_rate: float = 0.00025     # 佣金 万 2.5
    commission_min: float = 5.0          # 单笔最低 5 元
    stamp_tax_rate: float = 0.0005       # 印花税 0.05%（仅卖出）
    transfer_fee_rate: float = 0.00001   # 过户费 0.001%（双向）
    slippage_bps: float = 5.0            # 滑点 5bp


@dataclass
class FeeBreakdown:
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    slippage_cost: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee

    def as_dict(self) -> dict:
        return {
            "commission": round(self.commission, 4),
            "stamp_tax": round(self.stamp_tax, 4),
            "transfer_fee": round(self.transfer_fee, 4),
            "slippage_cost": round(self.slippage_cost, 4),
            "total": round(self.total, 4),
        }


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    qty: int
    signal_dt: datetime          # 信号产生的 K 线时间
    signal_price: float          # 信号当根收盘价（仅记录，不用于成交）
    source: str = "D1"           # 'D1' | 'M30'
    score: float = 0.0           # 信号强度，用于多周期资金竞争排序
    stop_price: float | None = None
    take_profit: float | None = None
    max_hold_days: int | None = None
    # 订单级风控参数（多策略并联时每个策略用自己的止损）
    stop_loss_pct: float | None = None
    # 持仓规则（由策略声明）：'unseal_next_open' = 买入当日没封板则次日开盘卖
    # 可叠多条，逗号分隔，例如 "unseal_next_open,break_ma_next_open:5"
    exit_rule: str | None = None
    # 买入方式（由策略声明）：'limit_up' = 打板，盘中触及涨停价才成交、成交价就是涨停价
    entry_rule: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    reject_reason: RejectReason | None = None
    exec_dt: datetime | None = None
    exec_price: float | None = None
    fees: FeeBreakdown = field(default_factory=FeeBreakdown)


@dataclass
class Position:
    symbol: str
    qty: int
    avg_cost: float              # 含费用的每股成本
    open_dt: datetime
    source: str = "D1"
    stop_price: float | None = None
    take_profit: float | None = None
    max_hold_days: int | None = None
    high_water: float = 0.0      # 移动止损用
    entry_fees: float = 0.0
    # 持仓规则：'unseal_next_open' = 买入当日没封板，次日开盘清仓（打板失败走人）
    exit_rule: str | None = None
    failed: bool = False         # 已判定触发离场规则，次日开盘执行
    fail_reason: str = "fail_exit"   # 触发原因：fail_exit / break_ma

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized(self, price: float) -> float:
        return (price - self.avg_cost) * self.qty


@dataclass
class Fill:
    """一笔成交，同时也是交易明细的一行。"""

    order_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    dt: datetime
    signal_dt: datetime
    fees: FeeBreakdown
    source: str = "D1"
    reason: str = ""             # 退出原因：stop/take_profit/signal/max_hold/...
    realized_pnl: float = 0.0    # 卖出时结算（已扣买入摊薄费用与卖出费用）
    entry_price: float = 0.0     # 卖出时的持仓成本价（含买入费用摊到每股）；买入为成交价
    pnl_pct: float = 0.0         # 卖出时的收益率 = realized_pnl / (成本价 × 股数)

    @property
    def latency_minutes(self) -> float:
        """信号到成交的延迟（分钟）。"""
        return (self.dt - self.signal_dt).total_seconds() / 60.0


@dataclass
class Snapshot:
    """每日收盘后的账户快照。"""

    date: date
    cash: float
    frozen: float
    market_value: float
    equity: float
    nav: float
    positions: int
    total_fees: float
    realized_pnl: float
    unrealized_pnl: float


@dataclass
class BacktestConfig:
    init_cash: float = 100_000.0
    risk_pct: float = 0.01           # 单笔风险（按权益百分比）——risk_amount 为空时生效
    risk_amount: float | None = None  # 单笔固定止损金额（元）——优先级高于 risk_pct
    max_positions: int = 10
    max_position_pct: float = 0.20   # 单票最大 20%
    stop_loss_pct: float | None = 0.08
    take_profit_pct: float | None = None
    atr_stop_mult: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_days: int | None = 20   # 默认 20 个自然日，避免仓位被长期锁死
    fees: FeeConfig = field(default_factory=FeeConfig)
    period: str = "D1"               # 'D1' | 'M30' | 'combo'
    lot_size: int = 100
    # 多策略并联：source → 资金份额（0~1），空表示不限制
    strategy_budget: dict = field(default_factory=dict)
    # 多策略并联：source → 该策略自己的参数（stop_loss_pct / take_profit_pct / max_hold_days）
    strategy_params: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
    config: BacktestConfig
    snapshots: list[Snapshot]
    fills: list[Fill]
    orders: list[Order]
    metrics: dict
    rejected: dict[str, int]
    warnings: list[str] = field(default_factory=list)

    @property
    def nav_series(self) -> list[tuple[str, float]]:
        return [(s.date.isoformat(), s.nav) for s in self.snapshots]
