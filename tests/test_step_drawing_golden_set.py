"""二维工程图到 STEP Golden Set 的离线门禁测试。"""

from pathlib import Path

import cadquery as cq

from backend.agents.step.drawing import (
    PROJECT_ROOT,
    build_feature_model,
    create_feature_ir,
    load_golden_cases,
    validate_golden_extraction,
)
from backend.agents.step.graph import build_drawing_to_step_graph
from backend.agents.step.families.registry import resolve_family


def _complete_adc_extraction() -> dict:
    cases = load_golden_cases()
    case = cases["adc_sot23_6lead"]
    symbols = {
        "nominal_pin_count": "n",
        "terminal_pitch": "p",
        "pin_span": "p1",
        "total_height": "A",
        "housing_height": "A2",
        "body_standoff": "A1",
        "overall_width": "E",
        "body_width": "E1",
        "body_length": "D",
        "terminal_length": "L",
        "terminal_thickness": "c",
        "terminal_width": "B",
        "mold_draft_angle_top_deg": "α",
        "mold_draft_angle_bottom_deg": "β",
    }
    dimensions = []
    for name, expected in case["required_dimensions"].items():
        dimensions.append({
            "canonical_name": name,
            "symbol": symbols[name],
            "raw_value": str(expected["nominal"]),
            "nominal_value": expected["nominal"],
            "minimum_value": None,
            "maximum_value": None,
            "tolerance_plus": None,
            "tolerance_minus": None,
            "unit": "deg" if name.endswith("_deg") else "mm",
            "evidence_text": f"表格 {symbols[name]} NOM",
            "evidence_type": "dimension_table",
            "confidence": 0.99,
        })
    return {
        "part_type": case["expected_part_type"],
        "package_type": case["expected_package_type"],
        "identified_views": ["top", "front", "side", "isometric"],
        "identified_features": case["required_features"],
        "dimensions": dimensions,
        "unresolved_required_fields": [],
        "ambiguities": [],
        "overall_confidence": 0.99,
    }


def test_golden_set_paths_exist():
    for case in load_golden_cases().values():
        assert (PROJECT_ROOT / case["drawing_path"]).is_file()
        if case.get("reference_image_path"):
            assert (PROJECT_ROOT / case["reference_image_path"]).is_file()
        if case.get("golden_reference_step_path"):
            assert (PROJECT_ROOT / case["golden_reference_step_path"]).is_file()


def test_complete_adc_extraction_passes_gate():
    case = load_golden_cases()["adc_sot23_6lead"]

    gate = validate_golden_extraction(case, _complete_adc_extraction())

    assert gate["passed"] is True


def test_adc_semantic_aliases_are_normalized_by_feature_planner():
    case = load_golden_cases()["adc_sot23_6lead"]
    extraction = _complete_adc_extraction()
    extraction["part_type"] = "Small Outline Transistor"
    extraction["package_type"] = "SOT-23"
    extraction["identified_features"] = [
        "molded_body",
        "gullwing_lead",
        "two_side_lead_array",
        "mold_draft_angle_top",
        "mold_draft_angle_bottom",
    ]

    gate = validate_golden_extraction(case, extraction)

    assert gate["passed"] is True


def test_adc_missing_one_critical_dimension_stops():
    case = load_golden_cases()["adc_sot23_6lead"]
    extraction = _complete_adc_extraction()
    extraction["dimensions"] = [
        item for item in extraction["dimensions"]
        if item["canonical_name"] != "terminal_thickness"
    ]

    gate = validate_golden_extraction(case, extraction)

    assert gate["passed"] is False
    assert "terminal_thickness" in gate["missing_required_fields"]


def test_rc_reference_image_cannot_fill_unlabelled_dimensions():
    case = load_golden_cases()["rc_three_section_axial"]
    extraction = {
        "part_type": case["expected_part_type"],
        "package_type": case["expected_package_type"],
        "identified_features": case["required_features"],
        "dimensions": [
            {
                "canonical_name": name,
                "raw_value": str(expected["nominal"]),
                "nominal_value": expected["nominal"],
                "unit": "mm",
                "evidence_text": name,
                "evidence_type": "drawing_label",
                "confidence": 0.99,
            }
            for name, expected in case["required_dimensions"].items()
            if expected["nominal"] is not None
        ],
        "unresolved_required_fields": case["known_missing_dimensions"],
        "overall_confidence": 0.95,
    }

    gate = validate_golden_extraction(case, extraction)

    assert gate["passed"] is False
    assert set(case["known_missing_dimensions"]).issubset(
        gate["missing_required_fields"]
    )


def test_adc_feature_ir_builds_body_and_six_independent_leads():
    case = load_golden_cases()["adc_sot23_6lead"]
    feature_ir = create_feature_ir(case, _complete_adc_extraction())

    model = build_feature_model(cq, feature_ir)

    assert len(model.Solids()) == 7
    assert len(model.Faces()) >= 40
    assert feature_ir["family_id"] == "ic/gullwing_ic"
    assert feature_ir["category_id"] == "ic"


def test_family_registry_selects_planner_without_case_condition_chain():
    cases = load_golden_cases()

    assert resolve_family(cases["adc_sot23_6lead"]) is not None


def test_future_family_placeholder_is_registered_but_blocks_building():
    try:
        resolve_family({"family_id": "connector/cn/pin_header"})
    except NotImplementedError:
        pass
    else:
        raise AssertionError("未实现的 Family 必须显式阻断，不能生成占位模型")


def test_feature_modules_do_not_call_cadquery_workplane_directly():
    feature_root = PROJECT_ROOT / "backend" / "agents" / "step" / "features"

    for path in feature_root.glob("*.py"):
        assert ".Workplane(" not in path.read_text(encoding="utf-8"), path.name


def test_drawing_graph_contains_hard_extraction_gate():
    graph = build_drawing_to_step_graph().get_graph()

    assert {
        "extract_drawing",
        "validate_drawing_extraction",
        "stop_insufficient_extraction",
        "create_feature_ir",
        "build_drawing_step",
        "verify_drawing_step",
        "compare_golden_reference",
        "render_drawing_views",
    }.issubset(graph.nodes)
