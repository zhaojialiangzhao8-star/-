import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

base = Path(r"C:\Users\wang1\Documents\deepseek-harness\default-workspace\facet\reports")

# 基准
b = base / "_random_bench.json"
if b.exists():
    bench = json.loads(b.read_text(encoding="utf-8"))
    print("随机基准：")
    for k, v in bench.items():
        print(
            "  {:<9} 均值 {:+.1f}%  区间 [{:+.1f}%, {:+.1f}%]  n={}".format(
                k, v["mean"] * 100, v["lo"] * 100, v["hi"] * 100, v["n"]
            )
        )
    print()

# 结果
f = base / "all_strategies_v2.json"
if not f.exists():
    print("还没有策略结果")
    raise SystemExit

d = json.loads(f.read_text(encoding="utf-8"))
c = Counter(x["split"] for x in d)
print("已记录 {} 条  分布: {}".format(len(d), dict(c)))
print()

# 按策略聚合
by = defaultdict(list)
for x in d:
    by[x["strategy"]].append(x)

print("{:<34}{:<8}{:<8}{:<8}{:>9}{:>6}{:>12}".format("策略", "训练", "验证", "留出", "均收益", "交易", "总评"))
print("-" * 92)

rows = []
for sid, rs in by.items():
    vs = {r["split"]: r["verdict"] for r in rs}
    stars = sum(1 for v in vs.values() if v == "★")
    harms = sum(1 for v in vs.values() if v == "↓↓")
    avg = sum(r["ret"] for r in rs) / len(rs)
    tr = sum(r["trades"] or 0 for r in rs)
    if stars >= 2:
        total = "★ 值得继续"
    elif harms >= 2:
        total = "↓↓ 有害"
    elif stars == 1:
        total = "? 单段有效"
    else:
        total = "× 噪声"
    rows.append((sid, vs, avg, tr, total))

rows.sort(key=lambda x: -x[2])
for sid, vs, avg, tr, total in rows:
    print(
        "{:<34}{:<8}{:<8}{:<8}{:>+8.1f}%{:>6}{:>12}".format(
            sid,
            vs.get("train", "—"),
            vs.get("valid", "—"),
            vs.get("holdout", "—"),
            avg * 100,
            tr,
            total,
        )
    )

print()
print("=" * 92)
n_star = sum(1 for r in rows if r[4].startswith("★"))
n_harm = sum(1 for r in rows if r[4].startswith("↓↓"))
n_noise = sum(1 for r in rows if r[4].startswith("×"))
n_one = sum(1 for r in rows if r[4].startswith("?"))
print("★ 值得继续: {}".format(n_star))
print("? 单段有效: {}".format(n_one))
print("× 噪声:     {}".format(n_noise))
print("↓↓ 有害:    {}".format(n_harm))
