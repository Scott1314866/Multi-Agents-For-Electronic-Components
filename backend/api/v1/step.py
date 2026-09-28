"""API endpoints for STEP drawing generation jobs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from backend.agents.step.human_nodes import validate_human_answer
from backend.agents.step.persistence import (
    get_postgres_step_snapshot,
    invoke_postgres_step_graph,
    open_postgres_step_job_lock,
)
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal, get_current_user


router = APIRouter()
logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
STEP_JOB_ROOT = PROJECT_ROOT / "output" / "step_agent"
MAX_IMAGE_SIZE_BYTES = 25 * 1024 * 1024
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
PREVIEW_VIEWS = ("isometric", "front", "top", "right")
_background_tasks: set[asyncio.Task] = set()


class HumanInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=256)
    answer: dict[str, Any]


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"不可序列化的 STEP 任务结果：{type(value).__name__}")


async def _update_job_failed(drawing_id: str, tenant_id: str, error: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE step_drawings
                SET status = 'failed', error_msg = :error_msg,
                    needs_review = FALSE,
                    package_params = COALESCE(package_params, '{}'::jsonb)
                        || '{"pending_input": null, "projection_sync_failed": false,
                             "worker_interrupted": false}'::jsonb,
                    updated_at = NOW()
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {
                "id": drawing_id,
                "tenant_id": tenant_id,
                "error_msg": error[:4000],
            },
        )
        await session.commit()


def _pending_input(snapshot: dict) -> dict | None:
    interrupts = snapshot.get("interrupts") or []
    if not interrupts:
        return None
    item = interrupts[0]
    request = item.get("value")
    if not isinstance(request, dict) or request.get("stage") not in {
        "package", "routing", "review"
    }:
        raise ValueError("STEP checkpoint 含有无法识别的人工询问")
    return {**request, "interrupt_id": item["id"]}


def _preview_paths(row: Any) -> dict[str, str]:
    """Read current and older projections without modifying paused checkpoints."""
    params = row.get("package_params") or {}
    if isinstance(params, str):
        params = json.loads(params)
    paths = (
        params.get("preview_paths")
        or (params.get("pending_input") or {}).get("previews")
        or (params.get("golden_comparison") or {}).get("previews")
        or {}
    )
    result = {
        view: paths[view] for view in PREVIEW_VIEWS
        if isinstance(paths.get(view), str) and paths[view]
    }
    if row.get("preview_path"):
        result.setdefault("isometric", row["preview_path"])
    return result


def _preview_urls(drawing_id: str | uuid.UUID, row: Any) -> dict[str, str]:
    return {
        view: f"/api/v1/step/drawings/{drawing_id}/artifacts/preview_{view}"
        for view in _preview_paths(row)
    }


def _public_pending_input(pending: dict | None, drawing_id: str | uuid.UUID, row: Any) -> dict | None:
    if not pending:
        return pending
    if pending.get("stage") == "routing":
        projected = dict(pending)
        options = list(pending.get("options") or ["confirm", "change", "cancel"])
        if "confirm" in options:
            try:
                validate_human_answer(pending, {"action": "confirm"})
            except ValueError as exc:
                options.remove("confirm")
                projected["confirmation_unavailable_reason"] = str(exc)
        projected["options"] = options
        return projected
    if pending.get("stage") != "review":
        return pending
    params = row.get("package_params") or {}
    if isinstance(params, str):
        params = json.loads(params)
    return {
        **pending,
        "step_file_url": (
            f"/api/v1/step/drawings/{drawing_id}/artifacts/step"
            if row.get("output_path") else None
        ),
        "preview_urls": _preview_urls(drawing_id, row),
        "modeling_assumptions": pending.get("modeling_assumptions")
        or (params.get("feature_ir") or {}).get("assumptions", []),
    }


def _job_status(snapshot: dict) -> str:
    """Map graph outcomes honestly; an ordinary stopped graph is not success."""
    pending = _pending_input(snapshot)
    if pending:
        return "pending_review" if pending["stage"] == "review" else "awaiting_input"
    state = snapshot.get("values") or {}
    result = state.get("result") or {}
    graph_status = state.get("status") or result.get("status") or ""
    if graph_status == "failed" or result.get("status") == "failed":
        return "failed"
    review = state.get("human_review") or {}
    if review.get("action") == "reject" or graph_status == "rejected":
        return "rejected"
    if (
        graph_status.startswith("stopped")
        or graph_status in {"cancelled", "needs_human_follow_up"}
    ):
        return "stopped"
    if snapshot.get("next_nodes"):
        return "ai_processing"
    step_path = result.get("step_file") or state.get("artifact_paths", {}).get("step")
    if not step_path:
        return "stopped"
    if review.get("action") == "approve":
        return "reviewed"
    if graph_status in {"matched", "completed"}:
        return "completed"
    # An old graph may end with review_required without an actual interrupt.
    # There is no resumable question in that case, so it cannot claim success.
    return "stopped"


async def _persist_job_snapshot(
    drawing_id: str,
    tenant_id: str,
    original_filename: str,
    snapshot: dict,
) -> None:
    state = snapshot.get("values") or {}
    result = state.get("result") or {}
    job_status = _job_status(snapshot)
    previews = result.get("previews") or state.get("preview_paths") or {}
    preview_path = previews.get("isometric") or next(iter(previews.values()), None)
    step_path = result.get("step_file") or state.get("artifact_paths", {}).get("step")
    classification = state.get("view_classification") or {}
    review = state.get("human_review") or {}
    reviewed_by = review.get("user_id") if review.get("action") in {"approve", "reject"} else None
    reviewed_at = (
        datetime.fromisoformat(review["answered_at"])
        if reviewed_by and review.get("answered_at") else None
    )
    error = None
    if job_status == "failed":
        error = "; ".join(str(item) for item in state.get("errors", [])) or "STEP Agent 执行失败"
    package_params = {
        "mode": "image",
        "original_filename": original_filename,
        "graph_status": state.get("status"),
        "result_status": result.get("status"),
        "image_sha256": state.get("image_meta", {}).get("sha256"),
        "category_id": result.get("category_id") or state.get("category_id") or classification.get("category_id", ""),
        "subcategory_id": result.get("subcategory_id") or state.get("subcategory_id") or classification.get("subcategory_id"),
        "family_id": result.get("family_id") or state.get("family_id") or classification.get("family_id", ""),
        "model_source": result.get("model_source", state.get("model_source")),
        "feature_ir": state.get("feature_ir", {}),
        "preview_paths": previews,
        "verification": result.get("verification", state.get("verification", {})),
        "golden_comparison": result.get("golden_comparison", state.get("golden_comparison", {})),
        "reference_step_search": result.get("reference_step_search", state.get("reference_step_search", {})),
        "jev_decision": result.get("jev_decision", state.get("jev_decision", {})),
        "human_package": state.get("human_package"),
        "human_route": state.get("human_route"),
        "human_review": review or None,
        "human_history": state.get("human_history", []),
        "pending_input": _pending_input(snapshot),
        "checkpoint_id": snapshot.get("checkpoint_id"),
        "next_nodes": snapshot.get("next_nodes", []),
        "missing_fields": result.get("missing_fields", []),
        "conflicting_fields": result.get("conflicting_fields", []),
        "evidence_report": result.get("evidence_report"),
        "projection_sync_failed": False,
        "projection_sync_error": None,
        "worker_interrupted": False,
    }
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE step_drawings
                SET status = :status,
                    output_path = :output_path,
                    preview_path = :preview_path,
                    package_params = (COALESCE(package_params, '{}'::jsonb)
                        - 'accepted_input') || CAST(:package_params AS jsonb),
                    error_msg = :error_msg,
                    needs_review = :needs_review,
                    reviewed_by = CAST(:reviewed_by AS uuid),
                    reviewed_at = CAST(:reviewed_at AS timestamptz),
                    updated_at = NOW()
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {
                "id": drawing_id,
                "tenant_id": tenant_id,
                "status": job_status,
                "output_path": step_path,
                "preview_path": preview_path,
                "package_params": json.dumps(package_params, ensure_ascii=False, default=_json_default),
                "error_msg": error[:4000] if error else None,
                "needs_review": job_status == "pending_review",
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            },
        )
        await session.commit()
    logger.info("step.job_checkpoint_saved", drawing_id=drawing_id, status=job_status)


async def _mark_job_recoverable(
    drawing_id: str,
    tenant_id: str,
    error: str,
    *,
    projection_failed: bool,
) -> None:
    """Keep an interrupted execution or failed business projection recoverable."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE step_drawings
                SET status = 'ai_processing', needs_review = FALSE,
                    error_msg = :error_msg,
                    package_params = COALESCE(package_params, '{}'::jsonb)
                        || CAST(:recovery_update AS jsonb),
                    updated_at = NOW()
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {
                "id": drawing_id,
                "tenant_id": tenant_id,
                "error_msg": error[:4000],
                "recovery_update": json.dumps({
                    "pending_input": None,
                    "projection_sync_failed": projection_failed,
                    "projection_sync_error": error[:4000] if projection_failed else None,
                    "worker_interrupted": not projection_failed,
                }, ensure_ascii=False),
            },
        )
        await session.commit()


