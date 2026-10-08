"""OrCAD 符号生成的 REST 接口。

四层结构（与 STEP / PCB 接口同构）：

    _submit_job            校验 + 落盘 PDF + 落库 + 起后台 task
      └ _run_job           持 per-job advisory lock
          └ _run_owned_job 拿到锁后重读 DB，判定该喂什么给图
              └ _sync_job_projection  把 graph state 摊平成 package_params

与 PCB 接口的区别只有一处：输入是**一份 PDF**（datasheet），不是参数 JSON ——
参数是 Agent 从 PDF 里读出来的。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from langgraph.types import Command
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.agents.symbol.persistence import (
    get_postgres_symbol_snapshot,
    invoke_postgres_symbol_graph,
    open_postgres_symbol_job_lock,
)
from backend.agents.symbol.human_nodes import validate_human_answer
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal, get_current_user

router = APIRouter()
logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYMBOL_JOB_ROOT = PROJECT_ROOT / "output" / "symbol_agent"

#: 上传上限（手册通常几 MB，给足余量）。
MAX_PDF_SIZE_BYTES = 64 * 1024 * 1024
MAX_HINT_LENGTH = 2000

#: 可下载的产物：TCL 脚本（总是有）与 Capture 生成的二进制。
ARTIFACT_NAMES = {"tcl", "olb", "dsn"}
ARTIFACT_MEDIA_TYPES = {".tcl": "text/plain; charset=utf-8"}

_background_tasks: set[asyncio.Task] = set()


class HumanInputRequest(BaseModel):
    """回答一次人工询问。"""

    interrupt_id: str = Field(min_length=1)
    answer: dict[str, Any] = Field(description="例如 {'action': 'choose', 'value': 'LQFP100'}")


# ── 小工具 ────────────────────────────────────────────────────────


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"无法序列化 {type(value)!r}")


def _package_params(row: Any) -> dict:
    raw = row["package_params"]
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _discard_background_task(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("symbol.background_task_unhandled", exc_info=exc)


# ── 状态映射 ──────────────────────────────────────────────────────


def _pending_input(snapshot: dict) -> dict | None:
    interrupts = snapshot.get("interrupts") or []
    if not interrupts:
        return None
    first = interrupts[0]
    value = first.get("value") or {}
    return {"interrupt_id": first.get("id"), **value}


def _public_pending_input(pending: dict | None) -> dict | None:
    """对外暴露前裁剪：路径类字段不外泄。"""
    if not pending:
        return None
    hidden = {"output_dir", "work_dir", "recent_messages"}
    return {key: value for key, value in pending.items() if key not in hidden}


def _job_status(snapshot: dict) -> str:
    """把图的结果诚实地映射成任务状态。普通停止的图**不是**成功。"""
    pending = _pending_input(snapshot)
    if pending:
        return "awaiting_input"
    state = snapshot.get("values") or {}
    result = state.get("result") or {}
    graph_status = state.get("status") or result.get("status") or ""

    if graph_status == "failed" or result.get("status") == "failed":
        return "failed"
    if graph_status == "cancelled" or result.get("status") == "cancelled":
        return "rejected"
    if graph_status.startswith("stopped"):
        return "stopped"
    if snapshot.get("next_nodes"):
        return "ai_processing"
    if graph_status == "completed" and (result.get("artifacts") or {}).get("olb"):
        return "completed"
    # 没有可续的问题、也没拿到产物时，不能声称成功。
    return "stopped"


# ── 数据库与投影 ──────────────────────────────────────────────────


async def _update_job_failed(drawing_id: str, tenant_id: str, error: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE symbol_drawings
                SET status = 'failed', error_msg = :error, updated_at = NOW()
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id, "error": error[:4000]},
        )
        await session.commit()


async def _mark_job_recoverable(
    drawing_id: str, tenant_id: str, error: str, *, projection_failed: bool
) -> None:
    params = {
        "projection_sync_failed": projection_failed,
        "projection_sync_error": error[:2000],
        "worker_interrupted": True,
    }
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE symbol_drawings
                SET status = 'ai_processing',
                    package_params = COALESCE(package_params, '{}'::jsonb)
                        || CAST(:params AS jsonb),
                    error_msg = :error,
                    updated_at = NOW()
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {
                "id": drawing_id,
                "tenant_id": tenant_id,
                "params": json.dumps(params, ensure_ascii=False),
                "error": error[:4000],
            },
        )
        await session.commit()


async def _read_worker_job(drawing_id: str, tenant_id: str) -> dict | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, tenant_id, package_params, status
                FROM symbol_drawings
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _persist_job_snapshot(drawing_id: str, tenant_id: str, snapshot: dict) -> None:
    """把 graph state 摊平成 ``package_params`` 并向业务表投影。

    只存摘要 —— ``locate`` / ``check_report`` 这些还好，但绝不能把
    MinerU 的 ``content_list`` 塞进来（它已经落盘了）。
    """
    state = snapshot.get("values") or {}
    result = state.get("result") or {}
    job_status = _job_status(snapshot)
    error: str | None = None
    if job_status == "failed":
        error = "; ".join(str(item) for item in state.get("errors", [])) or "符号生成失败"

    artifacts = state.get("artifact_paths") or (result.get("artifacts") or {})
    package_params = {
        "task": "generate_symbol",
        "original_filename": state.get("original_filename", ""),
        "device": state.get("device", ""),
        "package": state.get("package", ""),
        "pin_page": state.get("pin_page"),
        "graph_status": state.get("status"),
        "result_status": result.get("status"),
        "reason": result.get("reason"),
        "verdict": result.get("verdict"),
        "score": result.get("score"),
        "confidence": result.get("confidence"),
        "check_report": state.get("check_report") or {},
        "warnings": [
            *(state.get("warnings") or []),
            *(state.get("table_warnings") or []),
            *(state.get("document_notes") or []),
        ],
        "device_candidates": state.get("device_candidates", []),
        "package_candidates": state.get("package_candidates", []),
        "layout_summary": (state.get("layout") or {}).get("summary", ""),
        "artifacts": artifacts,
        "human_history": state.get("human_history", []),
        "pending_input": _pending_input(snapshot),
        "checkpoint_id": snapshot.get("checkpoint_id"),
        "next_nodes": snapshot.get("next_nodes", []),
        "projection_sync_failed": False,
        "projection_sync_error": None,
        "retryable": job_status == "failed",
    }

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE symbol_drawings
                SET status = :status,
                    output_path = :output_path,
                    package_params = (COALESCE(package_params, '{}'::jsonb)
                        - 'accepted_input') || CAST(:package_params AS jsonb),
                    error_msg = :error_msg,
                    needs_review = :needs_review,
                    updated_at = NOW()
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {
                "id": drawing_id,
                "tenant_id": tenant_id,
                "status": job_status,
                "output_path": artifacts.get("olb") or artifacts.get("tcl"),
                "package_params": json.dumps(
                    package_params, ensure_ascii=False, default=_json_default
                ),
                "error_msg": error[:4000] if error else None,
                "needs_review": job_status in {"awaiting_input", "pending_review", "stopped"},
            },
        )
        await session.commit()
    logger.info("symbol.job_checkpoint_saved", drawing_id=drawing_id, status=job_status)


async def _sync_job_projection(
    drawing_id: str, tenant_id: str, snapshot: dict | None = None
) -> None:
    """投影失败**不等于**任务失败。"""
    try:
        if snapshot is None:
            snapshot = await get_postgres_symbol_snapshot(drawing_id)
        await _persist_job_snapshot(drawing_id, tenant_id, snapshot)
    except Exception as exc:
        logger.error("symbol.job_projection_failed", drawing_id=drawing_id, exc_info=True)
        await _mark_job_recoverable(
            drawing_id, tenant_id, f"状态投影失败：{exc}", projection_failed=True
        )


# ── 执行 ──────────────────────────────────────────────────────────


def _initial_state(params: dict) -> dict:
    return {
        "task": "generate_symbol",
        "pdf_path": params.get("pdf_path", ""),
        "original_filename": params.get("original_filename") or "",
        "work_dir": params.get("work_dir") or "",
        "output_dir": params.get("output_dir") or "",
        "strict_pages": bool(params.get("strict_pages")),
        "device": params.get("device") or "",
        "package": params.get("package") or "",
        "errors": [],
    }


async def _run_owned_job(
    drawing_id: str,
    tenant_id: str,
    *,
    resume_input: dict | None = None,
    recovering: bool = False,
) -> None:
    """拿到锁之后再读一次 DB 决定喂什么，绝不重放已消费的输入。"""
    row = await _read_worker_job(drawing_id, tenant_id)
    if row is None:
        return
    params = _package_params(row)
    if row["status"] != "ai_processing" and not params.get("projection_sync_failed"):
        return

    accepted = params.get("accepted_input")
    if recovering or resume_input is not None or accepted:
        try:
            snapshot = await get_postgres_symbol_snapshot(drawing_id)
        except Exception as exc:
            await _mark_job_recoverable(
                drawing_id, tenant_id, f"恢复时无法读取 checkpoint：{exc}",
                projection_failed=True,
            )
            return
        if not snapshot.get("checkpoint_id"):
            await _update_job_failed(
                drawing_id,
                tenant_id,
                "任务失去执行进程且没有 PostgreSQL checkpoint，无法安全恢复；请重新提交新任务",
            )
            return
        interrupts = snapshot.get("interrupts") or []
        matching = next(
            (
                item
                for item in interrupts
                if accepted and item["id"] == accepted.get("interrupt_id")
            ),
            None,
        )
        if matching is not None:
            graph_input: Any = Command(resume={matching["id"]: accepted["answer"]})
        elif interrupts or not snapshot.get("next_nodes"):
            await _sync_job_projection(drawing_id, tenant_id, snapshot)
            return
        else:
            graph_input = None
    else:
        graph_input = _initial_state(params)

    try:
        await invoke_postgres_symbol_graph(drawing_id, graph_input)
    except Exception as exc:
        logger.error("symbol.job_exception", drawing_id=drawing_id, exc_info=True)
        await _update_job_failed(drawing_id, tenant_id, str(exc))
        return
    await _sync_job_projection(drawing_id, tenant_id)


async def _run_job(
    drawing_id: str,
    tenant_id: str,
    *,
    resume_input: dict | None = None,
    recovering: bool = False,
) -> None:
    async with open_postgres_symbol_job_lock(drawing_id, wait=not recovering) as acquired:
        if not acquired:
            logger.info("symbol.recovery_active_worker_skipped", drawing_id=drawing_id)
            return
        try:
            await _run_owned_job(
                drawing_id, tenant_id, resume_input=resume_input, recovering=recovering
            )
        except asyncio.CancelledError:
            await _mark_job_recoverable(
                drawing_id, tenant_id, "进程关闭时任务被中断", projection_failed=True
            )
            raise
        except Exception as exc:
            logger.error("symbol.job_unhandled", drawing_id=drawing_id, exc_info=True)
            await _update_job_failed(drawing_id, tenant_id, str(exc))


async def _submit_job(
    file: UploadFile,
    *,
    device: str,
    package: str,
    strict_pages: bool,
    current_user: dict,
) -> dict:
    filename = Path(file.filename or "datasheet.pdf").name
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="仅支持 PDF 格式的手册"
        )
    content = await file.read(MAX_PDF_SIZE_BYTES + 1)
    if len(content) > MAX_PDF_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PDF 超过 64 MB 限制",
        )
    if not content.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="文件缺少 %PDF- 签名"
        )

    drawing_id = str(uuid.uuid4())
    tenant_id = str(current_user["tenant_id"])
    job_dir = SYMBOL_JOB_ROOT / drawing_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_dir / "input.pdf"
    pdf_path.write_bytes(content)

    params = {
        "task": "generate_symbol",
        "pdf_path": str(pdf_path.resolve()),
        "original_filename": filename,
        "work_dir": str(job_dir.resolve()),
        "output_dir": str((job_dir / "artifacts").resolve()),
        "device": device.strip(),
        "package": package.strip(),
        "strict_pages": strict_pages,
        "retryable": False,
    }
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO symbol_drawings (id, tenant_id, status, package_params)
                    VALUES (CAST(:id AS uuid), :tenant_id, 'ai_processing',
                            CAST(:package_params AS jsonb))
                """),
                {
                    "id": drawing_id,
                    "tenant_id": tenant_id,
                    "package_params": json.dumps(
                        params, ensure_ascii=False, default=_json_default
                    ),
                },
            )
            await session.commit()
    except Exception:
        pdf_path.unlink(missing_ok=True)
        logger.error("symbol.job_insert_failed", drawing_id=drawing_id, exc_info=True)
        raise HTTPException(status_code=503, detail="任务暂时无法登记，请稍后重试")

    task = asyncio.create_task(_run_job(drawing_id, tenant_id))
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    logger.info("symbol.job_submitted", drawing_id=drawing_id, tenant_id=tenant_id)
    return {
        "drawing_id": drawing_id,
        "status": "ai_processing",
        "status_url": f"/api/v1/symbol/drawings/{drawing_id}",
    }


