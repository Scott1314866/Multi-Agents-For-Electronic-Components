from __future__ import annotations

import asyncio
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from backend.agents.step import human_nodes
from backend.agents.step.graph import build_image_to_step_graph
from backend.agents.step.human_nodes import validate_human_answer
from backend.agents.step.persistence import serialize_step_snapshot
from backend.api.v1 import step as step_api
from backend.agents.step.dimension_input import (
    parse_dimension_answer,
    parse_package_pin_count,
)
from backend.agents.step.image_nodes import route_after_dimension_gate
from backend.agents.step.vision.evidence_fusion import validate_fused_dimensions
from backend.agents.step.vision.schemas import FusedEvidence, FusedParameter


def _question(fields: dict[str, object]) -> dict:
    return {"stage": "dimensions", "options": ["provide", "cancel"], "fields": fields}


def test_intent_and_slot_fill_from_chinese_operator_text():
    request = _question({
        "housing_height": {"unit": "mm"},
        "terminal_thickness": {"unit": "mm"},
    })
    answer = parse_dimension_answer(request, {
        "action": "provide",
        "text": "把本体高度设为 1.2 mm，引脚厚度=0.15mm",
    })
    assert answer["values"] == {
        "housing_height": {"value": 1.2, "unit": "mm"},
        "terminal_thickness": {"value": 0.15, "unit": "mm"},
    }


def test_structured_slots_convert_units_and_validate_pin_count():
    answer = parse_dimension_answer(_question({
        "body_length": {}, "nominal_pin_count": {},
    }), {"action": "provide", "values": {
        "body_length": {"value": 0.1, "unit": "inch"},
        "nominal_pin_count": 16,
    }})
    assert answer["values"]["body_length"] == {"value": 2.54, "unit": "mm"}
    assert answer["values"]["nominal_pin_count"] == {"value": 16, "unit": "count"}


@pytest.mark.parametrize("value", [-1, 0, "NaN", 1001])
def test_invalid_operator_dimensions_are_rejected(value):
    with pytest.raises(ValueError):
        parse_dimension_answer(_question({"housing_height": {}}), {
            "action": "provide", "values": {"housing_height": value},
        })


def test_only_asked_slots_can_be_filled():
    with pytest.raises(ValueError, match="未询问"):
        parse_dimension_answer(_question({"housing_height": {}}), {
            "action": "provide", "values": {"terminal_length": 0.7},
        })


def test_human_answer_validator_performs_intent_and_slot_fill():
    result = validate_human_answer(
        _question({"housing_height": {"unit": "mm"}}),
        {"action": "provide", "text": "本体高度为1.2毫米"},
    )
    assert result["values"]["housing_height"] == {"value": 1.2, "unit": "mm"}


def test_human_answer_validator_recognizes_cancel_intent():
    result = validate_human_answer(
        _question({"housing_height": {"unit": "mm"}}),
        {"text": "尺寸无法确认，取消任务"},
    )
    assert result["action"] == "cancel"


def test_dimension_interrupt_is_exposed_as_awaiting_input():
    snapshot = {
        "interrupts": [{"id": "interrupt-dim-1", "value": {
            "stage": "dimensions", "fields": {"housing_height": {"unit": "mm"}},
        }}],
        "values": {}, "next_nodes": ["ask_missing_dimensions"],
    }
    pending = step_api._pending_input(snapshot)
    assert pending["stage"] == "dimensions"
    assert pending["interrupt_id"] == "interrupt-dim-1"
    assert step_api._job_status(snapshot) == "awaiting_input"


@pytest.mark.parametrize(("text", "count"), [
    ("TSSOP-16", 16), ("LQFP64", 64), ("RU-16", 16),
    ("AD7792BRUZ", None), ("AD7792-16", None), ("TSSOP-0", None),
])
def test_package_pin_count_requires_an_explicit_package_suffix(text, count):
    assert parse_package_pin_count(text) == count


