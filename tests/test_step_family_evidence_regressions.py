"""Focused synthetic regression checks for STEP family evidence contracts."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.agents.step import image_nodes
from backend.agents.step.families import registry
from backend.agents.step.families.ic import gullwing_ic, quad_gullwing_ic
from backend.agents.step.features import gullwing_lead
from backend.agents.step.human_nodes import routing_question
from backend.agents.step.vision.schemas import (
    FusedEvidence,
    FusedParameter,
    QwenViewClassificationResult,
    QwenViewSemanticResult,
    SemanticAssignment,
    ViewRegion,
)
from backend.agents.step.workers import _worker_node


def _parameter(name: str, value: float, *, unit: str | None = None,
               raw_texts: list[str] | None = None,
               token_ids: list[str] | None = None) -> FusedParameter:
    unit = unit or ("count" if name == "nominal_pin_count" else
                    "deg" if name.endswith("_deg") else "mm")
    token_ids = token_ids or [f"tok_{name}"]
    return FusedParameter(
        canonical_name=name,
        value=value,
        unit=unit,
        evidence_ids=[f"ev_{name}_{token_ids[0]}"],
        token_ids=token_ids,
        token_bboxes=[(10, 10, 30, 25) for _ in token_ids],
        target_feature="synthetic_test_evidence",
        ocr_confidence=0.99,
        semantic_confidence=0.99,
        raw_texts=raw_texts or [str(value)],
    )


def _gullwing(*, optional: dict[str, FusedParameter] | None = None) -> FusedEvidence:
    values = {
        "nominal_pin_count": 8.0,
        "terminal_pitch": 1.25,
        "pin_span": 3.75,
        "total_height": 2.4,
        "housing_height": 1.8,
        "body_standoff": 0.6,
        "overall_width": 7.0,
        "body_width": 4.8,
        "body_length": 6.0,
        "terminal_length": 1.1,
        "terminal_thickness": 0.2,
        "terminal_width": 0.45,
    }
    params = [_parameter(name, value) for name, value in values.items()]
    params.extend((optional or {}).values())
    return FusedEvidence(
        family_id=gullwing_ic.FAMILY_ID,
        package_type="TSSOP-8",
        identified_features=list(gullwing_ic.REQUIRED_FEATURES),
        parameters=params,
    )


def _quad(*, omit: set[str] | None = None) -> FusedEvidence:
    values = {
        "nominal_pin_count": 64.0,
        "total_height": 1.6,
        "housing_height": 1.4,
        "body_standoff": 0.05,
        "overall_length": 10.0,
        "overall_width": 10.0,
        "body_length": 8.0,
        "body_width": 8.0,
        "terminal_span": 6.0,
        "terminal_pitch": 0.4,
        "terminal_length": 0.6,
        "lead_projection": 1.0,
        "terminal_width": 0.3,
        "terminal_thickness": 0.1,
    }
    omitted = omit or set()
    params = [_parameter(name, value) for name, value in values.items()
              if name not in omitted]
    return FusedEvidence(
        family_id=quad_gullwing_ic.FAMILY_ID,
        package_type="LQFP64",
        identified_features=list(quad_gullwing_ic.REQUIRED_FEATURES),
        parameters=params,
    )


def test_gullwing_planner_accepts_human_parameter_without_fake_bbox():
    fused = _gullwing()
    original = next(item for item in fused.parameters if item.canonical_name == "body_width")
    human = original.model_copy(update={
        "value": 4.9,
        "evidence_ids": ["human_input:operator-question:body_width"],
        "token_ids": [],
        "token_bboxes": [],
        "line_ids": [],
        "target_feature": "operator_confirmed:body_width",
        "ocr_confidence": 0.0,
        "semantic_confidence": 0.0,
        "raw_texts": ["operator supplied body_width=4.9 mm"],
        "evidence_kind": "human_input",
    })
    fused = fused.model_copy(update={
        "parameters": [human if item.canonical_name == "body_width" else item for item in fused.parameters]
    })
    feature_ir = gullwing_ic.plan_from_evidence(fused, source_image_sha256="a" * 64)
    assert "human_input:operator-question:body_width" in feature_ir["source_dimensions"]["body_width"]["evidence_ids"]


def _indexed(fused: FusedEvidence) -> dict[str, FusedParameter]:
    return {item.canonical_name: item for item in fused.parameters}


def test_gullwing_explicit_pin_span_is_preserved_and_odd_count_is_not_derived():
    fused = _gullwing()
    indexed = _indexed(fused)
    indexed["pin_span"] = _parameter("pin_span", 9.99)
    explicit = fused.model_copy(update={"parameters": list(indexed.values())})
    derived = _indexed(registry.derive_family_parameters(explicit))
    assert derived["pin_span"].value == pytest.approx(9.99)

    odd = FusedEvidence(
        family_id=gullwing_ic.FAMILY_ID,
        package_type="SOIC-15",
        parameters=[_parameter("nominal_pin_count", 15),
                    _parameter("terminal_pitch", 0.65)],
    )
    assert "pin_span" not in _indexed(registry.derive_family_parameters(odd))


def test_quad_keeps_explicit_height_and_derives_projection_separately_from_foot():
    fused = _quad()
    params = _indexed(fused)
    params["housing_height"] = _parameter("housing_height", 1.4)
    params["terminal_length"] = _parameter("terminal_length", 0.6)
    derived = _indexed(registry.derive_family_parameters(
        fused.model_copy(update={"parameters": list(params.values())})
    ))
    assert derived["housing_height"].value == pytest.approx(1.4)
    assert derived["terminal_length"].value == pytest.approx(0.6)
    assert derived["lead_projection"].value == pytest.approx(1.0)


def test_quad_missing_foot_length_is_not_synthesized_from_projection():
    fused = registry.derive_family_parameters(_quad(omit={"terminal_length"}))
    with pytest.raises(ValueError, match="terminal_length"):
        quad_gullwing_ic.plan_from_evidence(fused, source_image_sha256="a" * 64)


def test_quad_expected_height_uses_body_and_lead_profile_under_A_limit(monkeypatch):
    fused = _quad()
    ir = quad_gullwing_ic.plan_from_evidence(fused, source_image_sha256="b" * 64)
    assert ir["source_dimensions"]["housing_height"]["value"] == pytest.approx(1.4)
    assert ir["expected_geometry"]["height_upper_bound"] == pytest.approx(1.6)
    assert ir["expected_geometry"]["bounding_box"]["zmax"] == pytest.approx(1.45)

    monkeypatch.setattr(gullwing_lead, "_profile", lambda _params, side: [(0.0, 1.55)])
    taller_lead = quad_gullwing_ic.plan_from_evidence(
        fused, source_image_sha256="b" * 64
    )
    assert taller_lead["expected_geometry"]["bounding_box"]["zmax"] == pytest.approx(1.55)

    monkeypatch.setattr(gullwing_lead, "_profile", lambda _params, side: [(0.0, 1.61)])
    with pytest.raises(ValueError, match="A 包络上限"):
        quad_gullwing_ic.plan_from_evidence(fused, source_image_sha256="b" * 64)


def test_gage_plane_angle_is_not_promoted_to_mold_draft_angle():
    result = QwenViewSemanticResult(
        region_id="detail",
        view_type="detail",
        assignments=[SemanticAssignment(
            canonical_name="mold_draft_angle_top_deg",
            token_ids=["angle"],
            target_feature="qwen",
            confidence=0.96,
        )],
        confidence=0.96,
    )
    reconciled = gullwing_ic.reconcile_view_semantics(result, {
        "region": {"region_id": "detail", "bbox": [0, 0, 200, 200]},
        "ocr_tokens": [
            {"token_id": "angle", "text": "8°", "bbox": [10, 10, 25, 20], "confidence": 0.99},
            {"token_id": "gage", "text": "Gage Plane", "bbox": [30, 10, 80, 20], "confidence": 0.99},
        ],
        "dimension_groups": [{
            "dimension_id": "angle_group", "evidence_type": "dimension_line",
            "bbox": [10, 10, 25, 20], "token_ids": ["angle"],
            "context_token_ids": ["gage"], "dimension_line_ids": [],
            "extension_line_ids": [], "orientation": "unknown", "confidence": 0.9,
        }],
    })
    names = [item.canonical_name for item in reconciled.assignments]
    assert "lead_angle_deg" in names
    assert "mold_draft_angle_top_deg" not in names


def test_missing_or_copied_mold_angles_use_reviewable_box():
    one_angle = _gullwing(optional={
        "mold_draft_angle_top_deg": _parameter(
            "mold_draft_angle_top_deg", 4.0, raw_texts=["α", "4°"], token_ids=["alpha"]
        )
    })
    box_ir = gullwing_ic.plan_from_evidence(one_angle, source_image_sha256="c" * 64)
    assert box_ir["features"][0]["feature_type"] == "molded_body_box"
    assert any("需人工审核" in item for item in box_ir["assumptions"])

    # The same evidence token cannot be duplicated into top and bottom fields.
    copied = _gullwing(optional={
        "mold_draft_angle_top_deg": _parameter(
            "mold_draft_angle_top_deg", 4.0, raw_texts=["α", "4°"], token_ids=["shared"]
        ),
        "mold_draft_angle_bottom_deg": _parameter(
            "mold_draft_angle_bottom_deg", 4.0, raw_texts=["β", "4°"], token_ids=["shared"]
        ),
    })
    copied_ir = gullwing_ic.plan_from_evidence(copied, source_image_sha256="c" * 64)
    assert copied_ir["features"][0]["feature_type"] == "molded_body_box"


def test_two_independent_explicit_mold_angles_enable_loft_and_old_golden_plan_stays():
    optional = {
        "mold_draft_angle_top_deg": _parameter(
            "mold_draft_angle_top_deg", 4.0, raw_texts=["α", "4°"], token_ids=["alpha"]
        ),
        "mold_draft_angle_bottom_deg": _parameter(
            "mold_draft_angle_bottom_deg", 5.0, raw_texts=["β", "5°"], token_ids=["beta"]
        ),
    }
    ir = gullwing_ic.plan_from_evidence(
        _gullwing(optional=optional), source_image_sha256="d" * 64
    )
    assert ir["features"][0]["feature_type"] == "drafted_body_loft"

    extraction = {"dimensions": [
        {"canonical_name": name, "nominal_value": value}
        for name, value in {
            "nominal_pin_count": 8, "terminal_pitch": 1.25, "pin_span": 3.75,
            "total_height": 2.4, "housing_height": 1.8, "body_standoff": 0.6,
            "overall_width": 7.0, "body_width": 4.8, "body_length": 6.0,
            "terminal_length": 1.1, "terminal_thickness": 0.2,
            "terminal_width": 0.45, "mold_draft_angle_top_deg": 4,
            "mold_draft_angle_bottom_deg": 5,
        }.items()
    ]}
    old_plan = gullwing_ic.plan({
        "case_id": "synthetic-golden-compatibility",
        "expected_part_type": "synthetic integrated circuit",
        "expected_package_type": "SOIC-8",
        "expected_topology": {"minimum_face_count": 36},
        "modeling_assumptions": [],
    }, extraction)
    assert old_plan["features"][0]["feature_type"] == "drafted_body_loft"


def test_family_optional_contract_and_assumptions_set_needs_review(tmp_path):
    contract = registry.image_family_catalog()[gullwing_ic.FAMILY_ID]
    assert set(contract["optional_parameters"]) == set(gullwing_ic.OPTIONAL_PARAMETERS)
    state = {
        "family_id": gullwing_ic.FAMILY_ID,
        "fused_evidence": _gullwing().model_dump(),
        "image_meta": {"sha256": "e" * 64},
        "output_dir": str(tmp_path),
        "artifact_paths": {},
    }
    result = asyncio.run(image_nodes.create_feature_ir_node(state))
    assert result["status"] == "feature_ir_created"
    assert result["needs_review"] is True
    assert "feature_ir" in result["artifact_paths"]


def test_numeric_view_fallback_never_overrides_footprint_or_electrical(monkeypatch, tmp_path):
    regions = [
        ViewRegion(region_id="foot", bbox=(0, 0, 100, 100), area_ratio=0.5),
        ViewRegion(region_id="electrical", bbox=(100, 0, 200, 100), area_ratio=0.5),
    ]
    monkeypatch.setattr(image_nodes, "split_view_regions", lambda _path: regions)
    monkeypatch.setattr(image_nodes, "ensure_region_coverage", lambda current, *_: current)
    monkeypatch.setattr(image_nodes, "assign_evidence_to_views", lambda tokens, lines, _regions: (tokens, lines))
    monkeypatch.setattr(image_nodes, "link_tokens_to_lines", lambda *_: [])
    monkeypatch.setattr(image_nodes, "detect_dimension_table_regions", lambda *_: ["foot"])
    monkeypatch.setattr(image_nodes, "render_region_overview", lambda *_: tmp_path / "overview.png")

    async def invoke(*_args, **_kwargs):
        return json.dumps({
            "family_id": gullwing_ic.FAMILY_ID,
            "package_type": "TSSOP-8",
            "identified_views": {"foot": "recommended_footprint", "electrical": "electrical"},
            "overall_confidence": 0.9,
        })
    monkeypatch.setattr(image_nodes, "_invoke_qwen", invoke)
    state = {
        "preprocessing": {"binary_path": str(tmp_path / "binary.png"), "width": 200, "height": 100},
        "image_meta": {"sha256": "f" * 64},
        "output_dir": str(tmp_path),
        "all_ocr_tokens": [
            {"token_id": f"n{i}", "text": f"{i + 1}.25", "bbox": (i * 2, 2, i * 2 + 1, 8), "confidence": 0.99}
            for i in range(8)
        ],
        "all_line_segments": [
            {"line_id": f"l{i}", "type": "outline", "start": (0, i), "end": (30, i), "confidence": 0.99}
            for i in range(8)
        ],
        "all_arrows": [],
        "artifact_paths": {},
    }
    result = asyncio.run(image_nodes.detect_views_node(state))
    assert result["status"] == "views_detected"
    assert result["view_classification"]["identified_views"] == {
        "foot": "recommended_footprint", "electrical": "electrical"
    }
    assert result["pending_view_ids"] == []


def test_worker_node_keeps_event_loop_responsive_and_waits_for_sync_work_on_cancel():
    started = threading.Event()
    release = threading.Event()

    def blocking_node():
        started.set()
        release.wait(timeout=3)
        return "finished"

    wrapped = _worker_node(ThreadPoolExecutor(max_workers=1))(blocking_node)
    assert inspect.iscoroutinefunction(wrapped)
    for node in (image_nodes.load_image_node, image_nodes.preprocess_image_node,
                 image_nodes.extract_all_evidence_node, image_nodes.build_dimension_groups_node,
                 image_nodes.build_step_node, image_nodes.verify_step_node):
        assert inspect.iscoroutinefunction(node)

    async def run_case():
        task = asyncio.create_task(wrapped())
        assert await asyncio.to_thread(started.wait, 1)
        # The synchronous function is waiting in its worker thread, while this
        # coroutine still gets event-loop time to run.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.25)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), "cancel must await the native worker before releasing its job lock"
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), "repeated cancellation must still retain the job lock"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    try:
        asyncio.run(run_case())
    finally:
        release.set()


def test_worker_runtime_error_after_cancel_does_not_mask_cancellation():
    started = threading.Event()
    release = threading.Event()

    def blocking_node():
        started.set()
        release.wait(timeout=3)
        raise RuntimeError("synthetic native worker failure")

    wrapped = _worker_node(ThreadPoolExecutor(max_workers=1))(blocking_node)

    async def run_case():
        task = asyncio.create_task(wrapped())
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    try:
        asyncio.run(run_case())
    finally:
        release.set()


@pytest.mark.parametrize(
    ("family_id", "package_type", "ocr_text"),
    [
        ("ic/quad_gullwing_ic", "LQFP64", "LQFP64"),
        ("ic/qfn_ufqfpn", "QFN-16", "QFN-16"),
    ],
)
def test_consistent_qfp_qfn_signature_adds_no_routing_question(
    family_id: str, package_type: str, ocr_text: str,
):
    classification = QwenViewClassificationResult(
        family_id=family_id,
        category_id="ic",
        package_type=package_type,
        identified_views={"region_outline": "composite_outline"},
        identified_features=[],
        unresolved_fields=[],
        ambiguities=[],
        overall_confidence=0.95,
    )
    tokens = [{"text": ocr_text, "confidence": 0.99}]
    reconciled = registry.reconcile_image_family_classification(classification, tokens)
    assert reconciled.ambiguities == []
    assert routing_question({
        "view_classification": reconciled.model_dump(),
        "all_ocr_tokens": tokens,
        "package_type_hint": "",
    }) is None


def test_matching_qfp_signature_preserves_existing_mixed_view_ambiguity():
    existing = (
        "The input region contains both the package outline and the recommended footprint."
    )
    classification = QwenViewClassificationResult(
        family_id="ic/quad_gullwing_ic",
        category_id="ic",
        package_type="LQFP64",
        identified_views={"region_001": "composite_outline"},
        ambiguities=[existing],
        overall_confidence=0.95,
    )
    tokens = [{"text": "LQFP64", "confidence": 0.99}]
    reconciled = registry.reconcile_image_family_classification(classification, tokens)
    assert reconciled.ambiguities == [existing]
    question = routing_question({
        "view_classification": reconciled.model_dump(),
        "all_ocr_tokens": tokens,
        "package_type_hint": "",
    })
    assert question is not None
    assert question["reasons"]


@pytest.mark.parametrize(
    ("family_id", "package_type"),
    [
        ("ic/gullwing_ic", "SOP16"),
        ("ic/quad_gullwing_ic", "SOP16"),
    ],
)
def test_qfp_signature_with_wrong_family_or_package_still_requires_routing(
    family_id: str, package_type: str,
):
    classification = QwenViewClassificationResult(
        family_id=family_id,
        category_id="ic",
        package_type=package_type,
        identified_views={"region_001": "composite_outline"},
        ambiguities=[],
        overall_confidence=0.95,
    )
    tokens = [{"text": "LQFP64", "confidence": 0.99}]
    reconciled = registry.reconcile_image_family_classification(classification, tokens)
    question = routing_question({
        "view_classification": reconciled.model_dump(),
        "all_ocr_tokens": tokens,
        "package_type_hint": "",
    })
    assert question is not None
    assert question["reasons"]
