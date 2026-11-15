"""把 DeepSeek API Key 写进本地 SQLite，写完立刻可用（不用重启服务）。

为什么不放环境变量：环境变量要重启进程才生效，而且要记在系统设置里；
写 SQLite 的话服务下一次读 key 就能拿到。

用法：
    python scripts/set_deepseek_key.py sk-xxxxxxxxxxxxxxxx
    python scripts/set_deepseek_key.py            # 交互式输入
    python scripts/set_deepseek_key.py --show     # 看当前配置
    python scripts/set_deepseek_key.py --clear    # 清掉

Key 申请：https://platform.deepseek.com/api_keys
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.infrastructure.storage.db import get_db  # noqa: E402

KEY_NAME = "ai.deepseek_key"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?", default="", help="sk- 开头的 Key；省略则交互输入")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()

    db = get_db()

    if args.show:
        cur = db.get_config(KEY_NAME) or ""
        if cur:
            print(f"已配置：{cur[:6]}…{cur[-4:]}（长度 {len(cur)}）")
        else:
            print("未配置")
        print(f"数据库：{Path(config.DATA_DIR) / 'database' / 'facet.db'}")
        return

    if args.clear:
        db.set_config(KEY_NAME, "")
        print("已清除")
        return

    key = args.key.strip()
    if not key:
        key = input("粘贴 DeepSeek API Key（sk- 开头）：").strip()

    if not key:
        print("没有输入，未修改")
        return
    if not key.startswith("sk-"):
        print(f"警告：Key 不是 sk- 开头（实际：{key[:6]}…），仍然写入")

    db.set_config(KEY_NAME, key)
    print(f"已写入：{key[:6]}…{key[-4:]}")
    print(f"数据库：{Path(config.DATA_DIR) / 'database' / 'facet.db'}")
    print()
    print("验证：")
    print("  curl -s http://127.0.0.1:3021/api/v1/ai/status")
    print("  或直接在个股分析页点右上角「AI 分析」")


if __name__ == "__main__":
    main()