def test_operator_values_keep_human_provenance_without_fake_image_coordinates():
    fused = FusedEvidence(
        family_id="ic/gullwing_ic", package_type="TSSOP-16",
        conflicting_fields=["housing_height", "total_height!=housing_height+body_standoff"],
        parameters=[FusedParameter(
            canonical_name="housing_height", value=1.0, unit="mm",
            evidence_ids=["ocr:e1"], token_ids=["ocr:1"], token_bboxes=[(1, 1, 4, 5)],
            target_feature="body", ocr_confidence=0.99, semantic_confidence=0.99,
        )],
    )
    state = {"fused_evidence": fused.model_dump()}
    update = human_nodes._store_operator_dimensions(
        state, {"housing_height": {"value": 1.2, "unit": "mm"}}, "question-1"
    )
    stored = FusedEvidence.model_validate(update["fused_evidence"])
    parameter = next(item for item in stored.parameters if item.canonical_name == "housing_height")
    assert parameter.value == 1.2
    assert parameter.evidence_kind == "human_input"
    assert parameter.evidence_ids == ["human_input:question-1:housing_height"]
    assert parameter.token_ids == []
    assert parameter.token_bboxes == []
    assert not stored.conflicting_fields
    assert update["human_dimension_history"][0]["superseded_candidates"]["housing_height"]


def test_operator_update_invalidates_old_derived_slots_before_recalculation():
    fused = FusedEvidence(
        family_id="ic/gullwing_ic", package_type="TSSOP-16",
        parameters=[FusedParameter(
            canonical_name="pin_span", value=4.5, unit="mm",
            evidence_ids=["ocr:count", "ocr:pitch"], token_ids=["ocr:1"],
            token_bboxes=[(1, 1, 4, 5)], target_feature="derived_chain:pin_span",
            ocr_confidence=0.99, semantic_confidence=0.99, evidence_kind="derived",
        )],
    )
    update = human_nodes._store_operator_dimensions(
        {"fused_evidence": fused.model_dump()},
        {"nominal_pin_count": {"value": 16, "unit": "count"}}, "q-count",
    )
    assert all(
        item["canonical_name"] != "pin_span"
        for item in update["fused_evidence"]["parameters"]
    )


def test_relationship_conflict_maps_to_all_operator_slots():
    fused = FusedEvidence(family_id="ic/gullwing_ic", package_type="TSSOP-16")
    state = {
        "fused_evidence": fused.model_dump(),
        "dimension_gate": {
            "missing_fields": ["terminal_thickness"],
            "conflicting_fields": [
                "pin_span!=pitch*(pins_per_side-1)",
                "total_height!=housing_height+body_standoff",
            ],
            "low_confidence_fields": [],
        },
    }
    assert human_nodes.actionable_dimension_fields(state, conflicts_only=True) == [
        "body_standoff", "housing_height", "nominal_pin_count", "pin_span",
        "terminal_pitch", "total_height",
    ]
    request = human_nodes._dimension_question(state, human_nodes.actionable_dimension_fields(state, conflicts_only=True), "q")
    assert len(request["conflict_details"]) == 2
    assert "引脚中心距关系不一致" in request["conflict_details"][0]


def test_conflict_routes_to_human_before_visual_review(monkeypatch):
    monkeypatch.setattr(
        "backend.agents.step.semantic_review_nodes.semantic_review_regions",
        lambda _state: True,
    )
    state = {
        "fused_evidence": FusedEvidence(family_id="ic/gullwing_ic", package_type="TSSOP-16").model_dump(),
        "dimension_gate": {
            "passed": False,
            "conflicting_fields": ["total_height!=housing_height+body_standoff"],
            "missing_fields": [], "low_confidence_fields": [],
        },
    }
    assert route_after_dimension_gate(state) == "human"


def test_failed_geometry_retry_reuses_thread_and_asks_only_related_dimensions():
    snapshot = {
        "values": {
            "status": "failed",
            "verification": {"passed": False},
            "fused_evidence": {
                "family_id": "ic/gullwing_ic", "package_type": "TSSOP-16",
                "parameters": [
                    {"canonical_name": "body_length", "value": 4.5},
                    {"canonical_name": "pin_span", "value": 4.55},
                    {"canonical_name": "terminal_width", "value": 0.3},
                ],
            },
        },
        "next_nodes": [], "interrupts": [],
    }
    relationship = step_api._failed_dimension_relationship(snapshot)
    assert relationship == "body_length<pin_span+terminal_width"

    async def run():
        graph = build_image_to_step_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "failed-geometry-original-thread"}}
        await graph.aupdate_state(config, {
            "status": "failed", "fused_evidence": snapshot["values"]["fused_evidence"],
            "human_history": [{"stage": "dimensions", "values": {"overall_width": 6.4}}],
        }, as_node="failed")
        await graph.aupdate_state(config, {
            "status": "dimensions_need_confirmation", "errors": [], "result": {},
            "dimension_gate": {"passed": False, "conflicting_fields": [relationship],
                               "missing_fields": [], "low_confidence_fields": []},
        }, as_node="validate_dimensions")
        await graph.ainvoke(None, config)
        resumed = serialize_step_snapshot(await graph.aget_state(config))
        assert resumed["next_nodes"] == ["ask_missing_dimensions"]
        assert resumed["interrupts"][0]["value"]["stage"] == "dimensions"
        assert set(resumed["interrupts"][0]["value"]["fields"]) == {
            "body_length", "pin_span", "terminal_width",
        }
        assert len(resumed["values"]["human_history"]) == 1

    asyncio.run(run())