async def _read_worker_job(drawing_id: str, tenant_id: str) -> dict | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, tenant_id, status, source_image_path, package_params
                FROM step_drawings
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id},
        )
        row = result.mappings().fetchone()
    return dict(row) if row else None


def _package_params(row: dict) -> dict:
    params = row.get("package_params") or {}
    return json.loads(params) if isinstance(params, str) else dict(params)


async def _sync_job_projection(
    drawing_id: str,
    tenant_id: str,
    original_filename: str,
    snapshot: dict | None = None,
) -> None:
    """A projection error must not discard a valid resumable graph checkpoint."""
    try:
        if snapshot is None:
            snapshot = await get_postgres_step_snapshot("image", drawing_id)
        await _persist_job_snapshot(drawing_id, tenant_id, original_filename, snapshot)
    except Exception as exc:
        logger.error("step.projection_sync_failed", drawing_id=drawing_id, exc_info=True)
        await _mark_job_recoverable(
            drawing_id, tenant_id,
            f"checkpoint 业务状态同步失败，可在服务重启后恢复：{exc}",
            projection_failed=True,
        )


async def _run_owned_image_job(
    drawing_id: str,
    tenant_id: str,
    image_path: Path,
    output_dir: Path,
    original_filename: str,
    *,
    resume_input: dict | None,
    recovering: bool,
) -> None:
    """Recheck durable state after acquiring ownership; never replay new input."""
    row = await _read_worker_job(drawing_id, tenant_id)
    if row is None:
        return
    params = _package_params(row)
    if row["status"] != "ai_processing" and not params.get("projection_sync_failed"):
        return
    original_filename = params.get("original_filename") or original_filename
    accepted = params.get("accepted_input")
    if recovering or resume_input is not None or accepted:
        try:
            snapshot = await get_postgres_step_snapshot("image", drawing_id)
        except Exception as exc:
            await _mark_job_recoverable(
                drawing_id, tenant_id, f"恢复时无法读取 checkpoint：{exc}",
                projection_failed=True,
            )
            return
        if not snapshot.get("checkpoint_id"):
            await _update_job_failed(
                drawing_id, tenant_id,
                "任务失去执行进程且没有 PostgreSQL checkpoint，无法安全恢复；请重新提交新任务",
            )
            return
        interrupts = snapshot.get("interrupts") or []
        matching = next((item for item in interrupts if accepted and item["id"] == accepted.get("interrupt_id")), None)
        if matching is not None:
            # Only the already authenticated, validated durable answer may be
            # consumed. A stale answer never answers a later question.
            graph_input = Command(resume={matching["id"]: accepted["answer"]})
        elif interrupts or not snapshot.get("next_nodes"):
            await _sync_job_projection(drawing_id, tenant_id, original_filename, snapshot)
            return
        else:
            # None continues the checkpoint's next nodes. Supplying the original
            # dictionary here would restart the graph and repeat human stages.
            graph_input = None
    else:
        graph_input = {"image_path": str(image_path), "output_dir": str(output_dir)}

    try:
        await invoke_postgres_step_graph("image", drawing_id, graph_input)
    except Exception as exc:
        logger.error("step.job_exception", drawing_id=drawing_id, exc_info=True)
        await _update_job_failed(drawing_id, tenant_id, str(exc))
        return
    await _sync_job_projection(drawing_id, tenant_id, original_filename)


