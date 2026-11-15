"""策略库治理 —— 策略启用 / 淘汰的唯一入口。

`strategies/enabled.json` 是唯一真相：它决定 `ENGINE.registry` 加载哪些策略，
进而决定 `/api/strategies`、`/api/v1/strategy-ranking`、今日信号、智能选股
能看到什么。本模块只做一件事：把这份清单读出来给人看，并把人的决定写回去。

不要在别处再维护第二份启用状态 —— 那会让「策略库显示已启用」和
「引擎实际加载」对不上。
"""
from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ... import config

router = APIRouter(tags=["strategy-lib"])

_MANIFEST = Path(config.STRATEGY_DIR) / "enabled.json"
_LOCK = threading.RLock()


def _engine():
    """延迟取引擎 —— 引擎没起来（如 polars 缺失、代码测试）时也能读清单。"""
    from ...runtime import ENGINE

    return ENGINE


def _live_ids() -> set[str]:
    try:
        return set(_engine().registry)
    except Exception:  # noqa: BLE001
        return set()


# ---------------------------------------------------------------- 读 META
def _meta_from_source(path: Path) -> dict:
    """只解析 META 字面量，不执行策略代码。

    策略文件里的 META 绝大多数是纯字面量；解析不了再回退到 import。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "META" for t in node.targets):
            continue
        try:
            val = ast.literal_eval(node.value)
            if isinstance(val, dict):
                return val
        except (ValueError, TypeError):
            pass
        break
    return {}


def _meta_node(src: str):
    """返回 (META 的 Assign 节点, 源码文本)。找不到或不是字面量返回 (None, src)。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None, src
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == "META" for t in node.targets):
            try:
                ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None, src
            return node, src
    return None, src


def _meta_via_import(path: Path) -> dict:
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location(f"_slib_{path.stem}", path)
        if spec is None or spec.loader is None:
            return {}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        meta = getattr(mod, "META", None)
        return meta if isinstance(meta, dict) else {}
    except Exception:  # noqa: BLE001 — 单个策略坏掉不该影响列表
        return {}


def scan_strategy_files() -> list[dict]:
    """扫描策略目录，返回全部可用策略（含未启用的）。"""
    out: list[dict] = []
    for p in sorted(Path(config.STRATEGY_DIR).glob("*.py")):
        if p.name.startswith("_"):
            continue
        meta = _meta_from_source(p)
        if not meta:
            meta = _meta_via_import(p)
        sid = str(meta.get("id") or p.stem)
        out.append(
            {
                "id": sid,
                "name": str(meta.get("name") or sid),
                "description": str(meta.get("description") or ""),
                "tags": list(meta.get("tags") or []),
                "file": p.name,
                "source": "meta" if meta else "unknown",
            }
        )
    return out


# ---------------------------------------------------------------- 读写清单
def read_manifest() -> dict:
    with _LOCK:
        if not _MANIFEST.exists():
            return {"enabled": [], "disabled": {}, "note": "", "updated": None}
        try:
            raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"enabled.json 解析失败: {exc}") from exc
        if isinstance(raw, list):
            raw = {"enabled": raw, "disabled": {}}
        raw.setdefault("enabled", [])
        raw.setdefault("disabled", {})
        if not isinstance(raw["disabled"], dict):
            raw["disabled"] = {}
        return raw


def write_manifest(data: dict) -> None:
    with _LOCK:
        tmp = _MANIFEST.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        tmp.replace(_MANIFEST)