def test_gullwing_chain_catches_swapped_body_length_before_step_verification(tmp_path):
    from backend.agents.step.image_nodes import validate_dimension_chain_node

    dimensions = {
        "nominal_pin_count": (16, "count"), "terminal_pitch": (0.65, "mm"),
        "pin_span": (4.55, "mm"), "total_height": (1.2, "mm"),
        "housing_height": (1.05, "mm"), "body_standoff": (0.15, "mm"),
        "overall_width": (6.4, "mm"), "body_width": (4.4, "mm"),
        "body_length": (4.5, "mm"), "terminal_width": (0.3, "mm"),
    }

    def state_for(body_length):
        values = {**dimensions, "body_length": (body_length, "mm")}
        fused = FusedEvidence(
            family_id="ic/gullwing_ic", package_type="TSSOP-16",
            parameters=[FusedParameter(
                canonical_name=name, value=value, unit=unit,
                evidence_ids=[f"human_input:q:{name}"], target_feature="operator_confirmed",
                evidence_kind="human_input", ocr_confidence=0.0, semantic_confidence=0.0,
            ) for name, (value, unit) in values.items()],
        )
        return {"fused_evidence": fused.model_dump(), "output_dir": str(tmp_path)}

    bad = asyncio.run(validate_dimension_chain_node(state_for(4.5)))
    assert "body_length<pin_span+terminal_width" in bad["dimension_chain_result"]["conflicts"]
    corrected = asyncio.run(validate_dimension_chain_node(state_for(5.0)))
    assert "body_length<pin_span+terminal_width" not in corrected["dimension_chain_result"]["conflicts"]


def test_human_input_passes_gate_as_operator_evidence_not_ocr_confidence():
    fused = FusedEvidence(
        family_id="ic/gullwing_ic", package_type="TSSOP-16",
        parameters=[FusedParameter(
            canonical_name="housing_height", value=1.2, unit="mm",
            evidence_ids=["human_input:q:housing_height"], target_feature="operator_confirmed",
            ocr_confidence=0.0, semantic_confidence=0.0, evidence_kind="human_input",
        )],
    )
    gate = validate_fused_dimensions(
        fused, required_fields=("housing_height",), required_features=(),
    )
    assert gate.passed


@pytest.mark.asyncio
async def test_dimension_node_prompts_for_missing_fields_and_records_operator(tmp_path, monkeypatch):
    fused = FusedEvidence(family_id="ic/gullwing_ic", package_type="TSSOP-16")
    state = {
        "fused_evidence": fused.model_dump(),
        "dimension_gate": {
            "missing_fields": ["housing_height"],
            "conflicting_fields": [], "low_confidence_fields": [],
        },
        "output_dir": str(tmp_path),
    }
    seen = {}

    def fake_ask(request):
        seen.update(request)
        return {
            "action": "provide", "values": {"housing_height": {"value": 1.2, "unit": "mm"}},
            "user_id": "operator-1", "answered_at": "2026-09-28T00:00:00+00:00",
        }

    monkeypatch.setattr(human_nodes, "_ask", fake_ask)
    result = await human_nodes.ask_missing_dimensions_node(state)
    assert seen["stage"] == "dimensions"
    assert "housing_height" in seen["fields"]
    assert result["status"] == "dimensions_supplied"
    assert result["human_dimension_rounds"] == 1
    assert result["human_dimension_history"][0]["user_id"] == "operator-1"
    assert json.loads((tmp_path / "human_dimension_history.json").read_text())[0]["values"]


