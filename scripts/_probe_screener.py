import json
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:3021"
t0 = time.time()
try:
    with urllib.request.urlopen(BASE + "/api/v1/screener/all?limit_per_strategy=3&scope=ranked", timeout=1800) as r:
        d = json.loads(r.read())
    print("OK {:.0f}s  策略 {}  今日信号 {}".format(time.time() - t0, d["strategy_count"], d["total_signals_today"]))
    for x in d["items"][:6]:
        rk = "#{}".format(x["rank"]) if x.get("rank") else "-"
        print("  {:>3} {:<16} 今日 {:>2}".format(rk, x["name"], len(x.get("rows") or [])))
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print("HTTP {}: {}".format(e.code, body[:200]))
except Exception as e:
    print("{}: {!r}".format(type(e).__name__, str(e)[:120]))
