"""指数 + 板块。

指数：上证 / 深证 / 创业板 / 科创50，走腾讯（不封 IP，字段够用）。
板块：东财行业板块列表 + 板块成分，走 push2（有限流，缓 30 秒）。

为什么指数单独一个模块：它跟个股是两套代码体系（000001.SH 是上证指数，
000001.SZ 是平安银行），混在 quotes.py 里容易搞错。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from ...runtime import get_store

router = APIRouter(tags=["market"])

# 板块指数周期聚合（与个股 K 线口径一致）
_PERIOD_DUR = {"week": "1w", "month": "1mo", "quarter": "1q", "year": "1y"}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://gu.qq.com/"}
TX_URL = "https://qt.gtimg.cn/q={codes}"

# 常用指数：代码 → (腾讯代码, 中文名, 板块页用的 key)
INDICES = [
    ("000001.SH", "sh000001", "上证指数", "sh"),
    ("399001.SZ", "sz399001", "深证成指", "sz"),
    ("399006.SZ", "sz399006", "创业板指", "cyb"),
    ("000688.SH", "sh000688", "科创50", "kcb"),
    ("000300.SH", "sh000300", "沪深300", "hs300"),
    ("399905.SZ", "sz399905", "中证500", "zz500"),
]

_CACHE: dict[str, tuple[float, object]] = {}
_LOCK = threading.Lock()
_TTL = 3.0


def _cached(key: str, fn, ttl: float = _TTL):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _LOCK:
        _CACHE[key] = (now, val)
    return val


# 上一次成功的结果。两个源都挂时拿它兜底，比甩 503 让整个首页空白强。
_LAST_GOOD: dict[str, tuple[float, object]] = {}


def _curl_path() -> str | None:
    """系统 curl。这个环境里 python 的 ssl 握手会超时，curl 却能秒回，
    所以外网请求优先交给 curl（Windows 10 1803+ 自带）。"""
    p = shutil.which("curl")
    if p:
        return p
    for cand in (r"C:\Windows\System32\curl.exe", r"C:\Windows\SysWOW64\curl.exe"):
        if os.path.exists(cand):
            return cand
    return None


_CURL = _curl_path()


def _curl_get(url: str, headers: dict, timeout: float) -> bytes:
    args = [
        _CURL, "-sS", "--max-time", str(max(1, int(timeout))),
        "-A", headers.get("User-Agent", "Mozilla/5.0"),
    ]
    ref = headers.get("Referer")
    if ref:
        args += ["-e", ref]
    args.append(url)
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW，别弹黑框
    proc = subprocess.run(args, capture_output=True, timeout=timeout + 3,
                          creationflags=flags)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[:120]
        raise RuntimeError(err or f"curl exit {proc.returncode}")
    if not proc.stdout:
        raise RuntimeError("curl 返回空")
    return proc.stdout


def _urllib_get(url: str, headers: dict, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_get(url: str, headers: dict, timeout: float = 5.0, retries: int = 1) -> bytes:
    """带重试的 GET：优先 curl → 回退 urllib。"""
    order = ([("curl", _curl_get)] if _CURL else []) + [("urllib", _urllib_get)]
    last: Exception | None = None
    for attempt in range(retries + 1):
        for _name, fn in order:
            try:
                return fn(url, headers, timeout)
            except Exception as exc:  # noqa: BLE001
                last = exc
        if attempt < retries:
            time.sleep(0.2)
    raise last if last else RuntimeError("GET failed")


def _num(f: list[str], i: int) -> float:
    try:
        return float(f[i])
    except (IndexError, ValueError):
        return 0.0


def _fetch_tx() -> list[dict]:
    """腾讯：字段最全（开高低、成交额）。"""
    codes = ",".join(c for _, c, _, _ in INDICES)
    raw = _http_get(TX_URL.format(codes=codes), UA).decode("gbk", errors="replace")

    got: dict[str, dict] = {}
    for line in raw.splitlines():
        if '="' not in line:
            continue
        key = line.split('="', 1)[0].replace("v_", "").strip()
        body = line.split('="', 1)[1].rstrip('";').strip()
        f = body.split("~")
        if len(f) < 40:
            continue
        got[key] = {
            "price": _num(f, 3),
            "change": _num(f, 31),
            "change_pct": _num(f, 32),
            "high": _num(f, 33),
            "low": _num(f, 34),
            "amount": _num(f, 37) * 1e4,   # 腾讯给万元
            "name": f[1],
        }

    out = []
    for sym, tx, cn, key in INDICES:
        d = got.get(tx)
        if not d or not d["price"]:
            out.append({"symbol": sym, "name": cn, "key": key, "available": False})
            continue
        out.append({
            "symbol": sym,
            "name": d["name"] or cn,
            "key": key,
            "price": d["price"],
            "change": d["change"],
            "change_pct": d["change_pct"],
            "high": d["high"],
            "low": d["low"],
            "amount": d["amount"],
            "available": True,
        })
    return out


SINA_URL = "https://hq.sinajs.cn/list={codes}"
SINA_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn/",
}


def _fetch_sina() -> list[dict]:
    """新浪简版：只有现价/涨跌/涨跌幅/成交额，没有开高低。

    腾讯被限流或握手超时时用它顶上 —— 少两个字段，好过整块空白。
    """
    codes = ",".join("s_" + c for _, c, _, _ in INDICES)
    raw = _http_get(SINA_URL.format(codes=codes), SINA_UA, timeout=3.0).decode("gbk", errors="replace")

    got: dict[str, dict] = {}
    for line in raw.splitlines():
        if '="' not in line:
            continue
        key = line.split('="', 1)[0].replace("var hq_str_s_", "").strip()
        body = line.split('="', 1)[1].rstrip('";').strip()
        f = body.split(",")
        if len(f) < 6:
            continue
        got[key] = {
            "name": f[0],
            "price": _num(f, 1),
            "change": _num(f, 2),
            "change_pct": _num(f, 3),
            "amount": _num(f, 5) * 1e4,    # 新浪给万元
        }

    out = []
    for sym, tx, cn, key in INDICES:
        d = got.get(tx)
        if not d or not d["price"]:
            out.append({"symbol": sym, "name": cn, "key": key, "available": False})
            continue
        out.append({
            "symbol": sym,
            "name": d["name"] or cn,
            "key": key,
            "price": d["price"],
            "change": d["change"],
            "change_pct": d["change_pct"],
            "high": 0.0,
            "low": 0.0,
            "amount": d["amount"],
            "available": True,
        })
    return out


def _fetch_indices() -> list[dict]:
    """腾讯 → 新浪 → 上次成功结果。任一环节拿到数就返回。"""
    errors: list[str] = []
    for name, fn in (("tencent", _fetch_tx), ("sina", _fetch_sina)):
        try:
            items = fn()
            if any(x.get("available") for x in items):
                _LAST_GOOD["indices"] = (time.time(), items)
                return items
            errors.append(f"{name}: 全部标的不可用")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")

    last = _LAST_GOOD.get("indices")
    if last:
        age = int(time.time() - last[0])
        # 标记 stale，前端可据此提示"数据为 X 秒前"
        return [dict(x, stale=True, stale_age=age) for x in last[1]]

    raise RuntimeError("; ".join(errors))


@router.get("/indices")
def indices() -> dict:
    """主要指数实时行情（3 秒缓存；多源 + 重试 + 失败回退上次结果）。"""
    try:
        items = _cached("indices", _fetch_indices, ttl=3.0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"指数获取失败: {exc}") from exc
    stale = any(x.get("stale") for x in items)
    return {"count": len(items), "items": items, "stale": stale}


# 指数历史日线：首页点指数时打开 K 线用。
# 数据由 strategy_lib/fetch_indices.py 抓好落盘（enriched 表里只有个股，没有指数日线）。
_INDEX_DAILY = Path(
    os.environ.get("FACET_INDEX_DAILY")
    or (Path(os.environ.get("LOCALAPPDATA", ".")) / "Facet" / "data" / "index_daily.parquet")
)
_INDEX_CACHE: dict[str, object] = {}


def _load_index_daily():
    if "df" in _INDEX_CACHE:
        return _INDEX_CACHE["df"]
    if not _INDEX_DAILY.exists():
        raise HTTPException(503, f"指数日线数据不存在：{_INDEX_DAILY}")
    import polars as pl

    df = pl.read_parquet(_INDEX_DAILY).sort(["symbol", "date"])
    _INDEX_CACHE["df"] = df
    return df


@router.get("/index/kline")
def index_kline(symbol: str = Query(..., description="指数代码，如 000001.SH"), limit: int = Query(250, ge=10, le=2000)) -> dict:
    """单个指数的日线（给首页点指数弹 K 线用）。"""
    df = _load_index_daily()
    sub = df.filter(pl_col_eq("symbol", symbol))
    if sub.height == 0:
        avail = sorted(df["symbol"].unique().to_list())
        raise HTTPException(404, f"没有这个指数的日线: {symbol}（可用：{','.join(avail)}）")
    tail = sub.tail(limit)
    rows = [
        {
            "date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
        }
        for r in tail.iter_rows(named=True)
    ]
    return {"symbol": symbol, "name": tail["name"][-1], "count": len(rows), "rows": rows}


def pl_col_eq(name: str, value):
    """避免为了一个等值过滤在模块顶层 import polars（polars 只在用到时才载入）。"""
    import polars as pl

    return pl.col(name) == value


# ---------------------------------------------------------------- 板块
# 同花顺行业/概念一览表（HTML，稳定）。
# 为什么不用东财 push2：实测 push2/push2his 都会 RemoteDisconnected（有风控），
# 而同花顺这页持续可用、不需要鉴权，只是要解析 HTML。
THS_BOARD_URL = {
    "industry": "http://q.10jqka.com.cn/thshy/",
    "concept": "http://q.10jqka.com.cn/gn/",
}
THS_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://q.10jqka.com.cn/",
}

_ROW_RE = None
_TD_RE = None


def _parse_ths_boards(html: str, kind: str = "industry") -> list[dict]:
    """解析同花顺板块表。

    行业页 `/thshy/` 每行 12 个 <td>：
      序号 / 板块名(带 code) / 涨跌幅 / 成交量(万手) / 成交额(亿) / 净流入(亿)
      / 上涨家数 / 下跌家数 / 均价 / 领涨股 / 领涨股最新价 / 领涨股涨跌幅

    概念页 `/gn/` 每行只有 5 个 <td>，而且是「概念指数新增列表」：
      创建日期 / 概念名(带 code) / 说明 / 涨跌幅(多数是 --) / 成分数
    按涨幅排序的 ajax 接口要 hexin-v cookie，401 拿不到，所以概念这块
    只能给列表 + 成分，不给涨跌排序 —— 点进去看详情页照样有行情。
    """
    import re

    out: list[dict] = []
    body = html.split("<tbody", 1)
    if len(body) < 2:
        return out
    tbody = body[1].split("</tbody>", 1)[0]

    for tr in re.findall(r"<tr>(.*?)</tr>", tbody, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 5:
            continue

        def strip_tags(s: str) -> str:
            return re.sub(r"<[^>]+>", "", s).strip()

        def num(s: str) -> float:
            t = strip_tags(s).replace(",", "").replace("--", "0")
            if not t:
                return 0.0
            try:
                return float(t)
            except ValueError:
                return 0.0

        if kind == "concept":
            # 0 创建日期 / 1 概念名 / 2 说明 / 3 涨跌幅 / 4 成分数
            name_cell = tds[1]
            m = re.search(r"/code/(\d+)/", name_cell)
            code = m.group(1) if m else ""
            name = strip_tags(name_cell)
            if not code or not name:
                continue
            out.append(
                {
                    "code": code,
                    "name": name,
                    "change_pct": num(tds[3]) if len(tds) > 3 else 0.0,
                    "created": strip_tags(tds[0]),
                    "members": int(num(tds[4])) if len(tds) > 4 else 0,
                    "up_count": 0,
                    "down_count": 0,
                    "leader": "",
                    "leader_pct": 0.0,
                    "amount": 0.0,
                }
            )
            continue

        if len(tds) < 12:
            continue
        name_cell = tds[1]
        m = re.search(r"/code/(\d+)/", name_cell)
        code = m.group(1) if m else ""
        name = strip_tags(name_cell)
        leader_cell = tds[9]
        # 同花顺的龙头链接是 http://stockpage.10jqka.com.cn/300528/ —— 域名里带 stockpage，
        # 真正的路径只有 /300528/，所以按「/6位数字/」取，别按 /stockpage/ 匹配。
        lm = re.search(r"/(\d{6})/", leader_cell)
        out.append(
            {
                "code": code,
                "name": name,
                "change_pct": num(tds[2]),
                "volume": num(tds[3]),
                # 口径统一：amount 一律「元」，亿只是便利字段 amount_yi
                "amount": num(tds[4]) * 1e8,
                "amount_yi": num(tds[4]),
                "net_inflow": num(tds[5]),
                "up_count": int(num(tds[6])),
                "down_count": int(num(tds[7])),
                "avg_price": num(tds[8]),
                "leader": strip_tags(leader_cell),
                "leader_symbol": (lm.group(1) if lm else ""),
                "leader_price": num(tds[10]),
                "leader_pct": num(tds[11]),
            }
        )
    return out


def _fetch_boards(kind: str, limit: int) -> list[dict]:
    url = THS_BOARD_URL.get(kind, THS_BOARD_URL["industry"])
    req = urllib.request.Request(url, headers=THS_UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = r.read()
    html = raw.decode("gbk", errors="replace")
    rows = _parse_ths_boards(html, kind)
    return rows[:limit]


@router.get("/boards")
def boards(
    kind: str = Query("industry", description="industry=行业 / concept=概念"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """板块涨跌排行（同花顺，60 秒缓存）。"""
    if kind not in THS_BOARD_URL:
        kind = "industry"
    try:
        # 缓存按 kind 收口，不按 limit 分片：不同页面要不同条数，分片会把同一张表抓多遍
        items = _cached(f"boards:{kind}", lambda: _fetch_boards(kind, 200), ttl=60.0)[:limit]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"板块获取失败: {exc}") from exc
    return {"kind": kind, "count": len(items), "items": items}


def _fetch_board_members(code: str, limit: int) -> list[dict]:
    """板块成分股（同花顺详情页 HTML）。

    行业板块 code 是 881xxx，用 /thshy/detail/；概念板块 code 是 30xxxx，用 /gn/detail/。
    两页的表格结构完全一样（13 个 td），只是路径不同。
    """
    import re

    path = "thshy" if code.startswith("881") else "gn"
    url = f"http://q.10jqka.com.cn/{path}/detail/code/{code}/"
    req = urllib.request.Request(url, headers=THS_UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        html = r.read().decode("gbk", errors="replace")

    body = html.split("<tbody>", 1)
    if len(body) < 2:
        return []
    tbody = body[1].split("</tbody>", 1)[0]

    def strip_tags(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).strip()

    def num(s: str) -> float:
        t = strip_tags(s).replace(",", "").replace("--", "0")
        mult = 1.0
        if t.endswith("亿"):
            mult, t = 1e8, t[:-1]
        elif t.endswith("万"):
            mult, t = 1e4, t[:-1]
        try:
            return float(t) * mult
        except ValueError:
            return 0.0

    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", tbody, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 10:
            continue
        code6 = strip_tags(tds[1])
        if not re.fullmatch(r"\d{6}", code6):
            m = re.search(r"/stockpage/(\d{6})", tds[1])
            code6 = m.group(1) if m else ""
        if not code6:
            continue
        name = strip_tags(tds[2])
        if not name or re.fullmatch(r"\d{6}", name):
            continue
        mkt = "SH" if code6.startswith(("6", "9", "5")) else "BJ" if code6.startswith(("4", "8")) else "SZ"
        out.append(
            {
                "symbol": f"{code6}.{mkt}",
                "name": name,
                "price": num(tds[3]),
                "change_pct": num(tds[4]),
                "turnover": num(tds[7]),
                "vol_ratio": num(tds[8]),
                "amount": num(tds[10]) if len(tds) > 10 else 0.0,
                "market_cap": num(tds[12]) if len(tds) > 12 else 0.0,
            }
        )
        if len(out) >= limit:
            break
    return out


@router.get("/boards/{code}/members")
def board_members_by_name(
    code: str,
    name: str = Query("", description="板块名：传了就用本地成分表（和板块K线同源）"),
    kind: str = Query("industry"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """板块成分股（统一入口）。

    传 name → 走本地 industry_members.json（和 /boards/{code}/kline 的指数同源，
    保证「K线成分」和「成分列表」是同一批股票）；不传 → 回落到同花顺详情页。
    历史上 /dashboard/board-members 干的是同一件事，保留兼容但新代码走这里。
    """
    if name:
        try:
            from ...api import _symbol_names
            from ...runtime import STORE
            from ..screener.batch import _market_snapshot
            from .dashboard_api import _industry_map

            syms = (_industry_map() or {}).get(name) or []
            if syms:
                try:
                    snap = _market_snapshot(STORE)
                except Exception:  # noqa: BLE001
                    snap = {}
                names = _symbol_names()
                items = []
                for s in syms:
                    m = snap.get(s) or {}
                    items.append(
                        {
                            "symbol": s,
                            "name": names.get(s, ""),
                            "price": m.get("close"),
                            "change_pct": m.get("change_pct"),
                            "turnover": m.get("turnover"),
                            "amount": m.get("amount_yi"),
                        }
                    )
                return {"code": code, "name": name, "source": "local", "count": len(items), "items": items[:limit]}
        except Exception:  # noqa: BLE001
            pass

    data = board_detail(code, limit=min(limit, 100))
    data["name"] = name
    data["source"] = "ths"
    return data


@router.get("/boards/{code}")
def board_detail(code: str, limit: int = Query(30, ge=1, le=100)) -> dict:
    """板块成分股。同花顺详情页 `/thshy/detail/code/{code}/`，分页用 `?page=N`。"""
    try:
        items = _cached(f"board:{code}:{limit}", lambda: _fetch_board_members(code, limit), ttl=60.0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"板块成分获取失败: {exc}") from exc
    return {"code": code, "count": len(items), "items": items}


@router.get("/boards/{code}/kline")
def board_kline(
    code: str,
    kind: str = Query("industry"),
    name: str = Query("", description="板块名：传了就优先用本地成分表（比同花顺第 1 页更全、不带排序偏差）"),
    period: str = Query("day", description="day / week / month / quarter / year"),
    limit: int = Query(120, ge=20, le=3000),
    store=Depends(get_store),
) -> dict:
    """板块指数日线：成分股等权合成（同花顺板块详情里那根板块 K 线的口径）。

    为什么自算：THS 的板块指数点位接口要鉴权，东财 push2 有风控；
    成分股等权合成既稳定，又和右侧成分股列表同源，读数不会对不上。
    价格用后复权 close 算涨跌，所以尾巴不会因除权突变。
    """
    import polars as pl

    # 成分来源：优先本地 industry_members.json（全量、无排序偏差）；
    # 本地没有这个板块名时才退到同花顺详情页第 1 页（概念板块常用）。
    symbols: list[str] = []
    source = "ths"
    if name:
        try:
            from .dashboard_api import _industry_map

            local = (_industry_map() or {}).get(name) or []
            if local:
                symbols = list(local)
                source = "local"
        except Exception:  # noqa: BLE001
            symbols = []

    if not symbols:
        try:
            members = _cached(f"board:{code}:200", lambda: _fetch_board_members(code, 200), ttl=600.0)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"板块成分获取失败: {exc}") from exc
        symbols = [m["symbol"] for m in members]
    if not symbols:
        raise HTTPException(status_code=404, detail=f"板块 {code} 没有成分股")

    lf = store.scan()
    names = set(lf.collect_schema().names())
    cols = [c for c in ("symbol", "date", "open", "high", "low", "close", "volume") if c in names]
    frame = lf.filter(pl.col("symbol").is_in(symbols)).select(cols).collect()
    if frame.height == 0:
        raise HTTPException(status_code=404, detail=f"板块 {code} 的成分股在当前代际里没有行情")

    frame = (
        frame.sort(["symbol", "date"])
        .with_columns(pl.col("close").shift(1).over("symbol").alias("_prev"))
        .filter(pl.col("_prev").is_not_null() & (pl.col("_prev") > 0))
        .with_columns(
            (pl.col("close") / pl.col("_prev") - 1.0).alias("_r"),
            (pl.col("open") / pl.col("_prev") - 1.0).alias("_ro"),
            (pl.col("high") / pl.col("_prev") - 1.0).alias("_rh"),
            (pl.col("low") / pl.col("_prev") - 1.0).alias("_rl"),
        )
    )
    agg = (
        frame.group_by("date")
        .agg(
            pl.col("_r").mean().alias("_r"),
            pl.col("_ro").mean().alias("_ro"),
            pl.col("_rh").mean().alias("_rh"),
            pl.col("_rl").mean().alias("_rl"),
            pl.col("volume").sum().alias("volume"),
            pl.len().alias("members"),
        )
        .sort("date")
        .with_columns((1000.0 * (1.0 + pl.col("_r")).cum_prod()).alias("close"))
        .with_columns(
            [
                (pl.col("close") / (1.0 + pl.col("_r")) * (1.0 + pl.col("_ro"))).alias("open"),
                (pl.col("close") / (1.0 + pl.col("_r")) * (1.0 + pl.col("_rh"))).alias("high"),
                (pl.col("close") / (1.0 + pl.col("_r")) * (1.0 + pl.col("_rl"))).alias("low"),
            ]
        )
    )

    # 周/月/季/年：和个股 K 线同一套聚合口径（日线合成，零额外数据成本）
    period = (period or "day").lower()
    if period in _PERIOD_DUR:
        agg = (
            agg.with_columns(pl.col("date").dt.truncate(_PERIOD_DUR[period]).alias("date"))
            .group_by("date")
            .agg(
                pl.col("open").first(),
                pl.col("high").max(),
                pl.col("low").min(),
                pl.col("close").last(),
                pl.col("volume").sum(),
            )
            .sort("date")
        )

    agg = agg.tail(limit)

    rows = [
        {
            "date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
            "open": round(r["open"], 2),
            "high": round(r["high"], 2),
            "low": round(r["low"], 2),
            "close": round(r["close"], 2),
            "volume": float(r["volume"] or 0.0),
        }
        for r in agg.iter_rows(named=True)
    ]
    return {
        "code": code,
        "kind": kind,
        "name": name,
        "period": period,
        "source": source,
        "members": len(symbols),
        "count": len(rows),
        "rows": rows,
    }