# ── 路由 ──────────────────────────────────────────────────────────


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_symbol(
    file: UploadFile = File(..., description="芯片 datasheet PDF"),
    device: str = Form(default="", max_length=200, description="可选的型号提示"),
    package: str = Form(default="", max_length=200, description="可选的封装代码提示"),
    strict_pages: bool = Form(default=False, description="只送引脚段+封装段给 MinerU"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """提交一次符号生成。

    ``device`` / ``package`` 是**逃生舱口**：自动识别永远会失手，用户填了
    就以用户为准（型号来源优先级的第一档）。
    """
    if len(device) > MAX_HINT_LENGTH or len(package) > MAX_HINT_LENGTH:
        raise HTTPException(status_code=400, detail="提示信息过长")
    return await _submit_job(
        file,
        device=device,
        package=package,
        strict_pages=strict_pages,
        current_user=current_user,
    )


@router.get("/drawings/{drawing_id}")
async def get_drawing(
    drawing_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """查询任务状态、产物清单与待答问题。"""
    tenant_id = str(current_user["tenant_id"])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, status, package_params, output_path, error_msg,
                       needs_review, created_at, updated_at
                FROM symbol_drawings
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    params = _package_params(row)
    return {
        "drawing_id": str(row["id"]),
        "status": row["status"],
        "device": params.get("device", ""),
        "package": params.get("package", ""),
        "pin_page": params.get("pin_page"),
        "verdict": params.get("verdict"),
        "score": params.get("score"),
        "confidence": params.get("confidence"),
        "check_report": params.get("check_report") or {},
        "graph_status": params.get("graph_status"),
        "reason": params.get("reason"),
        "layout_summary": params.get("layout_summary", ""),
        "warnings": params.get("warnings", []),
        "artifacts": sorted((params.get("artifacts") or {}).keys()),
        "human_history": params.get("human_history", []),
        "pending_input": _public_pending_input(params.get("pending_input")),
        "error_msg": row["error_msg"],
        "needs_review": row["needs_review"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/drawings/{drawing_id}/human-input")
async def submit_human_input(
    drawing_id: str,
    request: HumanInputRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """回答当前的人工询问并续跑任务。"""
    tenant_id = str(current_user["tenant_id"])
    answer = dict(request.answer)
    # 身份由认证上下文注入，不接受客户端自报。
    answer["_actor"] = {
        "user_id": str(current_user["user_id"]),
        "answered_at": datetime.now(timezone.utc).isoformat(),
    }

    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    SELECT status, package_params FROM symbol_drawings
                    WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
                    FOR UPDATE
                """),
                {"id": drawing_id, "tenant_id": tenant_id},
            )
            row = result.mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            if row["status"] != "awaiting_input":
                raise HTTPException(status_code=409, detail="该任务当前不接受人工输入")
            params = _package_params(row)
            pending = params.get("pending_input") or {}
            if not pending or pending.get("interrupt_id") != request.interrupt_id:
                raise HTTPException(status_code=409, detail="人工问题已过期，请刷新后重试")

            try:
                validated = validate_human_answer(pending, answer)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            validated["_actor"] = answer["_actor"]

            params["accepted_input"] = {
                "interrupt_id": request.interrupt_id,
                "answer": validated,
            }
            claimed = await session.execute(
                text("""
                    UPDATE symbol_drawings
                    SET status = 'ai_processing',
                        package_params = CAST(:package_params AS jsonb),
                        updated_at = NOW()
                    WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
                      AND status = :previous_status
                    RETURNING id
                """),
                {
                    "id": drawing_id,
                    "tenant_id": tenant_id,
                    "previous_status": row["status"],
                    "package_params": json.dumps(
                        params, ensure_ascii=False, default=_json_default
                    ),
                },
            )
            if claimed.scalar_one_or_none() is None:
                raise HTTPException(status_code=409, detail="该问题已被回答")

    task = asyncio.create_task(_run_job(drawing_id, tenant_id, resume_input={"accepted": True}))
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    logger.info("symbol.human_input_accepted", drawing_id=drawing_id)
    return {"drawing_id": drawing_id, "status": "ai_processing"}


@router.get("/drawings")
async def list_drawings(
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """按租户列出最近的任务。"""
    tenant_id = str(current_user["tenant_id"])
    limit = max(1, min(limit, 100))
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, status, package_params, created_at
                FROM symbol_drawings
                WHERE tenant_id = :tenant_id
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"tenant_id": tenant_id, "limit": limit},
        )
        rows = result.mappings().all()
    return {
        "items": [
            {
                "drawing_id": str(row["id"]),
                "status": row["status"],
                "device": _package_params(row).get("device", ""),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]
    }


@router.get("/drawings/{drawing_id}/artifacts/{artifact_name}")
async def download_artifact(
    drawing_id: str,
    artifact_name: str,
    current_user: dict = Depends(get_current_user),
) -> FileResponse:
    """下载产物（TCL 脚本 / .OLB / .DSN）。"""
    if artifact_name not in ARTIFACT_NAMES:
        raise HTTPException(status_code=404, detail="产物不存在")

    tenant_id = str(current_user["tenant_id"])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT package_params FROM symbol_drawings
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    recorded = (_package_params(row).get("artifacts") or {}).get(artifact_name)
    if not recorded:
        raise HTTPException(status_code=404, detail="该产物尚未生成")

    job_root = (SYMBOL_JOB_ROOT / str(drawing_id)).resolve()
    artifact_path = Path(recorded).resolve()
    if not artifact_path.is_relative_to(job_root) or not artifact_path.is_file():
        raise HTTPException(status_code=404, detail="产物文件不存在")

    media_type = ARTIFACT_MEDIA_TYPES.get(artifact_path.suffix.lower(), "application/octet-stream")
    return FileResponse(artifact_path, media_type=media_type, filename=artifact_path.name)


# ── 启动恢复 ──────────────────────────────────────────────────────


async def recover_symbol_jobs() -> None:
    """启动时接回被中断的任务。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, tenant_id
                FROM symbol_drawings
                WHERE status = 'ai_processing'
                   OR package_params->>'projection_sync_failed' = 'true'
                ORDER BY created_at
            """)
        )
        rows = result.mappings().all()

    for row in rows:
        drawing_id = str(row["id"])
        tenant_id = str(row["tenant_id"])
        task = asyncio.create_task(_run_job(drawing_id, tenant_id, recovering=True))
        _background_tasks.add(task)
        task.add_done_callback(_discard_background_task)
        logger.info("symbol.recovery_scheduled", drawing_id=drawing_id)
