"""从腾讯拉日线（后复权）—— 东财 push2his 被限流时的主数据源。

为什么换腾讯：
  东财 `push2his` 在批量拉取时会间歇性返回 502 / RemoteDisconnected，
  一次全市场采集经常中途失败，导致数据停在昨天。
  腾讯 `web.ifzq.gtimg.cn` 实测稳定，而且不封 IP。

腾讯返回的数据格式（**注意列序和标准 OHLC 不同**）：
    [日期, 开, 收, 高, 低, 成交量]
    即 row[1]=open  row[2]=close  row[3]=high  row[4]=low  row[5]=volume
  这个顺序很容易踩坑：row[3] 是最高价不是收盘价。

复权参数：
    param=<code>,day,,,<count>,qfq   前复权
    param=<code>,day,,,<count>,hfq   后复权
    param=<code>,day,,,<count>,      不复权
  回测必须用 hfq（后复权），否则除权日会被当成暴跌信号。

用法：
    python scripts/fetch_history_tx.py --limit 4800 --bars 1400
    python scripts/fetch_history_tx.py --limit 50 --bars 250     # 快速验证
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import polars as pl

BACKEND = __import__("pathlib").Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}

# web.ifzq.gtimg.cn 批量拉会被限流（HTTP 501），ifzq.gtimg.cn 同样接口可用
KLINE_URLS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{bars},{fq}",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={code},day,,,{bars},{fq}",
]
SNAPSHOT_URL = "https://qt.gtimg.cn/q={codes}"


def tx_code(symbol: str) -> str:
    """600519.SH → sh600519

    指数要特别注意：沪深300 是 `sh000300`，不是 `sz000300`。
    规则：`.SH` 一律 sh；`.SZ` 一律 sz；没有后缀时才靠首位数字猜。
    """
    code, _, mkt = symbol.partition(".")
    m = mkt.upper()
    if m in ("SH", "SS"):
        return f"sh{code}"
    if m == "SZ":
        return f"sz{code}"
    if m == "BJ":
        return f"bj{code}"
    # 无后缀：6/9 开头当沪市，其余深市
    return f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_snapshot() -> list[dict]:
    """拿到要采的标的列表。

    优先级：
      1. data/symbol_names.json —— 4800 只全市场中文名表（最稳，不依赖任何接口）
      2. 当前代际里的 symbol 列
      3. 东财快照（需要网络，可能被限流）

    第 1 条是关键：如果先读代际，一旦某次采歪了（比如只采到 50 只），
    下一轮就只会读到那 50 只，越滚越小 —— 这个坑真踩过。
    """
    names_path = config.DATA_DIR / "symbol_names.json"
    if names_path.exists():
        try:
            names = json.loads(names_path.read_text(encoding="utf-8"))
            syms = sorted(names.keys())
            if len(syms) > 100:
                print(f"从 symbol_names.json 读到 {len(syms)} 只标的")
                return [{"symbol": s} for s in syms]
        except Exception as exc:  # noqa: BLE001
            print(f"读 symbol_names.json 失败（{exc}），继续回退")

    try:
        store = GenerationStore(config.DATA_DIR)
        store.require()
        syms = store.scan().select("symbol").unique().sort("symbol").collect()["symbol"].to_list()
        if syms:
            print(f"从当前代际读到 {len(syms)} 只标的")
            return [{"symbol": s} for s in syms]
    except Exception as exc:  # noqa: BLE001
        print(f"读代际失败（{exc}），改用东财快照")

    from scripts.fetch_history import snapshot as em_snapshot  # type: ignore

    snap = em_snapshot()
    out = [
        f"{s['code']}.SH" if s["code"].startswith(("6", "9")) else f"{s['code']}.SZ"
        for s in snap
        if _num(s["price"]) >= 3 and _num(s["amount"]) >= 1e8
    ]
    print(f"从东财快照拿到 {len(out)} 只标的")
    return [{"symbol": s} for s in out]


def fetch_bars(symbol: str, bars: int, fq: str = "hfq") -> list[dict]:
    code = tx_code(symbol)
    payload = None
    last_exc: Exception | None = None
    for tmpl in KLINE_URLS:
        url = tmpl.format(code=code, bars=bars, fq=fq)
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code in (403, 429, 501):   # 限流，换域名
                    break
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(0.3)
        if payload is not None:
            break
    if payload is None:
        raise last_exc or RuntimeError(f"{symbol} 所有域名都失败")

    data = (payload.get("data") or {}).get(code) or {}
    key = f"{fq}day" if f"{fq}day" in data else "day"
    raw = data.get(key) or []

    rows: list[dict] = []
    for p in raw:
        if not p or len(p) < 6:
            continue
        try:
            d = date.fromisoformat(str(p[0])[:10])
        except ValueError:
            continue
        o, c, h, lo, v = _num(p[1]), _num(p[2]), _num(p[3]), _num(p[4]), _num(p[5])
        if c <= 0 or v <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "date": d,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": v,
                # 腾讯不直接给成交额，用均价近似（收盘 × 量）
                "amount": round(c * v * 100, 2),
            }
        )
    return rows


def fetch_benchmark(bars: int, fq: str = "hfq") -> pl.DataFrame:
    """沪深300 作基准。"""
    rows = fetch_bars("000300.SH", bars, fq)
    return pl.DataFrame(
        [{"date": r["date"], "benchmark": r["close"]} for r in rows],
        schema={"date": pl.Date, "benchmark": pl.Float64},
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4800, help="采多少只（0=全部）")
    ap.add_argument("--bars", type=int, default=1400, help="每只多少根（5 年约 1220）")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--start", type=str, default="", help="YYYY-MM-DD，只要这天之后的（可选）")
    args = ap.parse_args()

    config.ensure_dirs()
    t0 = time.time()

    items = fetch_snapshot()
    if args.limit and args.limit < len(items):
        items = items[: args.limit]
    print(f"准备采 {len(items)} 只，每只 {args.bars} 根（{args.workers} 并发）…")

    rows: list[dict] = []
    done = 0
    failed: list[str] = []

    def work(it: dict) -> list[dict]:
        try:
            return fetch_bars(it["symbol"], args.bars)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{it['symbol']}:{type(exc).__name__}")
            return []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for batch in ex.map(work, items):
            rows.extend(batch)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(items)}  {len(rows):,} 行  {time.time()-t0:.0f}s  失败 {len(failed)}")

    if not rows:
        raise SystemExit("没拉到任何数据 —— 检查网络或腾讯接口是否变更")

    daily = pl.DataFrame(rows).sort(["symbol", "date"])
    if args.start:
        daily = daily.filter(pl.col("date") >= date.fromisoformat(args.start))

    bench = fetch_benchmark(args.bars)
    daily = daily.join(bench, on="date", how="left")

    daily_path = config.DATA_DIR / "daily" / "daily.parquet"
    daily.write_parquet(daily_path, compression="lz4")
    print(f"\ndaily -> {daily_path}")
    print(f"  rows={daily.height:,}  symbols={daily['symbol'].n_unique()}  失败 {len(failed)}")
    print(f"  日期范围: {daily['date'].min()} ~ {daily['date'].max()}")

    # 派生列 → 发布新代际
    from app.indicators import build_enriched  # noqa: E402

    enriched = build_enriched(daily)
    store = GenerationStore(config.DATA_DIR)
    gen = store.publish(enriched)
    print(f"\n已发布代际 {gen.generation[:8]}  rows={gen.rows:,}  {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
