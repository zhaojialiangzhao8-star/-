"""抓同花顺板块（二级行业 + 概念）的成分股，产出 industry_members.json / concept_members.json。

为什么不用 ajax 接口：`/thshy/index/field/.../ajax/1/` 需要 hexin-v cookie（401），
普通的分页 URL（不带 ajax）可以直出 HTML，翻页到没有新 code 为止。

用法：
    python scripts/fetch_boards_ths.py                # 行业 + 概念
    python scripts/fetch_boards_ths.py --only industry
    python scripts/fetch_boards_ths.py --workers 6
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://q.10jqka.com.cn/",
}
LOCK = threading.Lock()


def get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", errors="replace")


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


# ---------------------------------------------------------------- 板块清单
LIST_URL = "http://q.10jqka.com.cn/{path}/index/field/199112/order/desc/page/{page}/"


def list_boards(kind: str, max_pages: int = 20) -> dict[str, str]:
    """返回 {code: 板块名}。翻页直到没有新 code。"""
    path = "thshy" if kind == "industry" else "gn"
    seen: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        try:
            html = get(LIST_URL.format(path=path, page=page))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {kind} list p{page}: {exc}")
            break
        before = len(seen)
        # 每行：<a href=".../detail/code/881274/">影视院线</a>
        for m in re.finditer(r'/detail/code/(\d+)/[^>]*>([^<]{1,30})<', html):
            code, name = m.group(1), strip_tags(m.group(2))
            if code and name:
                seen.setdefault(code, name)
        if len(seen) == before:
            break
        time.sleep(0.15)
    return seen


# ---------------------------------------------------------------- 成分股
def board_members(code: str, max_pages: int = 30) -> list[str]:
    path = "thshy" if code.startswith("881") else "gn"
    out: list[str] = []
    for page in range(1, max_pages + 1):
        url = f"http://q.10jqka.com.cn/{path}/detail/code/{code}/"
        if page > 1:
            url += f"page/{page}/"
        try:
            html = get(url)
        except Exception:  # noqa: BLE001
            break
        body = html.split("<tbody", 1)
        if len(body) < 2:
            break
        rows = re.findall(r"<tr>(.*?)</tr>", body[1].split("</tbody>", 1)[0], re.S)
        got = 0
        for tr in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 3:
                continue
            cell = tds[1]
            m = re.search(r"/stockpage/(\d{6})/", cell) or re.search(r"\b(\d{6})\b", strip_tags(cell))
            if not m:
                continue
            c6 = m.group(1)
            mkt = "SH" if c6.startswith(("6", "9", "5")) else "BJ" if c6.startswith(("4", "8")) else "SZ"
            out.append(f"{c6}.{mkt}")
            got += 1
        if got == 0:
            break
        time.sleep(0.1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["industry", "concept", "both"], default="both")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="调试：只抓前 N 个板块")
    args = ap.parse_args()

    kinds = ["industry", "concept"] if args.only == "both" else [args.only]
    out: dict[str, dict[str, list[str]]] = {}

    for kind in kinds:
        boards = list_boards(kind)
        if args.limit:
            boards = dict(list(boards.items())[: args.limit])
        print(f"{kind}: 板块 {len(boards)} 个，开始抓成分…", flush=True)

        members: dict[str, list[str]] = {}
        done = 0
        t0 = time.time()

        def work(item: tuple[str, str]) -> tuple[str, list[str]]:
            code, name = item
            try:
                return name, board_members(code)
            except Exception:  # noqa: BLE001
                return name, []

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for name, syms in ex.map(work, boards.items()):
                if syms:
                    members[name] = syms
                done += 1
                if done % 40 == 0:
                    print(f"  {done}/{len(boards)}  {len(members)} 个有成分  {time.time()-t0:.0f}s", flush=True)

        out[kind] = members
        total = sum(len(v) for v in members.values())
        print(f"{kind}: 完成 {len(members)} 个板块，成分合计 {total} 条")

    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    if "industry" in out:
        p = data_dir / "industry_members.json"
        p.write_text(json.dumps(out["industry"], ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"→ {p}  ({len(out['industry'])} 个行业)")
    if "concept" in out:
        p = data_dir / "concept_members.json"
        p.write_text(json.dumps(out["concept"], ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"→ {p}  ({len(out['concept'])} 个概念)")


if __name__ == "__main__":
    main()
