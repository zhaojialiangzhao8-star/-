"""账户级回测编排器。

一天的顺序是固定的，不能调换：

    09:25  开盘撮合  ← 执行「上一交易日收盘后」产生的订单
    09:30  盘中      ← 用当日 high/low 检查止损止盈（仅对 T-1 及更早的持仓）
    15:00  收盘      ← 用**当日完整 K 线**算信号，生成 T+1 开盘执行的订单
    15:05  结算      ← 权益 = 现金 + 持仓市值，写快照

「不追当日收盘价成交」是这套引擎和旧信号统计最本质的区别。
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import date, datetime
from typing import Callable

import polars as pl

from . import metrics as M
from .broker import Broker, board_limit_pct
from .data import BarStore
from .models import (
    BacktestConfig,
    BacktestResult,
    Fill,
    Order,
    OrderStatus,
    RejectReason,
    Side,
    Snapshot,
)
from .portfolio import Portfolio
from .risk import adjust_stops, resolve_stop_price, resolve_target_price, size_by_risk

# 策略钩子：给定当日信号，决定是否下单。返回 None 表示用默认等权风险模型。
SignalHook = Callable[[date, dict[str, float], Portfolio], list[Order]]


class BacktestEngine:
    def __init__(self, config: BacktestConfig, bars: BarStore, is_st: set[str] | None = None) -> None:
        self.config = config
        self.bars = bars
        self.is_st = is_st or set()
        # 收盘价窗口：break_ma 规则算均线用（60 根足够 MA5/10/20）
        self._close_win: dict[str, deque] = {}
        # 高低价窗口：swing_high 规则判定摆动高点用（攒 30 根足够左看 10）
        self._high_win: dict[str, deque] = {}
        self._low_win: dict[str, deque] = {}
        self.broker = Broker(config.fees, config.lot_size)
        self.portfolio = Portfolio(config.init_cash, self.broker)
        self.pending: list[Order] = []
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.snapshots: list[Snapshot] = []
        self.rejected: dict[str, int] = {}
        self.warnings: list[str] = []
        self._seq = 0
        self._last_closes: dict[str, float] = {}

    # ---------- 工具 ----------
    def _oid(self) -> str:
        self._seq += 1
        return f"O{self._seq:06d}"

    def _reject(self, order: Order, reason: RejectReason) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.rejected[reason.value] = self.rejected.get(reason.value, 0) + 1

    def _last_equity(self) -> float:
        return self.snapshots[-1].equity if self.snapshots else self.config.init_cash

    # ---------- 开盘撮合 ----------
    def _source_params(self, source: str) -> dict:
        return (self.config.strategy_params or {}).get(source, {})

    def _source_used(self, source: str, prices: dict[str, float]) -> float:
        return sum(
            p.market_value(prices.get(sym, p.avg_cost))
            for sym, p in self.portfolio.positions.items()
            if p.source == source
        )

    def _open_auction(self, d: date, day_bars: dict) -> None:
        equity = self._last_equity()
        for order in self.pending:
            bar = day_bars.get(order.symbol)

            if order.side is Side.BUY:
                ok, reason = self.broker.check_buy(bar, order.symbol in self.is_st)
                if not ok:
                    self._reject(order, reason or RejectReason.NO_BAR)
                    continue

                if order.entry_rule == "limit_up":
                    # 打板单：日线数据看不到排队深度，采用保守口径 ——
                    # 只有当日**收盘仍然封在涨停价**才算打板成功（成交价 = 涨停价）；
                    # 触板后炸板/全天没摸到 → 视为没打上，废单。
                    limit = self._limit_up_price(bar)
                    if limit is None:
                        self._reject(order, RejectReason.NO_BAR)
                        continue
                    if bar.high < limit * 0.997 or bar.close < limit * 0.997:
                        self._reject(order, RejectReason.NO_TOUCH)
                        continue
                    if bar.open >= limit * 0.997 and bar.high <= bar.open * 1.0005:
                        self._reject(order, RejectReason.LIMIT_UP)   # 一字板排队买不到
                        continue
                    price = round(limit, 3)
                else:
                    price = self.broker.exec_price(bar, Side.BUY)

                # 订单级风控参数：多策略并联时每个策略用自己的止损/持仓期
                sp = self._source_params(order.source)
                eff = replace(
                    self.config,
                    stop_loss_pct=order.stop_loss_pct if order.stop_loss_pct is not None
                    else sp.get("stop_loss_pct", self.config.stop_loss_pct),
                    take_profit_pct=sp.get("take_profit_pct", self.config.take_profit_pct),
                    max_hold_days=order.max_hold_days if order.max_hold_days is not None
                    else sp.get("max_hold_days", self.config.max_hold_days),
                )
                stop = resolve_stop_price(price, order.stop_price, eff)
                target = resolve_target_price(price, order.take_profit, eff)
                qty, note = size_by_risk(
                    equity, self.portfolio.cash, price, stop, eff, self.broker
                )

                # 风险预算：该策略已占用市值 + 本次买入不得超过它的资金份额
                budget = (self.config.strategy_budget or {}).get(order.source)
                if budget is not None:
                    # 用上一交易日收盘价估算占用，绝不能用当日收盘价（未来数据）
                    used = self._source_used(order.source, self._last_closes)
                    room = equity * budget - used
                    if room <= 0:
                        self.rejected["budget"] = self.rejected.get("budget", 0) + 1
                        self._reject(order, RejectReason.LOT)
                        continue
                    qty = self.broker.affordable_qty(room, price, qty)

                if qty < self.config.lot_size:
                    self._reject(order, RejectReason.LOT)
                    continue
                fees = self.broker.calc_fees(price, qty, Side.BUY)
                fill = Fill(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=Side.BUY,
                    qty=qty,
                    price=price,
                    dt=datetime.combine(d, datetime.min.time()),
                    signal_dt=order.signal_dt,
                    fees=fees,
                    source=order.source,
                    reason="entry",
                    entry_price=price,
                )
                self.portfolio.apply_fill(fill)
                pos = self.portfolio.positions.get(order.symbol)
                if pos:
                    pos.stop_price = stop
                    pos.take_profit = target
                    pos.max_hold_days = (order.max_hold_days if order.max_hold_days is not None else eff.max_hold_days)
                    pos.exit_rule = order.exit_rule
                order.status = OrderStatus.FILLED
                order.exec_dt = fill.dt
                order.exec_price = price
                order.qty = qty
                order.fees = fees
                self.fills.append(fill)
                continue

            # SELL
            ok, reason = self.broker.check_sell(bar, order.symbol in self.is_st)
            if not ok:
                self._reject(order, reason or RejectReason.NO_BAR)
                continue
            self._do_sell(order, bar, d, price_override=None, reason="signal")

    def _do_sell(self, order: Order, bar, d: date, price_override: float | None, reason: str) -> None:
        pos = self.portfolio.positions.get(order.symbol)
        if pos is None:
            self._reject(order, RejectReason.NO_POSITION)
            return
        if pos.open_dt.date() >= d:
            self._reject(order, RejectReason.T1_LOCKED)
            return
        price = price_override if price_override else self.broker.exec_price(bar, Side.SELL)
        qty = pos.qty
        fees = self.broker.calc_fees(price, qty, Side.SELL)
        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=Side.SELL,
            qty=qty,
            price=round(price, 3),
            dt=datetime.combine(d, datetime.min.time()),
            # 卖出单的「信号时间」记持仓开仓日，持仓天数与延迟才可核对
            signal_dt=pos.open_dt,
            fees=fees,
            source=pos.source,
            reason=reason,
        )
        pnl = self.portfolio.apply_fill(fill)
        fill.realized_pnl = round(pnl, 2)
        # 亏损百分比：成本基准 = 持仓成本价（含买入费）× 卖出股数。
        # 口径与 realized_pnl 一致（都扣掉了卖出费用），所以止损 -12% 会显示成 -12.x%。
        cost_basis = pos.avg_cost * qty
        fill.entry_price = round(pos.avg_cost, 3)
        fill.pnl_pct = round(pnl / cost_basis, 6) if cost_basis > 0 else 0.0
        order.status = OrderStatus.FILLED
        order.exec_dt = fill.dt
        order.exec_price = fill.price
        order.qty = qty
        order.fees = fees
        self.fills.append(fill)

    # ---------- 盘中止损/止盈 ----------
    def _intraday_exits(self, d: date, day_bars: dict) -> None:
        for sym, pos in list(self.portfolio.positions.items()):
            if pos.open_dt.date() >= d:
                continue  # T+1
            bar = day_bars.get(sym)
            if bar is None:
                continue  # 停牌，无法卖出

            exit_price = 0.0
            reason = ""
            if pos.stop_price and pos.stop_price > 0:
                trig, px = self.broker.stop_triggered(bar, pos.stop_price)
                if trig:
                    exit_price, reason = px, "stop"
            if not reason and pos.take_profit and pos.take_profit > 0:
                trig, px = self.broker.target_triggered(bar, pos.take_profit)
                if trig:
                    exit_price, reason = px, "take_profit"
            if not reason and pos.max_hold_days:
                held = (d - pos.open_dt.date()).days
                if held >= pos.max_hold_days:
                    exit_price, reason = bar.close, "max_hold"

            if reason:
                ok, block_reason = self.broker.check_sell(bar, sym in self.is_st)
                if not ok:
                    key = (block_reason or RejectReason.NO_BAR).value
                    self.rejected[key] = self.rejected.get(key, 0) + 1
                    continue
                order = Order(
                    order_id=self._oid(),
                    symbol=sym,
                    side=Side.SELL,
                    qty=pos.qty,
                    signal_dt=datetime.combine(d, datetime.min.time()),
                    signal_price=bar.close,
                    source=pos.source,
                )
                self.orders.append(order)
                self._do_sell(order, bar, d, price_override=exit_price, reason=reason)

            # 移动止损上移
            if sym in self.portfolio.positions and pos.stop_price:
                pos.stop_price = adjust_stops(
                    max(pos.high_water, bar.high), pos.avg_cost, pos.stop_price, self.config
                )
                pos.high_water = max(pos.high_water, bar.high)

    # ---------- 打板失败处置 ----------
    def _judge_fail(self, d: date, day_bars: dict) -> None:
        """收盘后判定离场规则，命中就标记 failed，次日开盘执行卖出。

        支持多条规则（order/position 里逗号分隔）：
          unseal_next_open     买入当日没封住板 → 打板失败
          break_ma_next_open:N 收盘跌破 N 日均线 → 趋势走坏（当天买入不顺延到次日判）
        """
        for sym, pos in self.portfolio.positions.items():
            rules = [r.strip() for r in (pos.exit_rule or "").split(",") if r.strip()]
            if not rules:
                continue
            bar = day_bars.get(sym)
            if bar is None or bar.pre_close <= 0:
                continue

            for rule in rules:
                if rule == "unseal_next_open":
                    if pos.open_dt.date() != d:
                        continue
                    th = board_limit_pct(sym) * 0.92 * 100
                    chg = (bar.close / bar.pre_close - 1) * 100
                    # 封板的两个条件：涨幅到板 + 收盘价就在最高价上
                    if not (chg >= th and bar.close >= bar.high * (1 - 0.002)):
                        pos.failed = True
                        pos.fail_reason = "fail_exit"
                elif rule == "unseal_any_next_open":
                    # 断板就走：持仓期间任何一天收盘没封住，次日开盘清仓（龙头战法核心纪律）
                    th = board_limit_pct(sym) * 0.92 * 100
                    chg = (bar.close / bar.pre_close - 1) * 100
                    if not (chg >= th and bar.close >= bar.high * (1 - 0.002)):
                        pos.failed = True
                        pos.fail_reason = "break_board"
                elif rule.startswith("break_ma_next_open"):
                    if pos.open_dt.date() >= d:
                        continue  # T+1，买入当天不按均线判
                    period = int(rule.split(":")[1]) if ":" in rule else 5
                    ma = self._ma(sym, period)
                    if ma is not None and bar.close < ma:
                        pos.failed = True
                        pos.fail_reason = "break_ma"
                elif rule.startswith("swing_high_next_open"):
                    # 摆动高点确认 → 次日开盘卖出。
                    # 把 TradingView「确认高点做空」改成 A 股可用的「确认高点卖出」，
                    # 这是 swing_buy 策略的核心出场（实测比固定持有好 20 倍）。
                    if pos.open_dt.date() >= d:
                        continue  # T+1
                    period = int(rule.split(":")[1]) if ":" in rule else 10
                    if self._swing_high_confirmed(sym, period, bar.close):
                        pos.failed = True
                        pos.fail_reason = "swing_high"

    # ---------- 均线（给 break_ma 规则用） ----------
    @staticmethod
    def _limit_up_price(bar) -> float | None:
        """涨停价（按前收 × 板块幅度推算，复权数据用 0.3% 容差核对）。"""
        if bar is None or bar.pre_close <= 0:
            return None
        pct = board_limit_pct(bar.symbol)
        return bar.pre_close * (1 + pct)

    def _push_closes(self, closes: dict[str, float]) -> None:
        for sym, px in closes.items():
            dq = self._close_win.get(sym)
            if dq is None:
                dq = deque(maxlen=60)
                self._close_win[sym] = dq
            dq.append(px)

    def _push_bars(self, day_bars: dict) -> None:
        """攒 high/low 窗口，给 swing_high 规则用。"""
        for sym, bar in day_bars.items():
            hq = self._high_win.get(sym)
            if hq is None:
                hq = deque(maxlen=30)
                self._high_win[sym] = hq
            lq = self._low_win.get(sym)
            if lq is None:
                lq = deque(maxlen=30)
                self._low_win[sym] = lq
            hq.append(bar.high)
            lq.append(bar.low)

    def _swing_high_confirmed(self, sym: str, left: int, close: float) -> bool:
        """上一根是否为确认的摆动高点。

        口径与策略里的摆动买点对称：
          候选高点 = 前一根的 high，且它左边 left 根都更低
          确认     = 当前这根**收盘价**跌破候选高点那根的 low
        （_judge_fail 在收盘后调用，此时今天这根已入队，所以 [-1]=今天、[-2]=候选）
        """
        hi = list(self._high_win.get(sym) or [])
        lo = list(self._low_win.get(sym) or [])
        if len(hi) < left + 2 or len(lo) != len(hi):
            return False
        peak = hi[-2]
        prev = hi[-(left + 2):-2]
        if len(prev) < left or any(p >= peak for p in prev):
            return False
        return close < lo[-2]

    def _ma(self, sym: str, period: int) -> float | None:
        dq = self._close_win.get(sym)
        if not dq or len(dq) < period:
            return None
        vals = list(dq)[-period:]
        return sum(vals) / period

    def _fail_exit_open(self, d: date, day_bars: dict) -> None:
        """失败的持仓在次日开盘无条件卖出（买不到的那一侧除外：跌停/停牌再等一天）。"""
        for sym, pos in list(self.portfolio.positions.items()):
            if not pos.failed or pos.open_dt.date() >= d:
                continue
            bar = day_bars.get(sym)
            if bar is None:
                self.rejected["suspended"] = self.rejected.get("suspended", 0) + 1
                continue
            ok, reason = self.broker.check_sell(bar, sym in self.is_st)
            if not ok:
                key = (reason or RejectReason.NO_BAR).value
                self.rejected[key] = self.rejected.get(key, 0) + 1
                continue
            order = Order(
                order_id=self._oid(),
                symbol=sym,
                side=Side.SELL,
                qty=pos.qty,
                signal_dt=datetime.combine(pos.open_dt.date(), datetime.min.time()),
                signal_price=bar.open,
                source=pos.source,
                exit_rule=pos.exit_rule,
            )
            self.orders.append(order)
            # 不传 price_override：走 exec_price(bar, SELL) = 开盘价 ×(1-滑点)
            self._do_sell(order, bar, d, price_override=None, reason=pos.fail_reason or "fail_exit")

    # ---------- 主循环 ----------
    def run(
        self,
        hits: pl.DataFrame,
        start: date | None = None,
        end: date | None = None,
    ) -> BacktestResult:
        # 策略可以声明持仓规则（可选列 exit_rule）：
        #   'unseal_next_open' → 买入当日收盘没封板，次日开盘无条件清仓
        rules: dict[tuple[str, date], str] = {}
        if hits.height and "exit_rule" in hits.columns:
            for row in hits.select("symbol", "date", "exit_rule").iter_rows(named=True):
                if row["exit_rule"]:
                    rules[(row["symbol"], row["date"])] = row["exit_rule"]

        # 买入方式（可选列 entry_rule）：'limit_up' = 打板，涨停价成交
        entries: dict[tuple[str, date], str] = {}
        if hits.height and "entry_rule" in hits.columns:
            for row in hits.select("symbol", "date", "entry_rule").iter_rows(named=True):
                if row["entry_rule"]:
                    entries[(row["symbol"], row["date"])] = row["entry_rule"]

        sig: dict[date, list[tuple[str, float, str]]] = {}
        if hits.height:
            h = hits.select("symbol", "date", "score")
            h = h.with_columns(
                hits["source"].cast(pl.Utf8) if "source" in hits.columns else pl.lit("D1").alias("source")
            )
            for row in h.iter_rows(named=True):
                sig.setdefault(row["date"], []).append(
                    (row["symbol"], float(row["score"] or 0), row["source"] or "D1")
                )
            # 排序必须带确定性 tiebreaker（symbol）：
            # 只按 score 降序时，同分信号的先后取决于上游 group_by 的顺序，
            # 而 pos 数有限（max_positions）→ 谁被拒单会随运行而变，结果不可复现。
            for d in sig:
                sig[d].sort(key=lambda x: (-x[1], x[0]))

        cal = [d for d in self.bars.calendar if (not start or d >= start) and (not end or d <= end)]
        if not cal:
            raise ValueError("回测区间内没有行情数据")

        for d in cal:
            day_bars = self.bars.day(d)
            if not day_bars:
                continue

            # 0) 打板失败的持仓：开盘第一件事就卖掉（先卖后买，卖出的钱当天能用）
            self._fail_exit_open(d, day_bars)

            # 1) 开盘撮合昨天的单
            self._open_auction(d, day_bars)
            self.pending = [o for o in self.pending if o.status is OrderStatus.PENDING]

            # 2) 盘中止损/止盈
            self._intraday_exits(d, day_bars)

            # 3) 收盘后出信号（只看当日已完成的 K 线）
            if d in sig:
                # 仓位上限必须把「已挂出、尚未成交」的买单算进去，否则同一天会超额下单
                slots = self.config.max_positions - len(self.portfolio.positions) - sum(
                    1
                    for o in self.pending
                    if o.side is Side.BUY and o.status is OrderStatus.PENDING
                )
                for sym, score, src in sig[d]:
                    if sym in self.portfolio.positions:
                        continue
                    if slots <= 0:
                        self.rejected["max_positions"] = self.rejected.get("max_positions", 0) + 1
                        continue
                    bar = day_bars.get(sym)
                    if bar is None:
                        continue
                    order = Order(
                        order_id=self._oid(),
                        symbol=sym,
                        side=Side.BUY,
                        qty=0,
                        signal_dt=datetime.combine(d, datetime.min.time()),
                        signal_price=bar.close,
                        score=score,
                        source=src,
                        exit_rule=rules.get((sym, d)),
                        entry_rule=entries.get((sym, d)),
                    )
                    self.pending.append(order)
                    self.orders.append(order)
                    slots -= 1

            # 4) 收盘结算
            self._last_closes = self.bars.closes(d)
            self._push_closes(self._last_closes)
            self._push_bars(day_bars)
            self.snapshots.append(self.portfolio.snapshot(d, self._last_closes))

            # 5) 收盘后判定离场规则（没封住 / 破均线）→ 明天开盘走
            self._judge_fail(d, day_bars)

        result_metrics = M.compute(self.snapshots, self.fills, self.config)
        return BacktestResult(
            config=self.config,
            snapshots=self.snapshots,
            fills=self.fills,
            orders=self.orders,
            metrics=result_metrics,
            rejected=self.rejected,
            warnings=self.warnings,
        )
