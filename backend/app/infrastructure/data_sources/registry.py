"""Provider 注册表 —— 按能力路由到具体数据源。

用法：
    from app.infrastructure.data_sources.registry import registry
    registry.providers()                      # 所有已注册源
    registry.capabilities()                   # 能力矩阵
    registry.find(Capability.MINUTE)          # 支持分钟线的源
    registry.best(Capability.DAILY)           # 优先级最高的源

优先级由注册顺序决定：先注册的优先（东财日线 → 新浪 30 分钟）。
"""
from __future__ import annotations

from .base import Capability, DataProvider, ProviderInfo
from .eastmoney import EastmoneyProvider
from .sina import SinaProvider
from .tdx_local import TdxLocalProvider
from .tencent import TencentProvider
from .tencent_daily import TencentDailyProvider
from .sina_daily import SinaDailyProvider
from .tdx_protocol import TdxProtocolProvider
from .vipdoc_local import VipdocProvider


class Registry:
    def __init__(self) -> None:
        self._providers: dict[str, DataProvider] = {}
        self._order: list[str] = []

    def register(self, provider: DataProvider) -> None:
        name = provider.name
        if name not in self._providers:
            self._order.append(name)
        self._providers[name] = provider

    def get(self, name: str) -> DataProvider:
        if name not in self._providers:
            raise KeyError(f"未注册的数据源: {name}（已注册 {self._order}）")
        return self._providers[name]

    def providers(self) -> list[DataProvider]:
        return [self._providers[n] for n in self._order]

    def find(self, capability: Capability) -> list[DataProvider]:
        out = []
        for name in self._order:
            p = self._providers[name]
            if capability in p.info().capabilities:
                out.append(p)
        return out

    def best(self, capability: Capability) -> DataProvider:
        found = self.find(capability)
        if not found:
            raise KeyError(f"没有任何数据源支持 {capability.value}")
        return found[0]

    def capabilities(self) -> dict[str, dict]:
        """能力矩阵（给前端「数据管理」页展示）。"""
        matrix: dict[str, dict] = {}
        for p in self.providers():
            info: ProviderInfo = p.info()
            matrix[p.name] = {
                "capabilities": sorted(c.value for c in info.capabilities),
                "max_daily_bars": info.max_daily_bars,
                "max_minute_bars": info.max_minute_bars,
                "adjust_support": sorted(info.adjust_support),
                "note": info.note,
            }
        return matrix


registry = Registry()
registry.register(EastmoneyProvider())
registry.register(SinaProvider())
registry.register(TencentProvider())
registry.register(TdxLocalProvider())
registry.register(TencentDailyProvider())
registry.register(SinaDailyProvider())
registry.register(TdxProtocolProvider())
registry.register(VipdocProvider())
# 腾讯日线：东财 502 时的备份源（只有日线，无快照/分钟）
