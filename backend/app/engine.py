"""策略引擎 — 加载 / 执行 / 回测。

契约（写策略的人只需要记三条）：
  1. 一个策略 = 一个 .py 文件，放在 app/strategies/ 下
  2. 文件里必须有 META(dict) 和 filter_frame(df, params) -> pl.DataFrame
  3. filter_frame 接收全量 enriched 表，返回命中行，必须含 symbol 和 date 两列

执行是向量化的：策略里写 pl.Expr，引擎一次 collect，不做逐股 for 循环。
"""
from __future__ import annotations

import importlib.util
import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import polars as pl

REQUIRED_META = ("id", "name")
REQUIRED_COLUMNS = ("symbol", "date")


class StrategyError(RuntimeError):
    """策略加载或执行失败。带上策略 id，前端可以直接定位。"""


@dataclass(frozen=True)
class StrategyDef:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    params: tuple[dict, ...]
    order_by: str | None
    descending: bool
    limit: int
    filter_fn: Callable[[pl.DataFrame, dict], pl.DataFrame]
    path: Path

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "params": list(self.params),
            "order_by": self.order_by,
            "descending": self.descending,
            "limit": self.limit,
            "source": self.path.name,
        }


def _normalize_params(raw: Any) -> list[dict]:
    """把 META['params'] 归一化成前端可直接渲染的表单定义。"""
    if not raw:
        return []
    items: list[dict] = []
    if isinstance(raw, dict):
        raw = [{"id": k, **(v if isinstance(v, dict) else {"default": v})} for k, v in raw.items()]
    if not isinstance(raw, (list, tuple)):
        return []
    for item in raw:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict) or not item.get("id"):
            continue
        norm = dict(item)
        norm.setdefault("label", str(norm["id"]))
        norm.setdefault("type", "float")
        norm.setdefault("default", 0 if norm["type"] in ("int", "float") else "")
        items.append(norm)
    return items


