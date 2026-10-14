"""Isolated symbol API validation, ownership and artifact projection."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import uuid

from fastapi import FastAPI
import httpx
import pytest

from backend.api.v1 import symbol


@pytest.fixture
def api(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    row = {"id": str(uuid.uuid4()), "tenant_id": "tenant-a", "status": "awaiting_input", "needs_review": True,
           "error_msg": None, "created_at": now, "updated_at": now,
           "package_params": {"pending_input": {"interrupt_id": "current", "stage": "device", "options": ["provide", "cancel"]}}}
    writes, runs = [], []

    class Result:
        def __init__(self, found=None, scalar=None):
            self.found, self.scalar = found, scalar

        def mappings(self):
            return self

        def first(self):
            return self.found

        def all(self):
            return self.found if isinstance(self.found, list) else []

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
            if str(sql).lstrip().startswith("INSERT INTO symbol_drawings"):
                writes.append(dict(params))
                row.update(id=params["id"], tenant_id=params["tenant_id"], status="ai_processing",
                           package_params=json.loads(params["package_params"]))
                return Result()
            if "offset" in params:
                if params["tenant_id"] != row["tenant_id"]:
                    return Result([])
                rows = row.get("_history_rows", [row])
                rows = [r for r in rows if bool(r.get("deleted_at")) == params.get("deleted", False)]
                return Result(rows[params["offset"]:params["offset"] + params["limit"]])
            if params["id"] != row["id"] or params["tenant_id"] != row["tenant_id"]:
                return Result()
            if "deleted_at IS NULL" in str(sql) and row.get("deleted_at"):
                return Result()
            if str(sql).lstrip().startswith("SELECT"):
                return Result(row)
            writes.append(dict(params))
            if "deleted_at" in params:
                row.update(deleted_at=params["deleted_at"], package_params=json.loads(params["package_params"]))
                return Result()
            if "previous_status" in params:
                if row["status"] != params["previous_status"]:
                    return Result()
                row.update(status="ai_processing", package_params=json.loads(params["package_params"]))
                return Result(scalar=row["id"])
            row["package_params"].pop("accepted_input", None)
            row["package_params"].update(json.loads(params["package_params"]))
            row.update({key: params[key] for key in ("status", "output_path", "error_msg", "needs_review")})
            return Result()

    async def worker(drawing_id, tenant_id, **kwargs):
        runs.append((drawing_id, tenant_id))

    monkeypatch.setattr(symbol, "AsyncSessionLocal", Session)
    monkeypatch.setattr(symbol, "_run_job", worker)
    monkeypatch.setattr(symbol, "SYMBOL_JOB_ROOT", tmp_path)
    @asynccontextmanager
    async def job_lock(*args, **kwargs):
        yield row.get("_lock_acquired", True)
    monkeypatch.setattr(symbol, "open_postgres_symbol_job_lock", job_lock)
    app = FastAPI()
    app.include_router(symbol.router, prefix="/symbol")
    app.dependency_overrides[symbol.get_current_user] = lambda: {"tenant_id": "tenant-a", "user_id": "authenticated-user"}
    return app, row, writes, runs, tmp_path


def test_image_upload_is_decoded_stored_and_source_downloaded(api):
    from pathlib import Path
    app, row, writes, runs, root = api
    source = (Path(__file__).parent / "golden/symbol/mcp3021/pin-diagram.png").read_bytes()
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/symbol/generate", files={"file": ("MCP3021.png", source, "image/png")}, data={"strict_pages": "true"})
            assert response.status_code == 202
            await asyncio.gather(*list(symbol._background_tasks))
            params = row["package_params"]
            assert params["input_kind"] == "image" and params["source_mime"] == "image/png"
            assert Path(params["source_path"]).read_bytes() == source
            assert Path(params["image_path"]).is_relative_to(root / row["id"])
            assert params["image_meta"]["width"] == 477
            assert symbol._initial_state(params)["auto_generate"] and not symbol._initial_state(params)["strict_pages"]
            assert len(writes) == len(runs) == 1
            response = await client.get(f"/symbol/drawings/{row['id']}/source")
            assert response.status_code == 200 and response.content == source
            assert response.headers["content-type"] == "image/png"
    asyncio.run(scenario())


@pytest.mark.parametrize("strict_pages", [False, True])
def test_pdf_upload_uses_pdf_workflow_and_downloads_original(api, monkeypatch, strict_pages):
    from pathlib import Path
    import fitz
    app, row, writes, runs, root = api
    with fitz.open() as document:
        document.new_page().insert_text((40, 40), "ADS1230 datasheet")
        source = document.tobytes()
    def forbidden(*args, **kwargs):
        pytest.fail("PDF upload called the image decoder")
    monkeypatch.setattr(symbol, "normalize_image", forbidden)
    monkeypatch.setattr(symbol, "MAX_IMAGE_BYTES", 1)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/symbol/generate", files={"file": ("ADS1230.PDF", source, "application/pdf")},
                                         data={"device": "ADS1230", "package": "TSSOP-16", "strict_pages": str(strict_pages).lower()})
            assert response.status_code == 202
            await asyncio.gather(*list(symbol._background_tasks))
            params = row["package_params"]
            assert params["input_kind"] == "pdf" and params["source_mime"] == "application/pdf"
            assert params["pdf_path"] == params["source_path"]
            assert Path(params["pdf_path"]).is_relative_to(root / row["id"])
            assert Path(params["pdf_path"]).read_bytes() == source
            state = symbol._initial_state(params)
            assert state["input_kind"] == "pdf" and not state["auto_generate"]
            assert state["strict_pages"] is strict_pages and not state["image_path"]
            assert params["device"] == "ADS1230" and params["package"] == "TSSOP-16"
            assert len(writes) == len(runs) == 1
            response = await client.get(f"/symbol/drawings/{row['id']}/source")
            assert response.status_code == 200 and response.content == source
            assert response.headers["content-type"] == "application/pdf"
            assert "ADS1230.PDF" in response.headers["content-disposition"]
    asyncio.run(scenario())


@pytest.mark.parametrize("filename,content", [("manual.pdf", b"bad pdf"), ("fake.png", b"%PDF-1.7"),
                                              ("fake.jpg", b"bad"), ("manual.txt", b"%PDF-1.7"), ("animation.gif", b"GIF89a")])
def test_invalid_pdf_images_and_unsupported_formats_are_rejected_before_task_registration(api, filename, content):
    app, _, writes, runs, _ = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/symbol/generate", files={"file": (filename, content)})
            assert response.status_code == 400 and not writes and not runs
    asyncio.run(scenario())


@pytest.mark.parametrize("filename,limit", [("large.png", "MAX_IMAGE_BYTES"), ("large.pdf", "MAX_PDF_SIZE_BYTES")])
def test_upload_size_limit_precedes_decode_and_registration(api, monkeypatch, filename, limit):
    app, _, writes, runs, _ = api
    monkeypatch.setattr(symbol, limit, 5)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/symbol/generate", files={"file": (filename, b"123456789")})
            assert response.status_code == 413 and not writes and not runs
    asyncio.run(scenario())


def test_delete_restore_is_reversible_and_preserves_files_and_input(api):
    app, row, writes, runs, root = api
    row["status"] = "failed"
    row["package_params"].update(device="CRCW040210K0FKED", checkpoint_id="saved-checkpoint")
    original = json.loads(json.dumps(row["package_params"]))
    source = root / "source.pdf"
    source.write_bytes(b"%PDF-source")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = f"/symbol/drawings/{row['id']}"
            response = await client.delete(url)
            assert response.status_code == 200 and response.json()["records_preserved"]
            assert row["deleted_at"] is not None and row["status"] == "failed"
            assert (await client.get("/symbol/drawings")).json()["items"] == []
            assert (await client.get("/symbol/drawings?deleted=true")).json()["items"][0]["drawing_id"] == row["id"]
            assert (await client.get(url)).json()["deleted_at"]
            assert (await client.delete(url)).status_code == 200
            assert len(writes) == 1
            assert source.read_bytes() == b"%PDF-source"
            for key, value in original.items():
                assert row["package_params"][key] == value
            assert (await client.post(url + "/restore")).status_code == 200
            assert row["deleted_at"] is None
            assert len((await client.get("/symbol/drawings")).json()["items"]) == 1
            assert (await client.get("/symbol/drawings?deleted=true")).json()["items"] == []
            assert [e["action"] for e in row["package_params"]["lifecycle_events"]] == ["delete", "restore"]
            assert row["package_params"]["lifecycle_events"][0]["user_id"] == "authenticated-user"
        assert not runs
    asyncio.run(scenario())


@pytest.mark.parametrize("condition", ["foreign", "processing", "locked"])
def test_delete_refuses_foreign_or_running_tasks(api, condition):
    app, row, writes, runs, _ = api
    if condition == "foreign":
        row["tenant_id"] = "tenant-b"
    elif condition == "processing":
        row["status"] = "ai_processing"
    else:
        row["_lock_acquired"] = False
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/symbol/drawings/{row['id']}")
            assert response.status_code == (404 if condition == "foreign" else 409)
        assert not writes and not runs and not row.get("deleted_at")
    asyncio.run(scenario())


def test_deleted_task_cannot_resume_or_be_overwritten_by_projection(api):
    app, row, writes, runs, _ = api
    row["deleted_at"] = datetime.now(timezone.utc)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/symbol/drawings/{row['id']}/human-input", json={
                "interrupt_id": "current", "answer": {"action": "provide", "value": "ADS1115"}})
            assert response.status_code == 409
        await symbol._persist_job_snapshot(row["id"], "tenant-a", {"values": {"status": "completed"}})
        assert not writes and not runs and row["status"] == "awaiting_input"
    asyncio.run(scenario())


def test_checkpoint_trace_retains_complete_state_and_supports_deleted_tasks(api, monkeypatch):
    app, row, _, _, _ = api
    checkpoint_id = str(uuid.uuid4())
    snapshot = {"checkpoint_id": checkpoint_id, "values": {"status": "failed", "table_pins": [{"pin_number": "1"}],
        "errors": ["MinerU missing token"], "document_dir": "job/mineru"}, "metadata": {"step": 3}, "next_nodes": []}
    calls = []
    async def snapshot_reader(drawing_id, **kwargs):
        calls.append((drawing_id, kwargs))
        if kwargs.get("checkpoint_id") and str(kwargs["checkpoint_id"]) != checkpoint_id:
            raise LookupError("missing")
        return snapshot
    async def history_reader(drawing_id, **kwargs):
        calls.append((drawing_id, kwargs))
        return [snapshot, snapshot]
    monkeypatch.setattr(symbol, "get_postgres_symbol_snapshot", snapshot_reader)
    monkeypatch.setattr(symbol, "list_postgres_symbol_snapshots", history_reader)
    row["deleted_at"] = datetime.now(timezone.utc)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = f"/symbol/drawings/{row['id']}"
            trace = (await client.get(url + "/trace")).json()
            assert trace["thread_id"] == "symbol:" + row["id"]
            assert trace["deleted_at"] and trace["latest_checkpoint"]["values"] == snapshot["values"]
            page = (await client.get(url + "/checkpoints?limit=1")).json()
            assert page["has_more"] and page["next_before"] == checkpoint_id
            assert (await client.get(url + f"/checkpoints/{checkpoint_id}")).json()["values"] == snapshot["values"]
            assert (await client.get(url + f"/checkpoints/{uuid.uuid4()}")).status_code == 404
            assert (await client.get(url + "/checkpoints?before=bad-id")).status_code == 422
    asyncio.run(scenario())
    assert calls


@pytest.mark.parametrize("suffix", ["trace", "checkpoints", "checkpoints/00000000-0000-0000-0000-000000000001", "restore"])
def test_foreign_task_trace_and_restore_are_denied_before_checkpoint_access(api, monkeypatch, suffix):
    app, row, writes, _, _ = api
    row["tenant_id"] = "other-tenant"
    async def forbidden(*args, **kwargs):
        pytest.fail("foreign checkpoint was accessed")
    monkeypatch.setattr(symbol, "get_postgres_symbol_snapshot", forbidden)
    monkeypatch.setattr(symbol, "list_postgres_symbol_snapshots", forbidden)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            method = client.post if suffix == "restore" else client.get
            assert (await method(f"/symbol/drawings/{row['id']}/{suffix}")).status_code == 404
        assert not writes
    asyncio.run(scenario())


def test_invalid_input_is_rejected_before_claiming_job(api):
    app, row, writes, runs, _ = api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/symbol/drawings/{row['id']}/human-input", json={"interrupt_id": "current", "answer": {"action": "approve"}})
            assert response.status_code == 422
        assert row["status"] == "awaiting_input"
        assert writes == [] and runs == []
        assert "accepted_input" not in row["package_params"]

    asyncio.run(scenario())


def test_valid_answer_is_normalized_and_actor_is_authenticated(api):
    app, row, writes, runs, _ = api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = {"interrupt_id": "current", "answer": {"action": "provide", "value": " ADS1115 ", "extra": "ignored", "_actor": {"user_id": "forged"}}}
            response = await client.post(f"/symbol/drawings/{row['id']}/human-input", json=body)
            assert response.status_code == 200
            await asyncio.gather(*list(symbol._background_tasks))
            answer = row["package_params"]["accepted_input"]["answer"]
            assert answer["value"] == "ADS1115" and "extra" not in answer
            assert answer["_actor"]["user_id"] == "authenticated-user"
            repeat = await client.post(f"/symbol/drawings/{row['id']}/human-input", json=body)
            assert repeat.status_code == 409
        assert len(writes) == 1 and len(runs) == 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            status = (await client.get(f"/symbol/drawings/{row['id']}")).json()
            assert status["pending_input"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("foreign,expired", [(True, False), (False, True)])
def test_foreign_and_stale_answers_cannot_claim_job(api, foreign, expired):
    app, row, writes, runs, _ = api
    if foreign:
        row["tenant_id"] = "tenant-b"

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/symbol/drawings/{row['id']}/human-input", json={"interrupt_id": "stale" if expired else "current", "answer": {"action": "provide", "value": "ADS1115"}})
            assert response.status_code == (404 if foreign else 409)
        assert writes == [] and runs == []

    asyncio.run(scenario())


def test_stopped_script_is_projected_and_downloadable(api):
    app, row, _, _, root = api
    job_dir = root / row["id"]
    job_dir.mkdir()
    script = job_dir / "ADS1115_gen.tcl"
    script.write_text("portable Tcl", encoding="utf-8")
    state = {"status": "stopped_no_toolchain", "device": "ADS1115", "original_filename": "ADS1115.pdf", "artifact_paths": {"tcl": str(script)}, "result": {"status": "stopped_no_toolchain"}}

    async def scenario():
        await symbol._persist_job_snapshot(row["id"], "tenant-a", {"values": state})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/symbol/drawings/{row['id']}/artifacts/tcl")
            assert response.status_code == 200 and response.text == "portable Tcl"
            status = (await client.get(f"/symbol/drawings/{row['id']}")).json()
            assert status["status"] == "stopped" and status["artifacts"] == ["tcl"]
        assert row["package_params"]["original_filename"] == "ADS1115.pdf"

    asyncio.run(scenario())


def test_capture_failure_log_is_projected_and_downloadable(api):
    app, row, _, _, root = api
    job_dir = root / row["id"]
    job_dir.mkdir()
    log = job_dir / "capture.log"
    log.write_text("License not found", encoding="utf-8")
    state = {"status": "failed", "errors": ["License not found"],
             "artifact_paths": {"capture_log": str(log)},
             "capture": {"execution": {"mode": "ssh", "exit_code": 1}, "log_tail": "License not found"}}

    async def scenario():
        await symbol._persist_job_snapshot(row["id"], "tenant-a", {"values": state})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/symbol/drawings/{row['id']}/artifacts/capture_log")
            assert response.status_code == 200 and response.text == "License not found"
            assert response.headers["content-type"].startswith("text/plain")
            status = (await client.get(f"/symbol/drawings/{row['id']}")).json()
            assert status["capture_execution"]["exit_code"] == 1
            assert status["capture_log_tail"] == "License not found"
            assert status["artifacts"] == ["capture_log"]

    asyncio.run(scenario())


def test_artifact_outside_job_directory_cannot_download(api):
    app, row, _, _, root = api
    outside = root / "other.tcl"
    outside.write_text("outside", encoding="utf-8")
    row["package_params"]["artifacts"] = {"tcl": str(outside)}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/symbol/drawings/{row['id']}/artifacts/tcl")
            assert response.status_code == 404

    asyncio.run(scenario())


def test_source_download_checks_ownership_and_path(api):
    app, row, _, _, root = api
    job_dir = root / row["id"]
    job_dir.mkdir()
    pdf = job_dir / "input.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    row["package_params"].update(pdf_path=str(pdf), original_filename="ADS1230.pdf")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = f"/symbol/drawings/{row['id']}/source"
            response = await client.get(url)
            assert response.status_code == 200 and response.content == pdf.read_bytes()
            assert response.headers["content-type"] == "application/pdf"
            row["tenant_id"] = "tenant-b"
            assert (await client.get(url)).status_code == 404
            row["tenant_id"] = "tenant-a"
            outside = root / "another-user.pdf"
            outside.write_bytes(pdf.read_bytes())
            row["package_params"]["pdf_path"] = str(outside)
            assert (await client.get(url)).status_code == 404

    asyncio.run(scenario())


def test_frontend_projection_has_layout_pins_and_safe_urls(api):
    app, row, _, _, _ = api
    state = {"status": "awaiting_input", "merged_pins": [{"pin_number": "1", "name": "A", "side": "left", "evidence": [{"internal": "not public"}]}],
             "layout": {"part_name": "ADS1230", "body_width": 100, "body_height": 20, "half_height": 10,
                        "pins": [{"number": "1", "name": "A", "x": -50, "y": 0, "hx": -60, "hy": 0, "side": "left"}]},
             "artifact_paths": {"tcl": "hidden/internal.tcl"}}

    async def scenario():
        await symbol._persist_job_snapshot(row["id"], "tenant-a", {"values": state})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = (await client.get(f"/symbol/drawings/{row['id']}")).json()
            assert body["layout"]["pins"][0]["hx"] == -60
            assert "evidence" not in body["pins"][0]
            assert body["artifact_urls"]["tcl"] == f"/api/v1/symbol/drawings/{row['id']}/artifacts/tcl"
            assert "hidden/internal" not in json.dumps(body)
    asyncio.run(scenario())


def test_list_pagination_and_metadata(api):
    app, row, _, _, _ = api
    row["package_params"].update(original_filename="ADS1230.pdf", package="TSSOP-16")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = (await client.get("/symbol/drawings?limit=20&offset=0")).json()
            assert body["items"][0]["original_filename"] == "ADS1230.pdf"
            assert body["items"][0]["package"] == "TSSOP-16"
            assert not body["has_more"] and body["offset"] == 0
            assert (await client.get("/symbol/drawings?offset=1")).json()["items"] == []
            assert (await client.get("/symbol/drawings?offset=-1")).status_code == 422
            assert (await client.get("/symbol/drawings?limit=10000")).status_code == 422
            row["_history_rows"] = [dict(row), dict(row), dict(row)]
            first = (await client.get("/symbol/drawings?limit=1")).json()
            assert first["has_more"] and len(first["items"]) == 1
            last = (await client.get("/symbol/drawings?limit=1&offset=2")).json()
            assert not last["has_more"] and len(last["items"]) == 1
            row["tenant_id"] = "tenant-b"
            assert (await client.get("/symbol/drawings")).json()["items"] == []
    asyncio.run(scenario())


def test_invalid_task_ids_are_rejected_before_database(api):
    app, _, writes, runs, _ = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for suffix in ("", "/source", "/artifacts/olb"):
                assert (await client.get(f"/symbol/drawings/not-a-uuid{suffix}")).status_code == 422
            assert (await client.post("/symbol/drawings/not-a-uuid/human-input", json={"interrupt_id": "a", "answer": {"action": "cancel"}})).status_code == 422
        assert writes == [] and runs == []
    asyncio.run(scenario())


def test_symbol_ui_and_whitelisted_assets_are_served(api):
    app, _, _, _, _ = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/symbol/ui")
            assert response.status_code == 200 and 'id="upload-form"' in response.text
            assert 'id="question-area"' in response.text and response.headers["cache-control"] == "no-store"
            for asset in ("app.js", "api.js", "styles.css"):
                assert (await client.get(f"/symbol/ui/assets/{asset}")).status_code == 200
            assert (await client.get("/symbol/ui/assets/config.py")).status_code == 404
    asyncio.run(scenario())


def test_generate_binds_frontend_multipart_and_validates_hint_lengths(api, monkeypatch):
    app, row, _, _, _ = api
    received = []
    async def submit(file, **kwargs):
        received.append({"filename": file.filename, "content": await file.read(), **kwargs})
        return {"drawing_id": row["id"], "status": "ai_processing"}
    monkeypatch.setattr(symbol, "_submit_job", submit)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/symbol/generate", files={"file": ("ADS1230.png", b"\x89PNG-fixture", "image/png")},
                                         data={"device": "ADS1230", "package": "TSSOP-16"})
            assert response.status_code == 202
            assert received[0]["strict_pages"] is False and received[0]["package"] == "TSSOP-16"
            assert received[0]["filename"] == "ADS1230.png" and received[0]["content"].startswith(b"\x89PNG")
            assert received[0]["current_user"]["user_id"] == "authenticated-user"
            response = await client.post("/symbol/generate", files={"file": ("ADS1230.pdf", b"%PDF-1.7", "application/pdf")},
                                         data={"device": "ADS1230", "package": "TSSOP-16", "strict_pages": "true"})
            assert response.status_code == 202 and received[1]["strict_pages"] is True
            assert received[1]["filename"] == "ADS1230.pdf" and received[1]["content"].startswith(b"%PDF-")
            response = await client.post("/symbol/generate", files={"file": ("a.png", b"\x89PNG")}, data={"device": "a" * 201})
            assert response.status_code == 422 and len(received) == 2
    asyncio.run(scenario())


def test_task_routes_require_authentication(monkeypatch):
    app = FastAPI()
    app.include_router(symbol.router, prefix="/symbol")
    def forbidden_db():
        raise AssertionError("Unauthenticated requests must never reach storage")
    monkeypatch.setattr(symbol, "AsyncSessionLocal", forbidden_db)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            identifier = str(uuid.uuid4())
            for url in ("/symbol/drawings", f"/symbol/drawings/{identifier}", f"/symbol/drawings/{identifier}/source", f"/symbol/drawings/{identifier}/artifacts/olb"):
                assert (await client.get(url)).status_code in {401, 403}
                assert (await client.get(url, headers={"Authorization": "Bearer invalid-token"})).status_code == 401
    asyncio.run(scenario())


def test_self_check_stop_exposes_report_and_reason(api):
    app, row, _, _, _ = api
    state = {"status": "stopped_check_failed", "check_report": {"ok": False, "verdict": "broken", "score": 75},
             "result": {"status": "stopped_check_failed", "reason": "引脚侧别未定", "verdict": "broken", "score": 75}}

    async def scenario():
        await symbol._persist_job_snapshot(row["id"], "tenant-a", {"values": state})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = (await client.get(f"/symbol/drawings/{row['id']}")).json()
            assert body["graph_status"] == "stopped_check_failed"
            assert body["reason"] == "引脚侧别未定"
            assert body["check_report"]["ok"] is False
            assert body["verdict"] == "broken"

    asyncio.run(scenario())
