"""Isolated API/CLI tests for generated STEP review artifacts.

These tests use temporary files and a fake SQL row; they do not access PostgreSQL,
run the drawing agent, or approve a review interrupt.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import uuid

from fastapi import FastAPI, HTTPException
import httpx
import pytest

from backend.agents.step.families.ic import gullwing_ic
from backend.api.v1 import step, verify_step_e2e


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def fetchone(self):
        return self.row


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, _query, params):
        row = self.rows.get((str(params["id"]), params["tenant_id"]))
        return _Result(row)


class _SessionFactory:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return _Session(self.rows)


@pytest.fixture
def artifact_api(monkeypatch, tmp_path):
    drawing_id = uuid.uuid4()
    root = tmp_path / str(drawing_id)
    root.mkdir()
    paths = {}
    for view in step.PREVIEW_VIEWS:
        path = root / f"{view}.png"
        path.write_bytes(f"png:{view}".encode())
        paths[view] = str(path)
    step_path = root / "candidate.step"
    step_path.write_bytes(b"solid candidate\nendsolid candidate\n")
    row = {
        "output_path": str(step_path),
        "preview_path": paths["isometric"],
        "package_params": {
            "preview_paths": paths,
            "feature_ir": {"assumptions": ["本体按包络简化，待审核"]},
        },
    }
    monkeypatch.setattr(step, "STEP_JOB_ROOT", tmp_path)
    monkeypatch.setattr(step, "AsyncSessionLocal", _SessionFactory({(str(drawing_id), "tenant-a"): row}))
    app = FastAPI()
    app.include_router(step.router, prefix="/api/v1/step")
    app.dependency_overrides[step.get_current_user] = lambda: {"tenant_id": "tenant-a"}
    return app, drawing_id, row, paths, step_path


@pytest.mark.asyncio
async def test_four_preview_views_and_legacy_preview_alias(artifact_api):
    app, drawing_id, _row, paths, _step_path = artifact_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for view, expected_path in paths.items():
            response = await client.get(f"/api/v1/step/drawings/{drawing_id}/artifacts/preview_{view}")
            assert response.status_code == 200
            assert response.content == Path(expected_path).read_bytes()
        legacy = await client.get(f"/api/v1/step/drawings/{drawing_id}/artifacts/preview")
        assert legacy.status_code == 200
        assert legacy.content == Path(paths["isometric"]).read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["step", "preview", "preview_isometric", "preview_front", "preview_top", "preview_right"])
async def test_invalid_name_and_cross_tenant_are_not_found(artifact_api, artifact):
    app, drawing_id, _row, _paths, _step_path = artifact_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.get(f"/api/v1/step/drawings/{drawing_id}/artifacts/preview_unknown")
        assert invalid.status_code == 404
        app.dependency_overrides[step.get_current_user] = lambda: {"tenant_id": "tenant-b"}
        cross_tenant = await client.get(f"/api/v1/step/drawings/{drawing_id}/artifacts/{artifact}")
        assert cross_tenant.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("view", step.PREVIEW_VIEWS)
async def test_preview_path_outside_job_root_is_not_found(artifact_api, tmp_path, view):
    app, drawing_id, row, _paths, _step_path = artifact_api
    outside = tmp_path / "outside.step"
    outside.write_bytes(b"outside")
    row["package_params"]["preview_paths"][view] = str(outside)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/step/drawings/{drawing_id}/artifacts/preview_{view}")
    assert response.status_code == 404


def test_review_payload_exposes_urls_and_assumptions(artifact_api):
    _app, drawing_id, row, _paths, _step_path = artifact_api
    payload = step._public_pending_input(
        {"stage": "review", "interrupt_id": "review-1", "question": "审核结果"},
        drawing_id,
        row,
    )
    assert payload["modeling_assumptions"] == ["本体按包络简化，待审核"]
    assert set(payload["preview_urls"]) == set(step.PREVIEW_VIEWS)
    assert payload["step_file_url"].endswith("/artifacts/step")


@pytest.mark.parametrize("legacy_source", ["pending_input", "golden_comparison"])
def test_review_payload_reads_legacy_preview_path_projection(artifact_api, legacy_source):
    _app, drawing_id, row, paths, _step_path = artifact_api
    row["package_params"].pop("preview_paths")
    row["package_params"][legacy_source] = {"previews": paths}
    payload = step._public_pending_input(
        {"stage": "review", "interrupt_id": "review-legacy"}, drawing_id, row,
    )
    assert set(payload["preview_urls"]) == set(step.PREVIEW_VIEWS)


@pytest.mark.parametrize("suggested", [None, {}, {"family_id": "misc"}, {"family_id": "transistor"}])
def test_legacy_routing_projection_removes_unexecutable_confirm_without_mutating_checkpoint(suggested):
    pending = {
        "stage": "routing", "interrupt_id": "routing-legacy",
        "suggested": suggested, "options": ["confirm", "change", "cancel"],
    }
    before = copy.deepcopy(pending)
    projected = step._public_pending_input(pending, uuid.uuid4(), {})
    assert projected["interrupt_id"] == "routing-legacy"
    assert projected["options"] == ["change", "cancel"]
    assert projected["confirmation_unavailable_reason"]
    assert pending == before


def test_routing_projection_keeps_confirm_for_a_resolvable_family():
    pending = {
        "stage": "routing", "interrupt_id": "routing-valid",
        "suggested": {"family_id": gullwing_ic.FAMILY_ID, "package_type": "SOP"},
        "options": ["confirm", "change", "cancel"],
    }
    projected = step._public_pending_input(pending, uuid.uuid4(), {})
    assert projected["options"] == pending["options"]
    assert "confirmation_unavailable_reason" not in projected


def _cli_fixture(tmp_path):
    job_root = tmp_path / "job"
    job_root.mkdir()
    drawing_id = str(uuid.uuid4())
    files = {"step": job_root / "candidate.step"}
    files["step"].write_bytes(b"step-data")
    for view in step.PREVIEW_VIEWS:
        files[view] = job_root / f"{view}.png"
        files[view].write_bytes(f"image-{view}".encode())
    job = {
        "step_file_url": "/step",
        "preview_urls": {view: f"/preview_{view}" for view in step.PREVIEW_VIEWS},
    }
    db_job = {
        "output_path": str(files["step"]),
        "package_params": {"preview_paths": {view: str(files[view]) for view in step.PREVIEW_VIEWS}},
    }
    bodies = {"/step": files["step"].read_bytes()}
    bodies.update({f"/preview_{view}": files[view].read_bytes() for view in step.PREVIEW_VIEWS})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=bodies[request.url.path]))
    client = httpx.Client(transport=transport, base_url="http://test")
    return drawing_id, job, db_job, files, client


def test_cli_download_hash_checks_each_view_and_step(tmp_path):
    _drawing_id, job, db_job, _files, client = _cli_fixture(tmp_path)
    verify_step_e2e.verify_artifact_downloads(client, {}, job, db_job)
    client.close()


@pytest.mark.parametrize("mutation", ["missing_view", "hash_mismatch", "path_mismatch"])
def test_cli_rejects_missing_or_inconsistent_review_artifacts(tmp_path, mutation):
    _drawing_id, job, db_job, files, client = _cli_fixture(tmp_path)
    if mutation == "missing_view":
        job["preview_urls"].pop("right")
    elif mutation == "hash_mismatch":
        client.close()
        def handler(request):
            if request.url.path == "/preview_front":
                return httpx.Response(200, content=b"wrong")
            body = request.url.path.removeprefix("/preview_")
            return httpx.Response(200, content=(files["step"].read_bytes() if request.url.path == "/step" else files[body].read_bytes()))
        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    else:
        # A local file can hash-match while still being unrelated to the graph
        # checkpoint. The persisted-state contract must reject that substitution.
        db_job["package_params"]["preview_paths"]["top"] = str(tmp_path / "outside.png")
        Path(db_job["package_params"]["preview_paths"]["top"]).write_bytes(b"image-top")
    if mutation == "path_mismatch":
        checkpoint_job = {
            "status": "pending_review", "pending_input": {"interrupt_id": "review-1", "stage": "review"},
            "needs_review": True,
        }
        db_job.update(status="pending_review", source_image_path="input.png", preview_path=str(files["isometric"]), needs_review=True)
        db_job["package_params"]["human_history"] = []
        db_job["package_params"]["pending_input"] = {"interrupt_id": "review-1"}
        snapshot = {
            "checkpoint_id": "checkpoint-1", "next_nodes": ["review_result"],
            "interrupts": [{"id": "review-1", "value": {"stage": "review"}}],
            "values": {
                "image_path": "input.png", "status": "review_required", "human_history": [],
                "verification": {"passed": True},
                "result": {"step_file": db_job["output_path"], "previews": {
                    view: str(files[view]) for view in step.PREVIEW_VIEWS
                }},
            },
        }
        with pytest.raises(RuntimeError, match="top 视图路径"):
            verify_step_e2e.check_persisted_state(checkpoint_job, db_job, snapshot, 1)
    else:
        with pytest.raises(RuntimeError):
            verify_step_e2e.verify_artifact_downloads(client, {}, job, db_job)
    client.close()


def test_pending_review_cli_path_does_not_submit_approval(monkeypatch, tmp_path):
    drawing_id, job, db_job, _files, client = _cli_fixture(tmp_path)
    pending = {"stage": "review", "interrupt_id": "review-1", "options": ["approve", "reject"]}
    job.update(status="pending_review", pending_input=pending, drawing_id=drawing_id)
    client.close()
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "synthetic"})
        if request.url.path.endswith(drawing_id):
            return httpx.Response(200, json=job)
        if request.url.path == "/step":
            return httpx.Response(200, content=_files["step"].read_bytes())
        view = request.url.path.removeprefix("/preview_")
        return httpx.Response(200, content=_files[view].read_bytes())

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    monkeypatch.setattr(verify_step_e2e.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(verify_step_e2e, "verify_persisted_job", lambda *_args: asyncio.sleep(0, result=(33, db_job)))
    monkeypatch.setattr(verify_step_e2e.getpass, "getpass", lambda *_args: "synthetic", raising=False)
    args = type("Args", (), {"drawing_id": drawing_id, "image": None, "username": "test-user", "base_url": "http://test", "timeout": 10, "interval": 0.01, "interactive": False})()
    result = verify_step_e2e.run(args)
    assert result == 2
    assert pending["stage"] == "review"
    assert not any(path.endswith("/human-input") for _method, path in requests)
    downloaded = {path for method, path in requests if method == "GET"}
    assert downloaded == {"/api/v1/step/drawings/" + drawing_id} | {
        "/step", *(f"/preview_{view}" for view in step.PREVIEW_VIEWS)
    }
