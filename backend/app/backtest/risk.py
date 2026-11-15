"""仓位与风控 — 固定风险模型（Van Tharp 口径）。

    风险金额 = 账户权益 × risk_pct
    每股风险 = 买入价 − 止损价
    理论股数 = 风险金额 / 每股风险

再依次受三条上限钳制，最后向下取整到 100 股：
    1. 单票市值 ≤ 权益 × max_position_pct
    2. 现金（含费用）
    3. 最大持仓数量（由 engine 在外层检查）

止损价缺失时（策略没给结构位），退回按 stop_loss_pct 计算。
"""
from __future__ import annotations

from .broker import Broker, lot_floor
from .models import BacktestConfig


def resolve_stop_price(entry: float, stop: float | None, config: BacktestConfig) -> float:
    if stop is not None and 0 < stop < entry:
        return stop
    if config.stop_loss_pct:
        return round(entry * (1 - config.stop_loss_pct), 3)
    return 0.0


def resolve_target_price(entry: float, target: float | None, config: BacktestConfig) -> float:
    if target is not None and target > entry:
        return target
    if config.take_profit_pct:
        return round(entry * (1 + config.take_profit_pct), 3)
    return 0.0


def size_by_risk(
    equity: float,
    cash: float,
    entry_price: float,
    stop_price: float,
    config: BacktestConfig,
    broker: Broker,
) -> tuple[int, str]:
    """返回 (股数, 说明)。股数 0 表示不可建仓。

    两种定仓方式，`risk_amount` 优先：
      · 固定金额：止损最多亏 risk_amount 元 → 股数 = risk_amount / (买价 − 止损价)
      · 固定比例：止损最多亏 权益 × risk_pct 元
    """
    if entry_price <= 0:
        return 0, "价格无效"
    per_share_risk = entry_price - stop_price
    if per_share_risk <= 0:
        return 0, "止损价不低于买入价"

    if config.risk_amount:
        risk_budget = float(config.risk_amount)
        mode = f"固定止损{risk_budget:.0f}元"
    else:
        risk_budget = equity * config.risk_pct
        mode = f"风险{config.risk_pct*100:.0f}%={risk_budget:.0f}元"

    raw = risk_budget / per_share_risk

    cap_by_position = (equity * config.max_position_pct) / entry_price
    raw = min(raw, cap_by_position)

    qty = lot_floor(raw, config.lot_size)
    qty = broker.affordable_qty(cash, entry_price, qty)
    if qty < config.lot_size:
        return 0, "资金或风险预算不足一手"
    actual = qty * per_share_risk
    return qty, f"{mode} · 实际止损{actual:.0f}元 / 仓位{qty * entry_price:.0f}元"


def adjust_stops(
    high_water: float,
    entry: float,
    stop: float,
    config: BacktestConfig,
) -> float:
    """移动止损：只上移不下移。"""
    if not config.trailing_stop_pct:
        return stop
    candidate = round(high_water * (1 - config.trailing_stop_pct), 3)
    return max(stop, candidate) if stop > 0 else candidate
