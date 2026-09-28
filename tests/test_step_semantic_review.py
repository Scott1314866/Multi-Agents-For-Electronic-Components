"""隔离回归：尺寸语义冲突复核只重绑已有、同视图证据。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.agents.step.image_nodes import route_after_dimension_gate
from backend.agents.step.state import ImageToStepState
from backend.agents.step.semantic_review_nodes import (
    MAX_SEMANTIC_REVIEW_ATTEMPTS,
    prepare_semantic_review_node,
    review_semantics_node,
    route_after_semantic_review,
    semantic_review_regions,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from backend.agents.step.vision.schemas import (
    GeometryLine,
    OCRToken,
    QwenSemanticResult,
    QwenViewSemanticResult,
    SemanticAssignment,
    VisualEvidenceBundle,
)
from backend.agents.step.vision.semantic_review import (
    find_semantic_collisions,
    validate_review_candidate,
)


def _token(token_id, text, region="side", *, role="unknown"):
    return OCRToken(
        token_id=token_id, text=text, bbox=(10, 10, 30, 20), confidence=0.99,
        source_region_id=region, unit_context="mm", value_role=role,
    )


def _line(line_id, region="side"):
    return GeometryLine(
        line_id=line_id, type="dimension_line", start=(5, 5), end=(35, 5),
        confidence=0.99, source_region_id=region,
    )


def _assignment(name, token_ids, line_ids=("side_line",)):
    return SemanticAssignment(
        canonical_name=name, token_ids=list(token_ids), line_ids=list(line_ids),
        target_feature="body", confidence=0.95,
    )


def _evidence():
    return VisualEvidenceBundle(
        image_sha256="a" * 64, image_size=(500, 400),
        ocr_tokens=[
            _token("shared", "0.60"), _token("same_value_other_token", "0.60"),
            _token("label", "A1"), _token("front_value", "1.20", "front"),
        ],
        lines=[_line("side_line"), _line("front_line", "front")],
    )


def test_same_numeric_token_assigned_to_incompatible_quantities_is_collision():
    semantics = QwenSemanticResult(
        family_id="ic/gullwing_ic", package_type="SOP",
        overall_confidence=0.9,
        assignments=[
            _assignment("body_standoff", ["shared"]),
            _assignment("terminal_length", ["shared"]),
        ],
    )
    assert find_semantic_collisions(_evidence(), semantics) == [{
        "token_ids": ["shared"],
        "names": ["body_standoff", "terminal_length"],
        "region_ids": ["side"],
    }]


def test_equal_values_from_distinct_tokens_and_non_gullwing_are_not_collisions():
    evidence = _evidence()
    independent = QwenSemanticResult(
        family_id="ic/gullwing_ic", package_type="SOP", overall_confidence=0.9,
        assignments=[
            _assignment("body_standoff", ["shared"]),
            _assignment("terminal_length", ["same_value_other_token"]),
            _assignment("nominal_pin_count", ["label"]),
        ],
    )
    assert find_semantic_collisions(evidence, independent) == []
    other_family = independent.model_copy(update={"family_id": "resistor/chip"})
    other_family.assignments[1].token_ids = ["shared"]
    assert find_semantic_collisions(evidence, other_family) == []


def test_nonexclusive_pair_and_repeated_same_canonical_assignment_are_not_collisions():
    semantics = QwenSemanticResult(
        family_id="ic/gullwing_ic", package_type="SOP", overall_confidence=0.9,
        assignments=[
            _assignment("body_standoff", ["shared"]),
            _assignment("body_standoff", ["shared"]),
            _assignment("body_width", ["shared"]),
        ],
    )
    assert find_semantic_collisions(_evidence(), semantics) == []


def test_review_candidate_requires_exact_metadata_contract_and_current_view_evidence():
    payload = {
        "ocr_tokens": [_token("shared", "0.60").model_dump()],
        "lines": [_line("side_line").model_dump()],
    }
    allowed = {"body_standoff", "terminal_length"}
    valid = QwenViewSemanticResult(
        region_id="side", view_type="side",
        assignments=[_assignment("body_standoff", ["shared"])], confidence=0.9,
    )
    assert validate_review_candidate(valid, payload, allowed, "side", "side") == []

    cases = [
        (valid.model_copy(update={"region_id": "front"}), "region_id mismatch"),
        (valid.model_copy(update={"view_type": "top"}), "view_type mismatch"),
        (QwenViewSemanticResult(
            region_id="side", view_type="side", unresolved_fields=["made_up"], confidence=0.9,
        ), "unresolved_fields outside contract"),
        (QwenViewSemanticResult(
            region_id="side", view_type="side",
            assignments=[_assignment("made_up", ["shared"])], confidence=0.9,
        ), "canonical_name outside contract"),
        (QwenViewSemanticResult(
            region_id="side", view_type="side",
            assignments=[_assignment("body_standoff", ["invented"])], confidence=0.9,
        ), "token_ids outside current payload"),
        (QwenViewSemanticResult(
            region_id="side", view_type="side",
            assignments=[_assignment("body_standoff", ["shared"], ["invented_line"])], confidence=0.9,
        ), "line_ids outside current payload"),
    ]
    for candidate, expected in cases:
        assert any(expected in issue for issue in validate_review_candidate(
            candidate, payload, allowed, "side", "side",
        ))


def test_review_candidate_rejects_cross_view_and_unmeasured_dimensions():
    token = _token("shared", "0.60").model_dump() | {"source_region_id": "front"}
    line = _line("side_line").model_dump()
    payload = {"ocr_tokens": [token], "lines": [line]}
    candidate = QwenViewSemanticResult(
        region_id="side", view_type="side",
        assignments=[_assignment("body_standoff", ["shared"])], confidence=0.9,
    )
    issues = validate_review_candidate(candidate, payload, {"body_standoff"}, "side", "side")
    assert any("cross-view evidence" in issue for issue in issues)

    no_value = {"ocr_tokens": [_token("label", "A1").model_dump()], "lines": [line]}
    candidate.assignments[0].token_ids = ["label"]
    issues = validate_review_candidate(candidate, no_value, {"body_standoff"}, "side", "side")
    assert any("no numeric value" in issue for issue in issues)


def test_explicit_count_and_single_table_cell_can_be_valid_without_dimension_line():
    count_token = _token("count", "28 PINS", role="unknown").model_dump()
    count_candidate = QwenViewSemanticResult(
        region_id="side", view_type="side",
        assignments=[_assignment("nominal_pin_count", ["count"], ())], confidence=0.9,
    )
    assert validate_review_candidate(
        count_candidate, {"ocr_tokens": [count_token], "lines": []},
        {"nominal_pin_count"}, "side", "side",
    ) == []

    cell = _token("cell", "10.5", role="table_maximum").model_dump()
    group = {
        "dimension_id": "row_28", "view_id": "side", "evidence_type": "table_row",
        "token_ids": ["cell", "row_label"], "row_label_token_ids": ["row_label"],
        "table_columns": {"max": "cell"}, "dimension_line_ids": [], "extension_line_ids": [],
    }
    table_candidate = QwenViewSemanticResult(
        region_id="side", view_type="side",
        assignments=[_assignment("body_length", ["cell"], ())], confidence=0.9,
    )
    assert validate_review_candidate(
        table_candidate, {"ocr_tokens": [cell], "lines": [], "dimension_groups": [group]},
        {"body_length"}, "side", "side",
    ) == []


def _node_state(tmp_path):
    front = {
        "region_id": "front", "view_type": "front", "confidence": 0.95,
        "assignments": [
            _assignment("body_standoff", ["front_value"], ["front_line"]).model_dump(),
        ],
        "unresolved_fields": [], "ambiguities": [], "identified_features": [],
    }
    side = {
        "region_id": "side", "view_type": "side", "confidence": 0.7,
        "assignments": [
            _assignment("body_standoff", ["shared"]).model_dump(),
            _assignment("terminal_length", ["shared"]).model_dump(),
        ],
        "unresolved_fields": [], "ambiguities": [], "identified_features": [],
    }
    return {
        "output_dir": str(tmp_path), "artifact_paths": {},
        "visual_evidence": _evidence().model_dump(),
        "view_classification": {
            "family_id": "ic/gullwing_ic",
            "identified_views": {"front": "front", "side": "side"},
        },
        "per_view_semantics": [front, side],
        "semantic_collisions": [{
            "token_ids": ["shared"], "names": ["body_standoff", "terminal_length"],
            "region_ids": ["side"],
        }],
        "semantic_review_attempts": 0,
        "semantic_review_history": [],
        "dimension_gate": {
            "passed": False, "status": "stopped_insufficient_extraction",
            "conflicting_fields": ["body_standoff", "terminal_length"],
            "missing_fields": [], "low_confidence_fields": [],
        },
        "dimension_chain_result": {"passed": False},
    }


def test_review_node_only_calls_source_view_and_preserves_correct_other_view(monkeypatch, tmp_path):
    import backend.agents.step.image_nodes as image_nodes

    state = _node_state(tmp_path)
    front_before = json.loads(json.dumps(state["per_view_semantics"][0]))
    assert semantic_review_regions(state) == ["side"]
    state.update(asyncio.run(prepare_semantic_review_node(state)))
    calls = []

    async def retrieve(_state):
        return {
            "status": "ok", "current_prompt_evidence": {"dimension_groups": []},
            "current_view_image_path": str(tmp_path / "side.png"),
        }

    async def invoke(_system, prompt, *, image_path):
        request = json.loads(prompt)
        calls.append((request, image_path))
        return json.dumps({
            "region_id": "side", "view_type": "side", "confidence": 0.9,
            "assignments": [{
                "canonical_name": "terminal_length", "token_ids": ["shared"],
                "line_ids": ["side_line"], "target_feature": "lead", "confidence": 0.9,
            }],
            "unresolved_fields": [], "ambiguities": [], "identified_features": [],
        })

    monkeypatch.setattr(image_nodes, "retrieve_view_evidence_node", retrieve)
    monkeypatch.setattr(image_nodes, "_invoke_qwen", invoke)
    result = asyncio.run(review_semantics_node(state))
    assert len(calls) == 1
    assert calls[0][0]["region_id"] == "side"
    assert calls[0][0]["current_view_evidence"]["coordinate_system"].startswith("原预处理图坐标")
    assert result["per_view_semantics"][0] == front_before
    reviewed_side = result["per_view_semantics"][1]
    assert [assignment["canonical_name"] for assignment in reviewed_side["assignments"]] == ["terminal_length"]
    assert result["semantic_review_attempts"] == 1
    assert result["semantic_review_history"][0]["regions"][0]["accepted"] is True


def test_invalid_review_candidate_is_audited_and_not_injected(monkeypatch, tmp_path):
    import backend.agents.step.image_nodes as image_nodes

    state = _node_state(tmp_path)
    original_side = json.loads(json.dumps(state["per_view_semantics"][1]))
    state.update(asyncio.run(prepare_semantic_review_node(state)))

    async def retrieve(_state):
        return {
            "status": "ok", "current_prompt_evidence": {"dimension_groups": []},
            "current_view_image_path": str(tmp_path / "side.png"),
        }

    async def invoke(*_args, **_kwargs):
        return json.dumps({
            "region_id": "side", "view_type": "side", "confidence": 0.9,
            "assignments": [{
                "canonical_name": "terminal_length", "token_ids": ["foreign_token"],
                "line_ids": ["side_line"], "target_feature": "lead", "confidence": 0.9,
            }],
            "unresolved_fields": [], "ambiguities": [], "identified_features": [],
        })

    monkeypatch.setattr(image_nodes, "retrieve_view_evidence_node", retrieve)
    monkeypatch.setattr(image_nodes, "_invoke_qwen", invoke)
    result = asyncio.run(review_semantics_node(state))
    assert result["per_view_semantics"][1] == original_side
    record = result["semantic_review_history"][0]["regions"][0]
    assert record["accepted"] is False
    assert any("outside current payload" in issue for issue in record["issues"])


def test_parse_failure_keeps_raw_response_and_original_candidate_in_audit(monkeypatch, tmp_path):
    import backend.agents.step.image_nodes as image_nodes

    state = _node_state(tmp_path)
    original_side = json.loads(json.dumps(state["per_view_semantics"][1]))
    state.update(asyncio.run(prepare_semantic_review_node(state)))

    async def retrieve(_state):
        return {
            "status": "ok", "current_prompt_evidence": {"dimension_groups": []},
            "current_view_image_path": str(tmp_path / "side.png"),
        }

    async def invoke(*_args, **_kwargs):
        return "not valid structured output"

    monkeypatch.setattr(image_nodes, "retrieve_view_evidence_node", retrieve)
    monkeypatch.setattr(image_nodes, "_invoke_qwen", invoke)
    result = asyncio.run(review_semantics_node(state))
    assert result["per_view_semantics"][1] == original_side
    record = result["semantic_review_history"][0]["regions"][0]
    assert record["accepted"] is False
    request_artifact = result["artifact_paths"][f"semantic_review_request_{record['request_id']}"]
    audit = json.loads(open(request_artifact, encoding="utf-8").read())
    assert audit["raw_response"] == "not valid structured output"
    assert audit["before"] == original_side
    assert any("复核未完成" in issue for issue in audit["issues"])


def test_audit_write_error_does_not_mask_model_cancellation(monkeypatch, tmp_path):
    import backend.agents.step.image_nodes as image_nodes

    state = _node_state(tmp_path)
    state.update(asyncio.run(prepare_semantic_review_node(state)))
    original_write = image_nodes._write_json
    request_writes = 0

    def write_json(path, payload):
        nonlocal request_writes
        if Path(path).stem.startswith("semantic_review_1_"):
            request_writes += 1
            if request_writes == 2:  # finally audit after model invocation
                raise OSError("synthetic audit disk failure")
        return original_write(path, payload)

    async def retrieve(_state):
        return {
            "status": "ok", "current_prompt_evidence": {"dimension_groups": []},
            "current_view_image_path": str(tmp_path / "side.png"),
        }

    async def cancel_model(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(image_nodes, "_write_json", write_json)
    monkeypatch.setattr(image_nodes, "retrieve_view_evidence_node", retrieve)
    monkeypatch.setattr(image_nodes, "_invoke_qwen", cancel_model)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(review_semantics_node(state))
    assert request_writes == 2


def test_memory_saver_checkpoints_each_reviewed_view_and_resume_skips_it(monkeypatch, tmp_path):
    import backend.agents.step.image_nodes as image_nodes

    state = _node_state(tmp_path)
    # 两个区域均待复核，以checkpoint边界验证已完成区域不会重跑。
    state["semantic_collisions"] = [{
        "token_ids": ["shared"],
        "names": ["body_standoff", "terminal_length"],
        "region_ids": ["front", "side"],
    }]
    saver = MemorySaver()
    builder = StateGraph(ImageToStepState)
    builder.add_node("prepare", prepare_semantic_review_node)
    builder.add_node("review", review_semantics_node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "review")
    builder.add_conditional_edges("review", route_after_semantic_review, {"next": "review", "merge": END})
    graph = builder.compile(checkpointer=saver, interrupt_after=["review"])
    config = {"configurable": {"thread_id": "semantic-review-resume"}}
    calls = []

    async def retrieve(review_state):
        region_id = review_state["pending_view_ids"][0]
        return {
            "status": "ok", "current_prompt_evidence": {"dimension_groups": []},
            "current_view_image_path": str(tmp_path / f"{region_id}.png"),
        }

    async def invoke(_system, prompt, *, image_path):
        request = json.loads(prompt)
        region_id = request["region_id"]
        calls.append(region_id)
        token, line, field, feature = (
            ("front_value", "front_line", "body_standoff", "body")
            if region_id == "front"
            else ("shared", "side_line", "terminal_length", "lead")
        )
        return json.dumps({
            "region_id": region_id, "view_type": region_id, "confidence": 0.9,
            "assignments": [{
                "canonical_name": field, "token_ids": [token], "line_ids": [line],
                "target_feature": feature, "confidence": 0.9,
            }],
            "unresolved_fields": [], "ambiguities": [], "identified_features": [],
        })

    monkeypatch.setattr(image_nodes, "retrieve_view_evidence_node", retrieve)
    monkeypatch.setattr(image_nodes, "_invoke_qwen", invoke)
    result = asyncio.run(graph.ainvoke(state, config))
    checkpoint = graph.get_state(config)
    assert result["semantic_review_attempts"] == 1
    assert result["semantic_review_pending_regions"] == ["side"]
    assert checkpoint.values["semantic_review_pending_regions"] == ["side"]
    assert calls == ["front"]

    # Same thread resumes from the committed checkpoint; front's committed result is not replayed.
    resumed = asyncio.run(graph.ainvoke(None, config))
    assert calls == ["front", "side"]
    assert resumed["semantic_review_pending_regions"] == []
    assert route_after_semantic_review(resumed) == "merge"
    assert resumed["semantic_review_attempts"] == 1
    reviewed_regions = [
        region["region_id"]
        for audit in resumed["semantic_review_history"]
        for region in audit["regions"]
    ]
    assert reviewed_regions == ["front", "side"]


def test_gate_routes_to_review_once_then_stops_when_budget_is_exhausted(tmp_path):
    state = _node_state(tmp_path)
    assert route_after_dimension_gate(state) == "review"
    exhausted = {**state, "semantic_review_attempts": MAX_SEMANTIC_REVIEW_ATTEMPTS}
    assert semantic_review_regions(exhausted) == []
    assert route_after_dimension_gate(exhausted) == "stop"


def test_passing_gate_bypasses_review_without_model_call(monkeypatch, tmp_path):
    import backend.agents.step.image_nodes as image_nodes

    state = _node_state(tmp_path)
    state["dimension_gate"] = {"passed": True, "status": "dimensions_valid"}
    assert semantic_review_regions(state) == []
    assert route_after_dimension_gate(state) == "continue"

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("clean gate must not invoke semantic review model")

    monkeypatch.setattr(image_nodes, "_invoke_qwen", fail_if_called)
    result = asyncio.run(review_semantics_node(state))
    assert result == {"semantic_review_pending_regions": [], "status": "semantics_reviewed"}
