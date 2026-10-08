"""API endpoints for STEP drawing generation jobs."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from backend.agents.step.human_nodes import validate_human_answer
from backend.agents.step.progress import emit_progress, progress_scope
from backend.agents.step.persistence import (
    get_postgres_step_snapshot,
    invoke_postgres_step_graph,
    open_postgres_step_job_lock,
    resume_postgres_step_graph_at_node,
    step_checkpoint_config,
)
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal, get_current_user


router = APIRouter()
logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
STEP_JOB_ROOT = PROJECT_ROOT / "output" / "step_agent"
MAX_IMAGE_SIZE_BYTES = 25 * 1024 * 1024
MAX_HUMAN_REQUEST_LENGTH = 2000
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


async def _append_execution_log(drawing_id: str, tenant_id: str, event: dict) -> None:
    """Append atomically without overwriting the checkpoint projection or answers."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            UPDATE step_drawings
            SET package_params = jsonb_set(COALESCE(package_params, '{}'::jsonb),
                '{execution_logs}', (
                    SELECT COALESCE(jsonb_agg(item ORDER BY ordinal), '[]'::jsonb)
                    FROM (
                        SELECT item, ordinal FROM jsonb_array_elements(
                            COALESCE(package_params->'execution_logs', '[]'::jsonb)
                            || CAST(:event AS jsonb)
                        ) WITH ORDINALITY AS entries(item, ordinal)
                        ORDER BY ordinal DESC LIMIT 600
                    ) AS recent
                ))
            WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
        """), {"id": drawing_id, "tenant_id": tenant_id,
               "event": json.dumps([event], ensure_ascii=False)})
        await session.commit()


async def _update_job_failed(drawing_id: str, tenant_id: str, error: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE step_drawings
                SET status = 'failed', error_msg = :error_msg,
                    needs_review = FALSE,
                    package_params = COALESCE(package_params, '{}'::jsonb)
                        || '{"pending_input": null, "projection_sync_failed": false,
                             "worker_interrupted": false, "retryable": true}'::jsonb,
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
    await emit_progress("本次执行未完成，请查看下方提示后重试。", phase="error")


def _pending_input(snapshot: dict) -> dict | None:
    interrupts = snapshot.get("interrupts") or []
    if not interrupts:
        return None
    item = interrupts[0]
    request = item.get("value")
    if not isinstance(request, dict) or request.get("stage") not in {
        "package", "routing", "dimensions", "review"
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
        if error == "STEP Agent 执行失败" and (state.get("verification") or {}).get("passed") is False:
            error = "STEP 几何校验未通过：模型外形与已识别尺寸不一致，可核对尺寸后在原任务上继续"
    package_params = {
        "mode": "image",
        "human_request": state.get("human_request", ""),
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
        "human_dimension_history": state.get("human_dimension_history", []),
        "pending_input": _pending_input(snapshot),
        "checkpoint_id": snapshot.get("checkpoint_id"),
        "next_nodes": snapshot.get("next_nodes", []),
        "missing_fields": result.get("missing_fields", []),
        "conflicting_fields": result.get("conflicting_fields", []),
        "evidence_report": result.get("evidence_report"),
        "projection_sync_failed": False,
        "projection_sync_error": None,
        "worker_interrupted": False,
        "retryable": job_status == "failed",
        "retry_from_checkpoint": False,
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
    terminal_messages = {
        "awaiting_input": ("waiting", "当前步骤已保存，等待你补充或确认。"),
        "pending_review": ("waiting", "模型已生成，等待你审核。"),
        "completed": ("completed", "任务已完成。"),
        "reviewed": ("completed", "模型审核通过，STEP 文件可下载。"),
        "rejected": ("warning", "模型已拒绝。"),
        "stopped": ("warning", "任务已暂停，请查看下方说明。"),
        "failed": ("error", "本次执行未完成，请查看下方提示。"),
    }
    if job_status in terminal_messages:
        phase, message = terminal_messages[job_status]
        await emit_progress(message, phase=phase)


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


def _restartable_source(row: dict) -> bool:
    source = Path(row.get("source_image_path") or "").resolve()
    job_root = (STEP_JOB_ROOT / str(row["id"])).resolve()
    return source.is_relative_to(job_root) and source.is_file()


def _failed_dimension_relationship(snapshot: dict) -> str | None:
    """Identify a failed geometry check that can be repaired from saved evidence."""
    state = snapshot.get("values") or {}
    if snapshot.get("interrupts") or snapshot.get("next_nodes"):
        return None
    if state.get("status") != "failed" or (state.get("verification") or {}).get("passed") is not False:
        return None
    fused = state.get("fused_evidence") or {}
    if fused.get("family_id") != "ic/gullwing_ic":
        return None
    values = {
        item.get("canonical_name"): item.get("value")
        for item in fused.get("parameters", []) if isinstance(item, dict)
    }
    try:
        body_length = float(values["body_length"])
        pin_span = float(values["pin_span"])
        terminal_width = float(values["terminal_width"])
    except (KeyError, TypeError, ValueError):
        return None
    if body_length + 0.05 < pin_span + terminal_width:
        return "body_length<pin_span+terminal_width"
    return None


def _failed_view_analysis(snapshot: dict) -> bool:
    state = snapshot.get("values") or {}
    errors = " ".join(str(item) for item in state.get("errors", []))
    return bool(
        state.get("status") == "failed" and state.get("current_view_id")
        and state.get("current_prompt_evidence") and not snapshot.get("interrupts")
        and not snapshot.get("next_nodes")
        and any(marker in errors for marker in (
            "length limit", "LengthFinishReason", "图纸分析输出", "图纸分析响应超时",
        ))
    )


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
    human_request: str,
    resume_input: dict | None,
    recovering: bool,
    restart: bool,
) -> None:
    """Recheck durable state after acquiring ownership; never replay new input."""
    row = await _read_worker_job(drawing_id, tenant_id)
    if row is None:
        return
    params = _package_params(row)
    if row["status"] != "ai_processing" and not params.get("projection_sync_failed"):
        return
    await emit_progress(
        "正在从已保存的进度恢复任务。" if recovering else
        "已收到确认，继续处理图纸。" if params.get("accepted_input") else "开始处理图纸。"
    )
    original_filename = params.get("original_filename") or original_filename
    accepted = params.get("accepted_input")
    if params.get("retry_from_checkpoint"):
        try:
            snapshot = await get_postgres_step_snapshot("image", drawing_id)
        except Exception as exc:
            await _mark_job_recoverable(
                drawing_id, tenant_id, f"重试时无法读取 checkpoint：{exc}",
                projection_failed=True,
            )
            return
        if snapshot.get("interrupts"):
            await _sync_job_projection(drawing_id, tenant_id, original_filename, snapshot)
            return
        if snapshot.get("next_nodes"):
            graph_input = None
        elif relationship := _failed_dimension_relationship(snapshot):
            gate = dict((snapshot.get("values") or {}).get("dimension_gate") or {})
            gate.update({
                "passed": False,
                "status": "dimensions_need_confirmation",
                "missing_fields": [],
                "conflicting_fields": [relationship],
                "low_confidence_fields": [],
            })
            try:
                await resume_postgres_step_graph_at_node(
                    "image", drawing_id,
                    {"dimension_gate": gate, "status": "dimensions_need_confirmation",
                     "errors": [], "result": {}, "verification": {}},
                    as_node="validate_dimensions",
                )
            except Exception as exc:
                logger.error("step.checkpoint_retry_failed", drawing_id=drawing_id, exc_info=True)
                await _update_job_failed(drawing_id, tenant_id, str(exc))
                return
            await _sync_job_projection(drawing_id, tenant_id, original_filename)
            return
        elif _failed_view_analysis(snapshot):
            await emit_progress("复用已识别的图纸证据，从失败的视图分析步骤继续。")
            try:
                await resume_postgres_step_graph_at_node(
                    "image", drawing_id,
                    {"status": "view_evidence_retrieved", "errors": [], "result": {}},
                    as_node="retrieve_view_evidence",
                )
            except Exception as exc:
                logger.error("step.view_retry_failed", drawing_id=drawing_id, exc_info=True)
                await _update_job_failed(drawing_id, tenant_id, str(exc))
                return
            await _sync_job_projection(drawing_id, tenant_id, original_filename)
            return
        else:
            graph_input = {
                "task": "generate_step", "mode": "image", "image_path": str(image_path),
                "output_dir": str(output_dir), "human_request": human_request,
            }
    elif restart:
        graph_input = {
            "task": "generate_step",
            "mode": "image",
            "image_path": str(image_path),
            "output_dir": str(output_dir),
            "human_request": human_request,
        }
    elif recovering or resume_input is not None or accepted:
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
        graph_input = {
            "task": "generate_step",
            "mode": "image",
            "image_path": str(image_path),
            "output_dir": str(output_dir),
            "human_request": human_request,
        }

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
    human_request: str = "",
    resume_input: dict | None = None,
    recovering: bool = False,
    restart: bool = False,
) -> None:
    """Own one job across processes and persist interrupted/recoverable states."""
    try:
        async with open_postgres_step_job_lock(drawing_id, wait=not recovering) as acquired:
            if not acquired:
                logger.info("step.recovery_active_worker_skipped", drawing_id=drawing_id)
                return
            try:
                async def save_event(event):
                    await _append_execution_log(drawing_id, tenant_id, event)

                with progress_scope(save_event):
                    await _run_owned_image_job(
                        drawing_id, tenant_id, image_path, output_dir, original_filename,
                        human_request=human_request,
                        resume_input=resume_input, recovering=recovering,
                        restart=restart,
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
            human_request=params.get("human_request", ""),
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


async def _submit_image_job(file: UploadFile, message: str, current_user: dict) -> dict:
    """Adapt user-facing form fields into the private image-agent state."""
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="请描述你希望生成的元器件模型")
    if len(message) > MAX_HUMAN_REQUEST_LENGTH:
        raise HTTPException(status_code=400, detail="需求描述不能超过 2000 个字符")
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
                            "task": "generate_step",
                            "human_request": message,
                            "original_filename": original_filename,
                            "retry_count": 0,
                            "retryable": False,
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
            human_request=message,
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


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_step_from_user_input(
    message: str = Form(..., min_length=1, max_length=MAX_HUMAN_REQUEST_LENGTH),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Public input adapter: users provide plain language and an image, never Agent JSON."""
    return await _submit_image_job(file, message, current_user)