@pytest.mark.asyncio
async def test_explicit_tssop_package_name_supplies_pin_count_as_human_evidence(tmp_path, monkeypatch):
    fused = FusedEvidence(family_id="ic/gullwing_ic", package_type="TSSOP-16")
    state = {
        "fused_evidence": fused.model_dump(),
        "package_type_hint": "TSSOP-16",
        "dimension_gate": {
            "missing_fields": ["nominal_pin_count"],
            "conflicting_fields": [], "low_confidence_fields": [],
        },
        "output_dir": str(tmp_path),
    }
    monkeypatch.setattr(human_nodes, "_ask", lambda _request: pytest.fail("count should be derived from explicit package name"))
    result = await human_nodes.ask_missing_dimensions_node(state)
    parameter = next(
        item for item in result["fused_evidence"]["parameters"]
        if item["canonical_name"] == "nominal_pin_count"
    )
    assert parameter["value"] == 16
    assert parameter["evidence_kind"] == "human_input"
    assert result["human_dimension_history"][0]["source"] == "explicit_package_name"


@pytest.mark.asyncio
async def test_package_count_mismatch_is_sent_back_for_operator_resolution(tmp_path, monkeypatch):
    fused = FusedEvidence(
        family_id="ic/gullwing_ic", package_type="TSSOP-20",
        parameters=[FusedParameter(
            canonical_name="nominal_pin_count", value=20, unit="count",
            evidence_ids=["ocr:count"], token_ids=["ocr:1"], token_bboxes=[(1, 1, 4, 5)],
            target_feature="pin_count", ocr_confidence=0.99, semantic_confidence=0.99,
            evidence_kind="identity_text",
        )],
    )
    state = {
        "fused_evidence": fused.model_dump(), "package_type_hint": "TSSOP-16",
        "dimension_gate": {"missing_fields": [], "conflicting_fields": [], "low_confidence_fields": []},
        "output_dir": str(tmp_path),
    }
    seen = {}

    def fake_ask(request):
        seen.update(request)
        return {"action": "provide", "values": {"nominal_pin_count": {"value": 16, "unit": "count"}}}

    monkeypatch.setattr(human_nodes, "_ask", fake_ask)
    result = await human_nodes.ask_missing_dimensions_node(state)
    assert seen["fields"]["nominal_pin_count"]["operator_package_hint"]["pin_count"] == 16
    assert result["fused_evidence"]["parameters"][0]["value"] == 16


@pytest.mark.asyncio
async def test_dimension_question_checkpoint_resume_accepts_natural_language(tmp_path):
    builder = StateGraph(dict)
    builder.add_node("ask_dimensions", human_nodes.ask_missing_dimensions_node)
    builder.add_edge(START, "ask_dimensions")
    builder.add_edge("ask_dimensions", END)
    graph = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "human-dimensions-resume"}}
    state = {
        "fused_evidence": FusedEvidence(
            family_id="ic/gullwing_ic", package_type="TSSOP-16",
        ).model_dump(),
        "dimension_gate": {
            "missing_fields": ["housing_height"],
            "conflicting_fields": [], "low_confidence_fields": [],
        },
        "output_dir": str(tmp_path),
    }
    paused = await graph.ainvoke(state, config=config)
    question = paused["__interrupt__"][0].value
    assert question["stage"] == "dimensions"
    assert "housing_height" in question["fields"]

    resumed = await graph.ainvoke(
        Command(resume={"text": "本体高度=1.2 mm"}), config=config,
    )
    parameter = next(
        item for item in resumed["fused_evidence"]["parameters"]
        if item["canonical_name"] == "housing_height"
    )
    assert parameter["value"] == 1.2
    assert parameter["evidence_kind"] == "human_input"
    snapshot = await graph.aget_state(config)
    assert not snapshot.next


def test_gate_routes_failed_supported_parameters_to_operator(monkeypatch):
    monkeypatch.setattr(
        "backend.agents.step.semantic_review_nodes.semantic_review_regions",
        lambda _state: [],
    )
    state = {
        "fused_evidence": {"family_id": "ic/gullwing_ic"},
        "dimension_gate": {
            "passed": False, "missing_fields": ["housing_height"],
            "conflicting_fields": [], "low_confidence_fields": [],
        },
    }
    assert route_after_dimension_gate(state) == "human"
    # Human supplied values can still conflict with the drawing; keep the
    # checkpoint open so the operator can correct them in a later round.
    assert route_after_dimension_gate({**state, "human_dimension_rounds": 3}) == "human"
