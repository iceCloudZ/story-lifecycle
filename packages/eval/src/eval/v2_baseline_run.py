"""v2 baseline 重评（§1.2）— baseline.py 原样重跑，Go 端点，落 snapshot_v2_20260806/。"""
import json
import logging
import shutil
import sys
from pathlib import Path

PACKAGE_ROOT = Path(r"D:\github\story-lifecycle\packages\eval")
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"

sys.path.insert(0, str(PACKAGE_ROOT / "src"))
sys.path.insert(0, r"D:\github\story-lifecycle\packages\story-lifecycle\src")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, encoding="utf-8")

from eval import baseline  # noqa: E402
from eval.judges import configure_llm_env  # noqa: E402


def main() -> dict:
    configure_llm_env()  # Go 端点 + OPENCODE_API_KEY（STORY_LLM_* 必须就位）
    res = baseline.run_baseline(
        dataset_dir=str(PACKAGE_ROOT / "dataset"),
        results_dir=str(SNAP_V2),
        concurrency=4,
    )
    # 改名 baseline_<date>.json/md → baseline_v2.json/md
    date = res["json"].rsplit("_", 1)[-1].rsplit(".", 1)[0]
    for suffix in ("json", "md"):
        src = SNAP_V2 / f"baseline_{date}.{suffix}"
        dst = SNAP_V2 / f"baseline_v2.{suffix}"
        if src.exists():
            shutil.copy2(src, dst)
    # partial 文件保留（断点续跑用），统计
    return {
        "count": res["count"],
        "errors": res["errors"],
        "consistency": res["consistency"],
        "json": str(SNAP_V2 / "baseline_v2.json"),
        "md": str(SNAP_V2 / "baseline_v2.md"),
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
