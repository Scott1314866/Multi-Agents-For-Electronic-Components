"""符号生成 Agent 的 LangGraph 编排。

一条主链，六个人工停点：

    validate_input → locate_pages → parse_document → pick_table → discover_names
      → ask_device ─┐
      → ask_package ─┤ 人工
      → render_pages → vision_extract → merge_channels → review_pins
      → resolve_conflicts ─┐
      → resolve_review_diffs ─┤ 人工
      → self_check ⇄ ask_check_questions ─┤ 人工（最多 3 轮）
      → build_layout → confirm_output ─┤ 人工
      → generate_capture → finalize

终点分四类，**都不冒充成功**：

* ``failed`` —— 技术失败；
* ``stopped_no_pin_table`` —— 找不到可解析的引脚表，**不产出占位符号**；
* ``stopped_offline`` —— MinerU 离线且无缓存；
* ``stopped_no_toolchain`` —— 没有 ``tclsh.exe``，TCL 已生成、等有环境的机器。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from backend.agents.symbol.human_nodes import (
    ask_check_questions_node,
    ask_device_node,
    ask_package_node,
    confirm_output_node,
    resolve_conflicts_node,
    resolve_review_diffs_node,
)
from backend.agents.symbol.nodes import (
    build_layout_node,
    discover_names_node,
    failed_node,
    finalize_node,
    generate_capture_node,
    locate_pages_node,
    merge_channels_node,
    parse_document_node,
    pick_table_node,
    render_pages_node,
    review_pins_node,
    route_after_capture,
    route_after_check_answer,
    route_after_conflicts,
    route_after_human_input,
    route_after_layout,
    route_after_self_check,
    route_continue_or_failed,
    self_check_node,
    stopped_no_pin_table_node,
    stopped_no_toolchain_node,
    stopped_offline_node,
    validate_input_node,
    vision_extract_node,
)
from backend.agents.symbol.state import SymbolDrawingState
from backend.core.memory import get_memory_saver


def build_symbol_graph(checkpointer=None):
    """构建并编译符号生成的状态图。

    Args:
        checkpointer: 外部注入（生产用 PostgreSQL saver）；留空则用进程内
            MemorySaver，供本地开发与测试。

    Returns:
        已编译的图。
    """
    builder = StateGraph(SymbolDrawingState)

    # ── 节点 ─────────────────────────────────────────────────────
    builder.add_node("validate_input", validate_input_node)
    builder.add_node("locate_pages", locate_pages_node)
    builder.add_node("parse_document", parse_document_node)
    builder.add_node("pick_table", pick_table_node)
    builder.add_node("discover_names", discover_names_node)
    builder.add_node("ask_device", ask_device_node)
    builder.add_node("ask_package", ask_package_node)
    builder.add_node("render_pages", render_pages_node)
    builder.add_node("vision_extract", vision_extract_node)
    builder.add_node("merge_channels", merge_channels_node)
    builder.add_node("review_pins", review_pins_node)
    builder.add_node("resolve_conflicts", resolve_conflicts_node)
    builder.add_node("resolve_review_diffs", resolve_review_diffs_node)
    builder.add_node("self_check", self_check_node)
    builder.add_node("ask_check_questions", ask_check_questions_node)
    builder.add_node("build_layout", build_layout_node)
    builder.add_node("confirm_output", confirm_output_node)
    builder.add_node("generate_capture", generate_capture_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("stopped_no_pin_table", stopped_no_pin_table_node)
    builder.add_node("stopped_offline", stopped_offline_node)
    builder.add_node("stopped_no_toolchain", stopped_no_toolchain_node)
    builder.add_node("failed", failed_node)

    # ── 边 ───────────────────────────────────────────────────────
    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges(
        "validate_input",
        route_continue_or_failed,
        {"continue": "locate_pages", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "locate_pages",
        route_continue_or_failed,
        {"continue": "parse_document", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "parse_document",
        route_continue_or_failed,
        {
            "continue": "pick_table",
            "stopped_offline": "stopped_offline",
            "failed": "failed",
        },
    )
    builder.add_conditional_edges(
        "pick_table",
        route_continue_or_failed,
        {
            "continue": "discover_names",
            "stopped_no_pin_table": "stopped_no_pin_table",
            "failed": "failed",
        },
    )
    builder.add_conditional_edges(
        "discover_names",
        route_continue_or_failed,
        {"continue": "ask_device", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "ask_device",
        route_after_human_input,
        {"continue": "ask_package", "cancelled": END},
    )
    builder.add_conditional_edges(
        "ask_package",
        route_after_human_input,
        {"continue": "render_pages", "cancelled": END},
    )

    for current, following in (
        ("render_pages", "vision_extract"),
        ("vision_extract", "merge_channels"),
        ("merge_channels", "review_pins"),
    ):
        builder.add_conditional_edges(
            current, route_continue_or_failed, {"continue": following, "failed": "failed"}
        )

    builder.add_conditional_edges(
        "review_pins",
        route_after_human_input,
        {"continue": "resolve_conflicts", "cancelled": END},
    )
    builder.add_conditional_edges(
        "resolve_conflicts",
        route_after_conflicts,
        {"continue": "resolve_review_diffs", "cancelled": END, "failed": "failed"},
    )
    builder.add_conditional_edges(
        "resolve_review_diffs",
        route_after_conflicts,
        {"continue": "self_check", "cancelled": END, "failed": "failed"},
    )
    builder.add_conditional_edges(
        "self_check",
        route_after_self_check,
        {"continue": "build_layout", "ask": "ask_check_questions", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "ask_check_questions",
        route_after_check_answer,
        {"recheck": "self_check", "cancelled": END},
    )
    builder.add_conditional_edges(
        "build_layout",
        route_after_layout,
        {"continue": "confirm_output", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "confirm_output",
        route_after_human_input,
        {"continue": "generate_capture", "cancelled": END},
    )
    builder.add_conditional_edges(
        "generate_capture",
        route_after_capture,
        {
            "continue": "finalize",
            "stopped": "stopped_no_toolchain",
            "failed": "failed",
        },
    )

    builder.add_edge("finalize", END)
    builder.add_edge("stopped_no_pin_table", END)
    builder.add_edge("stopped_offline", END)
    builder.add_edge("stopped_no_toolchain", END)
    builder.add_edge("failed", END)

    return builder.compile(
        checkpointer=(
            checkpointer if checkpointer is not None else get_memory_saver("symbol")
        )
    )


if __name__ == "__main__":
    graph = build_symbol_graph()
    print("图编译成功")
    print(list(graph.nodes.keys()))
