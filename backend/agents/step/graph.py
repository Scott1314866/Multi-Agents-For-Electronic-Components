"""STEP 数模生成助手的 LangGraph 工作流定义。"""

from langgraph.graph import END, START, StateGraph

from backend.agents.step.human_nodes import (
    ask_package_node,
    confirm_template_node,
    review_result_node,
    route_after_human_input,
)

from backend.agents.step.drawing_nodes import (
    build_drawing_step_node,
    compare_golden_reference_node,
    create_feature_ir_node,
    extract_drawing_node,
    failed_node,
    finalize_drawing_result_node,
    render_drawing_views_node,
    route_after_drawing_extraction,
    route_after_drawing_step,
    route_after_extraction_gate,
    stop_insufficient_extraction_node,
    validate_drawing_extraction_node,
    verify_drawing_step_node,
)
from backend.agents.step.image_nodes import (
    build_step_node as image_build_step_node,
    classify_family_node as image_classify_family_node,
    compare_golden_reference_node as image_compare_golden_reference_node,
    create_feature_ir_node as image_create_feature_ir_node,
    build_dimension_groups_node,
    detect_views_node,
    extract_all_evidence_node,
    failed_node as image_failed_node,
    finalize_result_node as image_finalize_result_node,
    fuse_evidence_node,
    jev_route_template_node,
    load_image_node,
    merge_view_results_node,
    preprocess_image_node,
    qwen_analyze_one_view_node,
    prepare_exact_reference_step_node,
    prepare_reference_step_node,
    retrieve_view_evidence_node,
    render_views_node,
    route_after_dimension_gate,
    route_after_early_reference_search,
    route_after_exact_reference_prepare,
    route_after_reference_prepare,
    route_after_reference_search,
    route_continue_or_failed,
    route_after_view_result,
    save_view_result_node,
    search_reference_step_node,
    store_evidence_locally_node,
    stop_insufficient_extraction_node as image_stop_insufficient_extraction_node,
    validate_dimensions_node,
    validate_dimension_chain_node,
    validate_reference_step_candidates_node,
    verify_step_node as image_verify_step_node,
)
from backend.agents.step.state import (
    DrawingToStepState,
    ImageToStepState,
)
from backend.core.memory import get_memory_saver
from backend.agents.step.semantic_review_nodes import (
    prepare_semantic_review_node,
    review_semantics_node,
    route_after_semantic_review,
)


