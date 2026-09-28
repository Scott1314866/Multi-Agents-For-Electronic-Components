"""工程图图片 → STEP 证据链的单元与内核回读测试。"""

from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

import cadquery as cq
import cv2
import numpy as np
import pytest

from backend.agents.step.families.connector.cn.dsub_connector import (
    REQUIRED_FEATURES,
    REQUIRED_PARAMETERS,
    plan_from_evidence,
)
from backend.agents.step.families.ic import gullwing_ic, qfn_ufqfpn, quad_gullwing_ic
from backend.agents.step.families.resistor import two_terminal_chip
from backend.agents.step.features import molded_body
from backend.agents.step.families.registry import (
    create_evidence_feature_ir,
    image_family_catalog,
    reconcile_image_family_classification,
    derive_family_parameters,
    template_category_catalog,
)
from backend.agents.step.families.taxonomy import (
    CAPACITOR_CHIP_FAMILY_ID,
    GULLWING_IC_FAMILY_ID,
    QFN_UFQFPN_FAMILY_ID,
    RESISTOR_CHIP_FAMILY_ID,
    component_category_catalog,
    resolve_family_selection,
)

from backend.agents.step.graph import build_image_to_step_graph
from backend.agents.step import image_nodes
from backend.agents.step.ir.executor import build_feature_model
from backend.agents.step.ir.primitives import CadQueryPrimitives
from backend.agents.step.cad_utils import prepare_reference_step, read_step_metrics
from backend.agents.step.reference_search import (
    classify_result_match,
    extract_manufacturer_names,
    extract_product_identifiers,
)
from backend.agents.step.jev_router import (
    build_jev_state,
    evaluate_jev_gate,
    resolve_geometric_family,
)
from backend.agents.step.vision.evidence_fusion import (
    fuse_evidence,
    validate_fused_dimensions,
)
from backend.agents.step.vision.dimension_grouping import (
    build_dimension_groups,
    build_table_dimension_groups,
    detect_dimension_table_regions,
)
from backend.agents.step.vision.ocr import (
    extract_ocr_tokens,
    parse_dimension_expression,
    parse_explicit_count_expression,
    parse_table_nominal_expression,
)
from backend.agents.step.vision.semantic_retrieval import (
    build_region_summaries,
    merge_view_semantics,
    retrieve_dimension_group_evidence,
    retrieve_view_evidence,
)
from backend.agents.step.vision.schemas import (
    FusedEvidence,
    FusedParameter,
    DimensionGroup,
    GeometryLine,
    OCRToken,
    QwenSemanticResult,
    QwenViewClassificationResult,
    QwenViewSemanticResult,
    SemanticAssignment,
    TokenLineLink,
    VisualEvidenceBundle,
    ViewRegion,
)
from backend.agents.step.vision.view_splitter import (
    ensure_region_coverage,
    split_view_regions,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_eight_category_template_tree_is_complete_and_importable():
    """八类及 Connector/CN/MT 必须具有真实、可导入的 Python 结构。"""
    catalog = component_category_catalog()

    assert list(catalog) == [
        "resistor", "capacitor", "inductor", "diode", "transistor",
        "connector", "ic", "misc",
    ]
    assert [catalog[name]["code"] for name in catalog] == [
        "01", "02", "03", "04", "05", "06", "07", "08",
    ]
    assert list(catalog["connector"]["subcategories"]) == ["cn", "mt"]
    assert catalog["connector"]["subcategories"]["cn"]["code"] == "01_CN"
    assert catalog["connector"]["subcategories"]["mt"]["code"] == "02_MT"

    modules = [
        "resistor.two_terminal_chip",
        "capacitor.two_terminal_chip",
        "inductor.template",
        "diode.template",
        "transistor.template",
        "connector.cn.dsub_connector",
        "connector.cn.pin_header",
        "connector.mt.template",
        "ic.gullwing_ic",
        "ic.quad_gullwing_ic",
        "ic.qfn_ufqfpn",
        "misc.template",
    ]
    for name in modules:
        assert importlib.import_module(
            f"backend.agents.step.families.{name}"
        ) is not None


def test_display_catalog_includes_unimplemented_templates_without_executing_them():
    display = template_category_catalog()

    assert display["inductor"]["templates"]["inductor"]["implemented"] is False
    assert display["connector"]["subcategories"]["mt"]["templates"][
        "connector/mt"
    ]["implemented"] is False
    assert display["connector"]["subcategories"]["cn"]["templates"][
        "connector/cn/pin_header"
    ]["implemented"] is False
    assert "inductor" not in image_family_catalog()
    placeholder = importlib.import_module(
        "backend.agents.step.families.inductor.template"
    )
    with pytest.raises(NotImplementedError, match="inductor"):
        placeholder.plan({}, {})


def test_legacy_two_terminal_requires_human_follow_up_when_identity_is_ambiguous():
    selection = resolve_family_selection("two_terminal_chip")

    assert selection["status"] == "needs_human_follow_up"
    assert selection["candidates"] == [
        RESISTOR_CHIP_FAMILY_ID, CAPACITOR_CHIP_FAMILY_ID,
    ]
    assert selection["follow_up_question"]


def test_legacy_family_ids_normalize_to_hierarchical_ids():
    assert resolve_family_selection("gullwing_ic")["family_id"] == GULLWING_IC_FAMILY_ID
    assert resolve_family_selection(
        "two_terminal_chip", identity_texts=["Chip Resistor 1206"]
    )["family_id"] == RESISTOR_CHIP_FAMILY_ID
    assert resolve_family_selection(
        "two_terminal_chip", identity_texts=["MLCC Capacitor 1206"]
    )["family_id"] == CAPACITOR_CHIP_FAMILY_ID
    assert resolve_family_selection("ic/ufqfpn")["family_id"] == QFN_UFQFPN_FAMILY_ID
    mismatch = resolve_family_selection(
        GULLWING_IC_FAMILY_ID, category_id="transistor"
    )
    assert mismatch["status"] == "invalid_hierarchy"
    assert mismatch["family_id"] == ""


def test_unimplemented_and_ambiguous_templates_use_distinct_safe_end_states(
    tmp_path: Path,
):
    unsupported = asyncio.run(image_nodes.validate_dimensions_node({
        "fused_evidence": FusedEvidence(
            category_id="inductor",
            family_id="inductor",
            package_type="",
        ).model_dump(),
        "output_dir": str(tmp_path / "unsupported"),
    }))
    follow_up = asyncio.run(image_nodes.validate_dimensions_node({
        "fused_evidence": FusedEvidence(
            family_id="",
            package_type="",
            unresolved_fields=["human_follow_up:family_id"],
        ).model_dump(),
        "output_dir": str(tmp_path / "follow_up"),
    }))

    assert unsupported["status"] == "stopped_unsupported_template"
    assert image_nodes.route_after_dimension_gate(unsupported) == "unsupported"
    assert follow_up["status"] == "needs_human_follow_up"
    assert image_nodes.route_after_dimension_gate(follow_up) == "follow_up"

    result = asyncio.run(image_nodes.stop_insufficient_extraction_node({
        **follow_up,
        "fused_evidence": {"category_id": "", "family_id": ""},
        "output_dir": str(tmp_path / "follow_up"),
    }))
    assert result["result"]["status"] == "needs_human_follow_up"
    assert result["result"]["candidates"] == [
        RESISTOR_CHIP_FAMILY_ID, CAPACITOR_CHIP_FAMILY_ID,
    ]


def test_ocr_each_scan_round_and_final_tokens_are_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """每轮 OCR 与最终去重 token 都必须输出统一结构化日志。"""
    image = np.full((80, 120), 255, dtype=np.uint8)
    image_path = tmp_path / "ocr_logging.png"
    assert cv2.imencode(".png", image)[1].tofile(image_path) is None

    class FakeEngine:
        def predict(self, _: np.ndarray):
            return [{
                "res": {
                    "dt_polys": [[[10, 10], [50, 10], [50, 30], [10, 30]]],
                    "rec_texts": ["0.95 BSC"],
                    "rec_scores": [0.99],
                }
            }]

    caplog.set_level(logging.INFO, logger="backend.agents.step.vision.ocr")
    tokens = extract_ocr_tokens(
        image_path, [], engine=FakeEngine(), rotations=(0,)
    )
    messages = [record.getMessage() for record in caplog.records]

    assert [token.text for token in tokens] == ["0.95 BSC"]
    assert any("ocr.scan_completed" in message for message in messages)
    assert any("ocr.scan_item" in message for message in messages)
    assert any("ocr.finalized" in message for message in messages)


def test_view_semantic_response_uses_conservative_confidence_when_omitted():
    """Qwen 遗漏视图汇总置信度时，应使用 assignment 最低置信度。"""
    item = image_nodes._parse_model_json(
        """{
          "region_id": "region_001",
          "view_type": "top_view",
          "assignments": [{
            "canonical_name": "body_length",
            "token_ids": ["ocr_001"],
            "line_ids": ["line_001"],
            "target_feature": "molded_body",
            "confidence": 0.91
          }]
        }""",
        QwenViewSemanticResult,
    )

    assert item.confidence == pytest.approx(0.91)


def test_multi_view_region_is_normalized_to_composite_outline():
    """同一区域包含多种投影视图时，应归一成可处理的复合外形视图。"""
    item = image_nodes._parse_model_json(
        """{
          "family_id": "quad_gullwing_ic",
          "package_type": "LQFP",
          "identified_views": {
            "region_001": ["front_view", "top_view", "detail_view"]
          },
          "overall_confidence": 0.95
        }""",
        QwenViewClassificationResult,
    )

    assert item.identified_views == {"region_001": "composite_outline"}


def _classification(family_id: str) -> QwenViewClassificationResult:
    """构造不含任何型号尺寸答案的第一阶段分类结果。"""
    contract = image_family_catalog().get(family_id, {})
    return QwenViewClassificationResult(
        family_id=family_id,
        package_type="",
        identified_views={"region_001": "front_view"},
        identified_features=list(contract.get("required_features", [])),
        unresolved_fields=list(contract.get("required_parameters", [])),
        ambiguities=[],
        overall_confidence=0.95,
    )


def test_multilead_sot_identity_prevents_two_terminal_family():
    """明确的多引脚 SOT 标题必须覆盖错误的两端器件分类。"""
    tokens = [
        OCRToken(
            token_id="ocr_identity_001",
            text="6-Lead Plastic Small Outline Transistor (SOT-23)",
            bbox=(10, 10, 500, 50),
            confidence=0.99,
        )
    ]

    corrected = reconcile_image_family_classification(
        _classification("two_terminal_chip"), tokens
    )

    assert corrected.family_id == "ic/gullwing_ic"
    assert corrected.category_id == "ic"
    assert corrected.identified_features == list(gullwing_ic.REQUIRED_FEATURES)
    assert corrected.unresolved_fields == list(gullwing_ic.REQUIRED_PARAMETERS)
    assert any("多引脚数量" in item for item in corrected.ambiguities)


def test_unidentified_multiterminal_conflict_stops_instead_of_using_chip_family():
    """只有多端子证据但无法确定结构族时必须清空 Family 并安全停止。"""
    tokens = [
        OCRToken(
            token_id="ocr_identity_002",
            text="8-Pin package",
            bbox=(10, 10, 200, 50),
            confidence=0.99,
        )
    ]

    corrected = reconcile_image_family_classification(
        _classification("two_terminal_chip"), tokens
    )

    assert corrected.family_id == ""
    assert corrected.unresolved_fields == ["supported_family"]
    assert corrected.overall_confidence == 0.0


def test_two_terminal_passive_identity_remains_unchanged():
    """没有多引脚冲突的片式被动器件应继续使用两端器件 Family。"""
    original = _classification("two_terminal_chip")
    tokens = [
        OCRToken(
            token_id="ocr_identity_003",
            text="Chip Capacitor 1206",
            bbox=(10, 10, 200, 50),
            confidence=0.99,
        )
    ]

    corrected = reconcile_image_family_classification(original, tokens)

    assert corrected.family_id == "capacitor/two_terminal_chip"
    assert corrected.category_id == "capacitor"
    assert corrected.subcategory_id is None


def test_nonrequired_unresolved_table_row_does_not_block_family_gate():
    """额外尺寸行应保留供审计，但不能冒充 Family 必需字段阻断建模。"""
    fused = _synthetic_gullwing_fused(pin_count=6).model_copy(update={
        "unresolved_fields": ["Foot Angle (table_extra)"],
    })

    gate = validate_fused_dimensions(
        fused,
        required_fields=gullwing_ic.REQUIRED_PARAMETERS,
        required_features=gullwing_ic.REQUIRED_FEATURES,
    )

    assert gate.passed is True
    assert gate.missing_fields == []


def test_required_unresolved_field_still_blocks_family_gate():
    """真正属于 Family 合同的未解决字段仍必须在 Feature IR 前停止。"""
    fused = _synthetic_gullwing_fused(pin_count=6).model_copy(update={
        "unresolved_fields": ["terminal_pitch"],
    })

    gate = validate_fused_dimensions(
        fused,
        required_fields=gullwing_ic.REQUIRED_PARAMETERS,
        required_features=gullwing_ic.REQUIRED_FEATURES,
    )

    assert gate.passed is False
    assert "terminal_pitch" in gate.missing_fields


def test_empty_view_does_not_zero_contributing_semantic_confidence():
    """无尺寸空视图保留审计记录，但不能把有效表格语义置信度压成零。"""
    classification = _classification("gullwing_ic").model_copy(update={
        "overall_confidence": 0.92,
    })
    empty_view = QwenViewSemanticResult(
        region_id="region_empty",
        view_type="isometric_view",
        unresolved_fields=[],
        ambiguities=["当前区域无尺寸证据"],
        confidence=0.0,
    )
    table_view = QwenViewSemanticResult(
        region_id="region_table",
        view_type="dimension_table",
        assignments=[SemanticAssignment(
            canonical_name="terminal_pitch",
            token_ids=["ocr_pitch_label", "ocr_pitch_nominal"],
            line_ids=[],
            target_feature="two_side_lead_array",
            confidence=0.96,
        )],
        confidence=0.96,
    )

    merged = merge_view_semantics(classification, [empty_view, table_view])

    assert merged.overall_confidence == pytest.approx(0.92)
    assert "当前区域无尺寸证据" in merged.ambiguities


def test_molded_body_consumes_both_orthogonal_draft_angles():
    """改变 β 必须改变本体几何，防止证据参数只进入 IR 却被 Builder 丢弃。"""
    primitives = CadQueryPrimitives(cq)
    common = {
        "length": 3.0,
        "width": 1.8,
        "height": 1.0,
        "standoff": 0.1,
        "draft_top_deg": 5.0,
    }
    straight_width = molded_body.build(
        primitives, {**common, "draft_bottom_deg": 0.0}
    )[0]
    drafted_width = molded_body.build(
        primitives, {**common, "draft_bottom_deg": 10.0}
    )[0]

    assert drafted_width.val().Volume() < straight_width.val().Volume()
    assert drafted_width.val().BoundingBox().xlen == pytest.approx(
        straight_width.val().BoundingBox().xlen
    )


def _synthetic_fused(*, overall_width: float = 50.0) -> FusedEvidence:
    """构造与真实盲测无关的合成 D-SUB 证据，用于 Builder 回归。"""
    values = {
        "circuit_count": 15.0,
        "overall_width": overall_width,
        "front_plate_height": 18.0,
        "plate_thickness": 2.0,
        "front_shell_top_width": 30.0,
        "front_shell_bottom_width": 26.0,
        "front_shell_height": 10.0,
        "front_projection_depth": 6.0,
        "shell_wall_thickness": 1.0,
        "mounting_center_span": 40.0,
        "mounting_hole_diameter": 4.0,
        "mounting_outer_diameter": 8.0,
        "rear_housing_width": 36.0,
        "rear_body_depth": 16.0,
        "rear_housing_height": 15.0,
        "contact_pitch": 3.0,
        "contact_column_span": 21.0,
        "contact_row_spacing": 4.0,
        "contact_outer_diameter": 2.0,
        "contact_inner_diameter": 1.0,
        "signal_pin_width": 1.0,
        "pin_tail_length": 5.0,
        "overall_height": 28.0,
    }
    parameters = []
    for index, name in enumerate(REQUIRED_PARAMETERS, start=1):
        parameters.append(FusedParameter(
            canonical_name=name,
            value=values[name],
            unit="count" if name == "circuit_count" else "mm",
            evidence_ids=[f"ocr_{index:03d}", f"line_{index:03d}"],
            token_ids=[f"ocr_{index:03d}"],
            line_ids=[f"line_{index:03d}"],
            token_bboxes=[(index, index, index + 10, index + 5)],
            target_feature="synthetic_test_feature",
            ocr_confidence=0.99,
            semantic_confidence=0.99,
            raw_texts=[f"({values[name]})"],
        ))
    return FusedEvidence(
        family_id="dsub_connector",
        package_type="synthetic-dsub",
        identified_features=list(REQUIRED_FEATURES),
        parameters=parameters,
    )


def _synthetic_gullwing_fused(*, pin_count: int = 8) -> FusedEvidence:
    """构造不对应任何 Golden 型号的鸥翼封装合成证据。"""
    values = {
        "nominal_pin_count": float(pin_count),
        "terminal_pitch": 1.25,
        "pin_span": (pin_count // 2 - 1) * 1.25,
        "total_height": 2.4,
        "housing_height": 1.8,
        "body_standoff": 0.6,
        "overall_width": 7.0,
        "body_width": 4.8,
        "body_length": 6.0,
        "terminal_length": 1.1,
        "terminal_thickness": 0.2,
        "terminal_width": 0.45,
        "mold_draft_angle_top_deg": 4.0,
        "mold_draft_angle_bottom_deg": 4.0,
    }
    parameters = []
    for index, name in enumerate(gullwing_ic.REQUIRED_PARAMETERS, start=1):
        parameters.append(FusedParameter(
            canonical_name=name,
            value=values[name],
            unit=(
                "count" if name == "nominal_pin_count"
                else "deg" if name.endswith("_deg")
                else "mm"
            ),
            evidence_ids=[f"ocr_g{index:03d}", f"line_g{index:03d}"],
            token_ids=[f"ocr_g{index:03d}"],
            line_ids=[f"line_g{index:03d}"],
            token_bboxes=[(index, index, index + 8, index + 4)],
            target_feature="synthetic_gullwing_feature",
            ocr_confidence=0.99,
            semantic_confidence=0.99,
            raw_texts=[str(values[name])],
        ))
    return FusedEvidence(
        family_id="gullwing_ic",
        package_type="synthetic-gullwing",
        identified_features=list(gullwing_ic.REQUIRED_FEATURES),
        parameters=parameters,
    )


def _synthetic_two_terminal_fused(
    *, length: float = 5.0, terminal_length: float = 0.8
) -> FusedEvidence:
    """构造不对应任何样本型号的两端片式器件证据。"""
    values = {
        "body_length": length,
        "body_width": 2.4,
        "body_height": 1.7,
        "terminal_length": terminal_length,
    }
    parameters = []
    for index, name in enumerate(two_terminal_chip.REQUIRED_PARAMETERS, start=1):
        parameters.append(FusedParameter(
            canonical_name=name,
            value=values[name],
            unit="mm",
            evidence_ids=[f"ocr_t{index:03d}"],
            token_ids=[f"ocr_t{index:03d}"],
            token_bboxes=[(index, index, index + 8, index + 4)],
            target_feature="synthetic_two_terminal_feature",
            ocr_confidence=0.99,
            semantic_confidence=0.99,
            raw_texts=[str(values[name])],
        ))
    return FusedEvidence(
        family_id="two_terminal_chip",
        package_type="synthetic-chip",
        identified_features=list(two_terminal_chip.REQUIRED_FEATURES),
        parameters=parameters,
    )


def _synthetic_quad_gullwing_fused(*, pin_count: int = 32) -> FusedEvidence:
    """构造不对应样本型号的四边鸥翼封装证据。"""
    per_side = pin_count // 4
    values = {
        "nominal_pin_count": float(pin_count),
        "total_height": 1.8,
        "housing_height": 1.6,
        "body_standoff": 0.2,
        "overall_length": 9.0,
        "overall_width": 9.0,
        "body_length": 7.0,
        "body_width": 7.0,
        "terminal_span": (per_side - 1) * 0.8,
        "terminal_pitch": 0.8,
        "terminal_length": 1.0,
        "lead_projection": 1.0,
        "terminal_width": 0.3,
        "terminal_thickness": 0.15,
    }
    parameters = [
        FusedParameter(
            canonical_name=name,
            value=value,
            unit="count" if name == "nominal_pin_count" else "mm",
            evidence_ids=[f"ocr_q{index:03d}"],
            token_ids=[f"ocr_q{index:03d}"],
            token_bboxes=[(index, index, index + 8, index + 4)],
            target_feature="synthetic_quad_gullwing",
            ocr_confidence=0.99,
            semantic_confidence=0.99,
            raw_texts=[str(value)],
        )
        for index, (name, value) in enumerate(values.items(), start=1)
    ]
    return FusedEvidence(
        family_id=quad_gullwing_ic.FAMILY_ID,
        package_type="synthetic-qfp",
        identified_features=list(quad_gullwing_ic.REQUIRED_FEATURES),
        parameters=parameters,
    )


def _synthetic_qfn_ufqfpn_fused(pin_count: int = 20) -> FusedEvidence:
    values = (
        {
            "nominal_pin_count": 56.0,
            "body_length": 7.0,
            "body_width": 7.0,
            "total_height": 0.9,
            "body_standoff": 0.05,
            "terminal_pitch": 0.4,
            "terminal_length": 0.4,
            "terminal_width": 0.18,
            "terminal_height": 0.203,
            "exposed_pad_length": 3.1,
            "exposed_pad_width": 3.1,
        }
        if pin_count == 56
        else {
            "nominal_pin_count": 20.0,
            "body_length": 3.0,
            "body_width": 3.0,
            "total_height": 0.55,
            "body_standoff": 0.02,
            "terminal_pitch": 0.5,
            "terminal_length": 0.35,
            "terminal_width": 0.25,
            "terminal_height": 0.152,
        }
    )
    parameters = [
        FusedParameter(
            canonical_name=name,
            value=value,
            unit="count" if name == "nominal_pin_count" else "mm",
            evidence_ids=[f"ocr_u{index:03d}"],
            token_ids=[f"ocr_u{index:03d}"],
            token_bboxes=[(index, index, index + 8, index + 4)],
            target_feature="synthetic_qfn_ufqfpn",
            ocr_confidence=0.99,
            semantic_confidence=0.99,
            raw_texts=[str(value)],
        )
        for index, (name, value) in enumerate(values.items(), start=1)
    ]
    return FusedEvidence(
        family_id=qfn_ufqfpn.FAMILY_ID,
        category_id="ic",
        package_type="QFN56" if pin_count == 56 else "UFQFPN20",
        identified_features=list(qfn_ufqfpn.REQUIRED_FEATURES),
        parameters=parameters,
    )


def test_ocr_dimension_expression_parser_preserves_symbols_and_tolerance():
    parsed = parse_dimension_expression("2-Ø.250(6.35±0.20) REF")

    assert parsed.quantity == 2
    assert parsed.symbol == "Ø"
    assert parsed.nominal_value == pytest.approx(6.35)
    assert parsed.minimum_value == pytest.approx(6.15)
    assert parsed.maximum_value == pytest.approx(6.55)
    assert parsed.is_reference is True
    assert parsed.unit == "mm"


def test_decimal_comma_and_explicit_angle_range_are_parsed_without_guessing():
    """欧式小数与显式角度范围必须保留原图数值边界。"""
    decimal = parse_dimension_expression("10,50 MAX")
    angle = parse_dimension_expression("0°-8°")

    assert decimal.nominal_value == pytest.approx(10.50)
    assert angle.nominal_value is None
    assert angle.minimum_value == pytest.approx(0.0)
    assert angle.maximum_value == pytest.approx(8.0)


def test_table_nominal_parser_accepts_attached_bsc_and_ref_suffixes():
    assert parse_table_nominal_expression("7BSC").nominal_value == pytest.approx(7.0)
    reference = parse_table_nominal_expression("0.203REF")
    assert reference.nominal_value == pytest.approx(0.203)
    assert reference.is_reference is True


def test_jedec_package_identity_provides_explicit_pin_count_evidence():
    """JEDEC 的 Gnn 身份码应形成计数证据，修订号不得误匹配。"""
    assert parse_explicit_count_expression("R-PDSO-G16") == 16
    assert parse_explicit_count_expression("28 PINS SHOWN") == 28
    assert parse_explicit_count_expression("40400644/G") is None
    assert parse_explicit_count_expression("02/11") is None

    token = OCRToken(
        token_id="ocr_identity",
        text="R-PDSO-G16",
        bbox=(12, 18, 180, 46),
        confidence=0.97,
        source_region_id="region_document",
    )
    groups, _ = build_dimension_groups([token], [], [])

    assert len(groups) == 1
    assert groups[0].evidence_type == "identity_text"
    assert groups[0].token_ids == ["ocr_identity"]
    assert groups[0].bbox == token.bbox


def test_jedec_identity_count_fuses_without_dimension_line():
    """身份计数可无尺寸线进入融合，但必须保留 OCR bbox 和证据 ID。"""
    token = OCRToken(
        token_id="ocr_identity",
        text="R-PDSO-G20",
        bbox=(20, 20, 190, 52),
        confidence=0.98,
        source_region_id="region_document",
    )
    evidence = VisualEvidenceBundle(
        image_sha256="sha256",
        image_size=(1000, 800),
        ocr_tokens=[token],
        lines=[],
        arrows=[],
        regions=[],
        token_line_links=[],
    )
    semantics = QwenSemanticResult(
        family_id="gullwing_ic",
        package_type="generic",
        assignments=[SemanticAssignment(
            canonical_name="nominal_pin_count",
            token_ids=["ocr_identity"],
            line_ids=[],
            target_feature="package_identity",
            confidence=0.95,
        )],
        overall_confidence=0.95,
    )

    fused = fuse_evidence(evidence, semantics)

    assert fused.parameters[0].value == 20
    assert fused.parameters[0].unit == "count"
    assert fused.parameters[0].evidence_kind == "identity_text"
    assert fused.parameters[0].token_bboxes == [token.bbox]


def test_header_identity_count_is_routed_to_composite_geometry_view(
    tmp_path: Path,
):
    """页眉身份计数应进入整页复合视图，而普通页眉文字仍被隔离。"""
    identity = OCRToken(
        token_id="ocr_identity",
        text="R-PDSO-G24",
        bbox=(20, 20, 190, 52),
        confidence=0.98,
        source_region_id="region_header",
    )
    title = OCRToken(
        token_id="ocr_title",
        text="PLASTIC SMALL OUTLINE",
        bbox=(210, 20, 500, 52),
        confidence=0.99,
        source_region_id="region_header",
    )
    regions = [
        ViewRegion(region_id="region_header", bbox=(0, 0, 600, 80), area_ratio=0.08),
        ViewRegion(region_id="region_document", bbox=(0, 0, 1000, 800), area_ratio=1.0),
    ]
    bundle = VisualEvidenceBundle(
        image_sha256="sha256",
        image_size=(1000, 800),
        regions=regions,
        ocr_tokens=[identity, title],
        lines=[],
        arrows=[],
        token_line_links=[],
    )
    result = asyncio.run(image_nodes.build_dimension_groups_node({
        "all_ocr_tokens": [identity.model_dump(), title.model_dump()],
        "all_line_segments": [],
        "view_classification": {
            "identified_views": {"region_document": "composite_outline"}
        },
        "visual_evidence": bundle.model_dump(),
        "output_dir": str(tmp_path),
        "artifact_paths": {},
        "errors": [],
    }))

    identity_groups = [
        group for group in result["dimension_groups"]
        if group["evidence_type"] == "identity_text"
    ]
    assert len(identity_groups) == 1
    assert identity_groups[0]["view_id"] == "region_document"
    assert identity_groups[0]["token_ids"] == ["ocr_identity"]
    retained_ids = {token["token_id"] for token in result["all_ocr_tokens"]}
    assert "ocr_identity" in retained_ids
    assert "ocr_title" not in retained_ids


def test_gullwing_prefers_explicit_identity_count_over_corner_pin_label():
    """唯一的封装身份计数应替代可能只是角标的引脚编号证据。"""
    result = QwenViewSemanticResult(
        region_id="region_document",
        view_type="composite_outline",
        assignments=[SemanticAssignment(
            canonical_name="nominal_pin_count",
            token_ids=["ocr_corner"],
            line_ids=["line_corner"],
            target_feature="corner_pin_label",
            confidence=0.95,
        )],
        confidence=0.95,
    )
    reconciled = gullwing_ic.reconcile_view_semantics(result, {
        "region": {"region_id": "region_document", "bbox": [0, 0, 1000, 800]},
        "ocr_tokens": [
            {
                "token_id": "ocr_identity",
                "text": "R-PDSO-G18",
                "bbox": [20, 20, 190, 52],
                "confidence": 0.98,
            },
            {
                "token_id": "ocr_corner",
                "text": "18",
                "bbox": [240, 200, 275, 230],
                "confidence": 0.99,
            },
        ],
        "dimension_groups": [{
            "dimension_id": "identity_0001",
            "evidence_type": "identity_text",
            "bbox": [20, 20, 190, 52],
            "token_ids": ["ocr_identity"],
            "dimension_line_ids": [],
            "extension_line_ids": [],
            "orientation": "horizontal",
            "confidence": 0.98,
        }],
    })

    count_assignments = [
        item for item in reconciled.assignments
        if item.canonical_name == "nominal_pin_count"
    ]
    assert len(count_assignments) == 1
    assert count_assignments[0].token_ids == ["ocr_identity"]
    assert count_assignments[0].line_ids == []


def test_page_frame_falls_back_to_horizontal_content_regions(tmp_path: Path):
    """整页图框吞并连通域时仍必须恢复多个内容区域。"""
    image = np.full((900, 1200), 255, dtype=np.uint8)
    cv2.rectangle(image, (5, 5), (1194, 894), 0, 4)
    cv2.rectangle(image, (100, 100), (1000, 350), 0, 8)
    cv2.rectangle(image, (150, 500), (1050, 620), 0, 8)
    cv2.rectangle(image, (200, 720), (900, 840), 0, 8)
    path = tmp_path / "bordered_sheet.png"
    assert cv2.imencode(".png", image)[1].tofile(path) is None

    regions = split_view_regions(path)

    assert len(regions) >= 2
    assert all(region.area_ratio <= 1.0 for region in regions)


def test_malformed_ocr_dimension_is_not_silently_repaired():
    """丢失公差符号或混入多组尺寸时，不得猜测一个数值。"""
    assert parse_dimension_expression("1.219(30.810.25)").nominal_value is None
    assert parse_dimension_expression("2-.126(3.20)9-.043(1.09)").nominal_value is None
    assert parse_dimension_expression("p1").nominal_value is None


def test_dimension_table_keeps_only_label_and_metric_nominal_in_prompt():
    """尺寸表的 MIN/MAX 留在 State，但不能进入单次语义数值引用。"""
    region = ViewRegion(
        region_id="region_table", bbox=(0, 0, 900, 300), area_ratio=0.4
    )
    tokens = [
        OCRToken(token_id="header_dimension", text="Dimension Limits", bbox=(10, 10, 180, 30), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="header_metric", text="MILLIMETERS", bbox=(500, 10, 880, 30), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="header_min", text="MIN", bbox=(520, 40, 580, 60), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="header_nom", text="NOM", bbox=(650, 40, 710, 60), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="header_max", text="MAX", bbox=(780, 40, 840, 60), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="row_label", text="Overall Width", bbox=(20, 90, 180, 115), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="row_min", text="4.0", bbox=(525, 90, 570, 115), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="row_nom", text="4.5", bbox=(655, 90, 700, 115), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="row_max", text="5.0", bbox=(785, 90, 830, 115), confidence=0.99, source_region_id=region.region_id),
    ]
    lines = [GeometryLine(
        line_id="table_line",
        type="line_segment",
        start=(0, 120),
        end=(900, 120),
        confidence=0.95,
        source_region_id=region.region_id,
    )]
    evidence = VisualEvidenceBundle(
        image_sha256="1" * 64,
        image_size=(900, 300),
        regions=[region],
        ocr_tokens=tokens,
        lines=lines,
    )

    table_ids = detect_dimension_table_regions(tokens, [region])
    groups, updated_tokens = build_table_dimension_groups(tokens, lines, set(table_ids))
    payload = retrieve_dimension_group_evidence(
        evidence.model_copy(update={"ocr_tokens": updated_tokens}),
        groups,
        region.region_id,
        max_tokens=10,
        max_dimension_groups=2,
    )

    assert table_ids == [region.region_id]
    assert len(groups) == 1
    assert payload["dimension_groups"][0]["token_ids"] == ["row_label", "row_nom"]
    assert {item["token_id"] for item in payload["ocr_tokens"]} == {
        "row_label", "row_nom"
    }


def test_dimension_table_detection_tolerates_header_punctuation_and_ocr_typo():
    region = ViewRegion(region_id="region_table", bbox=(0, 0, 500, 300), area_ratio=1.0)
    texts = ("Miimetred", "Inch", "Symbol", "Min.", "Nom.", "Max.")
    tokens = [
        OCRToken(
            token_id=f"ocr_header_{index}",
            text=text,
            bbox=(index * 50, 10, index * 50 + 45, 30),
            confidence=0.99,
            source_region_id=region.region_id,
        )
        for index, text in enumerate(texts)
    ]

    assert detect_dimension_table_regions(tokens, [region]) == [region.region_id]


def test_dimension_table_retrieval_compacts_rows_and_keeps_late_required_rows():
    """15 行表格应完整进入小批量 Prompt，且不携带误归类的数值标签。"""
    region = ViewRegion(region_id="table", bbox=(0, 0, 300, 400), area_ratio=1.0)
    tokens = []
    groups = []
    for index in range(15):
        label_id = f"label_{index:02d}"
        symbol_id = f"symbol_{index:02d}"
        noise_id = f"noise_{index:02d}"
        value_id = f"value_{index:02d}"
        row_y = index * 20
        tokens.extend([
            OCRToken(
                token_id=label_id,
                text=f"Dimension Field {index}",
                bbox=(10, row_y, 120, row_y + 15),
                confidence=0.99,
                source_region_id=region.region_id,
            ),
            OCRToken(
                token_id=symbol_id,
                text=f"A{index % 10}",
                bbox=(125, row_y, 145, row_y + 15),
                confidence=0.99,
                source_region_id=region.region_id,
            ),
            OCRToken(
                token_id=noise_id,
                text=".038 BSC",
                bbox=(150, row_y, 210, row_y + 15),
                confidence=0.99,
                source_region_id=region.region_id,
            ),
            OCRToken(
                token_id=value_id,
                text="1.00",
                bbox=(215, row_y, 260, row_y + 15),
                confidence=0.99,
                source_region_id=region.region_id,
                value_role="table_nominal",
            ),
        ])
        groups.append(DimensionGroup(
            dimension_id=f"table_{index:04d}",
            view_id=region.region_id,
            evidence_type="table_row",
            bbox=(10, row_y, 260, row_y + 15),
            token_ids=[label_id, symbol_id, noise_id, value_id],
            row_label_token_ids=[label_id, symbol_id, noise_id],
            table_columns={"nom": value_id},
            confidence=0.99,
        ))
    evidence = VisualEvidenceBundle(
        image_sha256="2" * 64,
        image_size=(300, 400),
        regions=[region],
        ocr_tokens=tokens,
    )

    payload = retrieve_dimension_group_evidence(
        evidence,
        groups,
        region.region_id,
        max_tokens=48,
        max_dimension_groups=16,
    )

    assert len(payload["dimension_groups"]) == 15
    assert payload["dimension_groups"][-1]["dimension_id"] == "table_0014"
    assert all(
        not any(token_id.startswith("noise_") for token_id in group["token_ids"])
        for group in payload["dimension_groups"]
    )


def test_key_value_dimension_table_recovers_nominal_without_inventing_tolerance():
    """两列表格可恢复名义值，但不得根据丢失符号补写公差。"""
    region = ViewRegion(
        region_id="region_key_value", bbox=(0, 0, 700, 220), area_ratio=0.5
    )
    tokens = [
        OCRToken(token_id="title", text="External Dimensions", bbox=(10, 10, 250, 35), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="label_l", text="Dimension L", bbox=(20, 70, 180, 100), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="value_l", text="5.0 0.20 mm", bbox=(350, 70, 510, 100), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="label_w", text="Dimension W", bbox=(20, 120, 180, 150), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="value_w", text="2.4 0.20 mm", bbox=(350, 120, 510, 150), confidence=0.99, source_region_id=region.region_id),
    ]
    groups, updated = build_table_dimension_groups(
        tokens, [], set(detect_dimension_table_regions(tokens, [region]))
    )
    token_index = {token.token_id: token for token in updated}

    assert len(groups) == 2
    assert token_index["value_l"].value_role == "table_nominal"
    parsed = image_nodes.parse_table_nominal_expression(token_index["value_l"].text)
    assert parsed.nominal_value == pytest.approx(5.0)
    assert parsed.tolerance_plus is None
    assert groups[0].extension_line_ids == []


def test_symbol_table_accepts_typ_and_short_dimension_symbols():
    """QFP 的 Symbol/MIN/TYP/MAX 表必须保留 A1、D1 等短符号。"""
    region = ViewRegion(region_id="region_qfp_table", bbox=(0, 0, 900, 360), area_ratio=0.4)
    tokens = [
        OCRToken(token_id="metric", text="millimeters", bbox=(250, 10, 600, 35), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="symbol", text="Symbol", bbox=(20, 45, 130, 70), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="min", text="Min", bbox=(260, 45, 320, 70), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="typ", text="Typ", bbox=(420, 45, 480, 70), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="max", text="Max", bbox=(580, 45, 640, 70), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="label", text="A1", bbox=(40, 110, 90, 140), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="vmin", text="0.05", bbox=(260, 110, 320, 140), confidence=0.99, source_region_id=region.region_id),
        OCRToken(token_id="vmax", text="0.15", bbox=(580, 110, 640, 140), confidence=0.99, source_region_id=region.region_id),
    ]
    table_ids = detect_dimension_table_regions(tokens, [region])
    groups, updated = build_table_dimension_groups(tokens, [], set(table_ids))
    token_index = {token.token_id: token for token in updated}

    assert table_ids == [region.region_id]
    assert len(groups) == 1
    assert groups[0].row_label_token_ids == ["label"]
    assert groups[0].table_columns["max"] == "vmax"
    assert token_index["vmax"].value_role == "table_maximum"


def test_qwen_view_retrieval_only_contains_current_region_evidence():
    """单视图请求不能泄露另一个视图的 token 或 line。"""
    evidence = VisualEvidenceBundle(
        image_sha256="f" * 64,
        image_size=(1000, 800),
        regions=[
            ViewRegion(region_id="region_front", bbox=(0, 0, 400, 400), area_ratio=0.2),
            ViewRegion(region_id="region_side", bbox=(600, 0, 1000, 400), area_ratio=0.2),
        ],
        ocr_tokens=[
            OCRToken(
                token_id="ocr_front",
                text=".500(12.70)",
                bbox=(100, 100, 180, 130),
                confidence=0.99,
                source_region_id="region_front",
            ),
            OCRToken(
                token_id="ocr_side",
                text=".250(6.35)",
                bbox=(700, 100, 780, 130),
                confidence=0.99,
                source_region_id="region_side",
            ),
        ],
        lines=[
            GeometryLine(
                line_id="line_front",
                type="dimension_line",
                start=(90, 150),
                end=(190, 150),
                confidence=0.9,
                source_region_id="region_front",
            ),
            GeometryLine(
                line_id="line_side",
                type="dimension_line",
                start=(690, 150),
                end=(790, 150),
                confidence=0.9,
                source_region_id="region_side",
            ),
        ],
        token_line_links=[
            TokenLineLink(token_id="ocr_front", line_ids=["line_front"], confidence=0.9),
            TokenLineLink(token_id="ocr_side", line_ids=["line_side"], confidence=0.9),
        ],
    )

    summaries = build_region_summaries(evidence)
    front = retrieve_view_evidence(evidence, "region_front")

    assert len(summaries) == 2
    assert {item["token_id"] for item in front["ocr_tokens"]} == {"ocr_front"}
    assert {item["line_id"] for item in front["lines"]} == {"line_front"}

    grouped = retrieve_dimension_group_evidence(
        evidence,
        [
            DimensionGroup(
                dimension_id="dim_front",
                view_id="region_front",
                bbox=(90, 90, 190, 160),
                token_ids=["ocr_front"],
                dimension_line_ids=["line_front"],
                orientation="horizontal",
                confidence=0.9,
            ),
            DimensionGroup(
                dimension_id="dim_side",
                view_id="region_side",
                bbox=(690, 90, 790, 160),
                token_ids=["ocr_side"],
                dimension_line_ids=["line_side"],
                orientation="horizontal",
                confidence=0.9,
            ),
        ],
        "region_front",
        max_tokens=1,
        max_dimension_groups=1,
    )
    assert [item["dimension_id"] for item in grouped["dimension_groups"]] == ["dim_front"]
    assert [item["token_id"] for item in grouped["ocr_tokens"]] == ["ocr_front"]


def test_graph_exposes_state_prompt_separation_and_view_loop():
    graph = build_image_to_step_graph().get_graph()
    node_names = set(graph.nodes)
    assert {
        "ask_package",
        "extract_all_evidence",
        "store_evidence_locally",
        "detect_views",
        "confirm_template",
        "jev_route_template",
        "build_dimension_groups",
        "retrieve_view_evidence",
        "qwen_analyze_one_view",
        "save_view_result",
        "merge_view_results",
        "validate_dimension_chain",
        "search_reference_step",
        "prepare_exact_reference_step",
        "validate_reference_step_candidates",
        "prepare_reference_step",
        "review_result",
    }.issubset(node_names)
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("__start__", "ask_package") in edges
    assert ("detect_views", "confirm_template") in edges
    assert ("confirm_template", "jev_route_template") in edges
    assert ("finalize_result", "review_result") in edges
    assert ("save_view_result", "retrieve_view_evidence") in edges
    assert ("save_view_result", "merge_view_results") in edges
    assert ("jev_route_template", "search_reference_step") in edges
    assert ("search_reference_step", "prepare_exact_reference_step") in edges
    assert ("search_reference_step", "build_dimension_groups") in edges
    assert ("prepare_exact_reference_step", "render_views") in edges
    assert ("prepare_exact_reference_step", "build_dimension_groups") in edges
    assert ("create_feature_ir", "validate_reference_step_candidates") in edges
    assert ("validate_reference_step_candidates", "prepare_reference_step") in edges
    assert ("validate_reference_step_candidates", "build_step") in edges
    assert ("prepare_reference_step", "verify_step") in edges
    assert ("prepare_reference_step", "build_step") in edges


def test_product_identifier_search_uses_only_existing_ocr_text():
    """检索关键词只能来自 OCR 产品标识，尺寸 token 不能被误当成料号。"""
    tokens = [
        {
            "token_id": "ocr_001",
            "text": "AD7792/AD7793",
            "bbox": [20, 10, 280, 60],
            "confidence": 0.99,
        },
        {
            "token_id": "ocr_002",
            "text": "5.10",
            "bbox": [300, 200, 350, 230],
            "confidence": 0.98,
        },
    ]

    assert extract_product_identifiers(tokens) == ["AD7792", "AD7793"]


def test_product_identifier_search_prioritizes_part_number_over_ordering_terms():
    """完整料号必须优先于材料、性别和订购说明中的混合数字文本。"""
    tokens = [
        {"text": "94V-O", "bbox": [10, 20, 80, 40], "confidence": 0.99},
        {"text": "F0-Female", "bbox": [10, 60, 100, 80], "confidence": 0.99},
        {"text": "09-9Circuits", "bbox": [10, 90, 130, 110], "confidence": 0.99},
        {
            "text": "FDB0902-F0DB3XXXXXA",
            "bbox": [10, 900, 250, 930],
            "confidence": 0.98,
        },
    ]

    identifiers = extract_product_identifiers(tokens)

    assert identifiers[0] == "FDB0902-F0DB3XXXXXA"
    assert "F0-Female" not in identifiers


def test_manufacturer_search_uses_explicit_drawing_text_only():
    """厂商只能来自图纸明文，不能根据料号或器件类别臆测。"""
    tokens = [
        {"text": "S PROPRIETARY TO TXGA INDUSTRIAL", "confidence": 0.98},
        {"text": "FDB0902-F0DB3XXXXXA", "confidence": 0.99},
    ]

    assert extract_manufacturer_names(tokens) == ["TXGA"]


def test_search_result_classification_distinguishes_product_and_package():
    """相同料号优先于仅封装相似的公开搜索结果。"""
    exact = {"title": "AD7792 TSSOP STEP model", "url": "https://example.com/a"}
    similar = {"title": "Generic TSSOP-16 package", "url": "https://example.com/b"}

    assert classify_result_match(exact, ["AD7792"], "TSSOP-16") == "exact"
    assert classify_result_match(similar, ["AD7792"], "TSSOP-16") == "similar"


def test_search_result_matches_ocr_placeholder_part_number():
    """订购码中的连续 X 应按单字符占位匹配官网具体料号。"""
    result = {
        "title": "FDB0902-F0DB300K6KA D-SUB 90 Degree Female 9Circuits",
        "url": "https://m.txga.com/m18product/FDB0902-F0DB300K6KA.html",
    }

    assert classify_result_match(
        result,
        ["FDB0902-F0DB3XXXXXA", "FDB0902"],
        "D-Sub 90 Degree Female PCB Connector",
    ) == "exact"


def test_similar_public_step_is_boundedly_adapted_from_feature_ir(tmp_path: Path):
    """相似 STEP 只按图纸 Feature IR 目标包围盒做受限适配。"""
    source_path = tmp_path / "source.step"
    output_path = tmp_path / "adapted.step"
    cq.exporters.export(
        cq.Workplane("XY").box(10.0, 8.0, 2.0, centered=(True, True, False)),
        str(source_path),
        exportType="STEP",
    )
    expected_bbox = {
        "xmin": -5.5,
        "xmax": 5.5,
        "ymin": -4.4,
        "ymax": 4.4,
        "zmin": 0.0,
        "zmax": 2.2,
    }

    prepare_reference_step(
        cq,
        source_path,
        output_path,
        expected_bbox,
        adapt_dimensions=True,
    )
    bbox = read_step_metrics(cq, output_path)["bounding_box"]

    assert bbox["xmax"] - bbox["xmin"] == pytest.approx(11.0, abs=1e-5)
    assert bbox["ymax"] - bbox["ymin"] == pytest.approx(8.8, abs=1e-5)
    assert bbox["zmax"] - bbox["zmin"] == pytest.approx(2.2, abs=1e-5)


def test_reference_routes_fall_back_without_compatible_candidate():
    """无候选或候选准备失败时必须回到原 Feature IR Builder。"""
    assert image_nodes.route_after_reference_search({
        "reference_step_search": {"selected_candidate": None}
    }) == "local"
    assert image_nodes.route_after_reference_prepare({
        "status": "reference_step_unusable"
    }) == "local"


def test_early_reference_routes_exact_product_before_qwen_semantics():
    """精确产品候选应直接回读，未命中时才进入逐视图语义分析。"""
    exact_state = {
        "reference_step_search": {
            "early_exact_candidate": {"local_path": "candidate.step"}
        }
    }
    missing_state = {"reference_step_search": {"early_exact_candidate": None}}

    assert image_nodes.route_after_early_reference_search(exact_state) == "exact"
    assert image_nodes.route_after_early_reference_search(missing_state) == "semantic"
    assert image_nodes.route_after_exact_reference_prepare({
        "status": "step_verified"
    }) == "render"
    assert image_nodes.route_after_exact_reference_prepare({
        "status": "reference_step_unusable"
    }) == "semantic"


def test_jev_state_is_compact_and_contains_no_dimension_only_tokens():
    """Jev 只接收产品/封装结构证据，不接收整图或纯尺寸 token。"""
    jev_state = build_jev_state(
        [
            {"text": "AD7792/AD7793", "confidence": 0.99},
            {"text": "16 LEAD TSSOP", "confidence": 0.98},
            {"text": "5.10", "confidence": 0.99},
            {"text": "0.20 MAX", "confidence": 0.75},
        ],
        {
            "category_id": "ic",
            "subcategory_id": None,
            "family_id": "ic/gullwing_ic",
            "package_type": "TSSOP-16",
            "identified_views": {"region_001": "top_view"},
            "identified_features": ["molded_body", "gullwing_lead"],
        },
    )

    assert "AD7792/AD7793" in jev_state["drawing_text"]
    assert "5.10" not in jev_state["drawing_text"]
    assert jev_state["pin_count_candidates"] == [16]


def test_jev_choice_gate_uses_probability_confidence_and_margin():
    """三个门槛必须同时满足才允许 Jev 自动路由。"""
    passed = evaluate_jev_gate({
        "choice": "ic",
        "confidence": 0.82,
        "probabilities": {
            "resistor": 0.02, "capacitor": 0.02, "inductor": 0.01,
            "diode": 0.01, "transistor": 0.01, "connector": 0.01,
            "ic": 0.88, "misc": 0.01, "unknown": 0.03,
        },
    })
    failed = evaluate_jev_gate({
        "choice": "ic",
        "confidence": 0.44,
        "probabilities": {
            "resistor": 0.02, "capacitor": 0.02, "inductor": 0.01,
            "diode": 0.01, "transistor": 0.01, "connector": 0.01,
            "ic": 0.54, "misc": 0.01, "unknown": 0.37,
        },
    })

    assert passed["auto_route"] is True
    assert failed["auto_route"] is False


def test_jev_business_category_resolves_to_existing_geometric_family():
    """ADC/IC 是业务类别，最终仍应解析成现有封装几何 Family。"""
    state = {
        "package_type": "TSSOP-16",
        "_fallback_geometric_family": "ic/gullwing_ic",
        "_fallback_category": "ic",
        "drawing_text": ["AD7792/AD7793"],
    }

    assert resolve_geometric_family(
        "ic", state, set(image_family_catalog())
    ) == "ic/gullwing_ic"


def test_jev_node_auto_routes_and_unconfigured_mode_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Jev 高置信结果可路由；未配置 API 时必须保留原 Qwen Family。"""
    classification = {
        "category_id": "ic",
        "subcategory_id": None,
        "family_id": "ic/gullwing_ic",
        "package_type": "TSSOP-16",
        "identified_views": {"region_001": "top_view"},
        "identified_features": ["molded_body"],
        "unresolved_fields": [],
        "ambiguities": [],
        "overall_confidence": 0.91,
    }
    tokens = [{
        "text": "AD7792/AD7793 16 LEAD TSSOP",
        "confidence": 0.99,
    }]

    async def fake_choice(*_args, **_kwargs):
        return {
            "provider": "typesafe",
            "model": "jev-test",
            "choice": "ic",
            "confidence": 0.90,
            "probabilities": {
                "resistor": 0.02, "capacitor": 0.02, "inductor": 0.01,
                "diode": 0.01, "transistor": 0.01, "connector": 0.02,
                "ic": 0.88, "misc": 0.01, "unknown": 0.02,
            },
            "usage": {},
        }

    monkeypatch.setattr(image_nodes, "call_jev_choice", fake_choice)
    monkeypatch.setattr(image_nodes, "get_settings", lambda: SimpleNamespace(
        typesafe_api_key="test-key",
        typesafe_base_url="https://api.typesafe.ai/v1",
        typesafe_model="jev-test",
    ))
    routed = asyncio.run(image_nodes.jev_route_template_node({
        "view_classification": classification,
        "all_ocr_tokens": tokens,
        "output_dir": str(tmp_path / "routed"),
    }))

    monkeypatch.setattr(image_nodes, "get_settings", lambda: SimpleNamespace(
        typesafe_api_key="",
        typesafe_base_url="https://api.typesafe.ai/v1",
        typesafe_model="jev-test",
    ))
    fallback = asyncio.run(image_nodes.jev_route_template_node({
        "view_classification": classification,
        "all_ocr_tokens": tokens,
        "output_dir": str(tmp_path / "fallback"),
    }))

    assert routed["jev_decision"]["status"] == "auto_routed"
    assert routed["view_classification"]["family_id"] == "ic/gullwing_ic"
    assert routed["view_classification"]["category_id"] == "ic"
    assert fallback["jev_decision"]["route_source"] == "qwen_fallback"
    assert fallback["view_classification"]["family_id"] == "ic/gullwing_ic"


def test_composite_outline_is_processed_as_geometry_view():
    """复合封装外形图不能因标签不是 front/top/side 而被跳过。"""
    assert image_nodes._is_geometry_view("composite_outline") is True
    assert image_nodes._is_geometry_view("package outline") is True
    assert image_nodes._is_geometry_view("revision_table") is False


def test_gullwing_view_contract_uses_family_specific_front_and_side_fields():
    """鸥翼封装端视/侧视不能误用 D-SUB 的视图字段合同。"""
    contract = image_family_catalog()[gullwing_ic.FAMILY_ID]

    front = image_nodes._view_family_contract(contract, "front_view")
    side = image_nodes._view_family_contract(contract, "side_view")

    assert "terminal_pitch" in front["required_parameters"]
    assert "terminal_width" in front["required_parameters"]
    assert "terminal_length" in side["required_parameters"]
    assert "terminal_thickness" in side["required_parameters"]
    assert "front_plate_height" not in front["required_parameters"]


def test_gullwing_composite_projection_reconciles_cross_view_semantic_drift():
    """整页复合图应按主视图尺寸列和右侧详图纠正字段串位。"""
    values = {
        "pitch": ("0.70", (80, 100, 140, 124)),
        "tw": ("0.32", (260, 100, 320, 124)),
        "bl": ("5.20", (170, 440, 230, 464)),
        "bw": ("4.60", (330, 300, 390, 324)),
        "ow": ("6.80", (490, 300, 550, 324)),
        "thickness": ("0.16", (680, 300, 740, 324)),
        "length": ("0.82", (820, 440, 880, 464)),
        "angle": ("0-7", (700, 500, 760, 524)),
        "height": ("1.30", (180, 800, 240, 824)),
        "standoff": ("0.06", (400, 820, 460, 844)),
    }
    tokens = [
        {
            "token_id": token_id,
            "text": text,
            "bbox": list(bbox),
            "confidence": 0.99,
            "unit_context": "mm",
            "value_role": "unknown",
        }
        for token_id, (text, bbox) in values.items()
    ]
    groups = [
        {
            "dimension_id": f"dim_{index:04d}",
            "evidence_type": "dimension_line",
            "bbox": token["bbox"],
            "token_ids": [token["token_id"]],
            "dimension_line_ids": [f"line_{index:04d}"],
            "extension_line_ids": [],
            "orientation": "horizontal",
            "confidence": 0.9,
        }
        for index, token in enumerate(tokens, start=1)
    ]
    result = QwenViewSemanticResult(
        region_id="region_document",
        view_type="composite_outline",
        assignments=[
            SemanticAssignment(canonical_name=name, token_ids=[token_id], line_ids=[], target_feature="qwen", confidence=0.95)
            for name, token_id in (
                ("terminal_pitch", "pitch"),
                ("terminal_width", "tw"),
                ("body_length", "ow"),
                ("overall_width", "bw"),
                ("body_width", "length"),
                ("housing_height", "bw"),
                ("terminal_thickness", "length"),
                ("terminal_length", "thickness"),
                ("mold_draft_angle_top_deg", "angle"),
                ("total_height", "height"),
                ("body_standoff", "standoff"),
            )
        ],
        confidence=0.95,
    )

    reconciled = gullwing_ic.reconcile_view_semantics(
        result,
        {
            "region": {"region_id": "region_document", "bbox": [0, 0, 1000, 1000]},
            "ocr_tokens": tokens,
            "dimension_groups": groups,
        },
    )
    indexed = {item.canonical_name: item for item in reconciled.assignments}

    assert indexed["overall_width"].token_ids == ["ow"]
    assert indexed["body_width"].token_ids == ["bw"]
    assert indexed["terminal_width"].token_ids == ["tw"]
    assert indexed["body_length"].token_ids == ["bl"]
    assert indexed["terminal_thickness"].token_ids == ["thickness"]
    assert indexed["terminal_length"].token_ids == ["length"]
    # The unlabeled angle remains in source OCR but is not assigned to either
    # molded-body side or treated as a lead angle without lead/Gage context.
    assert any(token["token_id"] == "angle" for token in tokens)
    assert "mold_draft_angle_top_deg" not in indexed
    assert "mold_draft_angle_bottom_deg" not in indexed
    assert "lead_angle_deg" not in indexed
    assert indexed["total_height"].token_ids == ["height"]
    assert indexed["body_standoff"].token_ids == ["standoff"]
    # Preserve the explicitly mapped housing-height evidence; deriving A-A1 must
    # not erase a direct assignment from the same source bundle.
    assert indexed["housing_height"].token_ids == ["bw"]


def test_invalid_preserved_housing_height_is_rejected_by_dimension_chain(tmp_path: Path):
    """保留直接候选不代表信任错误值；A 应约束 A2+A1。"""
    base = _synthetic_gullwing_fused()
    values = {"housing_height": 4.6, "total_height": 1.3, "body_standoff": 0.06}
    fused = base.model_copy(update={
        "family_id": "ic/gullwing_ic",
        "parameters": [
            item.model_copy(update={"value": values[item.canonical_name]})
            if item.canonical_name in values else item
            for item in base.parameters
        ],
    })
    chain = asyncio.run(image_nodes.validate_dimension_chain_node({
        "fused_evidence": fused.model_dump(),
        "output_dir": str(tmp_path),
    }))
    assert "total_height!=housing_height+body_standoff" in chain["dimension_chain_result"]["conflicts"]
    gate = asyncio.run(image_nodes.validate_dimensions_node({
        "fused_evidence": chain["fused_evidence"],
        "output_dir": str(tmp_path),
    }))
    assert gate["dimension_gate"]["status"] == "stopped_insufficient_extraction"
    assert "total_height!=housing_height+body_standoff" in gate["dimension_gate"]["conflicting_fields"]


def test_gullwing_top_crop_separates_height_chain_from_terminal_detail():
    """俯视复合裁区不能把总体高度和离板高度误当成端子尺寸。"""
    values = {
        "height": ("1.30", (650, 700, 710, 730)),
        "max": ("MAX", (650, 735, 715, 760)),
        "standoff_max": ("0.18", (80, 760, 140, 790)),
        "standoff_min": ("0.06", (80, 805, 140, 835)),
        "lead_width_max": ("0.32", (470, 800, 530, 830)),
        "lead_width_min": ("0.20", (470, 845, 530, 875)),
    }
    tokens = [
        {
            "token_id": token_id,
            "text": text,
            "bbox": list(bbox),
            "confidence": 0.99,
            "unit_context": "mm",
            "value_role": "unknown",
        }
        for token_id, (text, bbox) in values.items()
    ]
    groups = [
        {
            "dimension_id": f"dim_{index:04d}",
            "evidence_type": "dimension_line",
            "bbox": token["bbox"],
            "token_ids": [token["token_id"]],
            "dimension_line_ids": [f"line_{index:04d}"],
            "extension_line_ids": [],
            "orientation": "vertical",
            "confidence": 0.9,
        }
        for index, token in enumerate(tokens, start=1)
        if token["token_id"] != "max"
    ]
    result = QwenViewSemanticResult(
        region_id="top_with_height_projection",
        view_type="top",
        assignments=[
            SemanticAssignment(
                canonical_name="terminal_length",
                token_ids=["height"],
                line_ids=["line_0001"],
                target_feature="qwen",
                confidence=0.9,
            ),
            SemanticAssignment(
                canonical_name="terminal_thickness",
                token_ids=["standoff_max", "standoff_min"],
                line_ids=["line_0002", "line_0003"],
                target_feature="qwen",
                confidence=0.9,
            ),
        ],
        confidence=0.9,
    )

    reconciled = gullwing_ic.reconcile_view_semantics(
        result,
        {
            "region": {
                "region_id": "top_with_height_projection",
                "bbox": [0, 0, 1000, 1000],
            },
            "ocr_tokens": tokens,
            "dimension_groups": groups,
        },
    )
    indexed = {item.canonical_name: item for item in reconciled.assignments}

    assert indexed["total_height"].token_ids == ["height"]
    assert indexed["body_standoff"].token_ids == [
        "standoff_max", "standoff_min"
    ]
    assert indexed["terminal_width"].token_ids == [
        "lead_width_max", "lead_width_min"
    ]
    assert "terminal_length" not in indexed
    assert "terminal_thickness" not in indexed


def test_dimension_retrieval_keeps_direct_max_context_token():
    """非表格尺寸组必须携带 MAX/MIN 等直接上下文供 Family 校正。"""
    evidence = VisualEvidenceBundle(
        image_sha256="a" * 64,
        image_size=(1000, 1000),
        regions=[ViewRegion(
            region_id="region_top",
            bbox=(0, 0, 1000, 1000),
            area_ratio=1.0,
        )],
        ocr_tokens=[
            OCRToken(
                token_id="height",
                text="1.20",
                bbox=(400, 600, 460, 630),
                confidence=0.99,
                source_region_id="region_top",
                unit_context="mm",
            ),
            OCRToken(
                token_id="max_marker",
                text="MAX",
                bbox=(400, 635, 470, 665),
                confidence=0.99,
                source_region_id="region_top",
                unit_context="mm",
            ),
        ],
        lines=[GeometryLine(
            line_id="line_height",
            type="dimension_line",
            start=(380, 590),
            end=(480, 590),
            source_region_id="region_top",
            confidence=0.9,
        )],
    )
    groups = [DimensionGroup(
        dimension_id="dim_height",
        view_id="region_top",
        evidence_type="dimension_line",
        bbox=(380, 590, 480, 665),
        token_ids=["height"],
        context_token_ids=["max_marker"],
        dimension_line_ids=["line_height"],
        confidence=0.9,
    )]

    payload = retrieve_dimension_group_evidence(
        evidence,
        groups,
        "region_top",
    )

    assert {item["token_id"] for item in payload["ocr_tokens"]} == {
        "height", "max_marker"
    }
    assert payload["dimension_groups"][0]["token_ids"] == [
        "height", "max_marker"
    ]


def test_low_region_coverage_adds_full_page_composite_candidate():
    """局部区域只覆盖页眉时，应保留全页候选供后续识别主体视图。"""
    regions = [
        ViewRegion(region_id="header", bbox=(0, 0, 1000, 80), area_ratio=0.1)
    ]
    tokens = [
        OCRToken(
            token_id=f"ocr_{index:03d}",
            text="TITLE" if index == 0 else f"{index}.00",
            bbox=(20 + index * 60, 20 if index == 0 else 300, 70 + index * 60,
                  50 if index == 0 else 330),
            confidence=0.99,
        )
        for index in range(8)
    ]

    covered = ensure_region_coverage(regions, tokens, (1000, 800))

    assert [item.region_id for item in covered] == ["header", "region_document"]
    assert covered[-1].bbox == (0, 0, 1000, 800)


def test_three_stacked_dimension_values_are_fused_as_explicit_envelope():
    """共线堆叠的 MAX/NOM/MIN 三值应保留为可追溯外包络。"""
    tokens = [
        OCRToken(
            token_id=f"ocr_{index}",
            text=text,
            bbox=(100, 100 + index * 35, 160, 125 + index * 35),
            confidence=0.99,
            source_region_id="top",
            unit_context="mm",
        )
        for index, text in enumerate(("5.10", "5.00", "4.90"), start=1)
    ]
    line = GeometryLine(
        line_id="line_001",
        type="dimension_line",
        start=(80, 90),
        end=(180, 90),
        confidence=0.95,
        source_region_id="top",
    )
    evidence = VisualEvidenceBundle(
        image_sha256="3" * 64,
        image_size=(400, 400),
        ocr_tokens=tokens,
        lines=[line],
    )
    semantics = QwenSemanticResult(
        family_id=gullwing_ic.FAMILY_ID,
        package_type="synthetic",
        identified_views={"top": "top_view"},
        identified_features=list(gullwing_ic.REQUIRED_FEATURES),
        assignments=[SemanticAssignment(
            canonical_name="body_length",
            token_ids=[item.token_id for item in tokens],
            line_ids=[line.line_id],
            target_feature="molded_body",
            confidence=0.95,
        )],
        overall_confidence=0.95,
    )

    fused = fuse_evidence(evidence, semantics)

    assert fused.parameters[0].canonical_name == "body_length"
    assert fused.parameters[0].value == pytest.approx(5.10)
    assert fused.parameters[0].evidence_ids == [
        "ocr_1", "ocr_2", "ocr_3", "line_001"
    ]


def test_bbox_and_evidence_ids_are_preserved_during_fusion():
    evidence = VisualEvidenceBundle(
        image_sha256="a" * 64,
        image_size=(1000, 800),
        ocr_tokens=[OCRToken(
            token_id="ocr_001",
            text=".500(12.70)",
            bbox=(100, 120, 220, 150),
            confidence=0.97,
            source_region_id="region_001",
        )],
        lines=[GeometryLine(
            line_id="line_001",
            type="dimension_line",
            start=(90, 170),
            end=(230, 170),
            confidence=0.91,
        )],
    )
    semantics = QwenSemanticResult(
        family_id="dsub_connector",
        package_type="synthetic",
        identified_features=list(REQUIRED_FEATURES),
        assignments=[SemanticAssignment(
            canonical_name="overall_width",
            token_ids=["ocr_001"],
            line_ids=["line_001"],
            target_feature="mounting_flange",
            confidence=0.96,
        )],
        overall_confidence=0.96,
    )

    fused = fuse_evidence(evidence, semantics)

    parameter = fused.parameters[0]
    assert parameter.value == pytest.approx(12.70)
    assert parameter.token_bboxes == [(100, 120, 220, 150)]
    assert parameter.evidence_ids == ["ocr_001", "line_001"]


def test_unique_table_value_wins_over_ambiguous_view_assignment():
    """同字段冲突时，唯一的明确表格数值应优先于普通视图误关联。"""
    evidence = VisualEvidenceBundle(
        image_sha256="c" * 64,
        image_size=(1000, 800),
        ocr_tokens=[
            OCRToken(
                token_id="ocr_view",
                text="8.20",
                bbox=(100, 120, 180, 150),
                confidence=0.98,
                unit_context="mm",
            ),
            OCRToken(
                token_id="ocr_table",
                text="10.50",
                bbox=(500, 620, 590, 650),
                confidence=0.99,
                unit_context="mm",
                value_role="table_maximum",
            ),
        ],
        lines=[GeometryLine(
            line_id="line_view",
            type="dimension_line",
            start=(90, 170),
            end=(190, 170),
            confidence=0.90,
        )],
    )
    semantics = QwenSemanticResult(
        family_id="gullwing_ic",
        package_type="synthetic",
        identified_features=[],
        assignments=[
            SemanticAssignment(
                canonical_name="body_length",
                token_ids=["ocr_view"],
                line_ids=["line_view"],
                target_feature="molded_body",
                confidence=0.90,
            ),
            SemanticAssignment(
                canonical_name="body_length",
                token_ids=["ocr_table"],
                line_ids=[],
                target_feature="molded_body",
                confidence=0.95,
            ),
        ],
        overall_confidence=0.95,
    )

    fused = fuse_evidence(evidence, semantics)

    assert fused.conflicting_fields == []
    assert fused.parameters[0].value == pytest.approx(10.50)
    assert fused.parameters[0].evidence_ids == ["ocr_table"]


def test_stacked_limit_pair_uses_explicit_maximum_envelope():
    """共享尺寸线的两个明确上下限应融合为最大包络，不得丢失字段。"""
    evidence = VisualEvidenceBundle(
        image_sha256="d" * 64,
        image_size=(1000, 800),
        ocr_tokens=[
            OCRToken(
                token_id="ocr_upper",
                text="0.38",
                bbox=(100, 100, 160, 125),
                confidence=0.99,
                unit_context="mm",
            ),
            OCRToken(
                token_id="ocr_lower",
                text="0.22",
                bbox=(100, 130, 160, 155),
                confidence=0.99,
                unit_context="mm",
            ),
        ],
        lines=[GeometryLine(
            line_id="line_limit",
            type="dimension_line",
            start=(80, 170),
            end=(180, 170),
            confidence=0.90,
        )],
    )
    semantics = QwenSemanticResult(
        family_id="gullwing_ic",
        package_type="synthetic",
        assignments=[SemanticAssignment(
            canonical_name="terminal_width",
            token_ids=["ocr_upper", "ocr_lower"],
            line_ids=["line_limit"],
            target_feature="gullwing_lead",
            confidence=0.95,
        )],
        overall_confidence=0.95,
    )

    fused = fuse_evidence(evidence, semantics)

    assert fused.missing_evidence_assignments == []
    assert fused.parameters[0].value == pytest.approx(0.38)


def test_parameter_without_evidence_id_cannot_enter_feature_ir():
    fused = _synthetic_fused()
    payload = fused.model_dump()
    payload["parameters"][0]["evidence_ids"] = []

    with pytest.raises(Exception):
        plan_from_evidence(
            FusedEvidence.model_validate(payload), source_image_sha256="b" * 64
        )


def test_missing_critical_dimension_stops_before_feature_ir():
    fused = _synthetic_fused()
    fused.parameters = [
        parameter
        for parameter in fused.parameters
        if parameter.canonical_name != "shell_wall_thickness"
    ]

    gate = validate_fused_dimensions(
        fused,
        required_fields=REQUIRED_PARAMETERS,
        required_features=REQUIRED_FEATURES,
    )

    assert gate.passed is False
    assert gate.status == "stopped_insufficient_extraction"
    assert "shell_wall_thickness" in gate.missing_fields


def test_golden_reference_node_is_only_reachable_after_rendering():
    graph = build_image_to_step_graph().get_graph()
    incoming = [
        edge.source for edge in graph.edges if edge.target == "compare_golden_reference"
    ]
    outgoing = [
        edge.target for edge in graph.edges if edge.source == "compare_golden_reference"
    ]

    assert incoming == ["render_views"]
    assert "finalize_result" in outgoing
    assert not {
        "load_image",
        "preprocess_image",
        "extract_visual_evidence",
        "qwen_assign_semantics",
        "fuse_evidence",
        "validate_dimensions",
        "classify_family",
        "create_feature_ir",
        "build_step",
    }.intersection(outgoing)


def test_dsub_builder_is_parameterized_by_feature_ir():
    first = plan_from_evidence(_synthetic_fused(overall_width=50.0), source_image_sha256="c" * 64)
    second = plan_from_evidence(_synthetic_fused(overall_width=56.0), source_image_sha256="d" * 64)

    first_model = build_feature_model(cq, first)
    second_model = build_feature_model(cq, second)

    assert first_model.BoundingBox().xlen == pytest.approx(50.0)
    assert second_model.BoundingBox().xlen == pytest.approx(56.0)
    assert len(first_model.Solids()) >= 20


def test_generated_step_can_be_reimported_by_opencascade(tmp_path: Path):
    feature_ir = plan_from_evidence(_synthetic_fused(), source_image_sha256="e" * 64)
    model = build_feature_model(cq, feature_ir)
    step_path = tmp_path / "synthetic_dsub.step"

    cq.exporters.export(model, str(step_path), exportType="STEP")
    metrics = read_step_metrics(cq, step_path)

    assert step_path.stat().st_size > 0
    assert metrics["solid_count"] >= 20
    assert metrics["face_count"] >= 50


def test_gullwing_image_family_is_registered_and_builder_is_parameterized():
    """图片链必须暴露鸥翼 family，且引脚数量来自 IR 而非固定六脚。"""
    assert "ic/gullwing_ic" in image_family_catalog()
    feature_ir = create_evidence_feature_ir(
        "ic/gullwing_ic",
        _synthetic_gullwing_fused(pin_count=8).model_dump(),
        source_image_sha256="9" * 64,
    )

    model = build_feature_model(cq, feature_ir)

    assert len(model.Solids()) == 9
    assert feature_ir["features"][1]["parameters"]["count"]["value"] == 8.0
    assert feature_ir["expected_geometry"]["solid_count"] == 9


def test_quad_gullwing_family_is_parameterized_and_builds_four_sides():
    """四边 QFP Family 必须按 IR 引脚数生成四侧阵列。"""
    assert quad_gullwing_ic.FAMILY_ID in image_family_catalog()
    feature_ir = create_evidence_feature_ir(
        quad_gullwing_ic.FAMILY_ID,
        _synthetic_quad_gullwing_fused(pin_count=32).model_dump(),
        source_image_sha256="6" * 64,
    )

    model = build_feature_model(cq, feature_ir)

    assert len(model.Solids()) == 33
    assert model.BoundingBox().xlen == pytest.approx(9.0)
    assert model.BoundingBox().ylen == pytest.approx(9.0)
    assert feature_ir["features"][1]["parameters"]["count"]["value"] == 32.0


def test_ufqfpn20_routes_to_generic_no_lead_template_and_builds_terminals():
    classified = reconcile_image_family_classification(
        _classification(quad_gullwing_ic.FAMILY_ID),
        [OCRToken(
            token_id="ocr_title",
            text="UFQFPN20 - 20-lead, 3 x 3 mm, 0.5 mm pitch",
            bbox=(0, 0, 400, 30),
            confidence=0.99,
        )],
    )
    assert classified.family_id == qfn_ufqfpn.FAMILY_ID

    feature_ir = create_evidence_feature_ir(
        qfn_ufqfpn.FAMILY_ID,
        _synthetic_qfn_ufqfpn_fused().model_dump(),
        source_image_sha256="u" * 64,
    )
    model = build_feature_model(cq, feature_ir)

    assert len(model.Solids()) == 21
    assert model.BoundingBox().xlen == pytest.approx(3.0)
    assert model.BoundingBox().ylen == pytest.approx(3.0)
    assert model.BoundingBox().zlen == pytest.approx(0.55)
    assert feature_ir["features"][1]["feature_type"] == "quad_no_lead_terminal_array"


def test_qfn56_routes_by_structure_and_builds_exposed_pad():
    tokens = [
        OCRToken(
            token_id=f"ocr_qfn_{index}",
            text=text,
            bbox=(index * 20, 0, index * 20 + 18, 18),
            confidence=0.99,
        )
        for index, text in enumerate(("D2", "E2", "A3", "b", "L", "56"), start=1)
    ]
    classified = reconcile_image_family_classification(
        _classification(quad_gullwing_ic.FAMILY_ID), tokens
    )
    assert classified.family_id == qfn_ufqfpn.FAMILY_ID

    feature_ir = create_evidence_feature_ir(
        qfn_ufqfpn.FAMILY_ID,
        _synthetic_qfn_ufqfpn_fused(pin_count=56).model_dump(),
        source_image_sha256="q" * 64,
    )
    model = build_feature_model(cq, feature_ir)

    assert len(model.Solids()) == 58
    assert model.BoundingBox().xlen == pytest.approx(7.0)
    assert model.BoundingBox().ylen == pytest.approx(7.0)
    assert model.BoundingBox().zlen == pytest.approx(0.9)
    assert feature_ir["expected_geometry"]["solid_count"] == 58
    assert "exposed_pad_length" in feature_ir["features"][1]["parameters"]


def test_quad_gullwing_derives_pitch_but_requires_direct_terminal_foot_length():
    """Pitch 可由跨距派生；缺少脚底 L 时不能用外伸长度伪造。"""
    fused = _synthetic_quad_gullwing_fused(pin_count=32)
    fused.parameters = [
        item for item in fused.parameters
        if item.canonical_name not in {"terminal_pitch", "terminal_length"}
    ]
    fused.unresolved_fields = ["terminal_pitch", "terminal_length"]

    derived = derive_family_parameters(fused)
    indexed = {item.canonical_name: item for item in derived.parameters}

    assert indexed["terminal_pitch"].value == pytest.approx(0.8)
    assert "terminal_length" not in indexed
    assert "terminal_pitch" not in derived.unresolved_fields
    assert "terminal_length" in derived.unresolved_fields
    assert len(indexed["terminal_pitch"].evidence_ids) >= 2
    with pytest.raises(ValueError, match="terminal_length"):
        create_evidence_feature_ir(
            quad_gullwing_ic.FAMILY_ID,
            fused.model_dump(),
            source_image_sha256="q" * 64,
        )


def test_two_terminal_family_is_registered_and_geometry_uses_feature_ir():
    """新增型号应复用参数化 Family，改变证据尺寸必须改变几何。"""
    assert "resistor/two_terminal_chip" in image_family_catalog()
    assert "capacitor/two_terminal_chip" in image_family_catalog()
    first_ir = create_evidence_feature_ir(
        "resistor/two_terminal_chip",
        _synthetic_two_terminal_fused(length=5.0).model_dump(),
        source_image_sha256="7" * 64,
    )
    second_ir = create_evidence_feature_ir(
        "resistor/two_terminal_chip",
        _synthetic_two_terminal_fused(length=6.0, terminal_length=1.0).model_dump(),
        source_image_sha256="8" * 64,
    )
    capacitor_ir = create_evidence_feature_ir(
        "capacitor/two_terminal_chip",
        _synthetic_two_terminal_fused(length=5.0).model_dump(),
        source_image_sha256="6" * 64,
    )
    first = build_feature_model(cq, first_ir)
    second = build_feature_model(cq, second_ir)
    capacitor = build_feature_model(cq, capacitor_ir)

    assert len(first.Solids()) == 3
    assert len(capacitor.Solids()) == 3
    assert first_ir["family_id"] == "resistor/two_terminal_chip"
    assert first_ir["category_id"] == "resistor"
    assert capacitor_ir["family_id"] == "capacitor/two_terminal_chip"
    assert capacitor_ir["category_id"] == "capacitor"
    assert capacitor.BoundingBox().xlen == pytest.approx(first.BoundingBox().xlen)
    assert first.BoundingBox().xlen == pytest.approx(5.0)
    assert second.BoundingBox().xlen == pytest.approx(6.0)
    assert sorted(solid.BoundingBox().xlen for solid in first.Solids()) == pytest.approx(
        [0.8, 0.8, 3.4]
    )
    assert sorted(solid.BoundingBox().xlen for solid in second.Solids()) == pytest.approx(
        [1.0, 1.0, 4.0]
    )


@pytest.mark.parametrize(
    "family_id", [
        "ic/gullwing_ic",
        "ic/quad_gullwing_ic",
        "resistor/two_terminal_chip",
    ]
)
def test_family_without_reference_never_reads_dsub_golden(
    family_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """未配置本 Family 原厂 STEP 时只要求复核，不能误读 D-SUB Golden。"""
    step_path = tmp_path / "candidate.step"
    step_path.write_bytes(b"candidate")
    previews = {}
    for view in ("isometric", "front", "top", "right"):
        path = tmp_path / f"{view}.png"
        path.write_bytes(b"preview")
        previews[view] = str(path)
    read_calls: list[Path] = []

    def forbidden_read(_cq, path: Path):
        read_calls.append(Path(path))
        raise AssertionError("未配置参考时不应读取任何 STEP")

    monkeypatch.setattr(image_nodes, "read_step_metrics", forbidden_read)
    result = asyncio.run(image_nodes.compare_golden_reference_node({
        "family_id": family_id,
        "feature_ir": {"family_id": family_id, "features": []},
        "artifact_paths": {"step": str(step_path)},
        "preview_paths": previews,
        "verification": {"passed": True},
        "output_dir": str(tmp_path),
    }))

    assert read_calls == []
    assert result["golden_comparison"]["status"] == "review_required"
    assert result["golden_comparison"]["reason"] == "golden_reference_not_configured"


def test_production_chain_and_golden_manifest_contain_no_manual_dimensions():
    manifest_text = (
        PROJECT_ROOT / "tests" / "golden" / "step_drawing_golden_set.json"
    ).read_text(encoding="utf-8")
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "backend" / "agents" / "step").rglob("*.py")
    )
    for forbidden in ("30.81", "24.99", "16.33", "7.90", "2.77"):
        assert forbidden not in manifest_text
        assert forbidden not in production_text
