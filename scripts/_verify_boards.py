import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:3021"


def g(p, t=30):
    with urllib.request.urlopen(BASE + p, timeout=t) as r:
        return json.loads(r.read())


for kind in ("industry", "concept"):
    b = g("/api/v1/boards?kind=" + kind + "&limit=6")
    print("{}: {} 个".format(kind, b["count"]))
    for c in b["items"][:3]:
        if kind == "industry":
            print("  {:<14} {:>+6.2f}%  涨{}/跌{}  龙头 {}".format(
                c["name"], c["change_pct"], c["up_count"], c["down_count"], c.get("leader", "")))
        else:
            print("  {:<14} 创建 {}  成分 {}".format(c["name"], c.get("created"), c.get("members")))
    first = b["items"][0]
    d = g("/api/v1/boards/" + first["code"] + "?limit=4")
    names = ", ".join("{} {:+.2f}%".format(m["name"], m["change_pct"]) for m in d["items"][:4])
    print("  → 成分 {} 只: {}".format(d["count"], names))
    print()

h = g("/api/v1/health")
i = g("/api/v1/indices")
s = g("/api/v1/screener/all?limit_per_strategy=2")
print("health  {:,} 行  策略 {}".format(h["rows"], h["strategies"]))
print("indices {} 个".format(i["count"]))
print("screener cached={} 策略 {} 信号 {}".format(s["cached"], s["strategy_count"], s["total_signals_today"]))
