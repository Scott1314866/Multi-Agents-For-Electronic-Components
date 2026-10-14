"""Image-only symbol input: genuine raster validation, graph and faithful layout."""
import asyncio
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from PIL import Image
import pytest

from backend.agents.symbol import nodes
from backend.agents.symbol.contracts.image_symbol import normalize_image, read_image_payload, build_image_layout
from backend.agents.symbol.contracts.layout import describe, preview
from backend.agents.symbol.graph import build_symbol_graph
from backend.api.v1 import symbol

GOLDEN = Path(__file__).parent / "golden/symbol/mcp3021"


def payload():
    return json.loads((GOLDEN / "reading.json").read_text(encoding="utf-8"))


def test_real_mcp3021_image_reading_preserves_names_positions_and_alignment():
    _, meta = normalize_image((GOLDEN / "pin-diagram.png").read_bytes())
    assert (meta["width"], meta["height"]) == (477, 378)
    reading = read_image_payload(payload(), image_size=meta)
    assert not reading.questions
    assert {p.pin_number: p.name for p in reading.pins} == {"1": "VDD", "2": "VSS", "3": "AIN", "4": "SDA", "5": "SCL"}
    layout = build_image_layout(reading.pins, reading.geometry, "MCP3021")
    actual = {p.number: p for p in layout.pins}
    assert layout.body_height == 2 * layout.body_width
    assert actual["5"].y == actual["1"].y < actual["2"].y < actual["3"].y == actual["4"].y
    assert actual["1"].side == "left" and actual["5"].side == "right"
    rows = [line for line in preview(layout).splitlines() if "|" in line]
    assert rows[0].strip().startswith("1") and rows[0].strip().endswith("5")
    assert rows[1].strip().endswith("|")
    assert rows[2].strip().startswith("3") and rows[2].strip().endswith("4")
    assert "引脚间距 70/140 DBO" in describe(layout)


def test_small_opposite_side_reading_drift_does_not_break_alignment():
    data = payload()
    data["pins"][3]["y"] += .005
    data["pins"][4]["y"] += .005
    reading = read_image_payload(data, image_size={"width": 477, "height": 378})
    actual = {p.number: p for p in build_image_layout(reading.pins, reading.geometry, "MCP3021").pins}
    assert actual["1"].y == actual["5"].y and actual["3"].y == actual["4"].y


@pytest.mark.parametrize("content", [b"%PDF-1.7", b"not an image", b""])
def test_image_validation_rejects_wrong_content(content):
    with pytest.raises(ValueError):
        normalize_image(content)


def test_alpha_is_composited_to_white_and_jpeg_can_be_normalized():
    for fmt, mode in [("PNG", "RGBA"), ("JPEG", "RGB")]:
        buffer = BytesIO()
        Image.new(mode, (20, 20), (0, 0, 0, 0) if mode == "RGBA" else "white").save(buffer, fmt)
        normalized, meta = normalize_image(buffer.getvalue())
        assert meta["format"] == fmt and normalized.startswith(b"\x89PNG")
        assert Image.open(BytesIO(normalized)).getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("mutation", ["duplicate", "bad_number", "bad_bbox", "bad_coordinate", "nonfinite"])
def test_invalid_model_readings_cannot_generate_symbols(mutation):
    data = payload()
    if mutation == "duplicate": data["pins"][-1]["pin_number"] = "1"
    elif mutation == "bad_number": data["pins"][-1]["pin_number"] = "一"
    elif mutation == "bad_bbox": data["body_bbox"] = [0, 0, 0, 0]
    elif mutation == "bad_coordinate": data["pins"][0]["y"] = 2
    else: data["pins"][0]["x"] = float("nan")
    with pytest.raises(ValueError):
        read_image_payload(data, image_size={"width": 477, "height": 378})


@pytest.fixture
def image_graph(monkeypatch, tmp_path):
    seen = {"captures": []}
    data = payload()
    async def forbidden(*args, **kwargs):
        pytest.fail("image flow called PDF/MinerU/independent rereview")
    async def vision(images, prompt):
        seen["prompt"] = prompt
        return deepcopy(data)
    async def capture(layout, out_dir):
        seen["captures"].append(layout)
        folder = Path(out_dir); folder.mkdir(parents=True, exist_ok=True)
        paths = {kind: folder / f"MCP3021.{kind}" for kind in ("tcl", "olb", "dsn")}
        for path in paths.values(): path.write_text("offline fixture")
        return SimpleNamespace(passed=True, script=paths["tcl"], olb=paths["olb"], dsn=paths["dsn"], log="PASS")
    monkeypatch.setattr("backend.agents.symbol.graph.locate_pages_node", forbidden)
    monkeypatch.setattr(nodes, "_parse_with_mineru", forbidden)
    monkeypatch.setattr(nodes, "_review", forbidden)
    monkeypatch.setattr(nodes, "_ask_vision", vision)
    monkeypatch.setattr(nodes, "_run_capture", capture)
    state = {"input_kind": "image", "image_path": str((GOLDEN / "pin-diagram.png").resolve()),
             "work_dir": str(tmp_path), "auto_generate": True}
    return build_symbol_graph(InMemorySaver()), state, seen, data


def test_clear_image_graph_generates_without_manual_interrupt(image_graph):
    graph, state, seen, _ = image_graph
    result = asyncio.run(graph.ainvoke(state, {"configurable": {"thread_id": str(uuid.uuid4())}}))
    assert result["status"] == "completed" and not result.get("__interrupt__")
    assert result["check_report"]["score"] == 100 and result["human_output"]["action"] == "auto"
    assert len(seen["captures"]) == 1 and len(result["merged_pins"]) == 5


def test_only_uncertain_image_pin_needs_manual_name_confirmation(image_graph):
    graph, state, seen, data = image_graph
    data["pins"][2].update(name="", confidence=.3, uncertain=True)
    async def scenario():
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await graph.ainvoke(state, config)
        stop = result["__interrupt__"][0]
        assert stop.value["stage"] == "review_diffs"
        assert [(i["pin_number"], i["field"]) for i in stop.value["items"]] == [("3", "name")]
        assert not seen["captures"]
        result = await graph.ainvoke(Command(resume={stop.id: {"action": "resolve", "resolutions": [
            {"pin_number": "3", "field": "name", "value": "AIN"}]}}), config)
        assert result["status"] == "completed" and len(seen["captures"]) == 1
    asyncio.run(scenario())


def test_missing_image_pin_is_blocked_before_capture(image_graph):
    graph, state, seen, data = image_graph
    data["pins"].pop()
    result = asyncio.run(graph.ainvoke(state, {"configurable": {"thread_id": str(uuid.uuid4())}}))
    assert result["status"] == "stopped_check_failed"
    assert not seen["captures"]
    assert any(f["code"] == "image_pin_count_mismatch" for f in result["check_report"]["findings"])


def test_image_initial_state_preserves_paths_and_authorized_generation():
    state = symbol._initial_state({"input_kind": "image", "image_path": "input.png", "source_path": "source.jpg"})
    assert state["image_path"] == "input.png" and state["source_path"] == "source.jpg" and state["auto_generate"]
