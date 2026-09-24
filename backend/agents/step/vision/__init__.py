"""工程图图片的本地 OCR、几何检测和证据融合模块。"""

from backend.agents.step.vision.evidence_fusion import (
    fuse_evidence,
    validate_fused_dimensions,
)
from backend.agents.step.vision.dimension_grouping import (
    assign_evidence_to_views,
    build_table_dimension_groups,
    build_dimension_groups,
    detect_dimension_table_regions,
    normalize_line_segments,
)
from backend.agents.step.vision.line_detector import detect_geometry, link_tokens_to_lines
from backend.agents.step.vision.ocr import (
    extract_ocr_tokens,
    infer_document_unit_context,
    parse_dimension_expression,
    parse_explicit_count_expression,
    parse_table_nominal_expression,
)
from backend.agents.step.vision.preprocess import (
    image_as_data_url,
    preprocess_image,
    read_image,
)
from backend.agents.step.vision.semantic_retrieval import (
    build_region_summaries,
    merge_view_semantics,
    retrieve_dimension_group_evidence,
    retrieve_view_evidence,
)
from backend.agents.step.vision.schemas import (
    FusedEvidence,
    DimensionGroup,
    QwenSemanticResult,
    QwenViewClassificationResult,
    QwenViewSemanticResult,
    VisualEvidenceBundle,
)
from backend.agents.step.vision.view_splitter import (
    ensure_region_coverage,
    render_region_crop,
    render_region_overview,
    split_view_regions,
)

__all__ = [
    "FusedEvidence",
    "DimensionGroup",
    "QwenSemanticResult",
    "QwenViewClassificationResult",
    "QwenViewSemanticResult",
    "VisualEvidenceBundle",
    "build_region_summaries",
    "assign_evidence_to_views",
    "build_dimension_groups",
    "build_table_dimension_groups",
    "detect_dimension_table_regions",
    "detect_geometry",
    "extract_ocr_tokens",
    "infer_document_unit_context",
    "fuse_evidence",
    "parse_dimension_expression",
    "parse_explicit_count_expression",
    "parse_table_nominal_expression",
    "image_as_data_url",
    "preprocess_image",
    "read_image",
    "retrieve_view_evidence",
    "retrieve_dimension_group_evidence",
    "merge_view_semantics",
    "normalize_line_segments",
    "link_tokens_to_lines",
    "split_view_regions",
    "ensure_region_coverage",
    "render_region_overview",
    "render_region_crop",
    "validate_fused_dimensions",
]
