"""以单张工程图图片运行严格证据链 STEP Agent。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.agents.step.graph import build_image_to_step_graph


async def async_main(image_path: Path) -> int:
    """只把 image_path 传入 Agent，并打印真实终态。"""
    graph = build_image_to_step_graph()
    state = await graph.ainvoke(
        {"image_path": str(image_path.resolve())},
        config={
            "configurable": {
                "thread_id": f"step-image-{uuid.uuid4().hex[:10]}"
            }
        },
    )
    result = {
        "status": state.get("status"),
        "result": state.get("result", {}),
        "output_dir": state.get("output_dir"),
        "artifact_paths": state.get("artifact_paths", {}),
        "errors": state.get("errors", []),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if state.get("status") != "failed" else 1


def parse_args() -> argparse.Namespace:
    """解析唯一必填图片参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_path", type=Path)
    return parser.parse_args()


def main() -> int:
    """同步命令行入口。"""
    args = parse_args()
    return asyncio.run(async_main(args.image_path))


if __name__ == "__main__":
    raise SystemExit(main())
