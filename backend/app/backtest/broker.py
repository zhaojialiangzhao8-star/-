"""A股撮合 — 只做能用 OHLC 数据严格判定的事，判不了的一律保守拒绝。

能判定的：
  · 停牌（当日无 bar）
  · 一字板（open==high==low）：上涨 → 买不进（开盘即封死，排不到）；下跌 → 卖不出
  · 开盘已在涨停价（open ≥ pre_close × (1+涨跌幅) − 容差）→ 买不进
  · 开盘已在跌停价 → 卖不出
  · 现金不足 → 按可用现金缩量，缩到不足 100 股则拒单
  · T+1（当日买入不可卖）
  · 100 股整数倍
  · 佣金/印花税/过户费/滑点

判定不了的（必须保守假设，不能假装知道）：
  · 盘中订单队列优先级 → 一律用开盘价成交
  · 开盘后是否曾打开涨停 → 不建模
  · 集合竞价的撮合量 → 不建模
"""
from __future__ import annotations

from .models import Bar, FeeBreakdown, FeeConfig, RejectReason, Side

# 涨停/跌停判定的容差（百分点）：数据是复权价，直接按 round(pre_close×幅度) 算涨停价
# 会因为复权/合成噪声差几分钱，导致「开盘一字涨停」被当成可以买入。
PRICE_LIMIT_TOL = 0.005


def board_limit_pct(symbol: str, is_st: bool = False) -> float:
    """按板块返回涨跌幅限制。ST 优先。"""
    if is_st:
        return 0.05
    code = symbol.split(".")[0]
    if code.startswith("688") or code.startswith("689"):
        return 0.20          # 科创板
    if code.startswith("30"):
        return 0.20          # 创业板
    if code.startswith(("8", "4", "92")):
        return 0.30          # 北交所
    return 0.10              # 沪深主板


def limit_prices(pre_close: float, symbol: str, is_st: bool = False) -> tuple[float, float]:
    pct = board_limit_pct(symbol, is_st)
    return round(pre_close * (1 - pct), 2), round(pre_close * (1 + pct), 2)


def lot_floor(qty: float, lot: int = 100) -> int:
    if qty <= 0:
        return 0
    return int(qty // lot) * lot


class Broker:
    def __init__(self, fees: FeeConfig, lot_size: int = 100) -> None:
        self.fees = fees
        self.lot_size = lot_size

    # ---------- 费用 ----------
    def calc_fees(self, price: float, qty: int, side: Side) -> FeeBreakdown:
        gross = price * qty
        commission = max(gross * self.fees.commission_rate, self.fees.commission_min)
        stamp = gross * self.fees.stamp_tax_rate if side is Side.SELL else 0.0
        transfer = gross * self.fees.transfer_fee_rate
        slip = gross * (self.fees.slippage_bps / 10_000.0)
        return FeeBreakdown(
            commission=commission, stamp_tax=stamp, transfer_fee=transfer, slippage_cost=slip
        )

    # ---------- 成交价 ----------
    def exec_price(self, bar: Bar, side: Side) -> float:
        """开盘价 ± 滑点。买入向上、卖出向下。"""
        slip = self.fees.slippage_bps / 10_000.0
        px = bar.open * (1 + slip) if side is Side.BUY else bar.open * (1 - slip)
        return round(px, 3)

    # ---------- 可成交性 ----------
    def check_buy(self, bar: Bar | None, is_st: bool = False) -> tuple[bool, RejectReason | None]:
        """买入只拦「买不到」，不对开盘涨幅设门槛（打板要第三天开盘无条件买）。

        买不到的两种情形：
          1. 一字板：open == high == low，全天一个价，挂单排不到
          2. 开盘即涨停且全天没打开：open 已在涨停价，且 high 没超过 open
             —— 开盘封到收盘，中途没有成交机会
        其余情况（高开、低开、跳空、开盘涨停但盘中打开）一律按开盘价成交。
        """
        if bar is None:
            return False, RejectReason.SUSPENDED
        if bar.open <= 0:
            return False, RejectReason.NO_BAR

        pct = board_limit_pct(bar.symbol, is_st)
        pre = bar.pre_close

        # 1) 一字板
        if bar.is_one_word:
            if pre <= 0 or bar.close >= pre:
                return False, RejectReason.LIMIT_UP
            return False, RejectReason.LIMIT_DOWN

        # 2) 开盘即封死（涨停/跌停），容差 0.3 个百分点兜住复权与合成噪声
        if pre > 0:
            open_pct = bar.open / pre - 1.0
            if open_pct >= pct - PRICE_LIMIT_TOL and bar.high <= bar.open * 1.0005:
                return False, RejectReason.LIMIT_UP
            if open_pct <= -(pct - PRICE_LIMIT_TOL) and bar.low >= bar.open * 0.9995:
                return False, RejectReason.LIMIT_DOWN
        return True, None

    def check_sell(self, bar: Bar | None, is_st: bool = False) -> tuple[bool, RejectReason | None]:
        """卖出：一字跌停 / 开盘即跌停封死 → 卖不出；一字涨停反而卖得掉（买盘排满）。"""
        if bar is None:
            return False, RejectReason.SUSPENDED
        if bar.open <= 0:
            return False, RejectReason.NO_BAR

        pct = board_limit_pct(bar.symbol, is_st)
        pre = bar.pre_close

        if bar.is_one_word:
            if pre <= 0 or bar.close <= pre:
                return False, RejectReason.LIMIT_DOWN
            return True, None

        if pre > 0:
            open_pct = bar.open / pre - 1.0
            if open_pct <= -(pct - PRICE_LIMIT_TOL) and bar.low >= bar.open * 0.9995:
                return False, RejectReason.LIMIT_DOWN
        return True, None

    # ---------- 现金约束下的数量 ----------
    def affordable_qty(self, cash: float, price: float, want: int) -> int:
        """在现金（含费用）约束下能买的最大股数，已向下取整到一手。"""
        qty = lot_floor(want, self.lot_size)
        while qty > 0:
            fees = self.calc_fees(price, qty, Side.BUY)
            if price * qty + fees.total <= cash:
                return qty
            qty -= self.lot_size
        return 0

    # ---------- 止损触发（用当根 high/low，避免用到未来数据） ----------
    @staticmethod
    def stop_triggered(bar: Bar, stop_price: float, side_holding: Side = Side.BUY) -> tuple[bool, float]:
        """多仓止损：low 触及 stop 即触发；跳空低开则按开盘价成交（亏损会大于计划）。

        返回 (是否触发, 成交价)。
        """
        if stop_price <= 0:
            return False, 0.0
        if bar.open <= stop_price:
            return True, bar.open          # 跳空低开，按开盘价
        if bar.low <= stop_price:
            return True, stop_price        # 盘中击穿，按止损价
        return False, 0.0

    @staticmethod
    def target_triggered(bar: Bar, target: float) -> tuple[bool, float]:
        """止盈：high 触及目标即触发；跳空高开按开盘价。"""
        if target <= 0:
            return False, 0.0
        if bar.open >= target:
            return True, bar.open
        if bar.high >= target:
            return True, target
        return False, 0.0