@router.post("/drawings", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
async def submit_step_drawing_compat(
    file: UploadFile = File(...),
    message: str = Form("生成这张工程图对应的 STEP 模型"),
    current_user: dict = Depends(get_current_user),
):
    """Backward-compatible upload endpoint; new clients should use /generate."""
    return await _submit_image_job(file, message, current_user)


@router.get("/ui", include_in_schema=False)
async def step_generation_ui():
    """Serve the minimal natural-language upload page."""
    return FileResponse(Path(__file__).with_name("step_ui.html"), media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/drawings/{drawing_id}")
async def get_step_drawing(
    drawing_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    include_checkpoint: bool = True,
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
    params = _package_params(dict(row))

    # Read checkpoints only after the tenant-scoped ownership query succeeds.
    checkpoint_error = None
    try:
        if include_checkpoint or row["status"] == "failed":
            snapshot = await get_postgres_step_snapshot("image", drawing_id)
            pending = _pending_input(snapshot) if row["status"] in {"awaiting_input", "pending_review"} else None
        else:
            # Poll durable business metadata. Answers still re-read and validate
            # the live checkpoint under the row lock in the human-input route.
            snapshot = {"checkpoint_id": params.get("checkpoint_id"), "next_nodes": params.get("next_nodes", [])}
            pending = params.get("pending_input") if row["status"] in {"awaiting_input", "pending_review"} else None
        checkpoint_available = snapshot.get("checkpoint_id") is not None
    except Exception as exc:
        # A saver/initialization failure must not hide the business error that
        # the worker already persisted. Cached metadata is not re-verified here.
        logger.error("step.checkpoint_read_failed", drawing_id=str(drawing_id), exc_info=True)
        snapshot, pending, checkpoint_available = {}, None, False
        checkpoint_error = f"检查点暂时不可读取（{type(exc).__name__}）"
    repairable_relationship = (
        _failed_dimension_relationship(snapshot) if row["status"] == "failed" else None
    )
    error_msg = row["error_msg"]
    if repairable_relationship and (not error_msg or error_msg == "STEP Agent 执行失败"):
        error_msg = "模型外形校验发现塑封本体长度与引脚排列尺寸不匹配；可以在原任务中核对后继续"
    return {
        "drawing_id": str(row["id"]),
        "status": row["status"],
        "restartable": (
            row["status"] == "stopped"
            and _restartable_source(dict(row))
        ),
        "step_file_url": (
            f"/api/v1/step/drawings/{drawing_id}/artifacts/step"
            if row["output_path"] else None
        ),
        "preview_url": (
            f"/api/v1/step/drawings/{drawing_id}/artifacts/preview"
            if row["preview_path"] else None
        ),
        "result": params,
        "execution_logs": params.get("execution_logs", []),
        "error_msg": error_msg,
        "stop_reason": {
            "graph_status": params.get("graph_status"),
            "result_status": params.get("result_status"),
            "evidence_report": params.get("evidence_report"),
            "missing_fields": params.get("missing_fields", []),
            "conflicting_fields": params.get("conflicting_fields", []),
        } if row["status"] == "stopped" else None,
        "retryable": bool(params.get("retryable", row["status"] == "failed")),
        "retry_strategy": "continue_from_dimensions" if repairable_relationship else "continue_from_view" if _failed_view_analysis(snapshot) else "rerun_from_image",
        "checkpoint_metadata_source": "checkpoint" if include_checkpoint or row["status"] == "failed" else "projection",
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


@router.post("/drawings/{drawing_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_failed_step_drawing(
    drawing_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
):
    """Restart a failed generation from its saved image and request, keeping the job ID."""
    tenant_id = str(current_user["tenant_id"])
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(text("""
                SELECT id, status, source_image_path, package_params
                FROM step_drawings
                WHERE id = :id AND tenant_id = :tenant_id
                FOR UPDATE
            """), {"id": drawing_id, "tenant_id": tenant_id})
            row = result.mappings().fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="STEP 任务不存在")
            if row["status"] != "failed":
                raise HTTPException(status_code=409, detail="只有执行失败的任务可以重试")
            image_path = Path(row["source_image_path"] or "").resolve()
            job_root = (STEP_JOB_ROOT / str(drawing_id)).resolve()
            if not image_path.is_relative_to(job_root) or not image_path.is_file():
                raise HTTPException(status_code=409, detail="原始图纸文件不可用，无法重试")
            params = _package_params(dict(row))
            retry_count = int(params.get("retry_count", 0)) + 1
            await session.execute(text("""
                UPDATE step_drawings
                SET status = 'ai_processing', error_msg = NULL, needs_review = FALSE,
                    package_params = COALESCE(package_params, '{}'::jsonb)
                        || CAST(:retry_update AS jsonb),
                    updated_at = NOW()
                WHERE id = :id AND tenant_id = :tenant_id
            """), {
                "id": drawing_id,
                "tenant_id": tenant_id,
                "retry_update": json.dumps({
                    "retryable": False,
                    "retry_from_checkpoint": True,
                    "retry_count": retry_count,
                    "pending_input": None,
                    "accepted_input": None,
                    "projection_sync_failed": False,
                    "worker_interrupted": False,
                }, ensure_ascii=False),
            })
            original_filename = params.get("original_filename", "drawing")
            human_request = params.get("human_request", "")

    task = asyncio.create_task(_run_image_job(
        str(drawing_id), tenant_id, image_path,
        (STEP_JOB_ROOT / str(drawing_id) / "artifacts").resolve(),
        original_filename,
        human_request=human_request,
        restart=True,
    ))
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    return {
        "drawing_id": str(drawing_id),
        "status": "ai_processing",
        "retry_count": retry_count,
        "status_url": f"/api/v1/step/drawings/{drawing_id}",
    }


@router.post("/drawings/{drawing_id}/restart", status_code=status.HTTP_202_ACCEPTED)
async def restart_stopped_step_drawing(
    drawing_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
):
    """Start a fresh task from a stopped drawing, preserving the stopped record."""
    tenant_id = str(current_user["tenant_id"])
    new_id = uuid.uuid4()
    new_dir = (STEP_JOB_ROOT / str(new_id)).resolve()
    new_image: Path | None = None
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(text("""
                    SELECT id, status, source_image_path, package_params
                    FROM step_drawings
                    WHERE id = :id AND tenant_id = :tenant_id
                    FOR UPDATE
                """), {"id": drawing_id, "tenant_id": tenant_id})
                row = result.mappings().fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="STEP 任务不存在")
                if row["status"] != "stopped":
                    raise HTTPException(status_code=409, detail="只有已停止的任务可以重新运行")
                source = Path(row["source_image_path"] or "").resolve()
                old_dir = (STEP_JOB_ROOT / str(drawing_id)).resolve()
                if not source.is_relative_to(old_dir) or not source.is_file():
                    raise HTTPException(status_code=409, detail="原始图纸文件不可用，无法重新运行")
                params = _package_params(dict(row))
                filename = params.get("original_filename") or source.name
                new_image = new_dir / source.name
                new_dir.mkdir(parents=True, exist_ok=False)
                shutil.copy2(source, new_image)
                await session.execute(text("""
                    INSERT INTO step_drawings
                        (id, tenant_id, source_image_path, status, package_params)
                    VALUES
                        (:id, :tenant_id, :source_image_path, 'ai_processing', CAST(:params AS jsonb))
                """), {
                    "id": new_id,
                    "tenant_id": tenant_id,
                    "source_image_path": str(new_image),
                    "params": json.dumps({
                        "mode": "image",
                        "task": "generate_step",
                        "human_request": params.get("human_request", ""),
                        "original_filename": filename,
                        "retry_count": 0,
                        "retryable": False,
                        "restarted_from": str(drawing_id),
                    }, ensure_ascii=False),
                })
        task = asyncio.create_task(_run_image_job(
            str(new_id), tenant_id, new_image,
            (new_dir / "artifacts").resolve(),
            filename,
            human_request=params.get("human_request", ""),
        ))
        _background_tasks.add(task)
        task.add_done_callback(_discard_background_task)
    except Exception:
        if new_dir.is_relative_to(STEP_JOB_ROOT.resolve()) and new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)
        raise
    return {
        "drawing_id": str(new_id),
        "restarted_from": str(drawing_id),
        "status": "ai_processing",
        "status_url": f"/api/v1/step/drawings/{new_id}",
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
                SELECT id, status, source_image_path, output_path, preview_path, error_msg,
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
                "human_request": _package_params(dict(row)).get("human_request", ""),
                "retryable": bool(_package_params(dict(row)).get("retryable", row["status"] == "failed")),
                "restartable": row["status"] == "stopped" and _restartable_source(dict(row)),
                "stop_reason": {
                    "graph_status": _package_params(dict(row)).get("graph_status"),
                    "result_status": _package_params(dict(row)).get("result_status"),
                    "evidence_report": _package_params(dict(row)).get("evidence_report"),
                    "missing_fields": _package_params(dict(row)).get("missing_fields", []),
                    "conflicting_fields": _package_params(dict(row)).get("conflicting_fields", []),
                } if row["status"] == "stopped" else None,
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


@router.delete("/drawings/{drawing_id}")
async def delete_step_drawing(
    drawing_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
):
    """Delete a tenant-owned idle job and its checkpoints, then clean its files."""
    tenant_id = current_user["tenant_id"]
    async with AsyncExitStack() as ownership, AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    SELECT id, status FROM step_drawings
                    WHERE id = :id AND tenant_id = :tenant_id
                    FOR UPDATE
                """),
                {"id": drawing_id, "tenant_id": tenant_id},
            )
            row = result.mappings().fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="STEP 会话不存在")
            if row["status"] in {"pending", "ai_processing"}:
                raise HTTPException(status_code=409, detail="会话正在处理，请等待任务暂停或结束后再删除")
            # Share ownership with workers so a finishing/recovering worker
            # cannot recreate checkpoints or files after deletion.
            acquired = await ownership.enter_async_context(open_postgres_step_job_lock(drawing_id, wait=False))
            if not acquired:
                raise HTTPException(status_code=409, detail="会话正在保存进度，请稍后再删除")
            thread_ids = {
                f"thread_{mode}": step_checkpoint_config(mode, drawing_id)["configurable"]["thread_id"]
                for mode in ("image", "drawing")
            }
            # These tables may not exist for a job that failed before its
            # graph started. Table names are application constants.
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                exists = await session.execute(
                    text("SELECT to_regclass(:table)"), {"table": table}
                )
                if exists.scalar_one_or_none() is not None:
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE thread_id IN (:thread_image, :thread_drawing)"),
                        thread_ids,
                    )
            await session.execute(
                text("DELETE FROM step_drawings WHERE id = :id AND tenant_id = :tenant_id"),
                {"id": drawing_id, "tenant_id": tenant_id},
            )
        # A queued worker rechecks the row after acquiring its lock and exits
        # when it no longer exists. Never trust stored artifact paths for cleanup.
    files_deleted = True
    root = STEP_JOB_ROOT.resolve()
    job_root = (root / str(drawing_id)).resolve()
    if job_root.parent != root:
        files_deleted = False
        logger.warning("step.delete_unsafe_directory_skipped", drawing_id=str(drawing_id))
    elif job_root.exists():
        try:
            await asyncio.to_thread(shutil.rmtree, job_root)
        except OSError:
            files_deleted = False
            logger.error("step.delete_file_cleanup_failed", drawing_id=str(drawing_id), exc_info=True)
    return {"drawing_id": str(drawing_id), "deleted": True, "files_deleted": files_deleted}


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
