"""STEP 数模生成 Agent 的状态定义。

仅保留二维图纸 Golden Set 流程（``DrawingToStepState``）和
单张工程图图片证据链流程（``ImageToStepState``）两种工作流状态。
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class DrawingToStepState(TypedDict, total=False):
    """二维工程图到 STEP 的 Golden Set 工作流状态。"""

    golden_case_id: str
    drawing_path: str
    reference_image_path: str
    output_dir: str
    output_name: str

    golden_case: dict[str, Any]
    drawing_extraction: dict[str, Any]
    extraction_gate: dict[str, Any]
    feature_ir: dict[str, Any]

    artifact_paths: dict[str, str]
    preview_paths: dict[str, str]
    cadquery_version: str
    drawing_verification: dict[str, Any]
    golden_comparison: dict[str, Any]

    result: dict[str, Any]
    status: str
    errors: list[str]


class ImageToStepState(TypedDict, total=False):
    """单张工程图图片到 STEP 的证据链工作流状态。"""

    # 唯一必填输入
    image_path: str

    # 人工回答由 interrupt/resume 接收，和原始视觉证据分别持久化。
    package_type_hint: str
    human_package: dict[str, Any]
    human_route: dict[str, Any]
    human_review: dict[str, Any]
    human_history: list[dict[str, Any]]
    human_dimension_history: list[dict[str, Any]]
    human_dimension_rounds: int
    dimension_input_error: str | None
    needs_review: bool

    # 本地图片与视觉证据
    image_meta: dict[str, Any]
    preprocessing: dict[str, Any]
    all_ocr_tokens: list[dict[str, Any]]
    raw_line_segments: list[dict[str, Any]]
    all_line_segments: list[dict[str, Any]]
    all_arrows: list[dict[str, Any]]
    all_regions: list[dict[str, Any]]
    dimension_groups: list[dict[str, Any]]
    token_line_links: list[dict[str, Any]]
    evidence_index: dict[str, Any]
    visual_evidence: dict[str, Any]
    view_classification: dict[str, Any]
    jev_decision: dict[str, Any]
    pending_view_ids: list[str]
    current_view_id: str
    current_view_image_path: str
    current_prompt_evidence: dict[str, Any]
    current_view_result: dict[str, Any]
    per_view_semantics: list[dict[str, Any]]
    semantic_result: dict[str, Any]
    semantic_collisions: list[dict[str, Any]]
    semantic_review_attempts: int
    semantic_review_pending_regions: list[str]
    semantic_review_history: list[dict[str, Any]]
    fused_evidence: dict[str, Any]
    dimension_chain_result: dict[str, Any]
    dimension_gate: dict[str, Any]
    category_id: str
    subcategory_id: str | None
    family_id: str

    # Feature IR、CAD 和独立评测
    feature_ir: dict[str, Any]
    product_identifiers: list[str]
    manufacturer_names: list[str]
    reference_step_search: dict[str, Any]
    model_source: str
    artifact_paths: dict[str, str]
    verification: dict[str, Any]
    preview_paths: dict[str, str]
    golden_comparison: dict[str, Any]

    # 内部输出设置，不属于必填接口
    output_dir: str
    result: dict[str, Any]
    status: str
    errors: list[str]