@dataclass
class StrategyEngine:
    strategy_dir: Path
    registry: dict[str, StrategyDef] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.reload()

    # ---------- 加载 ----------
    def reload(self) -> None:
        """加载策略。

        支持白名单：`strategies/enabled.json` 存在时只加载里面的 id。
        文件不存在 = 全部加载（保持向后兼容）。删掉的插件文件仍在目录里，
        只是不加载 —— 想恢复就把 id 加回清单，或者直接删掉那个 json。
        """
        self.registry.clear()
        self.errors.clear()

        allow: set[str] | None = None
        manifest = Path(self.strategy_dir) / "enabled.json"
        if manifest.exists():
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
                ids = raw.get("enabled") if isinstance(raw, dict) else raw
                if isinstance(ids, list) and ids:
                    allow = {str(x) for x in ids}
            except Exception as exc:  # noqa: BLE001
                self.errors["enabled.json"] = f"{type(exc).__name__}: {exc}"

        for path in sorted(Path(self.strategy_dir).glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                self._load_file(path, allow)
            except Exception as exc:  # 单个策略坏掉不影响其他策略
                self.errors[path.name] = f"{type(exc).__name__}: {exc}"

    def _load_file(self, path: Path, allow: set[str] | None = None) -> None:
        spec = importlib.util.spec_from_file_location(f"facet_strategy_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise StrategyError(f"无法加载 {path.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        meta = getattr(module, "META", None)
        if not isinstance(meta, dict):
            raise StrategyError(f"{path.name}: 缺少 META dict")
        # 白名单在读完 META 之后判断（id 不一定等于文件名）
        if allow is not None and str(meta.get("id", "")).strip() not in allow:
            return
        for key in REQUIRED_META:
            if not str(meta.get(key, "")).strip():
                raise StrategyError(f"{path.name}: META['{key}'] 必填")

        fn = getattr(module, "filter_frame", None)
        if not callable(fn):
            raise StrategyError(f"{path.name}: 缺少 filter_frame(df, params) 函数")

        sid = str(meta["id"])
        if sid in self.registry:
            raise StrategyError(f"{path.name}: 策略 id '{sid}' 与其他文件重复")

        self.registry[sid] = StrategyDef(
            id=sid,
            name=str(meta["name"]),
            description=str(meta.get("description", "")),
            tags=tuple(meta.get("tags") or ()),
            params=tuple(_normalize_params(meta.get("params"))),
            order_by=meta.get("order_by"),
            descending=bool(meta.get("descending", True)),
            limit=int(meta.get("limit") or 0),
            filter_fn=fn,
            path=path,
        )

    # ---------- 执行 ----------
    def list_strategies(self) -> list[dict]:
        return [s.to_dict() for s in self.registry.values()]

    def resolve_params(self, strategy_id: str, overrides: dict | None) -> dict:
        spec = self.registry.get(strategy_id)
        if spec is None:
            raise StrategyError(f"策略不存在: {strategy_id}")
        merged = {p["id"]: p["default"] for p in spec.params}
        for key, value in (overrides or {}).items():
            if key in merged and value not in (None, ""):
                merged[key] = value
        return merged

    def run(self, strategy_id: str, overrides: dict | None, frame: pl.DataFrame) -> pl.DataFrame:
        spec = self.registry.get(strategy_id)
        if spec is None:
            raise StrategyError(f"策略不存在: {strategy_id}")
        params = self.resolve_params(strategy_id, overrides)
        try:
            out = spec.filter_fn(frame, params)
        except Exception as exc:
            raise StrategyError(f"策略 {strategy_id} 执行失败: {exc}") from exc
        if not isinstance(out, pl.DataFrame):
            raise StrategyError(f"策略 {strategy_id} 必须返回 pl.DataFrame")
        missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
        if missing:
            raise StrategyError(f"策略 {strategy_id} 返回缺少列: {missing}")
        if spec.order_by and spec.order_by in out.columns:
            out = out.sort(spec.order_by, descending=spec.descending, nulls_last=True)
        if spec.limit and spec.limit > 0:
            out = out.head(spec.limit)
        return out

    # ---------- 回测 ----------
    def backtest(
        self,
        strategy_id: str,
        overrides: dict | None,
        frame: pl.DataFrame,
        hold_days: int = 5,
        top_n: int = 10,
    ) -> dict:
        """等权轮动：每日取信号前 top_n 只买入，持有 hold_days 个交易日卖出。

        明确写下的近似（真实项目里这三条必须逐条验证，否则回测就是故事）：
          - 以当日收盘价成交，未计滑点与手续费
          - 当日买入当日即计入收益
          - 涨跌停不可成交、停牌、退市均未建模
        """
        signals = self.run(strategy_id, overrides, frame)
        if signals.is_empty():
            return {"curve": [], "trades": 0, "win_rate": 0.0, "total_return": 0.0,
                    "max_drawdown": 0.0, "hit_rows": 0, "note": "该参数下没有命中"}

        dates = sorted(frame["date"].unique().to_list())
        day_index = {d: i for i, d in enumerate(dates)}

        close: dict[str, dict] = {}
        ret: dict[str, dict] = {}
        for row in frame.select(["symbol", "date", "close", "ret_1d"]).iter_rows(named=True):
            close.setdefault(row["symbol"], {})[row["date"]] = row["close"]
            ret.setdefault(row["symbol"], {})[row["date"]] = row["ret_1d"] or 0.0

        by_day: dict = {}
        for row in signals.select(["symbol", "date"]).unique().iter_rows(named=True):
            by_day.setdefault(row["date"], []).append(row["symbol"])

        equity = 1.0
        curve: list[dict] = []
        positions: dict[str, tuple[float, Any]] = {}
        trade_returns: list[float] = []

        for d in dates:
            for sym, (entry_px, entry_date) in list(positions.items()):
                if day_index[d] - day_index[entry_date] >= hold_days:
                    exit_px = close.get(sym, {}).get(d)
                    if exit_px and entry_px:
                        trade_returns.append(exit_px / entry_px - 1)
                    positions.pop(sym, None)

            filled = 0
            for sym in by_day.get(d, []):
                if filled >= max(top_n - len(positions), 0):
                    break
                px = close.get(sym, {}).get(d)
                if px:
                    positions[sym] = (px, d)
                    filled += 1

            if positions:
                day_ret = sum(ret.get(s, {}).get(d, 0.0) for s in positions) / len(positions)
                equity *= 1 + day_ret
            curve.append({"date": str(d), "equity": round(equity, 6), "positions": len(positions)})

        peak = 0.0
        max_dd = 0.0
        for point in curve:
            peak = max(peak, point["equity"])
            max_dd = min(max_dd, point["equity"] / peak - 1)

        wins = sum(1 for r in trade_returns if r > 0)
        return {
            "curve": curve,
            "trades": len(trade_returns),
            "win_rate": round(wins / len(trade_returns), 4) if trade_returns else 0.0,
            "total_return": round(equity - 1, 6),
            "max_drawdown": round(max_dd, 6),
            "hit_rows": signals.height,
            "hold_days": hold_days,
            "top_n": top_n,
        }


def describe_error(exc: Exception) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
