"""独立验证 STEP HITL 节点、LangGraph interrupt/resume 与审核门禁。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
import pytest

from backend.agents.step.human_nodes import (
    ask_package_node,
    confirm_template_node,
    review_result_node,
    routing_question,
    validate_human_answer,
)
from backend.agents.step.families.taxonomy import (
    QFN_UFQFPN_FAMILY_ID,
    RESISTOR_CHIP_FAMILY_ID,
)
from backend.agents.step.image_nodes import jev_route_template_node
from backend.agents.step import image_nodes


def run(coro):
    return asyncio.run(coro)


def node_graph(node, *, saver=None):
    builder = StateGraph(dict)
    builder.add_node("human", node)
    builder.add_edge(START, "human")
    builder.add_edge("human", END)
    return builder.compile(checkpointer=saver or MemorySaver())


def config(thread_id: str | None = None):
    return {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}


@pytest.mark.parametrize(
    ("answer", "expected_status", "expected_hint"),
    [
        ({"action": "provide", "package_type": "TSSOP-16"}, "package_confirmed", "TSSOP-16"),
        ({"action": "auto"}, "package_confirmed", ""),
        ({"action": "cancel", "comment": "取消此任务"}, "cancelled", None),
    ],
)
def test_package_is_always_asked_and_supports_provide_auto_cancel(
    answer, expected_status, expected_hint
):
    graph = node_graph(ask_package_node)
    thread = config()
    paused = run(graph.ainvoke({"image_path": "synthetic.png"}, config=thread))

    assert paused["__interrupt__"][0].value["stage"] == "package"
    assert set(paused["__interrupt__"][0].value["options"]) == {"provide", "auto", "cancel"}

    resumed = run(graph.ainvoke(Command(resume=answer), config=thread))
    assert resumed["status"] == expected_status
    if expected_hint is not None:
        assert resumed["package_type_hint"] == expected_hint
        assert resumed["human_package"]["action"] == answer["action"]
    else:
        assert resumed["result"]["stage"] == "package"
        assert resumed["human_history"][-1]["stage"] == "package"


def test_invalid_package_answer_is_rejected_and_reasked_on_same_thread():
    graph = node_graph(ask_package_node)
    thread = config("package-invalid-then-valid")
    paused = run(graph.ainvoke({"image_path": "synthetic.png"}, config=thread))
    assert paused["__interrupt__"][0].value["stage"] == "package"

    reasked = run(graph.ainvoke(Command(resume={"action": "invent"}), config=thread))
    question = reasked["__interrupt__"][0].value
    assert question["stage"] == "package"
    assert "只接受 provide、auto 或 cancel" in question["validation_error"]

    resumed = run(graph.ainvoke(Command(resume={"action": "auto"}), config=thread))
    assert resumed["status"] == "package_confirmed"
    assert resumed["human_package"]["action"] == "auto"
    assert resumed["human_history"] == [
        {"stage": "package", "action": "auto", "comment": ""}
    ]
    snapshot = run(graph.aget_state(thread))
    assert snapshot.values["package_type_hint"] == ""
    assert not snapshot.next


def classification(
    family_id=QFN_UFQFPN_FAMILY_ID,
    *,
    category_id="ic",
    package_type="QFN-48",
    confidence=0.96,
    ambiguities=None,
    unresolved_fields=None,
):
    return {
        "family_id": family_id,
        "category_id": category_id,
        "subcategory_id": None,
        "package_type": package_type,
        "overall_confidence": confidence,
        "ambiguities": list(ambiguities or []),
        "unresolved_fields": list(unresolved_fields or []),
    }


def test_clear_high_confidence_routing_bypasses_question():
    state = {
        "view_classification": classification(),
        "package_type_hint": "QFN-48",
    }
    assert routing_question(state) is None


def test_unimplemented_but_registered_template_prompts_before_jev():
    state = {
        "view_classification": classification(
            family_id="misc", category_id="misc", package_type="TO-220", confidence=0.96
        ),
        "package_type_hint": "TO-220",
    }

    question = routing_question(state)

    assert question is not None
    assert question["stage"] == "routing"
    assert any("尚未实现" in reason for reason in question["reasons"])
    assert question["options"] == ["change", "cancel"]
    assert {item["family_id"]: item["implemented"] for item in question["candidates"]}["misc"] is False


@pytest.mark.parametrize(
    ("family_id", "category_id"),
    [
        ("", ""),
        ("unknown_family", ""),
        ("two_terminal_chip", ""),
        (QFN_UFQFPN_FAMILY_ID, "resistor"),
        ("transistor", "transistor"),
    ],
)
def test_unusable_routing_suggestion_cannot_be_confirmed(family_id, category_id):
    state = {"view_classification": classification(family_id, category_id=category_id)}
    question = routing_question(state)

    assert question["options"] == ["change", "cancel"]
    assert question["suggested"]["family_id"] == family_id
    assert "implemented=true" in question["fields"]["family_id"]
    with pytest.raises(ValueError, match="当前问题不提供 confirm"):
        validate_human_answer(question, {"action": "confirm"})


def test_implemented_low_confidence_suggestion_can_be_confirmed():
    question = routing_question({"view_classification": classification(confidence=0.5)})
    assert question["options"] == ["confirm", "change", "cancel"]
    answer = validate_human_answer(question, {"action": "confirm"})
    assert answer["family_id"] == QFN_UFQFPN_FAMILY_ID


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        ({"stage": "package", "options": ["cancel"]}, {"action": "auto"}),
        ({"stage": "routing", "options": ["cancel"]},
         {"action": "change", "family_id": QFN_UFQFPN_FAMILY_ID}),
        ({"stage": "review", "options": ["reject"], "can_approve": True}, {"action": "approve"}),
    ],
)
def test_validator_rejects_actions_not_offered_by_current_question(question, answer):
    with pytest.raises(ValueError, match="当前问题不提供"):
        validate_human_answer(question, answer)


@pytest.mark.parametrize("family_id", ["misc", "transistor"])
@pytest.mark.parametrize("action", ["confirm", "change"])
def test_registered_unimplemented_family_is_rejected_even_for_legacy_questions(family_id, action):
    # Old checkpoints may offer confirm or have no options at all. Neither may
    # authorize a placeholder family or silently substitute an implemented one.
    request = {"stage": "routing", "suggested": {"family_id": family_id}}
    with pytest.raises(ValueError, match="所选模板尚未实现"):
        validate_human_answer(request, {"action": action, "family_id": family_id})
    assert request["suggested"]["family_id"] == family_id


def test_invalid_routing_answer_reasks_before_accepting_implemented_change():
    graph = node_graph(confirm_template_node)
    thread = config()
    paused = run(graph.ainvoke({
        "view_classification": classification("transistor", category_id="transistor"),
    }, config=thread))
    assert "confirm" not in paused["__interrupt__"][0].value["options"]

    reasked = run(graph.ainvoke(Command(resume={"action": "confirm"}), config=thread))
    assert "当前问题不提供 confirm" in reasked["__interrupt__"][0].value["validation_error"]
    confirmed = run(graph.ainvoke(Command(resume={
        "action": "change", "family_id": QFN_UFQFPN_FAMILY_ID,
    }), config=thread))
    assert confirmed["status"] == "routing_confirmed"
    assert confirmed["human_route"]["family_id"] == QFN_UFQFPN_FAMILY_ID
    assert len(confirmed["human_history"]) == 1


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (
            {
                "view_classification": classification(
                    family_id="two_terminal_chip", category_id="", package_type="", confidence=0.96
                )
            },
            "无法确定属于电阻还是电容",
        ),
        (
            {
                "view_classification": classification(confidence=0.79),
                "package_type_hint": "QFN-48",
            },
            "置信度低于 0.80",
        ),
        (
            {
                "view_classification": classification(ambiguities=["多个候选封装"]),
                "package_type_hint": "QFN-48",
            },
            "存在歧义",
        ),
        (
            {
                "view_classification": classification(package_type="QFN-48"),
                "package_type_hint": "LQFP-64",
            },
            "封装与识别结果不一致",
        ),
    ],
)
def test_ambiguous_low_confidence_or_conflicting_identity_prompts(state, expected_reason):
    question = routing_question(state)
    assert question is not None
    assert question["stage"] == "routing"
    assert any(expected_reason in reason for reason in question["reasons"])


def test_manual_template_selection_is_preserved_through_jev(tmp_path):
    graph = node_graph(confirm_template_node)
    thread = config()
    initial = {
        "view_classification": classification(
            family_id="two_terminal_chip", category_id="", package_type="", confidence=0.55
        ),
        "package_type_hint": "",
        "all_ocr_tokens": [{"text": "R17", "confidence": 0.99}],
        "output_dir": str(tmp_path),
    }
    paused = run(graph.ainvoke(initial, config=thread))
    question = paused["__interrupt__"][0].value
    assert question["stage"] == "routing"
    assert question["suggested"]["family_id"] == "two_terminal_chip"

    answer = {
        "action": "change",
        "family_id": RESISTOR_CHIP_FAMILY_ID,
        "package_type": "0805 resistor",
    }
    confirmed = run(graph.ainvoke(Command(resume=answer), config=thread))
    assert confirmed["view_classification"]["family_id"] == RESISTOR_CHIP_FAMILY_ID
    assert confirmed["view_classification"]["category_id"] == "resistor"
    assert confirmed["view_classification"]["package_type"] == "0805 resistor"
    assert not any(
        str(field).split(":")[-1] in {"family_id", "category_id", "subcategory_id"}
        for field in confirmed["view_classification"].get("unresolved_fields", [])
    )

    routed = run(jev_route_template_node({
        **confirmed,
        "all_ocr_tokens": initial["all_ocr_tokens"],
        "output_dir": str(tmp_path),
    }))
    assert routed["view_classification"]["family_id"] == RESISTOR_CHIP_FAMILY_ID
    assert routed["jev_decision"]["route_source"] == "human_confirmation"
    assert routed["jev_decision"]["human_selection"]["family_id"] == RESISTOR_CHIP_FAMILY_ID


def test_matching_high_confidence_manual_package_skips_repeat_question_and_constrains_jev(
    tmp_path, monkeypatch
):
    graph = node_graph(confirm_template_node)
    initial = {
        "view_classification": classification(
            family_id=QFN_UFQFPN_FAMILY_ID,
            category_id="ic",
            package_type="QFN-48",
            confidence=0.98,
        ),
        "package_type_hint": "QFN-48",
        "output_dir": str(tmp_path),
    }
    confirmed = run(graph.ainvoke(initial, config=config()))
    assert "__interrupt__" not in confirmed
    assert confirmed["status"] == "routing_ready"

    called = False

    async def unexpected_jev_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("matched package must constrain Jev")

    monkeypatch.setattr(image_nodes, "call_jev_choice", unexpected_jev_call)
    routed = run(jev_route_template_node({**initial, **confirmed}))

    assert not called
    assert routed["view_classification"]["family_id"] == QFN_UFQFPN_FAMILY_ID
    assert routed["view_classification"]["package_type"] == "QFN-48"
    assert routed["jev_decision"]["route_source"] == "requested_package"
    assert routed["jev_decision"]["requested_package"] == "QFN-48"


def _review_state(tmp_path: Path, *, match_status: str, needs_review=False):
    step_file = tmp_path / "candidate.step"
    step_file.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="ascii")
    previews = {}
    for view in ("isometric", "front", "top", "right"):
        path = tmp_path / f"{view}.png"
        path.write_bytes(b"synthetic-preview")
        previews[view] = str(path)
    return {
        "output_dir": str(tmp_path),
        "result": {"status": "completed", "step_file": str(step_file), "previews": previews},
        "artifact_paths": {"step": str(step_file)},
        "preview_paths": previews,
        "verification": {"passed": True, "checks": []},
        "golden_comparison": {"status": match_status},
        "needs_review": needs_review,
        "human_history": [{"stage": "package", "action": "auto", "comment": ""}],
    }


def test_matched_review_bypasses_manual_interrupt_and_keeps_history(tmp_path):
    graph = node_graph(review_result_node)
    state = _review_state(tmp_path, match_status="matched")
    result = run(graph.ainvoke(state, config=config()))

    assert "__interrupt__" not in result
    assert result["status"] == "completed"
    assert result["needs_review"] is False
    assert result["result"]["human_history"] == state["human_history"]
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["status"] == "completed"


@pytest.mark.parametrize("source", ["result", "feature_ir"])
def test_review_exposes_modeling_assumptions_and_preserves_them_after_decision(tmp_path, source):
    graph = node_graph(review_result_node)
    thread = config()
    state = _review_state(tmp_path, match_status="matched")
    assumptions = ["body 使用 box 简化，未建模倒角和模塑斜度"]
    if source == "result":
        state["result"]["modeling_assumptions"] = assumptions
    else:
        state["feature_ir"] = {"assumptions": assumptions}

    paused = run(graph.ainvoke(state, config=thread))
    request = paused["__interrupt__"][0].value
    assert request["stage"] == "review"
    assert request["modeling_assumptions"] == assumptions
    resumed = run(graph.ainvoke(Command(resume={"action": "reject"}), config=thread))
    assert resumed["result"]["modeling_assumptions"] == assumptions
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["modeling_assumptions"] == assumptions


@pytest.mark.parametrize(
    ("answer", "expected_status"),
    [({"action": "approve", "comment": "尺寸与视图已复核"}, "reviewed"),
     ({"action": "reject", "comment": "封装轮廓不符"}, "rejected")],
)
def test_unmatched_candidate_requires_human_decision_and_records_history(
    tmp_path, answer, expected_status
):
    graph = node_graph(review_result_node)
    thread = config()
    state = _review_state(tmp_path, match_status="different")
    paused = run(graph.ainvoke(state, config=thread))

    event = paused["__interrupt__"][0].value
    assert event["stage"] == "review"
    assert event["can_approve"] is True
    resumed = run(graph.ainvoke(Command(resume=answer), config=thread))

    assert resumed["status"] == expected_status
    assert resumed["result"]["status"] == expected_status
    assert resumed["human_review"]["action"] == answer["action"]
    assert resumed["human_history"] == [
        {"stage": "package", "action": "auto", "comment": ""},
        {"stage": "review", **answer},
    ]
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["status"] == expected_status


def test_review_rejects_missing_artifact_before_any_interrupt(tmp_path):
    result = run(review_result_node({
        "output_dir": str(tmp_path),
        "result": {"status": "completed"},
        "verification": {"passed": True},
        "golden_comparison": {"status": "different"},
    }))

    assert result["status"] == "failed"
    assert result["needs_review"] is False
    assert "无法进入人工审核" in result["errors"][-1]


def test_answer_cannot_approve_when_review_says_not_approvable():
    with pytest.raises(ValueError, match="不能批准"):
        validate_human_answer(
            {"stage": "review", "can_approve": False},
            {"action": "approve"},
        )
