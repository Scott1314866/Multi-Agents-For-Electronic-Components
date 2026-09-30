"""PCB 封装生成 Agent 的 LangGraph 编排。

一张图，两种模式（由 ``state["mode"]`` 选择）：

* ``full``（默认）—— 生成装置 → 跑 Allegro 三阶段 → 判据 → 人工复核；
* ``emit_only`` —— 只生成 SKILL 与运行装置就收工，适合本机没有 Cadence 时
  先把脚本备好，或用于对拍回归。

人工停点两处：``confirm_spec``（写几何之前确认参数）与 ``review_result``
（判据出来后决定是否采纳）。指令性的坏参数不进人工流程，直接落
``stopped_invalid_spec`` —— 不该让人用"确认"把不合规的间距盖过去。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from backend.agents.pcb.human_nodes import confirm_spec_node, review_result_node
from backend.agents.pcb.nodes import (
    emit_only_done_node,
    emit_skills_node,
    failed_node,
    finalize_node,
    judge_node,
    load_spec_node,
    route_after_emit,
    route_after_human_input,
    route_after_judge,
    route_after_load,
    route_after_validate,
    route_continue_or_failed,
    run_build_node,
    run_props_node,
    run_verify_node,
    stopped_invalid_spec_node,
    stopped_no_toolchain_node,
    validate_spec_node,
)
from backend.agents.pcb.state import PcbPackageState
from backend.core.memory import get_memory_saver


def build_pcb_graph(checkpointer=None):
    """构建并编译 PCB 封装生成的状态图。

    执行链路：

        load_spec → validate_spec → confirm_spec [interrupt]
        → emit_skills → run_build → run_verify → run_props
        → judge → review_result [interrupt] → END

    终点分流：``stopped_invalid_spec`` / ``stopped_no_toolchain`` 是预期内的
    安全终点（参数不合规、本机无 Cadence），``failed`` 才是技术失败。

    Args:
        checkpointer: 外部注入的 checkpointer（生产用 PostgreSQL saver）；
            未传入时退回进程内 MemorySaver，供本地开发与测试。

    Returns:
        已编译的图。
    """
    builder = StateGraph(PcbPackageState)

    # ── 注册节点 ─────────────────────────────────────────────────
    builder.add_node("load_spec", load_spec_node)
    builder.add_node("validate_spec", validate_spec_node)
    builder.add_node("confirm_spec", confirm_spec_node)
    builder.add_node("emit_skills", emit_skills_node)
    builder.add_node("run_build", run_build_node)
    builder.add_node("run_verify", run_verify_node)
    builder.add_node("run_props", run_props_node)
    builder.add_node("judge", judge_node)
    builder.add_node("review_result", review_result_node)
    builder.add_node("emit_only_done", emit_only_done_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("stopped_invalid_spec", stopped_invalid_spec_node)
    builder.add_node("stopped_no_toolchain", stopped_no_toolchain_node)
    builder.add_node("failed", failed_node)

    # ── 边 ───────────────────────────────────────────────────────
    builder.add_edge(START, "load_spec")
    builder.add_conditional_edges(
        "load_spec",
        route_after_load,
        {"continue": "validate_spec", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "validate_spec",
        route_after_validate,
        {
            "continue": "confirm_spec",
            "invalid": "stopped_invalid_spec",
            "failed": "failed",
        },
    )
    builder.add_conditional_edges(
        "confirm_spec",
        route_after_human_input,
        {"continue": "emit_skills", "cancelled": END},
    )
    builder.add_conditional_edges(
        "emit_skills",
        route_after_emit,
        {"build": "run_build", "emit_only": "emit_only_done", "failed": "failed"},
    )

    stage_routes = {"continue": "run_verify", "stopped": "stopped_no_toolchain", "failed": "failed"}
    builder.add_conditional_edges("run_build", route_continue_or_failed, stage_routes)
    builder.add_conditional_edges(
        "run_verify",
        route_continue_or_failed,
        {"continue": "run_props", "stopped": "stopped_no_toolchain", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "run_props",
        route_continue_or_failed,
        {"continue": "judge", "stopped": "stopped_no_toolchain", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "judge",
        route_after_judge,
        {"continue": "review_result", "failed": "failed"},
    )

    builder.add_edge("review_result", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("emit_only_done", END)
    builder.add_edge("stopped_invalid_spec", END)
    builder.add_edge("stopped_no_toolchain", END)
    builder.add_edge("failed", END)

    return builder.compile(
        checkpointer=(
            checkpointer if checkpointer is not None else get_memory_saver("pcb")
        )
    )


if __name__ == "__main__":
    graph = build_pcb_graph()
    print("图编译成功")
    print(list(graph.nodes.keys()))