async def _run_image_job(
    drawing_id: str,
    tenant_id: str,
    image_path: Path,
    output_dir: Path,
    original_filename: str,
    *,
    resume_input: dict | None = None,
    recovering: bool = False,
) -> None:
    """Own one job across processes and persist interrupted/recoverable states."""
    try:
        async with open_postgres_step_job_lock(drawing_id, wait=not recovering) as acquired:
            if not acquired:
                logger.info("step.recovery_active_worker_skipped", drawing_id=drawing_id)
                return
            try:
                await _run_owned_image_job(
                    drawing_id, tenant_id, image_path, output_dir, original_filename,
                    resume_input=resume_input, recovering=recovering,
                )
            except asyncio.CancelledError:
                # Record interruption before releasing ownership so a recovering
                # worker cannot be overwritten by this worker's cleanup.
                try:
                    await _mark_job_recoverable(
                        drawing_id, tenant_id, "执行进程已中断，服务重启后将从 checkpoint 恢复",
                        projection_failed=False,
                    )
                except Exception:
                    logger.error("step.interruption_status_update_failed", drawing_id=drawing_id, exc_info=True)
                raise
            except Exception as exc:
                logger.error("step.job_runtime_exception", drawing_id=drawing_id, exc_info=True)
                try:
                    await _mark_job_recoverable(
                        drawing_id, tenant_id, f"任务执行基础设施暂时不可用：{exc}",
                        projection_failed=True,
                    )
                except Exception:
                    logger.error("step.job_failure_status_update_failed", drawing_id=drawing_id, exc_info=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Lock acquisition/release errors must not overwrite another owner's
        # progress. The durable processing record remains eligible for recovery.
        logger.error("step.job_lock_exception", drawing_id=drawing_id, exc_info=True)


async def recover_step_jobs() -> int:
    """Schedule restart recovery without re-creating uncheckpointed old jobs."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT id, tenant_id, source_image_path, package_params
            FROM step_drawings
            WHERE status = 'ai_processing'
               OR package_params->>'projection_sync_failed' = 'true'
            ORDER BY created_at
        """))
        rows = result.mappings().all()
    for row in rows:
        drawing_id = str(row["id"])
        params = _package_params(dict(row))
        task = asyncio.create_task(_run_image_job(
            drawing_id, str(row["tenant_id"]),
            Path(row["source_image_path"] or ""),
            (STEP_JOB_ROOT / drawing_id / "artifacts").resolve(),
            params.get("original_filename", "drawing"),
            recovering=True,
        ))
        _background_tasks.add(task)
        task.add_done_callback(_discard_background_task)
    return len(rows)


