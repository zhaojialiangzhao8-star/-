"""发布前唯一入口：跑完所有数据校验，任何一项失败就 exit 1。

包含两件事：
  1. 自检（check_generation）：唯一性 / 覆盖率 / 中位涨跌幅 / 成交额量级
  2. 双源比对（cross_check）：抽样和第二个源比当天涨跌幅

用法：
    python scripts/verify_data.py                 # 最新日
    python scripts/verify_data.py --date 2026-11-12 --sample 200
    python scripts/verify_data.py --skip-cross    # 对照源都挂了时只跑自检
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(title: str, args: list[str]) -> int:
    print(f"\n===== {title} =====")
    r = subprocess.run([PY, *args], cwd=str(ROOT))
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--source", default="tencent-daily")
    ap.add_argument("--skip-cross", action="store_true")
    args = ap.parse_args()

    codes = [run("自检", ["scripts/check_generation.py"])]

    if not args.skip_cross:
        cross = ["scripts/cross_check.py", "--sample", str(args.sample), "--source", args.source]
        if args.date:
            cross += ["--date", args.date]
        codes.append(run("双源比对", cross))
    else:
        print("\n（跳过双源比对）")

    bad = [c for c in codes if c != 0]
    print("\n" + "=" * 40)
    if bad:
        print(f"[FAIL] {len(bad)} 项未通过 —— 不要发布这一代")
        return 1
    print("[OK] 全部通过，可以发布")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
