"""Symbol workflow regressions with real graph interrupts and offline tools."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import pytest

from backend.agents.symbol import human_nodes, nodes
from backend.agents.symbol.graph import build_symbol_graph
from backend.agents.symbol.contracts.layout import build_layout
from backend.agents.symbol.contracts.models import Pin
from backend.agents.symbol.tools import capture
from backend.api.v1 import symbol


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def offline_graph(monkeypatch, tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    # Two model columns intentionally use different numbering.
    html = """<table><tr><th>NAME</th><th>ADS1113</th><th>ADS1115</th></tr>
    <tr><td>ADDR</td><td>3</td><td>1</td></tr>
    <tr><td>SCL</td><td>4</td><td>2</td></tr></table>"""
    content = [{"type": "table", "page_idx": 0, "table_body": html}]
    seen = {}

    async def locate(state):
        seen["input"] = dict(state)
        return {"locate": {"targets": {"pins": [1]}}, "status": "pages_located"}

    async def parse(pdf_path, pages):
        return SimpleNamespace(content_list=content, notes=[])

    async def render(state):
        return {"rendered_pages": [{"page": 1, "path": "offline.png"}], "status": "pages_rendered"}

    async def vision(images, prompt):
        seen["vision_prompt"] = prompt
        return {"pins": [{"pin_number": "1", "side": "left"}, {"pin_number": "2", "side": "right"}]}

    async def review(*args):
        return SimpleNamespace(passed=True, mismatches=[], missing=[], extra=[], notes="")

    async def generate(layout, out_dir):
        seen["layout"] = deepcopy(layout)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = {key: out / f"{layout['part_name']}.{key}" for key in ("olb", "dsn", "tcl")}
        for path in paths.values():
            path.write_text("offline artifact", encoding="utf-8")
        return SimpleNamespace(passed=True, olb=paths["olb"], dsn=paths["dsn"], script=paths["tcl"], log="PASS")

    monkeypatch.setattr("backend.agents.symbol.graph.locate_pages_node", locate)
    monkeypatch.setattr(nodes, "_parse_with_mineru", parse)
    monkeypatch.setattr("backend.agents.symbol.graph.render_pages_node", render)
    monkeypatch.setattr(nodes, "_ask_vision", vision)
    monkeypatch.setattr(nodes, "_review", review)
    monkeypatch.setattr(nodes, "_run_capture", generate)
    return build_symbol_graph(InMemorySaver()), {
        "pdf_path": str(pdf), "work_dir": str(tmp_path),
        "original_filename": "ADS1115.pdf", "device": "ADS1115", "package": "DGS",
        "strict_pages": True,
    }, seen


def test_graph_preserves_hints_and_generates_selected_model(offline_graph):
    graph, state, seen = offline_graph

    async def scenario():
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await graph.ainvoke(state, config)
        stop = result["__interrupt__"][0]
        assert stop.value["stage"] == "output"
        assert seen["input"]["strict_pages"] is True
        assert result["device"] == "ADS1115" and result["package"] == "DGS"
        assert [p["pin_number"] for p in result["merged_pins"]] == ["1", "2"]
        result = await graph.ainvoke(Command(resume={stop.id: {"action": "confirm"}}), config)
        assert result["status"] == "completed"
        assert seen["layout"]["part_name"] == "ADS1115"
        assert set(result["artifact_paths"]) == {"tcl", "olb", "dsn"}

    run(scenario())


def test_filename_candidate_is_written_to_device(offline_graph):
    graph, state, _ = offline_graph
    state.pop("device")
    result = run(graph.ainvoke(state, {"configurable": {"thread_id": str(uuid.uuid4())}}))
    assert result["device"] == "ADS1115"
    assert result["__interrupt__"][0].value["stage"] == "output"


@pytest.mark.parametrize("node,key", [(human_nodes.ask_device_node, "device"), (human_nodes.ask_package_node, "package")])
def test_manual_selection_updates_state(monkeypatch, node, key):
    monkeypatch.setattr(human_nodes, "_ask", lambda request: {"action": "provide", "value": "USER_VALUE"})
    assert run(node({}))[key] == "USER_VALUE"


@pytest.mark.parametrize("node,record_key", [(human_nodes.resolve_conflicts_node, "conflicts"), (human_nodes.resolve_review_diffs_node, "review")])
def test_resolution_changes_pins_without_mutating_checkpoint(monkeypatch, node, record_key):
    state = {"merged_pins": [{"pin_number": "1", "name": "AINO", "side": "unknown"}]}
    item = {"pin_number": "1", "field": "side", "resolved": None, "values": {}, "expected": "unknown", "observed": "right"}
    state[record_key] = [item] if record_key == "conflicts" else {"mismatches": [item]}
    original = deepcopy(state)
    monkeypatch.setattr(human_nodes, "_ask", lambda request: {"action": "resolve", "resolutions": [{"pin_number": "1", "field": "side", "value": "right"}]})
    out = run(node(state))
    assert out["merged_pins"][0]["side"] == "right"
    assert state == original


def test_self_check_blocks_unknown_sides_and_exposes_questions():
    out = run(nodes.self_check_node({"device": "ADS1115", "merged_pins": [{"pin_number": "1", "name": "A", "type": "PAS"}, {"pin_number": "2", "name": "B", "type": "PAS"}]}))
    assert out["check_report"]["questions"][0]["key"] == "package"
    assert any(f["code"] == "sides_unknown" for f in out["check_report"]["findings"])
    out["check_rounds"] = nodes.MAX_CHECK_ROUNDS
    assert nodes.route_after_self_check(out) == "stopped"


def test_fact_answers_survive_recheck_and_update_package(monkeypatch):
    monkeypatch.setattr(human_nodes, "_ask", lambda request: {"action": "provide", "values": {"package": "DGS"}})
    state = {"device": "ADS1115", "package": "", "check_report": {"questions": [{"key": "package"}], "answered": {"earlier": "value"}}}
    update = run(human_nodes.ask_check_questions_node(state))
    assert update["package"] == "DGS"
    state.update(update)
    report = run(nodes.self_check_node(state))["check_report"]
    assert report["answered"] == {"earlier": "value", "package": "DGS"}
    assert report["questions"] == []


def test_missing_toolchain_keeps_downloadable_script(monkeypatch, tmp_path):
    def unavailable(*args):
        raise FileNotFoundError("No Cadence")

    monkeypatch.setattr(capture, "resolve_tclsh", unavailable)
    state = {"device": "ADS1115", "work_dir": str(tmp_path), "merged_pins": [{"pin_number": "1", "name": "A", "type": "PAS", "side": "left"}, {"pin_number": "2", "name": "B", "type": "PAS", "side": "right"}]}
    state.update(run(nodes.build_layout_node(state)))
    state.update(run(nodes.generate_capture_node(state)))
    state.update(run(nodes.stopped_no_toolchain_node(state)))
    assert state["status"] == "stopped_no_toolchain"
    assert Path(state["artifact_paths"]["tcl"]).is_file()
    assert symbol._job_status({"values": state}) == "stopped"


def test_initial_state_retains_original_filename():
    assert symbol._initial_state({"original_filename": "ADS1115.pdf"})["original_filename"] == "ADS1115.pdf"


@pytest.mark.parametrize("resolutions", [
    [{"pin_number": "1", "field": "side", "value": "diagonal"}],
    [{"pin_number": "1", "field": "side", "value": ""}],
    [{"pin_number": "1", "field": "side", "value": "left"}] * 2,
])
def test_rejects_invalid_resolution_values(resolutions):
    request = {"stage": "conflicts", "items": [{"pin_number": "1", "field": "side"}]}
    with pytest.raises(ValueError):
        human_nodes.validate_human_answer(request, {"action": "resolve", "resolutions": resolutions})


def test_package_can_be_empty_when_question_allows_it():
    answer = human_nodes.validate_human_answer({"stage": "package", "options": ["provide"], "candidates": [], "fields": {"value": "可留空"}}, {"action": "provide", "value": ""})
    assert answer["value"] == ""


def test_manual_graph_answers_select_column_before_extraction(offline_graph):
    graph, state, _ = offline_graph
    state.update(device="", package="", original_filename="datasheet.pdf")

    async def scenario():
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await graph.ainvoke(state, config)
        for stage, value in (("device", "ADS1115"), ("package", "DGS")):
            stop = result["__interrupt__"][0]
            assert stop.value["stage"] == stage
            result = await graph.ainvoke(Command(resume={stop.id: {"action": "provide", "value": value}}), config)
        assert result["__interrupt__"][0].value["stage"] == "output"
        assert [p["pin_number"] for p in result["table_pins"]] == ["1", "2"]
        assert result["layout"]["part_name"] == "ADS1115"

    run(scenario())


def test_broken_graph_never_invokes_capture(offline_graph, monkeypatch):
    graph, state, seen = offline_graph

    async def unknown(images, prompt):
        return {"pins": [{"pin_number": "1", "side": "unknown"}, {"pin_number": "2", "side": "unknown"}]}

    monkeypatch.setattr(nodes, "_ask_vision", unknown)
    result = run(graph.ainvoke(state, {"configurable": {"thread_id": str(uuid.uuid4())}}))
    assert result["status"] == "stopped_check_failed"
    assert "layout" not in seen and "layout" not in result
    assert "__interrupt__" not in result


def test_check_package_change_reextracts_in_graph(offline_graph):
    graph, state, _ = offline_graph
    state["package"] = "SOIC8"  # Deliberately disagrees with the two-pin fixture.

    async def scenario():
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await graph.ainvoke(state, config)
        stop = result["__interrupt__"][0]
        assert stop.value["stage"] == "facts"
        result = await graph.ainvoke(Command(resume={stop.id: {"action": "provide", "values": {"package": "DGS"}}}), config)
        assert result["package"] == "DGS"
        assert result["check_rounds"] == 1
        assert result["check_report"]["answered"] == {"package": "DGS"}
        assert result["__interrupt__"][0].value["stage"] == "output"

    run(scenario())


def test_bad_pin_count_remains_blocked_after_answer(offline_graph):
    graph, state, _ = offline_graph
    state["package"] = "SOIC8"

    async def scenario():
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await graph.ainvoke(state, config)
        stop = result["__interrupt__"][0]
        result = await graph.ainvoke(Command(resume={stop.id: {"action": "provide", "values": {"package": "SOIC8"}}}), config)
        assert result["status"] == "stopped_check_failed"
        assert "layout" not in result

    run(scenario())


def test_review_missing_and_extra_reach_self_check():
    state = {"device": "ADS1115", "package": "DGS", "merged_pins": [
        {"pin_number": "1", "name": "A", "type": "PAS", "side": "left"},
        {"pin_number": "2", "name": "B", "type": "PAS", "side": "right"},
    ], "review": {"mismatches": [], "missing": ["1"], "extra": ["9"]}}
    report = run(nodes.self_check_node(state))["check_report"]
    codes = {finding["code"] for finding in report["findings"]}
    assert {"review_missing", "review_extra"} <= codes
    assert report["verdict"] == "suspect"


@pytest.mark.parametrize("answer", [
    {"action": "provide", "values": {"invented": "x"}},
    {"action": "provide", "values": {"package": "DGS", "device": "OTHER"}},
])
def test_fact_validation_rejects_unasked_fields(answer):
    with pytest.raises(ValueError):
        human_nodes.validate_human_answer({"stage": "facts", "items": [{"key": "package"}]}, answer)


def test_fact_cancel_does_not_require_values():
    assert human_nodes.validate_human_answer({"stage": "facts"}, {"action": "cancel"})["action"] == "cancel"


def test_partial_resolution_is_rejected():
    with pytest.raises(ValueError, match="全部"):
        human_nodes.validate_human_answer({"stage": "conflicts", "items": [{"pin_number": "1", "field": "side"}, {"pin_number": "2", "field": "side"}]}, {"action": "resolve", "resolutions": [{"pin_number": "1", "field": "side", "value": "left"}]})


def test_review_uses_image_page_number(monkeypatch):
    seen = []

    async def review(image, device, pins, page):
        seen.append(page)
        return SimpleNamespace(passed=True, mismatches=[], missing=[], extra=[], notes="")

    monkeypatch.setattr(nodes, "_review", review)
    run(nodes.review_pins_node({"pin_page": 26, "rendered_pages": [{"page": 23, "path": "pin.png"}]}))
    assert seen == [23]


@pytest.mark.parametrize("contents,passed", [(b"", False), (b"binary output", True)])
def test_capture_requires_nonempty_binary_artifacts(monkeypatch, tmp_path, contents, passed):
    monkeypatch.setattr(capture, "resolve_tclsh", lambda exe: Path("fake-tclsh.exe"))

    def subprocess_run(*args, **kwargs):
        for extension in ("OLB", "DSN"):
            (tmp_path / f"ADS1115.{extension}").write_bytes(contents)
        return SimpleNamespace(returncode=0, stdout=capture.PASS_MARKER.encode(), stderr=b"")

    monkeypatch.setattr(capture.subprocess, "run", subprocess_run)
    layout = build_layout([Pin("1", "A", "PAS", "left"), Pin("2", "B", "PAS", "right")], "ADS1115")
    assert capture.generate(layout, out_dir=tmp_path).passed is passed


def test_pdf_locator_runs_outside_event_loop(monkeypatch):
    import threading

    caller = threading.get_ident()
    worker_threads = []

    def locate(pdf):
        worker_threads.append(threading.get_ident())
        return SimpleNamespace(to_dict=lambda: {"targets": {"pins": [1]}})

    def slim(located, pdf, **kwargs):
        worker_threads.append(threading.get_ident())
        assert kwargs["strict"] is True
        return SimpleNamespace(pages=[1], notes=[])

    monkeypatch.setattr(nodes, "locate", locate)
    monkeypatch.setattr(nodes, "slim_pages", slim)
    out = run(nodes.locate_pages_node({"pdf_path": "input.pdf", "strict_pages": True}))
    assert out["slim_page_numbers"] == [1]
    assert len(worker_threads) == 2 and all(thread != caller for thread in worker_threads)


@pytest.mark.parametrize("part", ["../outside", "..\\outside", "", "CON", 'ADS"1115'])
def test_capture_rejects_invalid_filenames_before_writing(tmp_path, part):
    layout = build_layout([Pin("1", "A", "PAS", "left")], part)
    out_dir = tmp_path / "artifacts"
    with pytest.raises(ValueError):
        capture.generate(layout, out_dir=out_dir)
    assert not out_dir.exists()


def test_tcl_literal_values_round_trip_without_substitution():
    import tkinter

    interpreter = tkinter.Tcl()
    value = 'ADC "$secret" [expr 1+2] {text} \\name\nnext'
    interpreter.eval(f'set literal "{capture._tcl_string(value)}"')
    assert interpreter.getvar("literal") == value
