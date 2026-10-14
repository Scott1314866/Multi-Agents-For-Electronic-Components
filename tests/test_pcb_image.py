"""Image footprint input, asymmetric lands, checkpoints and upload contracts."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json
from pathlib import Path
import uuid

from fastapi import FastAPI
import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from PIL import Image
import pytest

from backend.agents.pcb import image_input as I
from backend.agents.pcb import skill_emitter as E
from backend.agents.pcb.derive import check_clearances, derive_package
from backend.agents.pcb.graph import build_pcb_graph
from backend.agents.pcb.human_nodes import validate_human_answer
from backend.agents.pcb.spec import PackageSpec, wson8_3x3
from backend.api.v1 import pcb

SAMPLE = Path(__file__).parent / "golden/pcb/recommended_six_pad/footprint.png"


def six_pad_spec():
    # Numbering, body and height are TEST INPUT supplied by an operator, not
    # extracted facts. The image only labels pin 1 and the mounting dimensions.
    return {"name": "SIX_PAD_TEST", "family": "TEST", "body_x": 1, "body_y": .8,
            "body_height_max": .5, "pin_count": 6, "pin_layout": "EXPLICIT",
            "pin_numbering": ["1", "2", "3", "4", "5", "6"], "ep_present": False,
            "pin1_marker": "NONE", "pads": [
                {"number": "1", "x": -.525, "y": -.45, "size_x": .5, "size_y": .65},
                {"number": "2", "x": 0, "y": -.45, "size_x": .25, "size_y": .65},
                {"number": "3", "x": .4, "y": -.45, "size_x": .25, "size_y": .65},
                {"number": "4", "x": .4, "y": .45, "size_x": .25, "size_y": .65},
                {"number": "5", "x": 0, "y": .45, "size_x": .25, "size_y": .65},
                {"number": "6", "x": -.4, "y": .45, "size_x": .25, "size_y": .65},
            ]}


def partial_reading():
    raw = six_pad_spec()
    for field in ("name", "family", "body_x", "body_y", "body_height_max", "pin_numbering", "pin1_marker"):
        raw.pop(field)
    for p in raw["pads"][1:]:
        p["number"] = None
    return I.ImageExtraction(spec=raw, source_units="mm", evidence={
        k: I.FieldEvidence(text="visible dimensional annotation", view="land pattern", confidence=.95)
        for k in raw}, uncertain_fields=["pads"],
        notes=["Missing body dimensions, height and unlabelled pin IDs"])


def test_recommended_image_keeps_partial_geometry_without_sample_defaults():
    candidate, issues = I.assess_extraction(partial_reading())
    assert candidate["pads"][0]["size_x"] == .5
    assert candidate["pads"][0]["x"] == -.525
    assert all(p["number"] is None for p in candidate["pads"][1:])
    assert "body_x" not in candidate and "body_height_max" not in candidate
    assert any("body_x" in x for x in issues)
    assert any("pads" in x for x in issues)
    assert not any(k in candidate for k in ("land_x", "land_y", "pin_pitch"))


def test_image_is_decoded_and_alpha_is_flattened():
    normalized, meta = I.normalize_image(SAMPLE.read_bytes())
    assert (meta["width"], meta["height"]) == (302, 292)
    assert len(meta["sha256"]) == 64
    with Image.open(BytesIO(normalized)) as image:
        assert image.format == "PNG" and image.mode == "RGB"
    stream = BytesIO()
    Image.new("RGBA", (3, 3), (0, 0, 0, 0)).save(stream, format="PNG")
    normalized, _ = I.normalize_image(stream.getvalue())
    assert Image.open(BytesIO(normalized)).getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("content", [b"", b"not an image", b"%PDF-1.7 fake"])
def test_invalid_image_content_is_rejected(content):
    with pytest.raises(ValueError):
        I.normalize_image(content)


def test_image_limits_and_animation(monkeypatch):
    monkeypatch.setattr(I, "MAX_IMAGE_PIXELS", 5)
    with pytest.raises(ValueError, match="3200"):
        I.normalize_image(SAMPLE.read_bytes())
    monkeypatch.setattr(I, "MAX_IMAGE_PIXELS", 32_000_000)
    stream = BytesIO()
    Image.new("RGB", (2, 2)).save(stream, format="PNG", save_all=True,
                                 append_images=[Image.new("RGB", (2, 2), "red")])
    with pytest.raises(ValueError, match="静态"):
        I.normalize_image(stream.getvalue())


def test_explicit_lands_keep_pin_one_width_offset_and_rectangular_body(tmp_path):
    spec = PackageSpec(**six_pad_spec())
    derived = derive_package(spec)
    assert check_clearances(spec) == []
    assert (derived.asm_half, derived.asm_half_y) == (.5, .4)
    assert derived.pin_pads["1"].copper == (.5, .65)
    assert derived.pin_pads["2"].copper == (.25, .65)
    assert derived.pin_pads["1"].mask == (.6, .75)
    text = E.emit_build_skill(spec, derived, tmp_path)
    assert 'bAddPin("1" (0.0 - 0.525)' in text
    assert 'bMakePadstack("SIX_PAD_TEST_LAND_2" 0.25 0.65 0.25 0.65 0.35 0.75)' in text
    assert "asmYPos = 0.40" in text
    assert "list(asmNeg:asmYNeg asmPos:asmYPos)" in text
    assert E.paren_depth(text) == 0
    for p in spec.pads:
        assert derived.silk_half - spec.silk_line_width / 2 >= abs(p.y) + derived.pin_pads[p.number].mask[1] / 2 + spec.silk_clearance - 1e-6


def test_explicit_clearance_checks_wide_and_opposite_row_lands():
    raw = six_pad_spec()
    raw["pads"][0]["x"] = -.4  # The wide pin is incorrectly centered on normal pitch.
    assert any("1-2" in i for i in check_clearances(PackageSpec(**raw)))
    raw = six_pad_spec()
    raw["pads"][5]["y"] = -.1  # Opposite row overlaps pin 1.
    assert any("1-6" in i for i in check_clearances(PackageSpec(**raw)))


@pytest.mark.parametrize("mutation", [
    {"name": "../../escape"}, {"name": 'x\") malicious'},
    {"body_x": float("inf")}, {"pin_numbering": ["1"] * 6},
    {"pin_pitch": .4}, {"paste_mode": "INSET", "paste_inset": .2},
])
def test_bad_explicit_specs_cannot_reach_emission(mutation):
    with pytest.raises(ValueError):
        PackageSpec(**{**six_pad_spec(), **mutation})


def test_paste_inset_is_emitted_instead_of_copper_dimensions(tmp_path):
    spec = PackageSpec(**{**six_pad_spec(), "paste_mode": "INSET", "paste_inset": .025})
    text = E.emit_build_skill(spec, derive_package(spec), tmp_path)
    assert "bMakePadstack(bLand landX landY 0.45 0.60 0.60 0.75)" in text


def test_image_graph_reconstruction_does_not_reextract_after_human_resume(monkeypatch, tmp_path):
    calls = []
    async def extract(*args, **kwargs):
        calls.append(args)
        return partial_reading()
    monkeypatch.setattr(I, "extract_image", extract)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "image-resume"}}
    first = build_pcb_graph(saver)
    async def scenario():
        out = await first.ainvoke({"image_path": str(SAMPLE), "work_dir": str(tmp_path), "mode": "emit_only"}, config)
        question = out["__interrupt__"][0]
        assert question.value["stage"] == "image_parameters"
        assert not list(tmp_path.glob("*.il"))
        # A bad reply must ask again and retain the partial extraction.
        second = build_pcb_graph(saver)
        out = await second.ainvoke(Command(resume={question.id: {"action": "submit"}}), config)
        assert out["__interrupt__"][0].value["validation_error"]
        question = out["__interrupt__"][0]
        out = await second.ainvoke(Command(resume={question.id: {"action": "submit", "spec": six_pad_spec()}}), config)
        assert out["status"] == "skills_ready"
        assert len(calls) == 1
        assert len(out["human_history"]) == 1
        assert out["human_image_parameters"]["spec"]["pads"][0]["x"] == -.525
        assert Path(out["artifact_paths"]["image_reading"]).is_file()
    asyncio.run(scenario())


def test_image_human_cancel_and_unsupported_reply():
    request = {"stage": "image_parameters", "options": ["submit", "cancel"], "spec": six_pad_spec()}
    assert validate_human_answer(request, {"action": "cancel"})["action"] == "cancel"
    with pytest.raises(ValueError):
        validate_human_answer({**request, "unsupported_reason": "Through hole"}, {"action": "submit"})


def test_existing_partial_question_hides_internal_pydantic_dump():
    pending = {'stage': 'image_parameters', 'spec': {'pads': [{'number': None}]},
               'issues': ['body_x：图中缺失，请补充', '30 validation errors for PackageSpec https://errors.pydantic.dev/2.11/v/string_type'],
               'validation_error': '30 validation errors for PackageSpec'}
    public = pcb._public_pending_input(pending)
    assert 'pydantic' not in json.dumps(public, ensure_ascii=False)
    assert '空缺脚号' in public['issues'][1]
    assert public['spec']['pads'][0]['number'] is None
    assert 'validation errors' in pending['validation_error']


def test_invalid_clearance_can_be_corrected_in_the_current_question(monkeypatch, tmp_path):
    async def extract(*args, **kwargs):
        return partial_reading()
    monkeypatch.setattr(I, "extract_image", extract)
    graph = build_pcb_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "bad-clearance"}}
    async def scenario():
        out = await graph.ainvoke({"image_path": str(SAMPLE), "work_dir": str(tmp_path), "mode": "emit_only"}, config)
        bad = six_pad_spec()
        bad["pads"][0]["x"] = -.4
        out = await graph.ainvoke(Command(resume={out["__interrupt__"][0].id: {"action": "submit", "spec": bad}}), config)
        assert out["__interrupt__"][0].value["stage"] == "image_parameters"
        assert "间距校验" in out["__interrupt__"][0].value["validation_error"]
        assert not list(tmp_path.glob("*.il"))
        out = await graph.ainvoke(Command(resume={out["__interrupt__"][0].id:
            {"action": "submit", "spec": six_pad_spec()}}), config)
        assert out["status"] == "skills_ready"
    asyncio.run(scenario())


def test_model_receives_image_via_multimodal_route_and_retries_empty_output(monkeypatch):
    from backend.agents.pcb import contours
    monkeypatch.setattr(contours, "read_two_row_topology", lambda *_: None)
    calls = []
    class Model:
        async def ainvoke(self, messages, **kwargs):
            assert kwargs["extra_body"] == {"enable_thinking": False}
            assert kwargs["max_tokens"] == 8192
            calls.append(messages)
            return None if len(calls) == 1 else partial_reading()
    def get_model(route, schema):
        assert route == "pcb_vision" and schema is I.ImageExtraction
        return Model()
    monkeypatch.setattr(I, "get_structured_llm", get_model)
    result = asyncio.run(I.extract_image(str(SAMPLE)))
    assert result.spec["pin_count"] == 6 and len(calls) == 2
    assert calls[0][1].content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.fixture
def api(monkeypatch, tmp_path):
    row = {"id": str(uuid.uuid4()), "tenant_id": "tenant-a", "status": "awaiting_input",
           "package_params": {"pending_input": {"stage": "image_parameters", "interrupt_id": "current",
                                                  "options": ["submit", "cancel"], "spec": six_pad_spec()}}}
    writes, runs = [], []
    class Result:
        def __init__(self, found=None, scalar=None):
            self.found, self.scalar = found, scalar
        def mappings(self):
            return self
        def first(self):
            return self.found
        def scalar_one_or_none(self):
            return self.scalar
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        @asynccontextmanager
        async def begin(self):
            yield self
        async def commit(self):
            pass
        async def execute(self, sql, params):
            query = str(sql).lstrip()
            if query.startswith("INSERT"):
                writes.append(params)
                row.update(id=params["id"], status="ai_processing", package_params=json.loads(params["package_params"]))
                return Result()
            if params["id"] != row["id"] or params["tenant_id"] != row["tenant_id"]:
                return Result()
            if query.startswith("SELECT"):
                return Result(row)
            writes.append(params)
            if "patch" in params:
                row.update(status="ai_processing")
                row["package_params"].update(json.loads(params["patch"]))
                return Result()
            if "previous_status" in params:
                row.update(status="ai_processing", package_params=json.loads(params["package_params"]))
                return Result(scalar=row["id"])
            row["package_params"].update(json.loads(params["package_params"]))
            return Result()
    async def worker(*args, **kwargs):
        runs.append(args)
    monkeypatch.setattr(pcb, "AsyncSessionLocal", Session)
    monkeypatch.setattr(pcb, "_run_job", worker)
    monkeypatch.setattr(pcb, "PCB_JOB_ROOT", tmp_path)
    app = FastAPI()
    app.include_router(pcb.router, prefix="/pcb")
    app.dependency_overrides[pcb.get_current_user] = lambda: {"tenant_id": "tenant-a", "user_id": "trusted"}
    return app, row, writes, runs, tmp_path


def test_upload_persists_normalized_image_and_state_before_dispatch(api):
    app, row, writes, runs, root = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/pcb/generate-image", files={"file": ("unsafe.exe", SAMPLE.read_bytes(), "application/octet-stream")},
                                         data={"mode": "emit_only", "message": "核对一脚", "execution_mode": "ssh", "allegro_timeout": "90"})
            assert response.status_code == 202
            await asyncio.gather(*list(pcb._background_tasks))
            params = row["package_params"]
            state = pcb._initial_state(params)
            assert state["input_kind"] == "image" and state["message"] == "核对一脚"
            assert state["execution_mode"] == "ssh" and state["allegro_timeout"] == 90
            assert Path(state["image_path"]).parent == root / row["id"]
            assert Path(state["image_path"]).is_file() and len(writes) == len(runs) == 1
            assert params["image_meta"]["width"] == 302
    asyncio.run(scenario())


def test_upload_rejects_invalid_bytes_before_registering_job(api):
    app, _, writes, runs, root = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/pcb/generate-image", files={"file": ("looks.png", b"invalid", "image/png")})
            assert response.status_code == 422
            assert not writes and not runs and not list(root.iterdir())
    asyncio.run(scenario())


@pytest.mark.parametrize("auxiliary", [False, True])
def test_pdf_error_identifies_main_or_auxiliary_file_before_registration(api, auxiliary):
    app, _, writes, runs, root = api
    async def scenario():
        files = [("file", ("footprint.png", SAMPLE.read_bytes(), "image/png"))] if auxiliary else []
        files.append(("context_images" if auxiliary else "file", ("datasheet.PDF", b"%PDF-1.7\n", "application/pdf")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/pcb/generate-image", files=files)
            assert response.status_code == 422
            detail = response.json()["detail"]
            assert ("第1张辅助图" if auxiliary else "主图") in detail
            assert "datasheet.PDF" in detail and ("PDF无法读取" in detail if auxiliary else "辅助资料" in detail)
            assert not writes and not runs and not list(root.iterdir())
    asyncio.run(scenario())


def test_supplementary_images_are_stored_and_recoverable(api):
    app, row, _, _, root = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/pcb/generate-image", files=[
                ("file", ("footprint.png", SAMPLE.read_bytes(), "image/png")),
                ("context_images", ("body.png", SAMPLE.read_bytes(), "image/png")),
            ])
            assert response.status_code == 202
            await asyncio.gather(*list(pcb._background_tasks))
            paths = pcb._initial_state(row['package_params'])['context_image_paths']
            assert len(paths) == 1 and Path(paths[0]).is_relative_to(root / row['id'])
            assert Path(paths[0]).is_file()
    asyncio.run(scenario())


def test_human_api_rejects_invalid_and_stale_input_before_claiming_task(api):
    app, row, writes, runs, _ = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = f"/pcb/packages/{row['id']}/human-input"
            response = await client.post(url, json={"interrupt_id": "old", "answer": {"action": "submit"}})
            assert response.status_code == 409
            response = await client.post(url, json={"interrupt_id": "current", "answer": {"action": "approve"}})
            assert response.status_code == 422 and not writes and not runs
            response = await client.post(url, json={"interrupt_id": "current", "answer": {"action": "submit", "_actor": {"user_id": "forged"}}})
            assert response.status_code == 200
            await asyncio.gather(*list(pcb._background_tasks))
            accepted = row["package_params"]["accepted_input"]
            assert accepted["answer"]["_actor"]["user_id"] == "trusted"
    asyncio.run(scenario())


def test_image_projection_retains_source_and_evidence_for_recovery(api):
    _, row, _, _, root = api
    state = {**pcb._initial_state({"mode": "emit_only", "image_path": str(root / "input.png")}),
             "input_kind": "image", "image_meta": {"width": 302}, "image_reading": partial_reading().model_dump(),
             "execution": {"mode": "ssh", "remote_dir": "C:/pcb/runs/test"},
             "build_run": {"returncode": 0, "ok": True},
             "extraction_issues": ["body_height_max missing"], "status": "image_extracted"}
    snapshot = {"values": state, "interrupts": [{"id": "x", "value": {"stage": "image_parameters"}}]}
    asyncio.run(pcb._persist_job_snapshot(row["id"], row["tenant_id"], snapshot))
    params = row["package_params"]
    assert params["image_reading"]["spec"]["pads"][0]["size_x"] == .5
    assert params["input_kind"] == "image" and params["image_path"] == state["image_path"]
    assert params["extraction_issues"] == state["extraction_issues"]
    assert params['execution'] == state['execution'] and params['build_run']['returncode'] == 0


def test_per_pin_verification_cannot_accept_uniform_pad_widths():
    from backend.agents.pcb.assertions import _judge_pin_geometry
    from backend.agents.pcb.parser import parse_verify_log
    spec = PackageSpec(**six_pad_spec())
    derived = derive_package(spec)
    lines = []
    for pin in derived.pins:
        # Wrong uniform pad width at pin 1 should fail even with correct centers.
        width, height = (.35, .75)
        x, y = pin.x, pin.y
        lines.append(f'PIN number=("{pin.number}") padstack=("test") location=(nil) bbox=((({x-width/2} {y-height/2}) ({x+width/2} {y+height/2}))) rot=(0.0)')
    checks = _judge_pin_geometry(spec, derived, parse_verify_log("\n".join(lines)))
    assert not checks[0].passed and all(c.passed for c in checks[1:])


def test_slow_model_has_a_bounded_deadline(monkeypatch):
    from backend.agents.pcb import contours
    monkeypatch.setattr(contours, "read_two_row_topology", lambda *_: None)
    class SlowModel:
        async def ainvoke(self, messages, **kwargs):
            await asyncio.sleep(10)
    monkeypatch.setattr(I, "get_structured_llm", lambda *args: SlowModel())
    monkeypatch.setattr(I, "VISION_TIMEOUT_SECONDS", .01)
    with pytest.raises(TimeoutError, match="识别超过"):
        asyncio.run(I.extract_image(str(SAMPLE)))


QUAD_SAMPLE = Path(__file__).parent / "golden/pcb/lqfp64/footprint.png"


def quad_geometry(**updates):
    return I.QuadFootprintGeometryReading(**{
        "pitch": .5, "pad_width": .3, "pad_length": 1.2, "outer_span_x": 12.7, "outer_span_y": 12.7,
        "bottom_numbers": ["1", "16"], "right_numbers": ["17", "32"],
        "top_numbers": ["48", "33"], "left_numbers": ["64", "49"],
        "source_units": "mm", "confidence": .95, "evidence": "Figure 72 labels .5, .3, 1.2, 12.7", **updates})


def test_four_row_contours_recognize_real_lqfp_without_reading_physical_sizes():
    from backend.agents.pcb.contours import read_four_row_topology
    assert read_four_row_topology(QUAD_SAMPLE.read_bytes())["pads_per_side"] == 16
    assert read_four_row_topology(SAMPLE.read_bytes()) is None


def test_quad_coordinates_match_labeled_extents_and_pin_directions():
    pads = I.pads_from_quad(quad_geometry(), 16)
    by_number = {p["number"]: p for p in pads}
    assert len(by_number) == 64
    assert by_number["1"] == {"number": "1", "x": -3.75, "y": -5.75, "size_x": .3, "size_y": 1.2}
    assert (by_number["16"]["x"], by_number["16"]["y"]) == (3.75, -5.75)
    assert (by_number["17"]["x"], by_number["17"]["y"]) == (5.75, -3.75)
    assert (by_number["33"]["x"], by_number["33"]["y"]) == (3.75, 5.75)
    assert (by_number["49"]["x"], by_number["49"]["y"]) == (-5.75, 3.75)
    spec = PackageSpec(name="LQFP64_10x10", body_x=10, body_y=10, body_height_max=1.6,
                       pin_count=64, pin_layout="EXPLICIT", pads=pads,
                       pin_numbering=[p["number"] for p in pads], ep_present=False, pin1_marker="NONE")
    assert check_clearances(spec) == []
    assert max(p["y"]+p["size_y"]/2 for p in pads) == pytest.approx(6.35)


def test_quad_endpoint_expansion_requires_matching_visible_count():
    numbers=["1", *([None]*14), "16"]
    pads=I.pads_from_quad(quad_geometry(bottom_numbers=numbers),16)
    assert [p["number"] for p in pads[:16]] == [str(n) for n in range(1,17)]
    pads=I.pads_from_quad(quad_geometry(bottom_numbers=["1","15"]),16)
    assert all(p["number"] is None for p in pads[:16])


def test_real_parser_exception_is_retried_with_corrective_feedback(monkeypatch):
    from langchain_core.exceptions import OutputParserException
    calls=[]
    class Model:
        async def ainvoke(self,messages,**kwargs):
            calls.append(messages)
            if len(calls)==1:raise OutputParserException("completion []")
            assert "不能输出 []" in messages[-1].content
            return quad_geometry()
    monkeypatch.setattr(I,"get_structured_llm",lambda *args:Model())
    assert asyncio.run(I._invoke_reading(I.QuadFootprintGeometryReading,[],2048)).pad_width==.3
    assert len(calls)==2


def test_repeated_empty_array_returns_a_short_error_and_never_guesses(monkeypatch):
    calls=[]
    class Model:
        async def ainvoke(self,*args,**kwargs):calls.append(1);return []
    monkeypatch.setattr(I,"get_structured_llm",lambda *args:Model())
    with pytest.raises(ValueError,match="连续返回空数组"):
        asyncio.run(I._invoke_reading(I.ImageExtraction,[],8192))
    assert len(calls)==2


def test_transport_failures_are_not_retried_as_parsing_errors(monkeypatch):
    calls=[]
    class Model:
        async def ainvoke(self,*args,**kwargs):calls.append(1);raise ConnectionError("unavailable")
    monkeypatch.setattr(I,"get_structured_llm",lambda *args:Model())
    with pytest.raises(ConnectionError):asyncio.run(I._invoke_reading(I.ImageExtraction,[],8192))
    assert len(calls)==1


def test_four_row_extraction_keeps_unknown_body_dimensions(monkeypatch):
    class Model:
        def __init__(self,schema):self.schema=schema
        async def ainvoke(self,*args,**kwargs):
            if self.schema is I.QuadFootprintGeometryReading:return quad_geometry()
            return I.PackageImageMetadata(ep_present=False,pin_count=64,evidence="No body dimensions on primary footprint",confidence=.95,source_units="mm")
    monkeypatch.setattr(I,"get_structured_llm",lambda route,schema:Model(schema))
    reading=asyncio.run(I.extract_image(str(QUAD_SAMPLE)))
    candidate,issues=I.assess_extraction(reading)
    assert len(candidate["pads"])==64 and candidate["pads"][0]["size_x"]==.3
    assert "body_x" not in candidate and "body_height_max" not in candidate
    assert any("body_x" in issue for issue in issues)
    assert not any("pydantic.dev" in issue for issue in issues)


def test_retry_failed_image_keeps_source_audit_and_rejects_duplicate_requests(api,monkeypatch):
    app,row,writes,runs,root=api
    source=root/row["id"]/"input.png";source.parent.mkdir();source.write_bytes(SAMPLE.read_bytes())
    row.update(status="failed",package_params={"input_kind":"image","image_path":str(source),"image_reading":{}})
    @asynccontextmanager
    async def lock(*args,**kwargs):yield True
    monkeypatch.setattr(pcb,"open_postgres_pcb_job_lock",lock)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
            assert (await c.post(f'/pcb/packages/{uuid.uuid4()}/retry')).status_code==404
            response=await c.post(f'/pcb/packages/{row["id"]}/retry')
            assert response.status_code==202
            await asyncio.gather(*list(pcb._background_tasks))
            assert row["package_params"]["accepted_retry"]["user_id"]=="trusted"
            assert len(row["package_params"]["retry_history"])==1
            assert source.read_bytes()==SAMPLE.read_bytes()
            assert (await c.post(f'/pcb/packages/{row["id"]}/retry')).status_code==409
            assert len(runs)==1
    asyncio.run(scenario())


def test_retry_recovery_does_not_replay_an_already_applied_retry(monkeypatch):
    retry={"id":"retry-1"}
    row={"status":"ai_processing","package_params":{"accepted_retry":retry}}
    snapshot={"checkpoint_id":"cp","values":{"extraction_retry_id":"retry-1","status":"image_extracted"},
              "next_nodes":[],"interrupts":[{"id":"q","value":{"stage":"image_parameters"}}]}
    events=[]
    async def read(*args):return row
    async def snap(*args):return snapshot
    async def sync(*args):events.append('project')
    async def rerun(*args,**kwargs):pytest.fail('must not re-extract an applied retry')
    monkeypatch.setattr(pcb,'_read_worker_job',read);monkeypatch.setattr(pcb,'get_postgres_pcb_snapshot',snap)
    monkeypatch.setattr(pcb,'_sync_job_projection',sync);monkeypatch.setattr(pcb,'resume_postgres_pcb_graph_at_node',rerun)
    asyncio.run(pcb._run_owned_job(str(uuid.uuid4()),'tenant-a',recovering=True))
    assert events==['project']

def test_empty_image_question_accepts_complete_manual_pad_table():
    from backend.agents.pcb.human_nodes import validate_human_answer
    request={'stage':'image_parameters','options':['submit','cancel'],
             'spec':{'name':'SOT223_MANUAL','family':'SMD','body_x':6.6,'body_y':3.7,'body_height_max':1.8,'ep_present':False,'pads':[]}}
    patch={'pin_layout':'EXPLICIT','pin1_marker':'NONE','pin_count':2,'pin_numbering':['1','2'],
           'pads':[{'number':str(i+1),'shape':'RECTANGLE','x':x,'y':-3.07,'size_x':.82,'size_y':1.16} for i,x in enumerate((-2.3,2.3))]}
    result=validate_human_answer(request,{'action':'submit','spec':patch})
    assert result['spec']['pin_count']==2
    assert result['spec']['pin_numbering']==['1','2']
    assert [p['x'] for p in result['spec']['pads']]==[-2.3,2.3]
