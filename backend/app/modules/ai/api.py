"""DeepSeek 接入 —— 让人可以直接问「这只票怎么样」「这个策略靠不靠谱」。

不是把整个 K 线表丢给模型：那样既贵又慢，而且模型会编。做法是**先把可计算
的事实整理成结构化摘要**（涨跌、均线位置、量比、板块、支撑压力、最近信号），
再让模型基于这些数字做解释和判断。

API 兼容 OpenAI 格式：
    POST https://api.deepseek.com/chat/completions
    {"model": "deepseek-chat", "messages": [...], "stream": false}

Key 来源（按优先级）：
  1. 环境变量 FACET_DEEPSEEK_KEY / DEEPSEEK_API_KEY
  2. SQLite configs 表里的 ai.deepseek_key（前端设置页写入）

没有 Key 时所有接口返回 503 并说明怎么配，不会假装能用。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["ai"])

API_URL = "https://api.deepseek.com/chat/completions"
MODELS = [
    {"id": "deepseek-chat", "name": "DeepSeek V3（通用，快）"},
    {"id": "deepseek-reasoner", "name": "DeepSeek R1（推理，慢但深）"},
]


def _get_key() -> str:
    for env in ("FACET_DEEPSEEK_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY"):
        v = os.getenv(env, "").strip()
        if v:
            return v
    try:
        from app.infrastructure.storage.db import get_db

        return (get_db().get_config("ai.deepseek_key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _call(messages: list[dict], model: str = "deepseek-chat", temperature: float = 0.3,
          max_tokens: int = 1200) -> dict:
    key = _get_key()
    if not key:
        raise HTTPException(
            status_code=503,
            detail=(
                "没有配置 DeepSeek API Key。两种配法：\n"
                "  1. 环境变量：set FACET_DEEPSEEK_KEY=sk-xxxx（重启服务生效）\n"
                "  2. 前端「数据管理 → AI 设置」里填，存 SQLite\n"
                "Key 在 https://platform.deepseek.com/api_keys 申请"
            ),
        )

    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
    ).encode()

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=502, detail=f"DeepSeek 返回 {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"DeepSeek 请求失败: {exc}") from exc

    choice = (payload.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "")
    usage = payload.get("usage") or {}
    return {
        "content": content,
        "model": payload.get("model", model),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# ---------------------------------------------------------------- 请求模型
class ChatReq(BaseModel):
    messages: list[dict] = Field(..., description="[{role, content}]，system 可选")
    model: str = "deepseek-chat"
    temperature: float = 0.3
    max_tokens: int = 1200


class AnalyzeReq(BaseModel):
    symbol: str
    question: str = ""
    model: str = "deepseek-chat"


@router.get("/ai/status")
def ai_status() -> dict:
    key = _get_key()
    return {
        "configured": bool(key),
        "key_preview": f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("已设置" if key else ""),
        "models": MODELS,
        "note": "Key 可放在环境变量 FACET_DEEPSEEK_KEY，或在前端设置里写入 SQLite",
    }


@router.post("/ai/key")
def set_key(body: dict) -> dict:
    """写 Key 进 SQLite（不落日志）。"""
    key = str(body.get("key") or "").strip()
    from app.infrastructure.storage.db import get_db

    get_db().set_config("ai.deepseek_key", key)
    return {"ok": True, "configured": bool(key)}


@router.post("/ai/chat")
def chat(req: ChatReq) -> dict:
    """通用对话。前端把上下文塞进 messages。"""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")
    return _call(req.messages, req.model, req.temperature, req.max_tokens)


def _gather_context(symbol: str) -> dict:
    """把这只票的可计算事实整理成一个 dict —— 给模型的是数字，不是原始 K 线。"""
    from app.runtime import STORE
    from app.engine import StrategyEngine  # noqa: F401  (确保 registry 已加载)

    store = STORE
    store.require()

    import polars as pl

    df = (
        store.scan()
        .filter(pl.col("symbol") == symbol)
        .select("date", "open", "high", "low", "close", "volume", "change_pct", "turnover_proxy")
        .sort("date")
        .tail(250)
        .collect()
    )
    if df.height < 30:
        raise HTTPException(status_code=404, detail=f"{symbol} 数据不足")

    closes = df["close"].to_list()
    vols = df["volume"].to_list()
    last = closes[-1]

    def ma(n: int) -> float | None:
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

    def ret(n: int) -> float | None:
        return round((last / closes[-n - 1] - 1) * 100, 2) if len(closes) > n else None

    ctx = {
        "symbol": symbol,
        "last_date": str(df["date"][-1]),
        "last_close": round(last, 2),
        "bars": df.height,
        "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60), "ma250": ma(250),
        "ret_1d": ret(0), "ret_5d": ret(5), "ret_20d": ret(20), "ret_60d": ret(60),
        "high_60": round(max(df["high"].tail(60).to_list()), 2),
        "low_60": round(min(df["low"].tail(60).to_list()), 2),
        "high_250": round(max(df["high"].to_list()), 2),
        "low_250": round(min(df["low"].to_list()), 2),
        "vol_ratio": round(vols[-1] / (sum(vols[-20:]) / 20), 2) if len(vols) >= 20 else None,
        "amplitude_pct": round((df["high"][-1] - df["low"][-1]) / closes[-2] * 100, 2),
        "turnover": round(float(df["turnover_proxy"][-1]) * 100, 2) if df["turnover_proxy"][-1] is not None else None,
        "up_days_20": sum(1 for i in range(-20, 0) if df["change_pct"][i] > 0),
        "limit_up_60": sum(1 for x in df["change_pct"].tail(60).to_list() if x >= 9.8),
    }

    # 支撑/压力/目标（复用 levels 的逻辑）
    try:
        from app.modules.analysis.levels_api import levels as levels_fn

        lv = levels_fn(symbol, 250, 5, store)
        ctx["levels"] = {
            "stop": lv["stop"],
            "supports": lv["supports"][:3],
            "resistances": lv["resistances"][:3],
            "targets": lv["targets"][:2],
        }
    except Exception:  # noqa: BLE001
        ctx["levels"] = None

    # 板块
    try:
        from app.modules.analysis.stock_api import _fetch_boards, _fetch_quote

        q = _fetch_quote(symbol)
        ctx["quote"] = {
            "name": q["name"], "price": q["price"], "change_pct": q["change_pct"],
            "pe_ttm": q["pe_ttm"], "pb": q["pb"],
            "total_cap_yi": q["total_cap"], "float_cap_yi": q["float_cap"],
            "limit_up": q["limit_up"], "limit_down": q["limit_down"],
        }
        b = _fetch_boards(symbol)
        ctx["industry"] = [x["name"] for x in b.get("industry", [])]
        ctx["concept"] = [x["name"] for x in b.get("concept", [])][:10]
    except Exception:  # noqa: BLE001
        ctx["quote"] = None

    # 命中我们的哪些策略
    try:
        from pathlib import Path as _P
        from app import config

        cache = _P(config.DATA_DIR) / "screener_cache.json"
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            hits = []
            for it in data.get("items", []):
                for row in it.get("rows", []):
                    if row.get("symbol") == symbol:
                        hits.append({"strategy": it["name"], "rank": it.get("rank"), "win_rate": it.get("win_rate")})
                        break
            ctx["strategy_hits"] = hits
    except Exception:  # noqa: BLE001
        ctx["strategy_hits"] = []

    return ctx


SYSTEM_PROMPT = """你是一个 A 股量化分析助手，服务于一个自建的量化终端。

