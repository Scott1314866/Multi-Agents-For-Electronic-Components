"""Automatic verification must reduce interruptions without hiding real conflicts."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.symbol import human_nodes, nodes
from backend.agents.symbol.contracts.extract import FigureInfo
from backend.agents.symbol.contracts.merge import merge_channels
from backend.agents.symbol.contracts.models import Pin
from backend.agents.symbol.contracts.pipeline import best_table
from backend.agents.symbol.contracts.review_policy import automatic_name_resolution
from backend.agents.symbol.tools.review import review_pins


def pin(name="INO", description="Analog Input Channel 0."):
    return {"pin_number": "16", "name": name, "type": "IN", "side": "top",
            "description": description, "evidence": [{"source": "vision", "agreement": True}]}


@pytest.mark.parametrize("expected,observed,description", [
    ("INO", "IN0", "Analog Input Channel 0."),
    ("AINO", "AIN0", "Analog Input Channel 0."),
    ("EPAD", "EP", "Exposed Pad. Solder to ground plane."),
    ("ref", " REF ", "Reference input."),
    ("16", "IN0", "Analog input."),
])
def test_evidence_backed_name_differences_pass_without_asking(expected, observed, description):
    result = automatic_name_resolution(pin(expected, description), expected, observed)
    assert result is not None
    assert result[0].strip().upper() == observed.strip().upper()


@pytest.mark.parametrize("expected,observed,description", [
    ("IN1", "IN2", "Analog Input Channel 1."),
    ("VINO", "VIN0", "Power supply."),
    ("INO", "IN0", "Analog input."),
    ("INO", "IN0", "Analog Input Channel 0 (A). Analog Input Channel 1 (B)."),
    ("EPAD", "EP", "Reserved pin."),
    ("RESET", "/RESET", "Reset."),
    ("IN+", "IN-", "Analog input."),
])
def test_real_or_unverified_differences_remain_unresolved(expected, observed, description):
    assert automatic_name_resolution(pin(expected, description), expected, observed,
                                     observation={"confidence": 1}) is None


@pytest.mark.parametrize("observation", [{"confidence": 0.5}, {"confidence": "bad"},
                                          {"confidence": 99}, {"uncertain": True}])
def test_low_or_invalid_confidence_is_not_accepted(observation):
    assert automatic_name_resolution(pin(), "INO", "IN0", observation=observation) is None


def test_unverified_position_and_stale_review_cannot_overwrite_names():
    unverified = pin()
    unverified["evidence"] = []
    assert automatic_name_resolution(unverified, "INO", "IN0") is None
    assert automatic_name_resolution(pin("IN2"), "INO", "IN0") is None


def test_only_material_differences_are_sent_to_user_and_auto_changes_survive(monkeypatch):
    state = {"merged_pins": [pin(), {**pin("VDD"), "pin_number": "1"}], "review": {
        "mismatches": [
            {"pin_number": "16", "field": "name", "expected": "INO", "observed": "IN0"},
            {"pin_number": "1", "field": "name", "expected": "VDD", "observed": "GND"},
        ]}}
    original = deepcopy(state)
    questions = []

    def ask(request):
        questions.append(request)
        assert [i["pin_number"] for i in request["items"]] == ["1"]
        return {"action": "resolve", "resolutions": [{"pin_number": "1", "field": "name", "value": "VDD"}]}

    monkeypatch.setattr(human_nodes, "_ask", ask)
    result = asyncio.run(human_nodes.resolve_review_diffs_node(state))
    assert len(questions) == 1 and state == original
    assert result["merged_pins"][0]["name"] == "IN0"
    assert result["merged_pins"][1]["name"] == "VDD"
    assert result["review"]["mismatches"][0]["resolution_source"] == "automatic_rule"
    assert [entry["action"] for entry in result["human_history"]] == ["auto", "resolve"]


def test_auto_only_differences_never_call_ask_and_leave_an_audit_record(monkeypatch):
    monkeypatch.setattr(human_nodes, "_ask", lambda request: pytest.fail("unexpected manual review"))
    state = {"merged_pins": [pin()], "review": {"mismatches": [
        {"pin_number": "16", "field": "name", "expected": "INO", "observed": "IN0"}]}}
    result = asyncio.run(human_nodes.resolve_review_diffs_node(state))
    assert result["status"] == "review_clear"
    assert result["review"]["auto_resolutions"][0]["reason"]
    assert result["human_history"][0]["source"] == "automatic_rule"
    # Re-entering a resolved checkpoint does not duplicate the audit record.
    assert asyncio.run(human_nodes.resolve_review_diffs_node({**state, **result})) == {"status": "review_clear"}


def pad_state():
    content = json.loads((Path(__file__).parent / "golden/symbol/ad7682/mineru_pin_tables.json")
                         .read_text(encoding="utf-8"))
    _, extraction = best_table(content, [11], device="AD7682BCPZRL7", package="LFCSP-20")
    figure = FigureInfo(pin_sides={
        "left": [str(n) for n in range(1, 6)], "bottom": [str(n) for n in range(6, 11)],
        "right": [str(n) for n in range(11, 16)], "top": [str(n) for n in range(16, 21)],
    }, page=11)
    merged = merge_channels(extraction.pins, figure, page=11)
    return {"device": "AD7682BCPZRL7", "package": "LFCSP-20", "human_package": {"value": "LFCSP-20"},
            "merged_pins": [asdict(p) for p in merged.pins], "conflicts": [asdict(c) for c in merged.conflicts],
            "review": {"mismatches": [], "missing": ["21"], "extra": []}}


def test_verified_pad_not_drawn_on_edge_gets_layout_default_without_asking(monkeypatch):
    state = pad_state()
    original = deepcopy(state)
    monkeypatch.setattr(human_nodes, "_ask", lambda request: pytest.fail("unnecessary pad question"))
    result = asyncio.run(human_nodes.resolve_conflicts_node(state))
    pad = next(p for p in result["merged_pins"] if p["pin_number"] == "21")
    assert pad["side"] == "bottom" and pad["side_order"] == 5
    assert result["conflicts"][0]["resolved"] == "bottom" and state == original
    report = asyncio.run(nodes.self_check_node({**state, **result}))["check_report"]
    assert report["score"] == 100 and report["ok"]
    assert {"exposed_pad", "exposed_pad_layout", "review_pad_not_shown"} <= {
        f["code"] for f in report["findings"]}


@pytest.mark.parametrize("mutation", ["missing_pin", "no_description", "unverified_edges", "wrong_number"])
def test_unverified_pad_or_missing_peripheral_pins_are_not_auto_resolved(monkeypatch, mutation):
    state = pad_state()
    if mutation == "missing_pin":
        state["merged_pins"] = [p for p in state["merged_pins"] if p["pin_number"] != "6"]
    elif mutation == "no_description":
        state["merged_pins"][-1]["description"] = ""
    elif mutation == "unverified_edges":
        state["merged_pins"][0]["side"] = "unknown"
    else:
        state["package"] = "QFN32"
    questions = []
    monkeypatch.setattr(human_nodes, "_ask", lambda q: (questions.append(q) or {"action": "cancel"}))
    asyncio.run(human_nodes.resolve_conflicts_node(state))
    assert questions and questions[0]["stage"] == "conflicts"


def test_review_preserves_polarity_differences_and_self_check_reports_them():
    pins = [Pin("1", "RESET", "IN", side="left"), Pin("2", "GND", "PAS", side="right")]
    client = SimpleNamespace(ask_json=lambda *args: {"pins": [
        {"pin_number": "1", "name": "/RESET", "side": "left"},
        {"pin_number": "2", "name": "GND", "side": "right"}]})
    result = review_pins(client, "unused.png", device="TEST", pins=pins, page=1)
    assert len(result.mismatches) == 1
    state = {"merged_pins": [asdict(p) for p in pins], "review": {
        "mismatches": [asdict(m) for m in result.mismatches]}}
    report = asyncio.run(nodes.self_check_node(state))["check_report"]
    assert any(f["code"] == "review_name_mismatch" for f in report["findings"])