def _discard_background_task(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    try:
        exception = task.exception()
    except asyncio.CancelledError:
        return
    if exception:
        logger.error("step.background_task_unhandled", error=str(exception))


@router.post("/drawings", status_code=status.HTTP_202_ACCEPTED)
async def submit_step_drawing(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload an engineering drawing and start asynchronous STEP generation."""
    original_filename = Path(file.filename or "drawing").name
    suffix = Path(original_filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅支持 PNG、JPEG、BMP、TIFF 或 WEBP 工程图图片",
        )

    content = await file.read(MAX_IMAGE_SIZE_BYTES + 1)
    if len(content) > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="图片超过 25 MB 限制",
        )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上传的图片为空",
        )

    drawing_id = str(uuid.uuid4())
    tenant_id = str(current_user["tenant_id"])
    job_dir = STEP_JOB_ROOT / drawing_id
    image_path = job_dir / f"input{suffix}"
    output_dir = job_dir / "artifacts"
    job_dir.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(content)

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO step_drawings
                        (id, tenant_id, source_image_path, status, package_params)
                    VALUES
                        (CAST(:id AS uuid), :tenant_id, :source_image_path,
                         'ai_processing', CAST(:package_params AS jsonb))
                """),
                {
                    "id": drawing_id,
                    "tenant_id": tenant_id,
                    "source_image_path": str(image_path.resolve()),
                    "package_params": json.dumps(
                        {
                            "mode": "image",
                            "original_filename": original_filename,
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            await session.commit()
    except Exception:
        image_path.unlink(missing_ok=True)
        raise

    task = asyncio.create_task(
        _run_image_job(
            drawing_id,
            tenant_id,
            image_path.resolve(),
            output_dir.resolve(),
            original_filename,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    logger.info("step.job_submitted", drawing_id=drawing_id, tenant_id=tenant_id)
    return {
        "drawing_id": drawing_id,
        "status": "ai_processing",
        "status_url": f"/api/v1/step/drawings/{drawing_id}",
    }


@router.get("/drawings/{drawing_id}")
async def get_step_drawing(
    drawing_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
):
    """Return job status and generated artifact metadata for the caller's tenant."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, status, source_image_path, output_path, preview_path,
                       package_params, error_msg, needs_review, reviewed_by,
                       reviewed_at, created_at, updated_at
                FROM step_drawings
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": current_user["tenant_id"]},
        )
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STEP 任务不存在")

    # Read checkpoints only after the tenant-scoped ownership query succeeds.
    checkpoint_error = None
    try:
        snapshot = await get_postgres_step_snapshot("image", drawing_id)
        pending = (
            _pending_input(snapshot)
            if row["status"] in {"awaiting_input", "pending_review"}
            else None
        )
        checkpoint_available = snapshot.get("checkpoint_id") is not None
    except Exception as exc:
        # A saver/initialization failure must not hide the business error that
        # the worker already persisted. Cached metadata is not re-verified here.
        logger.error("step.checkpoint_read_failed", drawing_id=str(drawing_id), exc_info=True)
        snapshot, pending, checkpoint_available = {}, None, False
        checkpoint_error = f"检查点暂时不可读取（{type(exc).__name__}）"
    return {
        "drawing_id": str(row["id"]),
        "status": row["status"],
        "step_file_url": (
            f"/api/v1/step/drawings/{drawing_id}/artifacts/step"
            if row["output_path"] else None
        ),
        "preview_url": (
            f"/api/v1/step/drawings/{drawing_id}/artifacts/preview"
            if row["preview_path"] else None
        ),
        "result": row["package_params"] or {},
        "error_msg": row["error_msg"],
        "needs_review": row["needs_review"],
        "preview_urls": _preview_urls(drawing_id, row),
        "pending_input": _public_pending_input(pending, drawing_id, row),
        "human_input_url": (
            f"/api/v1/step/drawings/{drawing_id}/human-input" if pending else None
        ),
        "checkpoint_id": snapshot.get("checkpoint_id"),
        "next_nodes": snapshot.get("next_nodes", []),
        "checkpoint_available": checkpoint_available,
        "checkpoint_error": checkpoint_error,
        "reviewed_by": str(row["reviewed_by"]) if row["reviewed_by"] else None,
        "reviewed_at": row["reviewed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.post("/drawings/{drawing_id}/human-input", status_code=status.HTTP_202_ACCEPTED)
async def submit_step_human_input(
    drawing_id: uuid.UUID,
    req: HumanInputRequest,
    current_user: dict = Depends(get_current_user),
):
    """Answer the current checkpoint question once, within the caller's tenant."""
    tenant_id = str(current_user["tenant_id"])
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    SELECT id, status, source_image_path, package_params
                    FROM step_drawings
                    WHERE id = :id AND tenant_id = :tenant_id
                    FOR UPDATE
                """),
                {"id": drawing_id, "tenant_id": tenant_id},
            )
            row = result.mappings().fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STEP 任务不存在")
            if row["status"] not in {"awaiting_input", "pending_review"}:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前任务没有等待人工回答，或该问题已被回答")

            try:
                snapshot = await get_postgres_step_snapshot("image", drawing_id)
                pending = _pending_input(snapshot)
            except Exception as exc:
                logger.error("step.checkpoint_resume_read_failed", drawing_id=str(drawing_id), exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="无法读取待回答检查点，请稍后重试；本次回答尚未接收",
                ) from exc
            if not pending or pending["interrupt_id"] != req.interrupt_id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="人工问题已过期，请重新获取任务状态")
            try:
                answer = validate_human_answer(pending, req.answer)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
            answer["_actor"] = {
                "user_id": str(current_user["user_id"]),
                "answered_at": datetime.now(timezone.utc).isoformat(),
            }
            accepted = {"interrupt_id": req.interrupt_id, "answer": answer}
            # The row lock serializes concurrent workers. The status predicate is
            # an additional compare-and-set guard, including repeat submissions.
            claimed = await session.execute(
                text("""
                    UPDATE step_drawings
                    SET status = 'ai_processing', needs_review = FALSE,
                        package_params = COALESCE(package_params, '{}'::jsonb)
                            || CAST(:input_update AS jsonb),
                        updated_at = NOW()
                    WHERE id = :id AND tenant_id = :tenant_id
                      AND status = :previous_status
                    RETURNING id
                """),
                {
                    "id": drawing_id,
                    "tenant_id": tenant_id,
                    "previous_status": row["status"],
                    "input_update": json.dumps(
                        {"pending_input": None, "accepted_input": accepted},
                        ensure_ascii=False,
                    ),
                },
            )
            if claimed.scalar_one_or_none() is None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该问题已被回答")
            stored_params = row["package_params"] or {}
            if isinstance(stored_params, str):
                stored_params = json.loads(stored_params)
            original_filename = stored_params.get("original_filename", "drawing")
            image_path = Path(row["source_image_path"])

    task = asyncio.create_task(
        _run_image_job(
            str(drawing_id), tenant_id, image_path,
            (STEP_JOB_ROOT / str(drawing_id) / "artifacts").resolve(),
            original_filename, resume_input=accepted,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    return {
        "drawing_id": str(drawing_id),
        "status": "ai_processing",
        "accepted_interrupt_id": req.interrupt_id,
        "status_url": f"/api/v1/step/drawings/{drawing_id}",
    }


@router.get("/drawings")
async def list_step_drawings(
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
):
    """List recent STEP jobs within the caller's tenant."""
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, status, output_path, preview_path, error_msg,
                       needs_review, package_params, created_at, updated_at
                FROM step_drawings
                WHERE tenant_id = :tenant_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {
                "tenant_id": current_user["tenant_id"],
                "limit": limit,
                "offset": offset,
            },
        )
        rows = result.mappings().all()
    return {
        "items": [
            {
                "drawing_id": str(row["id"]),
                "status": row["status"],
                "step_file_url": (
                    f"/api/v1/step/drawings/{row['id']}/artifacts/step"
                    if row["output_path"] else None
                ),
                "preview_url": (
                    f"/api/v1/step/drawings/{row['id']}/artifacts/preview"
                    if row["preview_path"] else None
                ),
                "error_msg": row["error_msg"],
                "needs_review": row["needs_review"],
                "preview_urls": _preview_urls(row["id"], row),
                "pending_input": _public_pending_input(
                    (row["package_params"] or {}).get("pending_input"), row["id"], row
                ),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/drawings/{drawing_id}/artifacts/{artifact_name}")
async def get_step_artifact(
    drawing_id: uuid.UUID,
    artifact_name: str,
    current_user: dict = Depends(get_current_user),
):
    """Download one generated artifact without exposing arbitrary filesystem paths."""
    if artifact_name not in {"step", "preview", *(f"preview_{view}" for view in PREVIEW_VIEWS)}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="产物不存在")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT output_path, preview_path, package_params
                FROM step_drawings
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": current_user["tenant_id"]},
        )
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STEP 任务不存在")

    view = "isometric" if artifact_name == "preview" else artifact_name.removeprefix("preview_")
    stored_path = row["output_path"] if artifact_name == "step" else _preview_paths(row).get(view)
    if not stored_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="产物尚未生成")
    artifact_path = Path(stored_path).resolve()
    job_root = (STEP_JOB_ROOT / str(drawing_id)).resolve()
    if not artifact_path.is_relative_to(job_root) or not artifact_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="产物文件不存在")

    if artifact_name == "step":
        return FileResponse(
            artifact_path,
            media_type="application/octet-stream",
            filename=f"{drawing_id}.step",
        )
    return FileResponse(
        artifact_path,
        media_type="image/png",
        filename=f"{drawing_id}_{view}.png",
    )
