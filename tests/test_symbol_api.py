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
            if params["id"] != row["id"] or params["tenant_id"] != row["tenant_id"]:
                return Result()
            if str(sql).lstrip().startswith("SELECT"):
                return Result(row)
            writes.append(dict(params))
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
    app = FastAPI()
    app.include_router(symbol.router, prefix="/symbol")
    app.dependency_overrides[symbol.get_current_user] = lambda: {"tenant_id": "tenant-a", "user_id": "authenticated-user"}
    return app, row, writes, runs, tmp_path


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