规则：
1. 只基于用户给你的数据回答，数据里没有的就直说"数据里没有"，不要编。
2. 涉及价格、涨跌幅、均线、支撑压力时，引用具体数字。
3. 分析框架：趋势（均线排列）→ 位置（离高低点多远）→ 量能（量比/换手）→ 风险（止损位多深、盈亏比多少）。
4. 不要给"建议买入/卖出"这类投资建议，而是给"如果买，止损在哪、目标在哪、盈亏比多少"这类可验证的量化判断。
5. 回答用中文，简洁，重点是数字和逻辑，不要套话。
"""


@router.post("/ai/analyze")
def analyze(req: AnalyzeReq) -> dict:
    """分析单只股票：先把数据整理成结构化摘要，再交给模型解释。"""
    ctx = _gather_context(req.symbol)
    question = req.question.strip() or "分析这只股票当前的趋势、位置、量能和风险，给止损/目标的量化判断"

    user_msg = (
        f"以下是 {req.symbol} 的结构化数据（JSON），请基于它回答：\n\n"
        f"```json\n{json.dumps(ctx, ensure_ascii=False, indent=1)}\n```\n\n"
        f"问题：{question}"
    )
    out = _call(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}],
        model=req.model,
        temperature=0.3,
        max_tokens=1500,
    )
    return {"symbol": req.symbol, "context": ctx, **out}


@router.post("/ai/interpret-backtest")
def interpret_backtest(body: dict) -> dict:
    """解读一次回测结果：胜率、盈亏比、回撤意味着什么。"""
    metrics = body.get("metrics") or {}
    strategy = body.get("strategy") or ""
    if not metrics:
        raise HTTPException(status_code=400, detail="缺少 metrics")

    user_msg = (
        f"策略 `{strategy}` 的账户级回测结果：\n\n"
        f"```json\n{json.dumps(metrics, ensure_ascii=False, indent=1)}\n```\n\n"
        "请回答：\n"
        "1. 这套参数的期望值是多少（胜率 × 平均盈 − 败率 × 平均亏）？\n"
        "2. 最大回撤出现在什么阶段，对这个策略意味着什么？\n"
        "3. 如果实盘，最可能先死在哪个指标上？\n"
        "4. 一句话结论：这个结果值得继续调，还是放弃。"
    )
    out = _call(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}],
        model=body.get("model", "deepseek-chat"),
        temperature=0.3,
        max_tokens=1500,
    )
    return {"strategy": strategy, **out}
