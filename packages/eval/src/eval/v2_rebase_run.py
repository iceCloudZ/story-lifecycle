"""v2_rebase 后台入口（Start-Process 用）。"""
import json
import logging
import sys

sys.path.insert(0, r"D:\github\story-lifecycle\packages\eval\src")
sys.path.insert(0, r"D:\github\story-lifecycle\packages\story-lifecycle\src")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, encoding="utf-8")

from eval.v2_rebase import main

if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
