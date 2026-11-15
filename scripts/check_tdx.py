"""检查通达信本地数据是否就绪，并把它和东财数据做一次对齐校验。

用法：
    python scripts/check_tdx.py
    python scripts/check_tdx.py --tdxdir "D:\\new_tdx"

它会做四件事：
  1. 数各市场有多少 .day / .lc5 文件，看最新一根 K 线的日期
  2. 抽 3 只票读日线，跟东财的后复权数据比对收盘价
  3. 明确指出两条数据源的差异（通达信不复权，除权日会有跳空）
  4. 给出下一步建议
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.infrastructure.data_sources.base import Capability  # noqa: E402
from app.infrastructure.data_sources.registry import registry  # noqa: E402
from app.infrastructure.data_sources.tdx_local import TdxLocalProvider  # noqa: E402

SAMPLE = ["600519.SH", "000001.SZ", "605258.SH"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tdxdir", default=None)
    args = ap.parse_args()

    tdx = TdxLocalProvider(args.tdxdir)
    info = tdx.info()
    print(f"通达信目录: {tdx.tdxdir}")
    print(f"能力: {sorted(c.value for c in info.capabilities) or '无（数据未下载）'}")
    print(f"说明: {info.note}\n")

    cov = tdx.coverage()
    print("=== 本地文件统计 ===")
    total = 0
    for m in cov["markets"]:
        total += m["symbols"]
        print(f"  {m['market']}: {m['symbols']:>5} 个日线文件 · 最新 {m['latest'] or '—'}")
    print(f"  合计 {total} 个\n")

    if total == 0:
        print("=" * 62)
        print("通达信本地数据还没下载。三步搞定：")
        print("  1. 打开通达信，登录任意行情主站")
        print("  2. 菜单：系统 → 盘后数据下载")
        print("     勾选「日线和实时行情数据」，分钟线可选（占几十 GB）")
        print("  3. 下载完成后重跑本脚本")
        print("=" * 62)
        return

    print("=== 抽样比对（通达信本地 vs 东财后复权）===")
    em = registry.get("eastmoney")
    for sym in SAMPLE:
        try:
            local = tdx.fetch_daily(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: 本地读取失败 —— {exc}")
            continue
        if not local:
            print(f"  {sym}: 本地 0 根")
            continue
        last = local[-1]
        line = f"  {sym}: 本地 {len(local):>5} 根 · 最新 {last.dt.date()} 收 {last.close:.2f} 量 {last.volume/1e4:.1f}万手"

        if em is not None:
            try:
                remote = em.fetch_daily(sym, adjust="back")
                if remote:
                    r = remote[-1]
                    rd = getattr(r, "dt", None) or getattr(r, "date", None)
                    rc = getattr(r, "close", None)
                    if rd is not None and rc is not None:
                        same_day = str(rd)[:10] == str(last.dt.date())
                        diff = (last.close / rc - 1) * 100 if rc else 0
                        tag = (
                            "一致" if same_day and abs(diff) < 0.5
                            else "不复权 vs 后复权，差异 %.1f%%" % diff
                        )
                        line += f"  | 东财 {str(rd)[:10]} 后复权 {rc:.2f} → {tag}"
            except Exception as exc:  # noqa: BLE001
                line += f"  | 东财比对失败 {type(exc).__name__}"
        print(line)

    print("\n=== 结论 ===")
    print("  日线可用：全市场本地读取，不受东财限流（>5/秒封 IP）影响")
    print("  但通达信本地**不复权**：分红送转日会有跳空，直接算收益率是错的")
    print("  建议：日线回测继续用 eastmoney 的后复权数据；tdx 只用来补")
    print("        (a) 分钟线（1/5/15/30/60，东财没有）")
    print("        (b) 东财限流时的兜底日线")


if __name__ == "__main__":
    main()