def build_drawing_to_step_graph(checkpointer=None):
    """构建二维工程图 → Feature IR → STEP 的 Golden Set 工作流。

    尺寸、特征或证据不足时，工作流在 ``stop_insufficient_extraction`` 安全结束，
    不创建占位模型；通过门禁后才允许执行白名单 Feature IR。

    Returns:
        已绑定 Checkpointer 的二维图纸工作流。未传入时使用进程内 MemorySaver
        （供本地开发和测试）；生产调用应注入 PostgreSQL checkpointer。
    """
    builder = StateGraph(DrawingToStepState)
    builder.add_node("extract_drawing", extract_drawing_node)
    builder.add_node("validate_drawing_extraction", validate_drawing_extraction_node)
    builder.add_node("create_feature_ir", create_feature_ir_node)
    builder.add_node("build_drawing_step", build_drawing_step_node)
    builder.add_node("verify_drawing_step", verify_drawing_step_node)
    builder.add_node("compare_golden_reference", compare_golden_reference_node)
    builder.add_node("render_drawing_views", render_drawing_views_node)
    builder.add_node("stop_insufficient_extraction", stop_insufficient_extraction_node)
    builder.add_node("finalize_drawing_result", finalize_drawing_result_node)
    builder.add_node("failed", failed_node)

    builder.add_edge(START, "extract_drawing")
    builder.add_conditional_edges(
        "extract_drawing",
        route_after_drawing_extraction,
        {"validate": "validate_drawing_extraction", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "validate_drawing_extraction",
        route_after_extraction_gate,
        {"continue": "create_feature_ir", "stop": "stop_insufficient_extraction"},
    )
    builder.add_conditional_edges(
        "create_feature_ir",
        route_after_drawing_step,
        {"continue": "build_drawing_step", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "build_drawing_step",
        route_after_drawing_step,
        {"continue": "verify_drawing_step", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "verify_drawing_step",
        route_after_drawing_step,
        {"continue": "compare_golden_reference", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "compare_golden_reference",
        route_after_drawing_step,
        {"continue": "render_drawing_views", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "render_drawing_views",
        route_after_drawing_step,
        {"continue": "finalize_drawing_result", "failed": "failed"},
    )
    builder.add_edge("stop_insufficient_extraction", END)
    builder.add_edge("finalize_drawing_result", END)
    builder.add_edge("failed", END)
    return builder.compile(
        checkpointer=(
            checkpointer
            if checkpointer is not None
            else get_memory_saver("step_drawing")
        )
    )


def build_image_to_step_graph(checkpointer=None):
    """构建严格证据链的单张工程图图片到 STEP 工作流。

    对外只要求 ``image_path``，开始时通过 interrupt 询问封装。
    Golden Reference 节点严格位于候选 STEP
    回读和四视图渲染之后，且没有回边，不能污染 OCR、语义或 Feature IR。

    Returns:
        已绑定 Checkpointer 的图片工作流。未传入时使用进程内 MemorySaver
        （供本地开发和测试）；生产调用应注入 PostgreSQL checkpointer。
    """
    builder = StateGraph(ImageToStepState)
    builder.add_node("ask_package", ask_package_node)
    builder.add_node("confirm_template", confirm_template_node)
    builder.add_node("review_result", review_result_node)
    builder.add_node("load_image", load_image_node)
    builder.add_node("preprocess_image", preprocess_image_node)
    builder.add_node("extract_all_evidence", extract_all_evidence_node)
    builder.add_node("store_evidence_locally", store_evidence_locally_node)
    builder.add_node("detect_views", detect_views_node)
    builder.add_node("jev_route_template", jev_route_template_node)
    builder.add_node("build_dimension_groups", build_dimension_groups_node)
    builder.add_node("retrieve_view_evidence", retrieve_view_evidence_node)
    builder.add_node("qwen_analyze_one_view", qwen_analyze_one_view_node)
    builder.add_node("save_view_result", save_view_result_node)
    builder.add_node("merge_view_results", merge_view_results_node)
    builder.add_node("fuse_evidence", fuse_evidence_node)
    builder.add_node("review_semantics", review_semantics_node)
    builder.add_node("prepare_semantic_review", prepare_semantic_review_node)
    builder.add_node("validate_dimension_chain", validate_dimension_chain_node)
    builder.add_node("validate_dimensions", validate_dimensions_node)
    builder.add_node("classify_family", image_classify_family_node)
    builder.add_node("create_feature_ir", image_create_feature_ir_node)
    builder.add_node("search_reference_step", search_reference_step_node)
    builder.add_node("prepare_exact_reference_step", prepare_exact_reference_step_node)
    builder.add_node(
        "validate_reference_step_candidates",
        validate_reference_step_candidates_node,
    )
    builder.add_node("prepare_reference_step", prepare_reference_step_node)
    builder.add_node("build_step", image_build_step_node)
    builder.add_node("verify_step", image_verify_step_node)
    builder.add_node("render_views", render_views_node)
    builder.add_node("compare_golden_reference", image_compare_golden_reference_node)
    builder.add_node("finalize_result", image_finalize_result_node)
    builder.add_node(
        "stopped_insufficient_extraction", image_stop_insufficient_extraction_node
    )
    builder.add_node(
        "needs_human_follow_up", image_stop_insufficient_extraction_node
    )
    builder.add_node(
        "stopped_unsupported_template", image_stop_insufficient_extraction_node
    )
    builder.add_node("failed", image_failed_node)

    builder.add_edge(START, "ask_package")
    builder.add_conditional_edges(
        "ask_package", route_after_human_input,
        {"continue": "load_image", "cancelled": END},
    )
    builder.add_conditional_edges(
        "confirm_template", route_after_human_input,
        {"continue": "jev_route_template", "cancelled": END},
    )
    ordered_nodes = (
        ("load_image", "preprocess_image"),
        ("preprocess_image", "extract_all_evidence"),
        ("extract_all_evidence", "store_evidence_locally"),
        ("store_evidence_locally", "detect_views"),
        ("detect_views", "confirm_template"),
        ("build_dimension_groups", "retrieve_view_evidence"),
        ("retrieve_view_evidence", "qwen_analyze_one_view"),
        ("qwen_analyze_one_view", "save_view_result"),
    )
    for current, following in ordered_nodes:
        builder.add_conditional_edges(
            current,
            route_continue_or_failed,
            {"continue": following, "failed": "failed"},
        )
    builder.add_conditional_edges(
        "jev_route_template",
        route_continue_or_failed,
        {"continue": "search_reference_step", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "search_reference_step",
        route_after_early_reference_search,
        {
            "exact": "prepare_exact_reference_step",
            "semantic": "build_dimension_groups",
            "failed": "failed",
        },
    )
    builder.add_conditional_edges(
        "prepare_exact_reference_step",
        route_after_exact_reference_prepare,
        {
            "render": "render_views",
            "semantic": "build_dimension_groups",
            "failed": "failed",
        },
    )
    builder.add_conditional_edges(
        "save_view_result",
        route_after_view_result,
        {
            "next": "retrieve_view_evidence",
            "merge": "merge_view_results",
            "failed": "failed",
        },
    )
    pre_gate_nodes = (
        ("merge_view_results", "fuse_evidence"),
        ("fuse_evidence", "validate_dimension_chain"),
        ("validate_dimension_chain", "validate_dimensions"),
    )
    for current, following in pre_gate_nodes:
        builder.add_conditional_edges(
            current,
            route_continue_or_failed,
            {"continue": following, "failed": "failed"},
        )
    builder.add_conditional_edges(
        "validate_dimensions",
        route_after_dimension_gate,
        {
            "continue": "classify_family",
            "stop": "stopped_insufficient_extraction",
            "follow_up": "needs_human_follow_up",
            "unsupported": "stopped_unsupported_template",
            "review": "prepare_semantic_review",
        },
    )
    builder.add_edge("prepare_semantic_review", "review_semantics")
    builder.add_conditional_edges(
        "review_semantics", route_after_semantic_review,
        {"next": "review_semantics", "merge": "merge_view_results"},
    )
    post_gate_nodes = (
        ("classify_family", "create_feature_ir"),
        ("create_feature_ir", "validate_reference_step_candidates"),
        ("build_step", "verify_step"),
        ("verify_step", "render_views"),
        ("render_views", "compare_golden_reference"),
        ("compare_golden_reference", "finalize_result"),
    )
    for current, following in post_gate_nodes:
        builder.add_conditional_edges(
            current,
            route_continue_or_failed,
            {"continue": following, "failed": "failed"},
        )
    builder.add_conditional_edges(
        "validate_reference_step_candidates",
        route_after_reference_search,
        {
            "reference": "prepare_reference_step",
            "local": "build_step",
            "failed": "failed",
        },
    )
    builder.add_conditional_edges(
        "prepare_reference_step",
        route_after_reference_prepare,
        {
            "verify": "verify_step",
            "local": "build_step",
            "failed": "failed",
        },
    )
    builder.add_edge("stopped_insufficient_extraction", END)
    builder.add_edge("needs_human_follow_up", END)
    builder.add_edge("stopped_unsupported_template", END)
    builder.add_conditional_edges(
        "finalize_result", route_continue_or_failed,
        {"continue": "review_result", "failed": "failed"},
    )
    builder.add_edge("review_result", END)
    builder.add_edge("failed", END)

    return builder.compile(
        checkpointer=(
            checkpointer
            if checkpointer is not None
            else get_memory_saver("step_image")
        )
    )



def select_graph_mode(mode: str, checkpointer=None):
    """按输入模式返回 STEP Agent 工作流。

    Args:
        mode: ``drawing`` 使用二维图纸 Golden Set 流程；
              ``image`` 使用严格证据链图片流程。

    Returns:
        对应模式的 LangGraph 可执行对象。
    """
    if mode == "drawing":
        return build_drawing_to_step_graph(checkpointer=checkpointer)
    if mode == "image":
        return build_image_to_step_graph(checkpointer=checkpointer)
    raise ValueError(f"不支持的 STEP Agent 模式：{mode}")


if __name__ == '__main__':
    graph = select_graph_mode("image")
    print('图编译成功')
    print('节点列表：', list(graph.nodes.keys()))
