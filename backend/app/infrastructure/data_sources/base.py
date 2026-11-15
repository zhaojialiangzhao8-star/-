"""数据源抽象 —— 策略和引擎只依赖这个接口，不碰具体供应商。

三层：
  · `Bar`            统一行情记录（价格、成交量单位、日期格式全部统一）
  · `Capability`     能力声明（日线 / 30分钟 / 快照 / 指数）
  · `DataProvider`   抽象基类，每个供应商实现自己支持的能力

约定（所有 Provider 必须遵守）：
  · 股票代码：`600519.SH` / `000001.SZ` / `430047.BJ`
  · 日期：`YYYY-MM-DD` 字符串
  · 成交量：**股**（不是手）；东方财富返回的是手，Provider 内部 ×100
  · 成交额：**元**
  · 复权：`none` / `forward` / `back`，默认 `back`（前复权会把早期价格减成负数）
  · 拿不到数据时抛 `ProviderError`，**绝不返回空列表冒充正常**
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Capability(str, Enum):
    DAILY = "daily"           # 日线
    MINUTE = "minute"         # 分钟线（period 由调用方指定）
    SNAPSHOT = "snapshot"     # 全市场快照
    INDEX = "index"           # 指数


class ProviderError(RuntimeError):
    """数据源错误。带 provider 名字，方便定位是哪家挂了。"""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


@dataclass(frozen=True)
class Bar:
    date: str          # YYYY-MM-DD（分钟线用 YYYY-MM-DD HH:MM）
    open: float
    high: float
    low: float
    close: float
    volume: float      # 单位：股
    amount: float = 0.0  # 单位：元
    # 供应商直接给出的派生字段；算不出来时留 0，由 indicators 统一补算
    change_pct: float = 0.0
    amplitude: float = 0.0
    turnover: float = 0.0


@dataclass
class ProviderInfo:
    name: str
    capabilities: set[Capability]
    max_daily_bars: int          # 单次最多能拿多少根日线
    max_minute_bars: int         # 单次最多能拿多少根分钟线
    adjust_support: set[str]     # 支持哪些复权口径
    note: str = ""


class DataProvider(ABC):
    """所有行情源的基类。子类只需实现它支持的能力。"""

    name: str = "base"

    @abstractmethod
    def info(self) -> ProviderInfo:
        """能力声明。注册表用它做路由。"""

    # ---------- 可选能力（不支持就抛，不要假装成功） ----------
    def fetch_daily(self, symbol: str, bars: int = 5000, adjust: str = "back") -> list[Bar]:
        raise ProviderError(self.name, f"不支持日线（capabilities={self.info().capabilities}）")

    def fetch_minute(self, symbol: str, period: int = 30, bars: int = 1000) -> list[Bar]:
        raise ProviderError(self.name, f"不支持 {period} 分钟线")

    def fetch_snapshot(self) -> list[dict]:
        raise ProviderError(self.name, "不支持全市场快照")

    def fetch_index(self, symbol: str, bars: int = 5000) -> list[Bar]:
        raise ProviderError(self.name, "不支持指数")

    # ---------- 工具 ----------
    @staticmethod
    def to_secid(symbol: str) -> tuple[str, str]:
        """`600519.SH` → ('1', '600519')。东财要求 market.code 形式。"""
        code, _, market = symbol.partition(".")
        market = market.upper()
        if market in ("SH", "SS"):
            return "1", code
        if market in ("SZ",):
            return "0", code
        if market in ("BJ",):
            return "0", code
        # 无后缀时按首位推断
        return ("1", code) if code.startswith(("6", "9")) else ("0", code)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"
