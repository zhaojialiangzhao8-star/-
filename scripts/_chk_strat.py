import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

with urllib.request.urlopen("http://127.0.0.1:3021/api/v1/strategies", timeout=30) as r:
    d = json.loads(r.read())

items = d.get("items") or d.get("strategies") or []
print("共 {} 个策略".format(len(items)))
for x in items:
    if str(x["id"]).startswith(("board_", "small_up")):
        print("  {:<22} {}".format(x["id"], x["name"]))