# ---------------------------------------------------------------- 排名
def _ranking_map() -> dict:
    """复用 strategy-ranking 的解析（账户回测报告），拿不到就空表。"""
    try:
        from ..screener.batch import _parse_ranking

        return _parse_ranking() or {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 接口
@router.get("/strategy-lib")
def strategy_lib() -> dict:
    """策略库全貌：全部策略文件 + 启用状态 + 淘汰理由 + 回测排名。

    `loaded` 是引擎此刻真正加载的 id —— 用来核对 enabled 是否生效。
    """
    man = read_manifest()
    enabled = [str(x) for x in man.get("enabled", [])]
    disabled = man.get("disabled", {}) or {}
    ranking = _ranking_map()
    live = _live_ids()

    items = []
    for s in scan_strategy_files():
        sid = s["id"]
        r = ranking.get(sid) or {}
        is_on = sid in enabled
        items.append(
            {
                **s,
                "enabled": is_on,
                "loaded": sid in live,
                "disabled_reason": None if is_on else disabled.get(sid),
                "rank": r.get("rank"),
                "return": r.get("return"),
                "mdd": r.get("mdd"),
                "sharpe": r.get("sharpe"),
                "win_rate": r.get("win_rate"),
                "trades": r.get("trades"),
            }
        )

    # 清单里有、但目录里找不到的策略（文件被删了）也要暴露出来
    known = {x["id"] for x in items}
    for sid in enabled:
        if sid not in known:
            items.append(
                {
                    "id": sid, "name": sid, "description": "", "tags": [], "file": None,
                    "source": "manifest", "enabled": True, "loaded": sid in live,
                    "disabled_reason": None, "rank": None, "return": None, "mdd": None,
                    "sharpe": None, "win_rate": None, "trades": None,
                    "missing": True,
                }
            )

    def sort_key(x: dict):
        return (not x["enabled"], x["rank"] is None, x["rank"] or 999, x["id"])

    items.sort(key=sort_key)
    n_on = sum(1 for x in items if x["enabled"])
    n_retired = sum(1 for x in items if (not x["enabled"]) and x.get("disabled_reason"))
    return {
        "enabled": enabled,
        "disabled": disabled,
        "note": man.get("note", ""),
        "method": man.get("method", ""),
        "updated": man.get("updated"),
        "random_baseline": man.get("random_baseline"),
        "loaded_count": len(live),
        "stats": {
            "total": len(items),
            "enabled": n_on,
            "retired": n_retired,
            "unaudited": len(items) - n_on - n_retired,
            "loaded": len(live),
        },
        "items": items,
    }


class ToggleBody(BaseModel):
    id: str = Field(..., min_length=1)
    enabled: bool


@router.post("/strategy-lib/toggle")
def toggle_strategy(body: ToggleBody) -> dict:
    """启用 / 停用一个策略，写回 enabled.json 并重载引擎。

    停用时如果清单里原本有淘汰理由，保留它；启用时清掉理由。
    """
    sid = body.id.strip()
    all_ids = {x["id"] for x in scan_strategy_files()}
    if sid not in all_ids:
        raise HTTPException(404, f"策略不存在: {sid}")

    man = read_manifest()
    enabled = [str(x) for x in man.get("enabled", [])]
    disabled = dict(man.get("disabled") or {})

    if body.enabled:
        if sid not in enabled:
            enabled.append(sid)
        disabled.pop(sid, None)
    else:
        enabled = [x for x in enabled if x != sid]
        if sid not in disabled:
            disabled[sid] = "手动停用"

    if not enabled:
        # 全关会让所有下游静默返回空表，宁可拒绝
        raise HTTPException(400, "至少保留一个启用的策略，否则下游会静默返回空结果")

    man["enabled"] = enabled
    man["disabled"] = disabled
    write_manifest(man)
    _engine().reload()

    return {
        "ok": True,
        "id": sid,
        "enabled": body.enabled,
        "loaded_count": len(_engine().registry),
        "errors": _engine().errors,
    }


@router.post("/strategy-lib/bulk")
def bulk_toggle(ids: list[str] = None, enabled: bool = True) -> dict:  # noqa: RUF013
    """批量启用 / 停用。ids 为空时作用于全部。"""
    target = set(ids or [x["id"] for x in scan_strategy_files()])
    man = read_manifest()
    cur = set(str(x) for x in man.get("enabled", []))
    disabled = dict(man.get("disabled") or {})
    if enabled:
        cur |= target
        for s in target:
            disabled.pop(s, None)
    else:
        cur -= target
        for s in target:
            disabled.setdefault(s, "批量停用")
    if not cur:
        raise HTTPException(400, "至少保留一个启用的策略")
    man["enabled"] = sorted(cur)
    man["disabled"] = disabled
    write_manifest(man)
    _engine().reload()
    return {"ok": True, "enabled_count": len(cur), "loaded_count": len(_engine().registry)}


@router.post("/strategy-lib/reload")
def reload_engine() -> dict:
    """只重载引擎，不动清单（改完策略文件后用）。"""
    eng = _engine()
    eng.reload()
    return {"count": len(eng.registry), "errors": eng.errors}


# ================================================================
# 单个策略的增删改 —— 用户自己决定库里有谁，不由系统替他决定
# ================================================================

RETIRED_DIR = Path(config.STRATEGY_DIR) / "_retired"


def _find_file(sid: str) -> Path:
    """按 id 找策略文件（id 不等于文件名，必须读 META）。"""
    for p in sorted(Path(config.STRATEGY_DIR).glob("*.py")):
        if p.name.startswith("_"):
            continue
        if str(_meta_from_source(p).get("id") or p.stem) == sid:
            return p
    raise HTTPException(404, f"策略不存在: {sid}")


def _rewrite_meta(path: Path, changes: dict) -> dict:
    """只改 META 字面量，其余源码原样保留。

    改之前先备份成 `<file>.bak`；META 不是纯字面量时拒绝（拒绝比改坏强）。
    """
    import pprint

    src = path.read_text(encoding="utf-8")
    node, _ = _meta_node(src)
    if node is None:
        raise HTTPException(400, f"{path.name}: META 不是纯字面量，无法安全改写")

    meta = ast.literal_eval(node.value)
    meta.update({k: v for k, v in changes.items() if v is not None})

    lines = src.splitlines(keepends=True)
    start, end = node.lineno - 1, node.end_lineno
    indent = lines[start][: node.col_offset]
    body = pprint.pformat(meta, width=100, sort_dicts=False)
    body = body.replace("\n", "\n" + indent)
    new_src = "".join(lines[:start]) + indent + "META = " + body + "\n" + "".join(lines[end:])

    bak = path.with_suffix(path.suffix + ".bak")
    bak.write_text(src, encoding="utf-8")
    path.write_text(new_src, encoding="utf-8")
    return meta


@router.get("/strategy-lib/{sid}")
def strategy_detail(sid: str, include_source: bool = Query(True, description="是否带源码")) -> dict:
    """单条详情：META + 源码 + 参数 + 是否可编辑。

    源码用于界面上「点开看这个策略到底写了什么」，所以默认带上；
    超过 200KB 的截断，避免把整页拖死。
    """
    p = _find_file(sid)
    src = p.read_text(encoding="utf-8", errors="replace")
    node, _ = _meta_node(src)
    meta = _meta_from_source(p) or _meta_via_import(p)
    man = read_manifest()
    enabled = [str(x) for x in man.get("enabled", [])]
    lines = src.splitlines()
    return {
        "id": sid,
        "file": p.name,
        "meta": meta,
        "params": meta.get("params") or [],
        "lines": len(lines),
        "chars": len(src),
        "editable": node is not None,
        "enabled": sid in enabled,
        "disabled_reason": (man.get("disabled") or {}).get(sid),
        "source": src if include_source else None,
        "truncated": include_source and len(src) > 200_000,
    }


class UpdateBody(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    params: list[dict] | None = None


@router.put("/strategy-lib/{sid}")
def update_strategy(sid: str, body: UpdateBody) -> dict:
    """改名称 / 描述 / 标签 / 参数默认值。源码其余部分不动。"""
    p = _find_file(sid)
    changes: dict = {}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "名称不能为空")
        changes["name"] = body.name.strip()
    if body.description is not None:
        changes["description"] = body.description
    if body.tags is not None:
        changes["tags"] = [str(t).strip() for t in body.tags if str(t).strip()]
    if body.params is not None:
        new_id = next((x.get("id") for x in body.params if x.get("id") == "id"), None)
        if new_id and new_id != sid:
            raise HTTPException(400, "参数里的 id 不允许改（它是全局主键）")
        changes["params"] = body.params
    if not changes:
        raise HTTPException(400, "没有要改的字段")

    _rewrite_meta(p, changes)
    _engine().reload()
    return {"ok": True, "id": sid, "changed": list(changes.keys())}


@router.delete("/strategy-lib/{sid}")
def delete_strategy(sid: str) -> dict:
    """删除策略：文件移到 `_retired/`（可恢复），并从启用清单移除。"""
    p = _find_file(sid)

    man = read_manifest()
    enabled = [str(x) for x in man.get("enabled", []) if str(x) != sid]
    if not enabled:
        raise HTTPException(400, "删掉它就没有启用策略了，至少保留一个")

    RETIRED_DIR.mkdir(parents=True, exist_ok=True)
    target = RETIRED_DIR / p.name
    if target.exists():
        target = RETIRED_DIR / f"{p.stem}.{int(time.time())}{p.suffix}"
    p.replace(target)

    disabled = dict(man.get("disabled") or {})
    disabled.pop(sid, None)
    man["enabled"], man["disabled"] = enabled, disabled
    write_manifest(man)
    _engine().reload()
    return {"ok": True, "id": sid, "moved_to": str(target), "enabled_count": len(enabled)}


@router.post("/strategy-lib/{sid}/restore")
def restore_strategy(sid: str) -> dict:
    """从 `_retired/` 恢复（按 id 反查文件名）。"""
    if not RETIRED_DIR.exists():
        raise HTTPException(404, "没有可恢复的策略")
    for p in sorted(RETIRED_DIR.glob("*.py")):
        if str(_meta_from_source(p).get("id") or p.stem) == sid:
            dest = Path(config.STRATEGY_DIR) / p.name
            if dest.exists():
                raise HTTPException(409, f"已存在同名文件: {dest.name}")
            p.replace(dest)
            _engine().reload()
            return {"ok": True, "id": sid, "restored_to": str(dest)}
    raise HTTPException(404, f"回收站里没有: {sid}")


TEMPLATE = '''"""__NAME__ —— 由策略库新建。

契约（见 CONTRIBUTING.md §3）：
  META 必填 id / name；filter_frame(df, params) 返回 pl.DataFrame，必须含 symbol、date。
  只读 enriched 表里的派生列，不要在策略里重算均线。
"""
import polars as pl

META = {{
    "id": "__ID__",
    "name": "__NAME__",
    "description": "",
    "tags": ["自定义"],
    "params": [
        {{"id": "lookback", "label": "回看天数", "type": "int", "default": 20}},
    ],
    "order_by": None,
    "descending": True,
}}


def filter_frame(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    n = int(params.get("lookback", 20))
    # TODO: 写你的条件。示例：收盘价站上 n 日均线
    return df.filter(pl.col("close") > pl.col("ma20").shift(0)).select(
        ["symbol", "date"]
    ) if n == 20 else df.filter(pl.lit(True)).select(["symbol", "date"])
'''


class NewBody(BaseModel):
    id: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    name: str = Field(..., min_length=1, max_length=40)


@router.post("/strategy-lib/new")
def create_strategy(body: NewBody) -> dict:
    """新建一个空策略骨架，随后可在界面上编辑。"""
    sid = body.id.strip()
    if any(str(_meta_from_source(p).get("id") or p.stem) == sid
           for p in Path(config.STRATEGY_DIR).glob("*.py")):
        raise HTTPException(409, f"id 已存在: {sid}")
    path = Path(config.STRATEGY_DIR) / f"{sid}.py"
    if path.exists():
        raise HTTPException(409, f"文件已存在: {path.name}")
    path.write_text(
        TEMPLATE.replace("__ID__", sid).replace("__NAME__", body.name.strip()),
        encoding="utf-8",
    )
    _engine().reload()
    return {"ok": True, "id": sid, "file": path.name}
