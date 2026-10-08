"""STEP human-input API contracts using isolated storage and real interrupts.

No external model, CAD generation, PostgreSQL connection or sample run occurs.
The fake SQL store exercises API ownership, status transitions and row locking;
the LangGraph saver exercises checkpoint identity across newly compiled graphs.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
import pytest

from backend.agents.step import persistence
from backend.agents.step.state import ImageToStepState
from backend.api.v1 import step


class _Result:
    def __init__(self, row=None, scalar=None):
        self.row = row
        self.scalar = scalar

    def mappings(self):
        return self

    def fetchone(self):
        return dict(self.row) if self.row else None

    def all(self):
        return [dict(self.row)] if self.row else []

    def scalar_one_or_none(self):
        return self.scalar


class _Store:
    def __init__(self, row):
        self.row = row
        self.lock = asyncio.Lock()
        self.statements = []
        self.deleted = False
        self.checkpoint_tables = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}

    def session(self):
        return _Session(self)


class _Session:
    def __init__(self, store):
        self.store = store
        self.locked = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.locked:
            self.store.lock.release()
            self.locked = False

    @asynccontextmanager
    async def begin(self):
        yield self

    async def commit(self):
        pass

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.store.statements.append((sql, dict(params or {})))
        if "to_regclass" in sql:
            return _Result(scalar=params["table"] if params["table"] in self.store.checkpoint_tables else None)
        if sql.startswith("DELETE FROM checkpoint"):
            return _Result()
        if params is None:
            assert "projection_sync_failed" in sql
            row = self.store.row
            return _Result(row=row if row["status"] == "ai_processing" or row["package_params"].get("projection_sync_failed") else None)
        if "FOR UPDATE" in sql:
            await self.store.lock.acquire()
            self.locked = True
        row = self.store.row
        if self.store.deleted or str(params["id"]) != str(row["id"]) or params["tenant_id"] != row["tenant_id"]:
            return _Result()
        if sql.startswith("DELETE FROM step_drawings"):
            self.store.deleted = True
            return _Result()
        if sql.lstrip().startswith("SELECT"):
            return _Result(row=row)
        if "event" in params:
            logs = row["package_params"].setdefault("execution_logs", [])
            logs.extend(json.loads(params["event"]))
            row["package_params"]["execution_logs"] = logs[-600:]
            return _Result()
        if "input_update" in params:
            if row["status"] != params["previous_status"]:
                return _Result()
            row["status"] = "ai_processing"
            row["needs_review"] = False
            row["package_params"].update(json.loads(params["input_update"]))
            return _Result(scalar=row["id"])
        if "recovery_update" in params:
            row.update(status="ai_processing", needs_review=False, error_msg=params["error_msg"])
            row["package_params"].update(json.loads(params["recovery_update"]))
            return _Result()
        if "status" in params:
            for key in ("status", "output_path", "preview_path", "needs_review", "reviewed_by", "reviewed_at", "error_msg"):
                row[key] = params[key]
            row["package_params"].pop("accepted_input", None)
            row["package_params"].update(json.loads(params["package_params"]))
            return _Result()
        if "SET status = 'failed'" in sql:
            row.update(status="failed", error_msg=params["error_msg"], needs_review=False)
            row["package_params"]["pending_input"] = None
            row["package_params"]["projection_sync_failed"] = False
            row["package_params"]["worker_interrupted"] = False
            return _Result()
        raise AssertionError(f"Unrecognized SQL in isolated store: {sql}")


@pytest.fixture
def harness(monkeypatch, tmp_path):
    drawing_id = uuid.uuid4()
    user_id = str(uuid.uuid4())
    row = {
        "id": drawing_id, "tenant_id": "tenant-a", "status": "ai_processing",
        "source_image_path": str(tmp_path / "input.png"),
        "output_path": None, "preview_path": None, "package_params": {},
        "error_msg": None, "needs_review": False, "reviewed_by": None,
        "reviewed_at": None, "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    store = _Store(row)
    saver = InMemorySaver()
    questions = [{"stage": "package", "question": "选择封装", "options": ["provide", "auto", "cancel"]}]
    captured = []
    snapshot_reads = []
    worker_gate = asyncio.Lock()
    lock_modes = []

    @asynccontextmanager
    async def job_lock(job_id, *, wait):
        assert str(job_id) == str(drawing_id)
        lock_modes.append(wait)
        if not wait and worker_gate.locked():
            yield False
            return
        async with worker_gate:
            yield True

    def build_graph():
        builder = StateGraph(ImageToStepState)
        for index, question in enumerate(questions):
            def ask(state, request=question, final=index == len(questions) - 1):
                answer = interrupt(request)
                stage = request["stage"]
                actor = answer.get("_actor", {})
                decision = {key: value for key, value in answer.items() if key != "_actor"}
                decision.update(actor)
                update = {
                    "human_history": [*state.get("human_history", []), {"stage": stage, **decision}],
                    {"package": "human_package", "routing": "human_route", "review": "human_review"}[stage]: decision,
                }
                outcome = "completed" if final else "processing"
                if stage == "review":
                    outcome = "reviewed" if answer["action"] == "approve" else "rejected"
                elif answer["action"] == "cancel":
                    outcome = "cancelled"
                update.update(status=outcome, result={"status": outcome, "step_file": str(tmp_path / "candidate.step")})
                return update

            builder.add_node(f"question_{index}", ask)
            builder.add_edge(START if index == 0 else f"question_{index - 1}", f"question_{index}")
        builder.add_edge(f"question_{len(questions) - 1}", END)
        return builder.compile(checkpointer=saver)

    async def invoke(mode, job_id, graph_input):
        captured.append(graph_input)
        return await build_graph().ainvoke(graph_input, persistence.step_checkpoint_config(mode, job_id))

    async def snapshot(mode, job_id):
        snapshot_reads.append(str(job_id))
        return persistence.serialize_step_snapshot(
            await build_graph().aget_state(persistence.step_checkpoint_config(mode, job_id))
        )

    async def seed(stage="package"):
        if stage == "review":
            questions[:] = [{"stage": "review", "question": "审核模型", "can_approve": True}]
        await invoke("image", drawing_id, {"output_dir": str(tmp_path)})
        paused = await snapshot("image", drawing_id)
        row["status"] = step._job_status(paused)
        row["needs_review"] = row["status"] == "pending_review"
        row["package_params"] = {"original_filename": "drawing.png", "pending_input": step._pending_input(paused)}
        return paused

    async def drain():
        await asyncio.gather(*tuple(step._background_tasks))

    app = FastAPI()
    app.include_router(step.router, prefix="/api/v1/step")
    current = {"user_id": user_id, "tenant_id": "tenant-a"}
    app.dependency_overrides[step.get_current_user] = lambda: current
    monkeypatch.setattr(step, "AsyncSessionLocal", store.session)
    monkeypatch.setattr(step, "invoke_postgres_step_graph", invoke)
    monkeypatch.setattr(step, "get_postgres_step_snapshot", snapshot)
    monkeypatch.setattr(step, "open_postgres_step_job_lock", job_lock)
    monkeypatch.setattr(step, "STEP_JOB_ROOT", tmp_path)
    return SimpleNamespace(
        app=app, row=row, store=store, seed=seed, drain=drain,
        snapshot=snapshot, reads=snapshot_reads, captured=captured,
        current=current, questions=questions, drawing_id=drawing_id,
        worker_gate=worker_gate, lock_modes=lock_modes,
        path=f"/api/v1/step/drawings/{drawing_id}",
    )


def test_pause_read_and_resume_use_same_checkpoint_and_server_actor(harness):
    async def run():
        await harness.seed()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            job = (await client.get(harness.path)).json()
            assert job["status"] == "awaiting_input"
            assert job["pending_input"]["stage"] == "package"
            assert job["checkpoint_id"] and job["next_nodes"] == ["question_0"]
            response = await client.post(harness.path + "/human-input", json={
                "interrupt_id": job["pending_input"]["interrupt_id"],
                "answer": {"action": "provide", "package_type": "TSSOP-16", "_actor": {"user_id": "forged"}, "feature_ir": {"bad": True}},
            })
            assert response.status_code == 202
            await harness.drain()
            command = harness.captured[-1]
            accepted = next(iter(command.resume.values()))
            assert accepted["_actor"]["user_id"] == harness.current["user_id"]
            assert "feature_ir" not in accepted
            assert harness.row["status"] == "completed"
            assert harness.row["package_params"]["human_package"]["package_type"] == "TSSOP-16"
            assert harness.row["package_params"]["human_history"][0]["user_id"] == harness.current["user_id"]
            finished = (await client.get(harness.path)).json()
            assert finished["pending_input"] is None
            assert finished["checkpoint_id"] != job["checkpoint_id"]
    asyncio.run(run())


def test_cross_tenant_cannot_read_or_resume_checkpoint(harness):
    async def run():
        await harness.seed()
        count = len(harness.reads)
        harness.current["tenant_id"] = "tenant-b"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            assert (await client.get(harness.path)).status_code == 404
            response = await client.post(harness.path + "/human-input", json={"interrupt_id": "anything", "answer": {"action": "auto"}})
            assert response.status_code == 404
        assert len(harness.reads) == count
        assert harness.row["status"] == "awaiting_input"
    asyncio.run(run())


def test_execution_logs_survive_projection_and_are_tenant_scoped(harness):
    async def run():
        await harness.seed()
        event = {"id": "event-one", "timestamp": "2026-10-08T07:00:00+00:00",
                 "phase": "started", "message": "开始：识别图纸"}
        await step._append_execution_log(str(harness.drawing_id), "tenant-a", event)
        await step._append_execution_log(str(harness.drawing_id), "tenant-b", {**event, "id": "foreign-event"})
        assert harness.row["package_params"]["execution_logs"] == [event]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            paused = (await client.get(harness.path)).json()
            assert paused["execution_logs"] == [event]
            response = await client.post(harness.path + "/human-input", json={
                "interrupt_id": paused["pending_input"]["interrupt_id"], "answer": {"action": "auto"},
            })
            assert response.status_code == 202
            await harness.drain()
            job = (await client.get(harness.path)).json()
            assert job["execution_logs"][0] == event
            assert job["execution_logs"][-1]["phase"] == "completed"
            assert job["result"]["human_history"]
            harness.current["tenant_id"] = "tenant-b"
            assert (await client.get(harness.path)).status_code == 404
    asyncio.run(run())


def test_light_poll_uses_durable_projection_without_reading_graph(harness):
    async def run():
        paused = await harness.seed()
        harness.row["package_params"].update(checkpoint_id=paused["checkpoint_id"], next_nodes=paused["next_nodes"])
        reads = len(harness.reads)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            data = (await client.get(harness.path + "?include_checkpoint=false")).json()
            assert len(harness.reads) == reads
            assert data["checkpoint_metadata_source"] == "projection"
            assert data["pending_input"]["interrupt_id"] == paused["interrupts"][0]["id"]
            assert data["checkpoint_id"] == paused["checkpoint_id"]
            await client.get(harness.path)
            assert len(harness.reads) == reads + 1
            harness.current["tenant_id"] = "tenant-b"
            assert (await client.get(harness.path + "?include_checkpoint=false")).status_code == 404
    asyncio.run(run())


def test_retry_truncated_view_keeps_ocr_and_skips_earlier_stages(harness, monkeypatch):
    calls = []
    async def run():
        failed = {"checkpoint_id": "failed-view", "next_nodes": [], "interrupts": [], "values": {
            "status": "failed", "current_view_id": "view-1", "current_prompt_evidence": {"ocr_tokens": ["evidence"]},
            "errors": ["Could not parse response content as the length limit was reached"],
        }}
        snapshots = [failed, {"checkpoint_id": "recovered", "next_nodes": [], "interrupts": [], "values": {
            "status": "completed", "artifact_paths": {"step": "candidate.step"},
        }}]
        async def snapshot(*args):
            return snapshots.pop(0)
        async def resume(mode, job_id, updates, *, as_node):
            calls.append((as_node, updates))
        monkeypatch.setattr(step, "get_postgres_step_snapshot", snapshot)
        monkeypatch.setattr(step, "resume_postgres_step_graph_at_node", resume)
        harness.row["status"] = "ai_processing"
        harness.row["package_params"]["retry_from_checkpoint"] = True
        await _recover(harness)
        assert calls == [("retrieve_view_evidence", {"status": "view_evidence_retrieved", "errors": [], "result": {}})]
        assert not harness.captured
        assert harness.row["status"] == "completed"
    asyncio.run(run())


@pytest.mark.parametrize("job_status", ["awaiting_input", "pending_review", "completed", "reviewed", "failed", "stopped", "rejected"])
def test_delete_idle_session_removes_record_checkpoints_and_only_its_files(harness, tmp_path, job_status):
    harness.row["status"] = job_status
    job_dir = tmp_path / str(harness.drawing_id)
    job_dir.mkdir()
    (job_dir / "drawing.png").write_bytes(b"test drawing")
    other_file = tmp_path / "another-session" / "keep.step"
    other_file.parent.mkdir()
    other_file.write_bytes(b"keep")
    # A stored path outside the job directory must never become a delete target.
    harness.row["source_image_path"] = str(other_file)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.delete(harness.path)
            assert response.status_code == 200
            assert response.json() == {"drawing_id": str(harness.drawing_id), "deleted": True, "files_deleted": True}
            assert (await client.get(harness.path)).status_code == 404
            assert (await client.delete(harness.path)).status_code == 404
            assert (await client.get(harness.path + "/artifacts/step")).status_code == 404
    asyncio.run(run())
    assert harness.store.deleted
    assert not job_dir.exists()
    assert other_file.read_bytes() == b"keep"
    deletes = [(sql, params) for sql, params in harness.store.statements if sql.startswith("DELETE FROM checkpoint")]
    assert len(deletes) == 3
    for _, params in deletes:
        assert params == {"thread_image": f"step:image:{harness.drawing_id}", "thread_drawing": f"step:drawing:{harness.drawing_id}"}


@pytest.mark.parametrize("job_status", ["pending", "ai_processing"])
def test_delete_processing_session_is_rejected(harness, job_status):
    harness.row["status"] = job_status

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            assert (await client.delete(harness.path)).status_code == 409
    asyncio.run(run())
    assert not harness.store.deleted
    assert not harness.lock_modes


def test_delete_other_tenant_session_does_not_touch_files_or_checkpoints(harness, tmp_path):
    harness.row["status"] = "completed"
    harness.current["tenant_id"] = "tenant-b"
    job_dir = tmp_path / str(harness.drawing_id)
    job_dir.mkdir()

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            assert (await client.delete(harness.path)).status_code == 404
    asyncio.run(run())
    assert not harness.store.deleted and job_dir.exists()
    assert not harness.lock_modes
    assert not any(sql.startswith("DELETE") for sql, _ in harness.store.statements)


def test_delete_session_rejects_an_active_worker_even_after_status_changes(harness):
    harness.row["status"] = "awaiting_input"

    async def run():
        async with harness.worker_gate:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
                assert (await client.delete(harness.path)).status_code == 409
    asyncio.run(run())
    assert not harness.store.deleted


def test_delete_session_without_checkpoint_tables(harness):
    harness.row["status"] = "failed"
    harness.store.checkpoint_tables.clear()

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            assert (await client.delete(harness.path)).status_code == 200
    asyncio.run(run())
    assert harness.store.deleted
    assert not any(sql.startswith("DELETE FROM checkpoint") for sql, _ in harness.store.statements)


def test_delete_reports_file_cleanup_failure_without_restoring_session(harness, tmp_path, monkeypatch):
    harness.row["status"] = "completed"
    job_dir = tmp_path / str(harness.drawing_id)
    job_dir.mkdir()

    def locked_file(_path):
        raise PermissionError("file in use")
    monkeypatch.setattr(step.shutil, "rmtree", locked_file)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.delete(harness.path)
            assert response.status_code == 200
            assert response.json()["files_deleted"] is False
    asyncio.run(run())
    assert harness.store.deleted and job_dir.exists()


def test_concurrent_answers_are_claimed_once(harness):
    async def run():
        paused = await harness.seed()
        body = {"interrupt_id": paused["interrupts"][0]["id"], "answer": {"action": "auto"}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            responses = await asyncio.gather(*[client.post(harness.path + "/human-input", json=body) for _ in range(2)])
            assert sorted(response.status_code for response in responses) == [202, 409]
            await harness.drain()
        assert len(harness.captured) == 2  # initial invocation plus one resume
        assert any("FOR UPDATE" in sql for sql, _ in harness.store.statements)
    asyncio.run(run())


def test_stale_answer_rejected_when_next_question_is_waiting(harness):
    async def run():
        harness.questions.append({"stage": "routing", "question": "确认路由", "suggested": {"family_id": "ic/gullwing_ic"}})
        paused = await harness.seed()
        body = {"interrupt_id": paused["interrupts"][0]["id"], "answer": {"action": "auto"}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            assert (await client.post(harness.path + "/human-input", json=body)).status_code == 202
            await harness.drain()
            assert harness.row["status"] == "awaiting_input"
            assert harness.row["package_params"]["pending_input"]["stage"] == "routing"
            assert (await client.post(harness.path + "/human-input", json=body)).status_code == 409
    asyncio.run(run())


def test_invalid_answer_keeps_original_pause(harness):
    async def run():
        paused = await harness.seed()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.post(harness.path + "/human-input", json={
                "interrupt_id": paused["interrupts"][0]["id"], "answer": {"action": "approve"},
            })
            assert response.status_code == 422
        assert harness.row["status"] == "awaiting_input"
        assert len(harness.captured) == 1
    asyncio.run(run())


@pytest.mark.parametrize("answer", [
    {"action": "confirm"},
    {"action": "change", "family_id": "transistor"},
])
def test_unavailable_routing_answer_does_not_consume_interrupt(harness, answer):
    from backend.agents.step.human_nodes import routing_question

    async def run():
        harness.questions[:] = [routing_question({"view_classification": {
            "family_id": "transistor", "category_id": "transistor",
            "package_type": "SOT23", "overall_confidence": 0.99,
        }})]
        paused = await harness.seed()
        interrupt_id = paused["interrupts"][0]["id"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.post(harness.path + "/human-input", json={
                "interrupt_id": interrupt_id, "answer": answer,
            })
            assert response.status_code == 422
            job = (await client.get(harness.path)).json()
            assert job["status"] == "awaiting_input"
            assert job["pending_input"]["interrupt_id"] == interrupt_id
            assert job["pending_input"]["options"] == ["change", "cancel"]
        assert len(harness.captured) == 1
        assert "accepted_input" not in harness.row["package_params"]
    asyncio.run(run())


def test_review_modeling_assumptions_are_visible_through_api(harness):
    async def run():
        assumptions = ["body 使用 box 简化，未建模倒角和模塑斜度"]
        harness.questions[:] = [{
            "stage": "review", "question": "审核模型", "can_approve": True,
            "options": ["approve", "reject"], "modeling_assumptions": assumptions,
        }]
        paused = await harness.seed()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.get(harness.path)
        assert response.status_code == 200
        job = response.json()
        assert job["status"] == "pending_review"
        assert job["pending_input"]["modeling_assumptions"] == assumptions
        assert job["pending_input"]["interrupt_id"] == paused["interrupts"][0]["id"]
        assert harness.row["package_params"]["pending_input"]["modeling_assumptions"] == assumptions
        assert len(harness.captured) == 1
    asyncio.run(run())


@pytest.mark.parametrize(("action", "expected"), [("approve", "reviewed"), ("reject", "rejected")])
def test_review_records_authenticated_reviewer_and_timestamp(harness, action, expected):
    async def run():
        paused = await harness.seed("review")
        assert harness.row["status"] == "pending_review"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.post(harness.path + "/human-input", json={
                "interrupt_id": paused["interrupts"][0]["id"], "answer": {"action": action, "comment": "checked"},
            })
            assert response.status_code == 202
            await harness.drain()
        assert harness.row["status"] == expected
        assert harness.row["reviewed_by"] == harness.current["user_id"]
        assert isinstance(harness.row["reviewed_at"], datetime)
        assert harness.row["needs_review"] is False
    asyncio.run(run())


def test_worker_exception_is_saved_without_secondary_logger_error(harness, monkeypatch):
    async def broken(*args):
        raise RuntimeError("original graph failure")
    monkeypatch.setattr(step, "invoke_postgres_step_graph", broken)
    asyncio.run(step._run_image_job(str(harness.drawing_id), "tenant-a", Path("input.png"), Path("output"), "image.png"))
    assert harness.row["status"] == "failed"
    assert harness.row["error_msg"] == "original graph failure"


def test_checkpoint_failure_keeps_business_error_readable(harness, monkeypatch):
    async def unavailable(*args):
        raise RuntimeError("saver unavailable")
    monkeypatch.setattr(step, "get_postgres_step_snapshot", unavailable)
    harness.row.update(status="failed", error_msg="original graph failure")
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.get(harness.path)
            assert response.status_code == 200
            job = response.json()
            assert job["status"] == "failed" and job["error_msg"] == "original graph failure"
            assert job["checkpoint_available"] is False
            assert job["checkpoint_error"] and job["checkpoint_id"] is None
            assert job["pending_input"] is None
    asyncio.run(run())


def test_resume_checkpoint_failure_does_not_consume_answer(harness, monkeypatch):
    async def unavailable(*args):
        raise RuntimeError("saver unavailable")
    async def run():
        paused = await harness.seed()
        monkeypatch.setattr(step, "get_postgres_step_snapshot", unavailable)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
            response = await client.post(harness.path + "/human-input", json={
                "interrupt_id": paused["interrupts"][0]["id"], "answer": {"action": "auto"},
            })
            assert response.status_code == 503
        assert harness.row["status"] == "awaiting_input"
        assert len(harness.captured) == 1
    asyncio.run(run())


@pytest.mark.parametrize("graph_status", ["cancelled", "stopped_insufficient_extraction", "stopped_unsupported_template", "needs_human_follow_up", "review_required"])
def test_stopped_or_unresumable_result_is_never_reported_as_success(graph_status):
    assert step._job_status({"values": {"status": graph_status}, "interrupts": [], "next_nodes": []}) == "stopped"


def test_snapshot_reader_is_read_only_and_preserves_thread_config(monkeypatch):
    seen = []
    job_id = uuid.uuid4()
    interrupt_record = SimpleNamespace(id="interrupt-1", value={"stage": "package"})
    raw = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "checkpoint-1"}},
        values={"status": "initial"}, next=("ask_package",),
        tasks=(SimpleNamespace(interrupts=(interrupt_record,)),),
    )
    @asynccontextmanager
    async def open_graph(mode, *, setup):
        assert mode == "image" and setup is False
        async def get_state(config):
            seen.append(config)
            return raw
        yield SimpleNamespace(aget_state=get_state)
    monkeypatch.setattr(persistence, "open_postgres_step_graph", open_graph)
    result = asyncio.run(persistence.get_postgres_step_snapshot("image", job_id))
    assert seen[0]["configurable"]["thread_id"] == f"step:image:{job_id}"
    assert seen[0]["recursion_limit"] == 256
    assert result["checkpoint_id"] == "checkpoint-1"
    assert result["interrupts"] == [{"id": "interrupt-1", "value": {"stage": "package"}}]
    assert result["next_nodes"] == ["ask_package"]


async def _recover(harness):
    await step._run_image_job(
        str(harness.drawing_id), "tenant-a", Path("original.png"), Path("original-output"),
        "drawing.png", recovering=True,
    )


def test_recovery_skips_a_job_owned_by_another_worker(harness):
    async def run():
        async with harness.worker_gate:
            await _recover(harness)
        assert harness.captured == []
        assert harness.store.statements == []
        assert harness.lock_modes == [False]
    asyncio.run(run())


def test_uncheckpointed_old_job_fails_without_recreating_input(harness):
    asyncio.run(_recover(harness))
    assert harness.row["status"] == "failed"
    assert "没有 PostgreSQL checkpoint" in harness.row["error_msg"]
    assert harness.captured == []


def test_recovery_restores_pending_question_without_answering(harness):
    async def run():
        paused = await harness.seed()
        harness.row["status"] = "ai_processing"
        harness.row["package_params"]["pending_input"] = None
        await _recover(harness)
        assert harness.row["status"] == "awaiting_input"
        assert harness.row["package_params"]["pending_input"]["interrupt_id"] == paused["interrupts"][0]["id"]
        assert len(harness.captured) == 1
    asyncio.run(run())


def test_recovery_consumes_only_the_durable_matching_answer_once(harness):
    async def run():
        paused = await harness.seed()
        accepted = {
            "interrupt_id": paused["interrupts"][0]["id"],
            "answer": {"action": "provide", "package_type": "TSSOP-16", "_actor": {
                "user_id": harness.current["user_id"], "answered_at": datetime.now(timezone.utc).isoformat(),
            }},
        }
        harness.row["status"] = "ai_processing"
        harness.row["package_params"]["accepted_input"] = accepted
        await _recover(harness)
        assert harness.row["status"] == "completed"
        assert next(iter(harness.captured[-1].resume.values())) == accepted["answer"]
        assert "accepted_input" not in harness.row["package_params"]
        await _recover(harness)
        assert len(harness.captured) == 2
    asyncio.run(run())


def test_stale_durable_answer_never_answers_next_question(harness):
    async def run():
        harness.questions.append({"stage": "routing", "question": "确认路由", "suggested": {"family_id": "ic/gullwing_ic"}})
        paused = await harness.seed()
        accepted = {"interrupt_id": paused["interrupts"][0]["id"], "answer": {"action": "auto"}}
        # Simulate a crash after the graph reached its next interrupt, before
        # synchronizing the business row or clearing the already consumed answer.
        await step.invoke_postgres_step_graph("image", harness.drawing_id, step.Command(resume={accepted["interrupt_id"]: accepted["answer"]}))
        harness.row["status"] = "ai_processing"
        harness.row["package_params"]["accepted_input"] = accepted
        await _recover(harness)
        assert len(harness.captured) == 2
        assert harness.row["status"] == "awaiting_input"
        assert harness.row["package_params"]["pending_input"]["stage"] == "routing"
        assert "accepted_input" not in harness.row["package_params"]
    asyncio.run(run())


def test_recovery_with_runnable_checkpoint_passes_none_not_original_input(harness, monkeypatch):
    snapshots = [
        {"checkpoint_id": "in-progress", "next_nodes": ["build_step"], "interrupts": [], "values": {"status": "feature_ir_created"}},
        {"checkpoint_id": "finished", "next_nodes": [], "interrupts": [], "values": {"status": "completed", "artifact_paths": {"step": "generated.step"}}},
    ]
    inputs = []
    async def get_snapshot(*args):
        return snapshots.pop(0)
    async def invoke(mode, job_id, graph_input):
        inputs.append(graph_input)
    monkeypatch.setattr(step, "get_postgres_step_snapshot", get_snapshot)
    monkeypatch.setattr(step, "invoke_postgres_step_graph", invoke)
    asyncio.run(_recover(harness))
    assert inputs == [None]
    assert harness.row["status"] == "completed"


def test_projection_failure_is_recoverable_without_rerunning_graph(harness, monkeypatch):
    persist = step._persist_job_snapshot
    calls = []
    async def fail_once(*args):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("temporary business update failure")
        return await persist(*args)
    monkeypatch.setattr(step, "_persist_job_snapshot", fail_once)
    async def run():
        paused = await harness.seed()
        accepted = {"interrupt_id": paused["interrupts"][0]["id"], "answer": {"action": "auto"}}
        harness.row["status"] = "ai_processing"
        harness.row["package_params"]["accepted_input"] = accepted
        await _recover(harness)
        assert harness.row["status"] == "ai_processing"
        assert harness.row["package_params"]["projection_sync_failed"] is True
        assert "temporary business update failure" in harness.row["error_msg"]
        graph_calls = len(harness.captured)
        await _recover(harness)
        assert harness.row["status"] == "completed"
        assert harness.row["package_params"]["projection_sync_failed"] is False
        assert harness.row["error_msg"] is None
        assert len(harness.captured) == graph_calls
    asyncio.run(run())


def test_cancelled_worker_records_recoverable_status_before_unlock(harness, monkeypatch):
    mark = step._mark_job_recoverable
    async def cancelled(*args):
        raise asyncio.CancelledError()
    async def mark_while_owned(*args, **kwargs):
        assert harness.worker_gate.locked()
        await mark(*args, **kwargs)
    monkeypatch.setattr(step, "invoke_postgres_step_graph", cancelled)
    monkeypatch.setattr(step, "_mark_job_recoverable", mark_while_owned)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await step._run_image_job(str(harness.drawing_id), "tenant-a", Path("input"), Path("out"), "input.png")
        assert harness.row["status"] == "ai_processing"
        assert harness.row["package_params"]["worker_interrupted"] is True
        assert not harness.worker_gate.locked()
    asyncio.run(run())


def test_startup_recovery_scans_failed_projection_records(harness):
    async def run():
        await harness.seed()
        harness.row["status"] = "failed"
        harness.row["package_params"]["projection_sync_failed"] = True
        assert await step.recover_step_jobs() == 1
        await harness.drain()
        assert harness.row["status"] == "awaiting_input"
        assert len(harness.captured) == 1
    asyncio.run(run())


@pytest.mark.parametrize("wait,available,expected", [(False, True, True), (False, False, False), (True, True, True)])
def test_advisory_lock_uses_dedicated_session_and_releases(monkeypatch, wait, available, expected):
    import psycopg

    statements = []
    lifecycle = []
    class Cursor:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, sql, params):
            statements.append((sql, params))
        async def fetchone(self):
            return (available,)
    class Connection:
        async def __aenter__(self):
            lifecycle.append("opened")
            return self
        async def __aexit__(self, *args):
            lifecycle.append("closed")
        def cursor(self):
            return Cursor()
    async def connect(dsn, *, autocommit):
        assert dsn == "test-dsn" and autocommit is True
        return Connection()
    monkeypatch.setattr(persistence, "_postgres_dsn", lambda: "test-dsn")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    job_id = uuid.uuid4()
    async def run():
        with pytest.raises(RuntimeError, match="worker stopped"):
            async with persistence.open_postgres_step_job_lock(job_id, wait=wait) as acquired:
                assert acquired is expected
                raise RuntimeError("worker stopped")
    asyncio.run(run())
    assert lifecycle == ["opened", "closed"]
    assert statements[0][1] == (f"step:image:{job_id}",)
    assert ("pg_advisory_lock" if wait else "pg_try_advisory_lock") in statements[0][0]
    assert len(statements) == (2 if expected else 1)
    if expected:
        assert "pg_advisory_unlock" in statements[-1][0]
