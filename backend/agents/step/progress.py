"""Public, task-scoped progress from actual LangGraph task events."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Awaitable, Callable
from uuid import uuid4

from backend.core.logger import get_logger

ProgressSink = Callable[[dict[str, Any]], Awaitable[None]]
_sink: ContextVar[ProgressSink | None] = ContextVar("step_progress_sink", default=None)
logger = get_logger(__name__)

# Only these public labels and aggregate counts leave the worker. Graph inputs,
# results, exceptions, local paths and model credentials are never forwarded.
NODE_LABELS = {
    "ask_package": "确认封装信息", "confirm_template": "确认封装模板",
    "ask_missing_dimensions": "补充图纸尺寸", "review_result": "审核生成模型",
    "load_image": "读取图纸", "preprocess_image": "预处理图纸",
    "extract_all_evidence": "识别图纸文字、尺寸与线条",
    "store_evidence_locally": "保存图纸识别结果", "detect_views": "识别图纸视图与器件类型",
    "jev_route_template": "选择建模模板", "build_dimension_groups": "整理尺寸分组",
    "retrieve_view_evidence": "检索视图尺寸证据", "qwen_analyze_one_view": "分析视图与尺寸",
    "save_view_result": "保存视图分析", "merge_view_results": "合并各视图结果",
    "fuse_evidence": "汇总尺寸证据", "prepare_semantic_review": "准备尺寸复核",
    "review_semantics": "复核尺寸含义", "validate_dimension_chain": "检查尺寸链",
    "validate_dimensions": "核对建模尺寸", "classify_family": "匹配器件类型",
    "create_feature_ir": "生成建模特征", "search_reference_step": "检索可用 STEP 参考模型",
    "prepare_exact_reference_step": "准备匹配的参考模型",
    "validate_reference_step_candidates": "校验参考模型候选", "prepare_reference_step": "准备参考模型",
    "build_step": "生成 STEP 模型", "verify_step": "校验模型几何与尺寸",
    "render_views": "渲染模型预览", "compare_golden_reference": "比对参考模型",
    "finalize_result": "整理生成结果", "failed": "结束未成功的任务",
    "stopped_insufficient_extraction": "检查缺失的尺寸证据",
    "needs_human_follow_up": "整理需要补充的信息",
    "stopped_unsupported_template": "检查模板支持情况",
    "extract_drawing": "识别工程图", "validate_drawing_extraction": "检查工程图识别结果",
    "build_drawing_step": "生成 STEP 模型", "verify_drawing_step": "校验模型几何与尺寸",
    "render_drawing_views": "渲染模型预览", "finalize_drawing_result": "整理生成结果",
    "stop_insufficient_extraction": "检查缺失的尺寸证据",
}


@contextmanager
def progress_scope(sink: ProgressSink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


async def emit_progress(message: str, *, phase: str = "info", node: str = "", **extra) -> None:
    sink = _sink.get()
    if sink is None:
        return
    event = {
        "id": str(uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase, "node": node, "message": message, **extra,
    }
    try:
        await sink(event)
    except Exception:
        # Observability must not turn a successful CAD operation into a failure.
        logger.warning("step.progress_write_failed", node=node, exc_info=True)


def _counts(node: str, result: dict) -> str:
    if node == "extract_all_evidence":
        return f" · {len(result.get('all_ocr_tokens') or [])} 条文字，{len(result.get('all_line_segments') or [])} 条线段"
    return ""


async def invoke_with_progress(graph: Any, input_state: Any, config: dict) -> Any:
    if _sink.get() is None:
        return await graph.ainvoke(input_state, config=config)
    started: dict[str, tuple[str, float]] = {}
    output = None
    try:
        async for kind, payload in graph.astream(input_state, config=config, stream_mode=["tasks", "values"]):
            if kind == "values":
                output = payload
                continue
            node, task_id = payload.get("name", ""), payload["id"]
            label = NODE_LABELS.get(node, "处理图纸")
            if "triggers" in payload:
                started[task_id] = (node, monotonic())
                await emit_progress(f"开始：{label}", phase="started", node=node)
                continue
            _, began = started.pop(task_id, (node, monotonic()))
            duration = round(max(0, monotonic() - began), 1)
            result = payload.get("result") or {}
            if payload.get("interrupts"):
                phase, message = "waiting", f"等待你确认：{label}"
            elif payload.get("error") or result.get("status") == "failed" or node == "failed":
                phase, message = "error", f"未完成：{label}，可查看下方提示后重试"
            elif str(result.get("status", "")).startswith("stopped") or node.startswith("stopped_"):
                phase, message = "warning", f"已暂停：{label}，请查看需要补充的信息"
            else:
                phase, message = "completed", f"完成：{label}{_counts(node, result)}"
            await emit_progress(message, phase=phase, node=node, duration_seconds=duration)
    except Exception:
        # The stream may raise before delivering the final task-result event.
        for node, began in started.values():
            await emit_progress(
                f"未完成：{NODE_LABELS.get(node, '处理图纸')}，可查看下方提示后重试",
                phase="error", node=node, duration_seconds=round(max(0, monotonic() - began), 1),
            )
        raise
    return output
