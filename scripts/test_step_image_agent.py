"""本地运行 STEP Agent；--interactive 由使用者回答，默认遇到人工问题退出 2。"""

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

def prompt_answer(request: dict) -> dict | None:
    """展示完整问题和候选值，仅接收实际使用者填写的 JSON。"""
    print(json.dumps(request, ensure_ascii=False, indent=2))
    while True:
        try:
            raw = input("输入 answer JSON（空行结束本地运行）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        try:
            answer = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"JSON 格式有误：{exc.msg}")
            continue
        if not isinstance(answer, dict) or not isinstance(answer.get("action"), str):
            print("需要包含 action 字段的 JSON 对象。")
            continue
        return answer


async def run_graph(graph, input_state: dict, config: dict, interactive: bool = False) -> int:
    """在同一图和 thread 上处理真实 interrupt；不替人选择封装或审核。"""
    from langgraph.types import Command

    state = await graph.ainvoke(input_state, config=config)
    while state.get("__interrupt__"):
        answers = {}
        for interruption in state["__interrupt__"]:
            request = {"interrupt_id": interruption.id, "request": interruption.value}
            if not interactive:
                print(json.dumps(request, ensure_ascii=False, indent=2))
                print("BLOCKED：等待人工输入，退出码 2；本地 MemorySaver 随进程退出失效。")
                print("需要跨进程恢复或数据库核验，请使用 API verify_step_e2e。")
                return 2
            answer = prompt_answer(request)
            if answer is None:
                print("BLOCKED：使用者未回答，退出码 2；本地 MemorySaver 随进程退出失效。")
                return 2
            answers[interruption.id] = answer
        state = await graph.ainvoke(Command(resume=answers), config=config)
    result = {
        "status": state.get("status"),
        "result": state.get("result", {}),
        "output_dir": state.get("output_dir"),
        "artifact_paths": state.get("artifact_paths", {}),
        "errors": state.get("errors", []),
        "human_history": state.get("human_history", []),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if state.get("status") in {"completed", "reviewed"} else 1


async def async_main(image_path: Path, interactive: bool = False) -> int:
    """只将图片路径交给 Agent，按真实结果区分成功、失败和暂停。"""
    from backend.agents.step.graph import build_image_to_step_graph

    if not image_path.is_file():
        raise FileNotFoundError(f"图片不存在：{image_path}")
    return await run_graph(
        build_image_to_step_graph(),
        {"image_path": str(image_path.resolve())},
        config={
            "configurable": {"thread_id": f"step-image-{uuid.uuid4().hex[:10]}"},
            "recursion_limit": 256,
        },
        interactive=interactive,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析唯一必填图片参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_path", type=Path)
    parser.add_argument("--interactive", action="store_true", help="由终端使用者回答封装、路由和审核问题")
    return parser.parse_args(argv)


def main() -> int:
    """同步命令行入口。"""
    args = parse_args()
    try:
        return asyncio.run(async_main(args.image_path, interactive=args.interactive))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
