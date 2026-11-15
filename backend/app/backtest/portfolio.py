"""账户记账 — 只有现金和持仓是真的，权益由盯市算出来。

一条硬规则：**绝不用「股票收益率平均」充当账户收益**。
权益必须 = 现金 + 持仓市值，持仓市值必须用当日收盘价。
"""
from __future__ import annotations

from datetime import date

from .broker import Broker
from .models import Fill, Position, Side, Snapshot


class Portfolio:
    def __init__(self, init_cash: float, broker: Broker) -> None:
        self.init_cash = init_cash
        self.cash = init_cash
        self.frozen = 0.0
        self.broker = broker
        self.positions: dict[str, Position] = {}
        self.total_fees = 0.0
        self.realized_pnl = 0.0

    # ---------- 成交入账 ----------
    def apply_fill(self, fill: Fill, dt_iso: str | None = None) -> float:
        """返回这笔成交实现的盈亏（买入记 0）。"""
        self.total_fees += fill.fees.total
        if fill.side is Side.BUY:
            cost = fill.price * fill.qty + fill.fees.total
            self.cash -= cost
            pos = self.positions.get(fill.symbol)
            if pos is None:
                self.positions[fill.symbol] = Position(
                    symbol=fill.symbol,
                    qty=fill.qty,
                    avg_cost=cost / fill.qty,
                    open_dt=fill.dt,
                    source=fill.source,
                    high_water=fill.price,
                    entry_fees=fill.fees.total,
                )
            else:
                total_qty = pos.qty + fill.qty
                pos.avg_cost = (pos.avg_cost * pos.qty + cost) / total_qty
                pos.qty = total_qty
                pos.high_water = max(pos.high_water, fill.price)
            return 0.0

        # 卖出
        pos = self.positions.get(fill.symbol)
        if pos is None:
            return 0.0
        proceeds = fill.price * fill.qty - fill.fees.total
        self.cash += proceeds
        pnl = (fill.price - pos.avg_cost) * fill.qty - fill.fees.total
        self.realized_pnl += pnl
        pos.qty -= fill.qty
        if pos.qty <= 0:
            self.positions.pop(fill.symbol, None)
        return pnl

    # ---------- 盯市 ----------
    def market_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            px = prices.get(sym, pos.avg_cost)
            total += pos.market_value(px)
        return total

    def unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            px = prices.get(sym, pos.avg_cost)
            total += pos.unrealized(px)
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.frozen + self.market_value(prices)

    def snapshot(self, dt: date, prices: dict[str, float]) -> Snapshot:
        mv = self.market_value(prices)
        eq = self.cash + self.frozen + mv
        return Snapshot(
            date=dt,
            cash=round(self.cash, 2),
            frozen=round(self.frozen, 2),
            market_value=round(mv, 2),
            equity=round(eq, 2),
            nav=round(eq / self.init_cash, 6),
            positions=len(self.positions),
            total_fees=round(self.total_fees, 2),
            realized_pnl=round(self.realized_pnl, 2),
            unrealized_pnl=round(self.unrealized(prices), 2),
        )

    def can_afford(self, price: float, qty: int) -> bool:
        fees = self.broker.calc_fees(price, qty, Side.BUY)
        return self.cash >= price * qty + fees.total
