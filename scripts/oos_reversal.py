"""急跌反弹 · 四关验证（样本内 / 样本外 / 逐年 / 水下期）。

判定标准（四关全过才可用）：
  1. 样本内夏普 > 0.5
  2. 样本外夏普 > 0，且衰减 < 60%
  3. 正收益年份 >= 60%
  4. 最长水下期 < 2 年（730 天）
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig, FeeConfig  # noqa: E402

IN_S, IN_E = date(2015, 1, 5), date(2022, 12, 30)
OUT_S, OUT_E = date(2023, 1, 3), date(2026, 11, 7)

THS = [-0.12, -0.15, -0.18]
STOPS = [0.08, 0.12]
HOLDS = [5, 10, 20]


def reversal(f: pl.DataFrame, th: float) -> pl.DataFrame:
    d = f.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("_r3"),
        (pl.col("volume") / pl.col("volume").shift(1).over("symbol")).alias("_vr"),
    )
    h = d.filter(
        (pl.col("_r3") < th) & (pl.col("close") > pl.col("open"))
        & (pl.col("_vr") > 1.2) & pl.col("_r3").is_not_null()
    )
    if h.height == 0:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "score": pl.Float64})
    return h.select("symbol", "date", (-pl.col("_r3") * 100).alias("score"))


def run(bars, hits, stop, hold, s, e):
    cfg = BacktestConfig(
        init_cash=100_000, risk_pct=0.03, max_positions=10, max_position_pct=0.25,
        stop_loss_pct=stop, max_hold_days=hold, fees=FeeConfig(),
    )
    return BacktestEngine(cfg, bars).run(hits, start=s, end=e)


def underwater_days(snapshots) -> int:
    nav = [s.nav for s in snapshots]
    dates = [s.date for s in snapshots]
    peak = nav[0] if nav else 1.0
    start = dates[0] if dates else None
    longest = 0
    for i, v in enumerate(nav):
        if v >= peak:
            peak, start = v, dates[i]
        elif start is not None:
            longest = max(longest, (dates[i] - start).days)
    return longest


def yearly_of(metrics) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in (metrics.get("monthly") or {}).items():
        y = k.split("-")[0]
        out[y] = (1 + out.get(y, 0)) * (1 + v) - 1
    return out


def main() -> None:
    raw = GenerationStore(config.DATA_DIR).collect()
    frame = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )
    bars = BarStore(frame)

    print("=== 第一关：样本内网格（2015-2022）===")
    print(f"{'跌幅':>6}{'止损':>6}{'持仓':>5}{'信号':>7}{'收益':>10}{'夏普':>7}{'回撤':>9}{'交易':>6}")
    print("-" * 60)
    grid = []
    cache = {th: reversal(frame, th) for th in THS}
    for th in THS:
        for stop in STOPS:
            for hold in HOLDS:
                r = run(bars, cache[th], stop, hold, IN_S, IN_E)
                m = r.metrics
                grid.append({"th": th, "stop": stop, "hold": hold, "sig": cache[th].height, "in": m})
                print(f"{th*100:>5.0f}%{stop*100:>5.0f}%{hold:>5}{cache[th].height:>7}"
                      f"{m['total_return']*100:>9.1f}%{(m['sharpe'] or 0):>7.2f}"
                      f"{m['max_drawdown']*100:>8.1f}%{m['trades']:>6}", flush=True)

    ok = [g for g in grid if g["in"].get("sharpe") is not None]
    best = max(ok, key=lambda g: g["in"]["sharpe"])
    print(f"\n样本内最优：跌幅 {best['th']*100:.0f}% / 止损 {best['stop']*100:.0f}% / 持仓 {best['hold']} 天"
          f"  → 夏普 {best['in']['sharpe']:.2f}，收益 {best['in']['total_return']*100:+.1f}%")

    print("\n=== 第二关：样本外（2023-2026）===")
    r_out = run(bars, cache[best["th"]], best["stop"], best["hold"], OUT_S, OUT_E)
    mo = r_out.metrics
    decay = (mo["sharpe"] or 0) / best["in"]["sharpe"] - 1
    print(f"  收益 {mo['total_return']*100:+.1f}%  年化 {(mo['annual_return'] or 0)*100:+.1f}%  "
          f"回撤 {mo['max_drawdown']*100:.1f}%  夏普 {(mo['sharpe'] or 0):.2f}  交易 {mo['trades']}")
    print(f"  夏普衰减 {decay*100:+.0f}%")

    print("\n=== 第三关：逐年（全区间 2015-2026）===")
    r_all = run(bars, cache[best["th"]], best["stop"], best["hold"], IN_S, OUT_E)
    ma = r_all.metrics
    yl = yearly_of(ma)
    pos = sum(1 for v in yl.values() if v > 0)
    for y in sorted(yl):
        print(f"    {y}: {yl[y]*100:+7.1f}%  {'#' * int(abs(yl[y]) * 80)}")
    print(f"  正收益年份 {pos}/{len(yl)} = {pos/len(yl)*100:.0f}%")

    print("\n=== 第四关：水下期 ===")
    uw = underwater_days(r_all.snapshots)
    print(f"  全区间 {ma['total_return']*100:+.1f}%  年化 {(ma['annual_return'] or 0)*100:+.1f}%  "
          f"回撤 {ma['max_drawdown']*100:.1f}%（持续 {ma['max_drawdown_days']} 天）")
    print(f"  夏普 {ma['sharpe']:.2f}  卡玛 {(ma['calmar'] or 0):.2f}  最长水下 {uw} 天")

    print("\n=== 判定 ===")
    checks = [
        ("样本内夏普 > 0.5", best["in"]["sharpe"] > 0.5, f"{best['in']['sharpe']:.2f}"),
        ("样本外夏普 > 0 且衰减 < 60%", (mo["sharpe"] or -1) > 0 and decay > -0.6,
         f"{mo['sharpe'] or 0:.2f} / {decay*100:+.0f}%"),
        ("正收益年份 >= 60%", pos / len(yl) >= 0.6, f"{pos/len(yl)*100:.0f}%"),
        ("最长水下 < 730 天", uw < 730, f"{uw} 天"),
    ]
    allpass = True
    for name, okv, val in checks:
        print(f"  {'✅' if okv else '❌'} {name:28} 实测 {val}")
        allpass = allpass and okv
    print(f"\n  总判定：{'通过，可小仓位实盘' if allpass else '未通过，不建议实盘'}")

    out = Path(__file__).resolve().parents[1] / "reports" / "oos_reversal.json"
    out.write_text(json.dumps({
        "best": {"th": best["th"], "stop": best["stop"], "hold": best["hold"]},
        "in": best["in"], "out": mo, "all": ma, "yearly": yl,
        "underwater_days": uw, "checks": {n: bool(v) for n, v, _ in checks},
    }, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON → {out}")


if __name__ == "__main__":
    main()
