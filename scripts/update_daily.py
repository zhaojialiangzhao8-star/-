"""每日增量更新：抓当日全市场 bar → 追加 daily → 重算 enriched → 发布新代际。

与 fetch_all_market.py 的区别：
  · 只抓「今天」一根，不重拉历史（1 分钟 vs 40 分钟）
  · 复权对齐：实时价是未复权的，用「库里最后收盘 / 实时昨收」换算成后复权口径
  · 幂等：同一天重复跑会覆盖当天那一行，不会重复插入
  · 默认只在收盘后跑（15:05 之前会拒绝，除非 --force）

跑法：
    python scripts/update_daily.py            # 收盘后跑
    python scripts/update_daily.py --force    # 忽略时间检查（调试）
    python scripts/update_daily.py --no-enrich  # 只更新 daily，不重建 enriched
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.indicators import build_enriched  # noqa: E402
from app.store import GenerationStore  # noqa: E402

CHUNK = 80
SINA = "http://hq.sinajs.cn/list={codes}"
HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
ENRICH_CHUNK = 400
INDEX_CODE = "sh000300"          # 基准指数（与库里 benchmark 口径一致：点位）


def to_sina(symbol: str) -> str:
    code, _, mkt = symbol.partition(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt.upper(), "sh") + code


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """批量抓实时行情，返回 {symbol: {open, pre, close, high, low, volume, amount}}。"""
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        codes = ",".join(to_sina(s) for s in chunk)
        try:
            req = urllib.request.Request(SINA.format(codes=codes), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=12) as resp:
                text = resp.read().decode("gbk", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            print(f"  块 {i} 失败 {type(exc).__name__}", flush=True)
            continue
        for line in text.split("\n"):
            if "=" not in line:
                continue
            head, _, payload = line.partition("=")
            sym = head.strip().replace("var hq_str_", "")
            f = payload.strip().strip('";').split(",")
            if len(f) < 10:
                continue
            try:
                o, pre, cur, hi, lo = (float(f[j]) for j in (1, 2, 3, 4, 5))
                vol, amt = float(f[8]), float(f[9])         # 股 / 元
            except ValueError:
                continue
            if pre <= 0 or cur <= 0:
                continue
            code = sym[2:]
            suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym[:2])
            if not suffix:
                continue
            out[f"{code}.{suffix}"] = {
                "open": o, "pre": pre, "close": cur, "high": hi, "low": lo,
                "volume": vol / 100.0, "amount": amt,          # 手
            }
    return out


def fetch_index() -> float | None:
    try:
        req = urllib.request.Request(SINA.format(codes=INDEX_CODE), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode("gbk", errors="ignore")
        f = text.split('"')[1].split(",")
        return float(f[3]) if len(f) > 3 else None           # 现价=最新点位
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略收盘时间检查")
    ap.add_argument("--no-enrich", action="store_true", help="只更新 daily，不重建 enriched")
    ap.add_argument("--dry-run", action="store_true", help="只抓行情、不写盘（调试用）")
    args = ap.parse_args()

    now = datetime.now()
    if not args.force and now.hour * 60 + now.minute < 15 * 60 + 5:
        print(f"现在 {now:%H:%M}，还没收盘（15:05 之后才更新）。调试可加 --force")
        return

    t0 = time.time()
    daily_path = Path(config.DATA_DIR) / "daily" / "daily.parquet"
    if not daily_path.exists():
        print(f"缺 {daily_path}，先跑 scripts/fetch_all_market.py")
        return

    lf = pl.scan_parquet(daily_path)
    last_date = lf.select(pl.col("date").max()).collect()[0, 0]
    today = date.today()
    print(f"库里最后一天 {last_date} · 抓取 {today}", flush=True)
    if last_date >= today:
        print("已经是最新，跳过抓取（仍会重建派生列如需）")
        last_rows = lf.filter(pl.col("date") == last_date).select(["symbol", "close"]).collect()
        prev_close = dict(zip(last_rows["symbol"].to_list(), last_rows["close"].to_list()))
    else:
        # 每只票的「库里最后收盘」，用来把实时价换算成后复权口径
        last_rows = (
            lf.sort("date").group_by("symbol").agg(pl.col("close").last().alias("close"))
            .collect()
        )
        prev_close = dict(zip(last_rows["symbol"].to_list(), last_rows["close"].to_list()))

        quotes = fetch_quotes(sorted(prev_close))
        print(f"  拿到 {len(quotes)}/{len(prev_close)} 只实时行情", flush=True)
        idx = fetch_index()
        print(f"  指数 {INDEX_CODE} = {idx}", flush=True)

        rows = []
        for sym, q in quotes.items():
            base = prev_close.get(sym)
            if not base or q["pre"] <= 0:
                continue
            f = base / q["pre"]                      # 复权因子
            close = q["close"] * f
            rows.append(
                {
                    "symbol": sym,
                    "date": today,
                    "open": round(q["open"] * f, 4),
                    "high": round(q["high"] * f, 4),
                    "low": round(q["low"] * f, 4),
                    "close": round(close, 4),
                    "volume": round(q["volume"], 2),
                    "amount": round(close * q["volume"] * 100, 2),
                    "amplitude": round((q["high"] - q["low"]) / q["pre"] * 100, 4),
                    "change_pct": round((q["close"] / q["pre"] - 1) * 100, 4),
                    "turnover": None,
                    "benchmark": idx,
                }
            )
        if not rows:
            print("没有可写入的行，退出")
            return
        new_df = pl.DataFrame(rows, schema={
            "symbol": pl.Utf8, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
            "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
            "amplitude": pl.Float64, "change_pct": pl.Float64, "turnover": pl.Float64,
            "benchmark": pl.Float64,
        })
        print(f"  今日 {new_df.height} 行", flush=True)
        if args.dry_run:
            print(new_df.head(5))
            print("--dry-run：不写盘")
            return

        # 去重后写回（同一天重复跑会覆盖）
        merged = pl.concat([lf.collect(), new_df], how="diagonal_relaxed")
        merged = merged.sort(["symbol", "date"]).unique(subset=["symbol", "date"], keep="last")
        merged.write_parquet(daily_path, compression="lz4")
        print(f"daily.parquet 已更新 → {merged.height:,} 行（{time.time()-t0:.0f}s）", flush=True)
        del merged

    if args.no_enrich:
        print("跳过 enriched 重建（--no-enrich）")
        return

    print("\n重建派生列（按股票分批）…", flush=True)
    t2 = time.time()
    out_dir = Path(config.DATA_DIR) / "enriched" / "_parts"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_syms = pl.scan_parquet(daily_path).select("symbol").unique().collect()["symbol"].to_list()
    for i in range(0, len(all_syms), ENRICH_CHUNK):
        chunk = all_syms[i : i + ENRICH_CHUNK]
        sub = pl.scan_parquet(daily_path).filter(pl.col("symbol").is_in(chunk)).collect()
        build_enriched(sub).write_parquet(out_dir / f"e_{i // ENRICH_CHUNK:04d}.parquet", compression="lz4")
        del sub
    tmp = Path(config.DATA_DIR) / "enriched" / "_merged.parquet"
    pl.scan_parquet(str(out_dir / "*.parquet")).sink_parquet(tmp, compression="lz4")
    store = GenerationStore(config.DATA_DIR)
    gen = store.publish_file(tmp)
    shutil.rmtree(out_dir, ignore_errors=True)
    tmp.unlink(missing_ok=True)
    print(f"代际 {gen.generation[:8]} 已发布 rows={gen.rows:,}（{time.time()-t2:.0f}s）")
    print(f"总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
