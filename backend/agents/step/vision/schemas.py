"""工程图视觉证据、语义映射和门禁结果的数据结构。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ViewRegion(BaseModel):
    """由本地图像算法切分的一块候选视图区域。"""

    region_id: str
    bbox: tuple[int, int, int, int]
    area_ratio: float = Field(ge=0.0, le=1.0)


class OCRToken(BaseModel):
    """PaddleOCR 输出的一条不可变文字证据。"""

    token_id: str
    text: str
    bbox: tuple[int, int, int, int]
    polygon: list[tuple[float, float]] = Field(default_factory=list)
    rotation_deg: int = 0
    confidence: float = Field(ge=0.0, le=1.0)
    source_region_id: str = "unassigned"
    unit_context: Literal["unknown", "mm", "deg", "count"] = "unknown"
    value_role: Literal[
        "unknown", "table_nominal", "table_minimum", "table_maximum"
    ] = "unknown"


class ArrowEvidence(BaseModel):
    """尺寸箭头候选证据。"""

    arrow_id: str
    bbox: tuple[int, int, int, int]
    center: tuple[float, float]
    confidence: float = Field(ge=0.0, le=1.0)


class GeometryLine(BaseModel):
    """OpenCV 检出的线段及其工程语义候选。"""

    line_id: str
    type: Literal[
        "dimension_line", "extension_line", "center_line", "outline", "line_segment"
    ]
    start: tuple[int, int]
    end: tuple[int, int]
    arrow_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    source_region_id: str = "unassigned"


class TokenLineLink(BaseModel):
    """OCR token 与附近尺寸线的本地几何关联。"""

    token_id: str
    line_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class DimensionGroup(BaseModel):
    """由确定性程序归并的尺寸文字、尺寸线、尺寸界线和箭头证据。"""

    dimension_id: str
    view_id: str = "unassigned"
    evidence_type: Literal["dimension_line", "table_row", "identity_text"] = "dimension_line"
    bbox: tuple[int, int, int, int]
    token_ids: list[str] = Field(min_length=1)
    row_label_token_ids: list[str] = Field(default_factory=list)
    context_token_ids: list[str] = Field(default_factory=list)
    table_columns: dict[str, str] = Field(default_factory=dict)
    dimension_line_ids: list[str] = Field(default_factory=list)
    extension_line_ids: list[str] = Field(default_factory=list)
    arrow_ids: list[str] = Field(default_factory=list)
    orientation: Literal["horizontal", "vertical", "oblique", "unknown"] = "unknown"
    confidence: float = Field(ge=0.0, le=1.0)


class VisualEvidenceBundle(BaseModel):
    """Qwen 语义节点可以读取的完整本地视觉证据。"""

    image_sha256: str
    image_size: tuple[int, int]
    regions: list[ViewRegion] = Field(default_factory=list)
    ocr_tokens: list[OCRToken] = Field(default_factory=list)
    lines: list[GeometryLine] = Field(default_factory=list)
    arrows: list[ArrowEvidence] = Field(default_factory=list)
    token_line_links: list[TokenLineLink] = Field(default_factory=list)


class SemanticAssignment(BaseModel):
    """Qwen 对一条尺寸证据的语义判断；禁止携带尺寸数值。"""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    token_ids: list[str] = Field(min_length=1)
    line_ids: list[str] = Field(default_factory=list)
    target_feature: str
    confidence: float = Field(ge=0.0, le=1.0)


class QwenViewClassificationResult(BaseModel):
    """Qwen 第一阶段对候选区域和器件族的轻量判断。"""

    model_config = ConfigDict(extra="forbid")

    family_id: str
    category_id: str = ""
    subcategory_id: str | None = None
    package_type: str
    identified_views: dict[str, str] = Field(default_factory=dict)
    identified_features: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


class QwenViewSemanticResult(BaseModel):
    """Qwen 第二阶段针对单一视图返回的尺寸语义。"""

    model_config = ConfigDict(extra="forbid")

    region_id: str
    view_type: str
    identified_features: list[str] = Field(default_factory=list)
    assignments: list[SemanticAssignment] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class QwenSemanticResult(BaseModel):
    """Qwen 只负责器件族、视图、特征与尺寸语义。"""

    model_config = ConfigDict(extra="forbid")

    family_id: str
    category_id: str = ""
    subcategory_id: str | None = None
    package_type: str
    identified_views: dict[str, str] = Field(default_factory=dict)
    identified_features: list[str] = Field(default_factory=list)
    assignments: list[SemanticAssignment] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


class ParsedDimension(BaseModel):
    """由本地解析器从 OCR 文本中恢复的尺寸表达式。"""

    raw_text: str
    nominal_value: float | None = None
    minimum_value: float | None = None
    maximum_value: float | None = None
    tolerance_plus: float | None = None
    tolerance_minus: float | None = None
    unit: str = "mm"
    quantity: int | None = None
    symbol: str = ""
    is_reference: bool = False


class FusedParameter(BaseModel):
    """同时具有数值、单位、bbox 和证据 ID 的规范参数。"""

    canonical_name: str
    value: float
    unit: str
    evidence_ids: list[str] = Field(min_length=1)
    token_ids: list[str] = Field(min_length=1)
    line_ids: list[str] = Field(default_factory=list)
    token_bboxes: list[tuple[int, int, int, int]] = Field(min_length=1)
    target_feature: str
    ocr_confidence: float = Field(ge=0.0, le=1.0)
    semantic_confidence: float = Field(ge=0.0, le=1.0)
    raw_texts: list[str] = Field(default_factory=list)
    evidence_kind: Literal[
        "dimension_line", "explicit_range", "table_row", "identity_text", "derived"
    ] = "dimension_line"


class FusedEvidence(BaseModel):
    """本地数值解析与 Qwen 语义映射融合后的结果。"""

    family_id: str
    category_id: str = ""
    subcategory_id: str | None = None
    package_type: str
    identified_views: dict[str, str] = Field(default_factory=dict)
    identified_features: list[str] = Field(default_factory=list)
    parameters: list[FusedParameter] = Field(default_factory=list)
    missing_evidence_assignments: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)
    unit_conflicts: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)


class DimensionGateResult(BaseModel):
    """Feature IR 之前的强制证据门禁结果。"""

    passed: bool
    status: Literal[
        "dimensions_valid",
        "stopped_insufficient_extraction",
        "needs_human_follow_up",
        "stopped_unsupported_template",
    ]
    missing_fields: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)
    low_confidence_fields: list[str] = Field(default_factory=list)
    evidence_report: str
