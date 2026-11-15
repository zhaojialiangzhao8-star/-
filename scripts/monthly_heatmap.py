"""月度收益热力图 — 判断收益是不是靠少数几个月的极端行情撑起来的。

每个策略可以带自己的参数（急跌反弹用网格最优的 -18%/止损12%/5天），
生成自包含 HTML 热力图（红涨绿跌），并额外输出一份 JSON 供报告引用。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "app" / "strategies"))

from app import config  # noqa: E402
from app.store import GenerationStore  # noqa: E402
from app.backtest.data import BarStore  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.models import BacktestConfig, FeeConfig  # noqa: E402
import _classic_lib as C  # noqa: E402
import composite_ic6 as M  # noqa: E402

OUT_HTML = Path(__file__).resolve().parents[1] / "reports" / "monthly_heatmap.html"
OUT_JSON = Path(__file__).resolve().parents[1] / "reports" / "monthly_heatmap.json"
START, END = date(2015, 1, 5), date(2026, 11, 7)


def reversal_param(f: pl.DataFrame, th: float = -0.18) -> pl.DataFrame:
    """参数化急跌反弹：3 日跌幅超阈值 + 当日收阳 + 量比 1.2 倍。"""
    d = f.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("_r3"),
        (pl.col("volume") / pl.col("volume").shift(1).over("symbol")).alias("_vr"),
    )
    hit = d.filter(
        (pl.col("_r3") < th)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("_vr") > 1.2)
        & pl.col("_r3").is_not_null()
    )
    return hit.select("symbol", "date", "close", (-pl.col("_r3") * 100).alias("score"))


BASE = dict(init_cash=100_000, risk_pct=0.01, max_positions=5, max_position_pct=0.2,
            fees=FeeConfig())

# (显示名, 信号函数, 覆盖参数)
CASES = [
    ("急跌反弹 -18% / 止损12% / 5天", lambda f: reversal_param(f, -0.18),
     dict(stop_loss_pct=0.12, max_hold_days=5)),
    ("急跌反弹 -12% / 止损8% / 20天", lambda f: C.run(f, "classic_reversal"),
     dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("52周新高", lambda f: C.run(f, "classic_week52_high"),
     dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("RSI超卖", lambda f: C.run(f, "classic_rsi_oversold"),
     dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("合成 IC6", lambda f: M.filter_frame(f, {"min_votes": 2, "min_score": 0.0}),
     dict(stop_loss_pct=0.08, max_hold_days=20)),
    ("均值回归", lambda f: C.run(f, "classic_mean_revert"),
     dict(stop_loss_pct=0.08, max_hold_days=20)),
]


def color(v: float | None) -> tuple[str, str]:
    if v is None:
        return "#121a24", "#657a92"
    a = min(abs(v) / 0.08, 1.0)
    if v > 0:
        return f"rgba(255,77,79,{0.10 + 0.55 * a:.3f})", "#ffd9d9" if a > 0.4 else "#e6edf6"
    if v < 0:
        return f"rgba(34,197,94,{0.10 + 0.55 * a:.3f})", "#d6ffe4" if a > 0.4 else "#e6edf6"
    return "#121a24", "#9fb0c5"


def build_html(results: dict[str, dict]) -> str:
    years = sorted({k.split("-")[0] for r in results.values() for k in r})
    months = [f"{m:02d}" for m in range(1, 13)]
    blocks = []
    for name, monthly in results.items():
        ytd: dict[str, float] = {}
        for k, v in monthly.items():
            y = k.split("-")[0]
            ytd[y] = (1 + ytd.get(y, 0.0)) * (1 + v) - 1
        rows = []
        for y in years:
            cells = []
            for m in months:
                key = f"{y}-{m}"
                v = monthly.get(key)
                bg, fg = color(v)
                cells.append(
                    f'<td style="background:{bg};color:{fg}">'
                    + (f"{v*100:+.1f}" if v is not None else "")
                    + "</td>"
                )
            total = ytd.get(y)
            bg, fg = color(total)
            rows.append(
                f'<tr><th class="yr">{y}</th>{"".join(cells)}'
                f'<td class="ytd" style="background:{bg};color:{fg}">'
                + (f"{total*100:+.1f}" if total is not None else "")
                + "</td></tr>"
            )
        blocks.append(
            f'<section><h2>{name}</h2><table><thead><tr><th class="yr">年份</th>'
            + "".join(f"<th>{m}</th>" for m in months)
            + '<th class="ytd">全年</th></tr></thead><tbody>'
            + "".join(rows)
            + "</tbody></table></section>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>月度收益热力图</title>
<style>
 body{{margin:0;background:#0a0e14;color:#e6edf6;font:13px/1.5 system-ui,-apple-system,"PingFang SC",sans-serif;padding:20px 24px 60px}}
 h1{{font-size:16px;margin:0 0 4px}} .sub{{color:#657a92;font-size:11.5px;margin-bottom:18px;font-family:ui-monospace,Consolas,monospace}}
 section{{margin-bottom:22px}} h2{{font-size:13px;margin:0 0 8px;color:#cbd5e1}}
 table{{border-collapse:separate;border-spacing:2px;font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
 th{{color:#657a92;font-weight:500;padding:4px 6px;text-align:center;min-width:44px}}
 th.yr{{text-align:left;min-width:48px}}
 td{{padding:5px 6px;text-align:right;border-radius:4px;min-width:44px;font-variant-numeric:tabular-nums}}
 td.ytd{{font-weight:700;border-left:2px solid #1e2a38}}
 .legend{{color:#657a92;font-size:11px;margin-top:6px}}
</style></head><body>
<h1>月度收益热力图</h1>
<div class="sub">账户回测 · 10 万本金 · 1% 风险 · 5 仓 · {START} ~ {END} · 红涨绿跌</div>
{''.join(blocks)}
<div class="legend">单元格为该月收益率(%)；最后一列是当年累计；空白 = 该月无交易。</div>
</body></html>"""


def main() -> None:
    raw = GenerationStore(config.DATA_DIR).collect()
    frame = raw.filter(
        (pl.col("close") > 0) & (pl.col("open") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
    )
    bars = BarStore(frame)
    results: dict[str, dict] = {}
    summary: list[dict] = []

    for name, fnh, override in CASES:
        hits = fnh(frame)
        cfg = BacktestConfig(**{**BASE, **override})
        r = BacktestEngine(cfg, bars).run(hits, start=START, end=END)
        m = r.metrics
        results[name] = m.get("monthly", {})
        summary.append({
            "name": name, "signals": hits.height, "final_equity": m["final_equity"],
            "total_return": m["total_return"], "annual_return": m["annual_return"],
            "max_drawdown": m["max_drawdown"], "sharpe": m["sharpe"], "calmar": m["calmar"],
            "trades": m["trades"], "win_rate": m["win_rate"], "payoff": m["payoff"],
            "stop": override["stop_loss_pct"], "hold": override["max_hold_days"],
        })
        print(f"{name:34} 收益 {m['total_return']*100:+8.1f}%  夏普 {(m['sharpe'] or 0):>5.2f}  "
              f"回撤 {m['max_drawdown']*100:7.1f}%  交易 {m['trades']:>4}  信号 {hits.height}", flush=True)

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(build_html(results), encoding="utf-8")
    OUT_JSON.write_text(json.dumps({"summary": summary, "monthly": results}, ensure_ascii=False), encoding="utf-8")
    print(f"\n热力图 → {OUT_HTML}\n数据 → {OUT_JSON}")


if __name__ == "__main__":
    main()
