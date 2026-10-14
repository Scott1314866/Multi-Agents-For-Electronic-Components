"""PCB 封装生成的 REST 接口。

四层结构（与 STEP 接口同构）：

    _submit_job            校验 + 落库 + 起后台 task
      └ _run_job           持 per-job advisory lock
          └ _run_owned_job 拿到锁后重读 DB，判定该喂什么给图
              └ _sync_job_projection  把 graph state 摊平成 package_params

三条不变式：**先落库再起 task**（否则崩溃后任务凭空消失）、
**投影失败不等于任务失败**（checkpoint 还在就能续）、
**停止不是成功**（``stopped_*`` 一律映射成 stopped，不许冒充 completed）。

图片入口接收封装尺寸图/推荐焊盘图，提取可追溯参数后交给同一 Allegro 链路。
原参数 JSON 入口保留，供上游与离线回归调用。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from langgraph.types import Command
from pydantic import BaseModel, Field
from sqlalchemy import text
from backend.api.source_attachments import read_attachments, public_attachments, attachment_response

from backend.agents.pcb.persistence import (
    get_postgres_pcb_snapshot,
    invoke_postgres_pcb_graph,
    open_postgres_pcb_job_lock,
    resume_postgres_pcb_graph_at_node,
)
from backend.agents.pcb.image_input import MAX_IMAGE_BYTES, normalize_image
from backend.agents.pcb.human_nodes import validate_human_answer
from backend.core.logger import get_logger
from backend.api.cadence import device_status
from backend.core.cadence_scheduler import cadence_scheduler
from backend.dependencies import AsyncSessionLocal, get_current_user

router = APIRouter()
logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PCB_JOB_ROOT = PROJECT_ROOT / "output" / "pcb_agent"
FRONTEND_ROOT = PROJECT_ROOT / "frontend" / "pcb"
SHARED_FRONTEND_ROOT = PROJECT_ROOT / "frontend" / "shared"
UI_ASSETS = {
    "app.js": (FRONTEND_ROOT / "app.js", "text/javascript"),
    "api.js": (FRONTEND_ROOT / "api.js", "text/javascript"),
    "styles.css": (FRONTEND_ROOT / "styles.css", "text/css"),
    "workspace.js": (SHARED_FRONTEND_ROOT / "workspace.js", "text/javascript"),
    "workspace.css": (SHARED_FRONTEND_ROOT / "workspace.css", "text/css"),
}


@router.get("/packages/{drawing_id}/attachments")
async def list_source_attachments(drawing_id: uuid.UUID, current_user: dict = Depends(get_current_user)):
    files = await read_attachments("pcb", drawing_id, current_user, root=PCB_JOB_ROOT, session_factory=AsyncSessionLocal)
    return public_attachments(files, f"/api/v1/pcb/packages/{drawing_id}/attachments")


@router.get("/packages/{drawing_id}/attachments/{name}")
async def get_source_attachment(drawing_id: uuid.UUID, name: str, current_user: dict = Depends(get_current_user)):
    if name not in {'source',*(f'context_{i}' for i in range(1,5)),*(f'document_{i}' for i in range(1,9))}:
        raise HTTPException(status_code=404, detail="附件不存在")
    files = await read_attachments("pcb", drawing_id, current_user, root=PCB_JOB_ROOT, session_factory=AsyncSessionLocal)
    return attachment_response(files, name)

#: 可下载的产物白名单。产物名 → 在 ``generated_files`` / ``artifact_paths`` 里的键。
ARTIFACT_KEYS = {
    "build_il",
    "verify_il",
    "props_il",
    "ilinit",
    "build_scr",
    "verify_scr",
    "props_scr",
    "dra",
    "psm",
    "land_pad",
    "ep_pad",
    "image_reading",
    "padstacks_zip",
    "preview_svg",
    "preview_png",
}
#: 给浏览器的友好后缀（下载时按产物种类命名）。
ARTIFACT_MEDIA_TYPES = {
    ".il": "text/plain; charset=utf-8",
    ".scr": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".zip": "application/zip",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

_background_tasks: set[asyncio.Task] = set()


# ── 请求模型 ──────────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    """提交一次封装生成。

    ``spec`` 就是 :class:`~backend.agents.pcb.spec.PackageSpec` 的字段集，
    见 ``backend.agents.pcb.spec.wson8_3x3`` 的示例。
    """

    spec: dict[str, Any] = Field(description="PackageSpec 的字段集")
    mode: Literal["full", "emit_only"] = Field(
        default="full",
        description="full = 生成并执行 Allegro；emit_only = 只生成 SKILL 装置",
    )
    allegro_exe: str | None = Field(
        default=None, description="所选设备的 allegro.exe 路径；留空使用服务端配置"
    )
    allegro_timeout: float | None = Field(default=None, gt=0, le=1800, description="单阶段超时（秒）")
    execution_mode: Literal["local", "ssh"] | None = Field(
        default=None, description="本机或 Symbol 配置的 SSH 设备；留空沿用服务端 PCB/Symbol 模式"
    )


class HumanInputRequest(BaseModel):
    """回答一次人工询问。"""

    interrupt_id: str = Field(min_length=1, description="来自 pending_input.interrupt_id")
    answer: dict[str, Any] = Field(description="例如 {'action': 'confirm'}")


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
    """任务结束后从持有集合里摘掉；意外异常在这里落日志。"""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("pcb.background_task_unhandled", exc_info=exc)


# ── 状态映射 ──────────────────────────────────────────────────────


def _pending_input(snapshot: dict) -> dict | None:
    """取当前待答的问题（含 interrupt id）。"""
    interrupts = snapshot.get("interrupts") or []
    if not interrupts:
        return None
    first = interrupts[0]
    value = first.get("value") or {}
    return {"interrupt_id": first.get("id"), **value}


def _public_pending_input(pending: dict | None) -> dict | None:
    """对外暴露前裁剪：不泄漏内部路径。"""
    if not pending:
        return None
    public = {
        key: value
        for key, value in pending.items()
        if key not in {"work_dir", "generated_files"}
    }
    if pending.get("stage") == "review":
        public["artifacts"] = sorted(ARTIFACT_KEYS)
    if pending.get("stage") == "image_parameters":
        def readable(value):
            if "validation errors for PackageSpec" in str(value) or "errors.pydantic.dev" in str(value):
                return "图片参数尚未完整：请补充封装名称、本体尺寸、高度和空缺脚号，再提交核对表。"
            return value
        public["issues"] = list(dict.fromkeys(readable(item) for item in pending.get("issues", [])))
        if pending.get("validation_error"):
            public["validation_error"] = readable(pending["validation_error"])
    return public


def _job_status(snapshot: dict) -> str:
    """把图的结果诚实地映射成任务状态。

    普通停止的图**不是**成功 —— 没有可续的问题、也没过人工审核时，
    一律归 stopped，不许冒充 completed。
    """
    pending = _pending_input(snapshot)
    if pending:
        return "pending_review" if pending.get("stage") == "review" else "awaiting_input"
    state = snapshot.get("values") or {}
    result = state.get("result") or {}
    graph_status = state.get("status") or result.get("status") or ""

    if graph_status == "failed" or result.get("status") == "failed":
        return "failed"
    review = state.get("human_review") or {}
    if review.get("action") == "reject" or graph_status == "rejected":
        return "rejected"
    if graph_status == "skills_ready":
        # emit_only 模式：装置齐了，但没执行 CAD，不能算 completed。
        return "pending_review"
    if graph_status.startswith("stopped"):
        return "stopped"
    if snapshot.get("next_nodes"):
        return "ai_processing"
    if review.get("action") == "approve" and graph_status == "reviewed":
        return "completed"
    return "stopped"


# ── 数据库与投影 ──────────────────────────────────────────────────


async def _update_job_failed(drawing_id: str, tenant_id: str, error: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE pcb_drawings
                SET status = 'failed', error_msg = :error, updated_at = NOW()
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id, "error": error[:4000]},
        )
        await session.commit()


async def _mark_job_recoverable(
    drawing_id: str, tenant_id: str, error: str, *, projection_failed: bool
) -> None:
    """把任务标成可恢复：留在 ``ai_processing``，但记下投影失败。"""
    params = {
        "projection_sync_failed": projection_failed,
        "projection_sync_error": error[:2000],
        "worker_interrupted": True,
    }
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE pcb_drawings
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
                FROM pcb_drawings
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _persist_job_snapshot(drawing_id: str, tenant_id: str, snapshot: dict) -> None:
    """把 graph state 摊平成 ``package_params`` 并向业务表投影。"""
    state = snapshot.get("values") or {}
    if not state:
        # Preserve source metadata when the database failed before checkpoint 1.
        await _update_job_failed(drawing_id,tenant_id,"执行前未保存检查点；原始资料已保留，可重新识别图片")
        return
    result = state.get("result") or {}
    job_status = _job_status(snapshot)
    review = state.get("human_review") or {}
    reviewed_by = (
        review.get("user_id") if review.get("action") in {"approve", "reject"} else None
    )
    reviewed_at = (
        datetime.fromisoformat(review["answered_at"])
        if reviewed_by and review.get("answered_at")
        else None
    )

    error: str | None = None
    if job_status == "failed":
        error = "; ".join(str(item) for item in state.get("errors", [])) or "PCB 封装生成失败"

    artifacts = result.get("artifacts") or state.get("artifact_paths") or {}
    if state.get("extraction_report_path"):
        artifacts = {**artifacts, "image_reading": state["extraction_report_path"]}
    package_params = {
        "mode": state.get("mode", "full"),
        "task": "generate_pcb_package",
        "spec": state.get("spec", {}),
        "input_kind": state.get("input_kind", "spec"),
        "image_path": state.get("image_path", ""),
        "context_image_paths": state.get("context_image_paths", []),
        "message": state.get("message", ""),
        "image_meta": state.get("image_meta", {}),
        "image_reading": state.get("image_reading", {}),
        "extraction_issues": state.get("extraction_issues", []),
        "source_extraction_issues": state.get("source_extraction_issues", state.get("extraction_issues", [])),
        "extraction_report_path": state.get("extraction_report_path", ""),
        "extraction_retry_id": state.get("extraction_retry_id"),
        "human_image_parameters": state.get("human_image_parameters"),
        "image_source_spec":state.get('image_source_spec',{}),
        "human_clarification_facts":state.get('human_clarification_facts',{}),
        "human_manufacturing_adjustments":state.get('human_manufacturing_adjustments',[]),
        "source_revision_id":state.get('source_revision_id',''),
        "source_document_paths":state.get('source_document_paths',[]),
        "context_source_info":state.get('context_source_info',[]),
        "spec_name": (state.get("spec") or {}).get("name", ""),
        "graph_status": state.get("status"),
        "result_status": result.get("status"),
        "derived_summary": state.get("derived_summary", ""),
        "clearance_issues": state.get("clearance_issues", []),
        "warnings": state.get("warnings", []),
        "verdict": state.get("verdict", {}),
        "artifacts": artifacts,
        "generated_files": state.get("generated_files", {}),
        "work_dir": state.get("work_dir", ""),
        "execution": state.get("execution", {}),
        "build_run": state.get("build_run", {}),
        "verify_run": state.get("verify_run", {}),
        "props_run": state.get("props_run", {}),
        "human_spec_confirmation": state.get("human_spec_confirmation"),
        "human_review": review or None,
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
                UPDATE pcb_drawings
                SET status = :status,
                    output_path = :output_path,
                    package_params = (COALESCE(package_params, '{}'::jsonb)
                        - 'accepted_input' - 'accepted_retry' - 'accepted_source') || CAST(:package_params AS jsonb),
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
                "output_path": artifacts.get("dra") or artifacts.get("build_il"),
                "package_params": json.dumps(package_params, ensure_ascii=False, default=_json_default),
                "error_msg": error[:4000] if error else None,
                "needs_review": job_status in {"pending_review", "awaiting_input"},
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            },
        )
        await session.commit()
    logger.info("pcb.job_checkpoint_saved", drawing_id=drawing_id, status=job_status)


async def _sync_job_projection(
    drawing_id: str, tenant_id: str, snapshot: dict | None = None
) -> None:
    """投影失败**不等于**任务失败：checkpoint 还在，下次能接着跑。"""
    try:
        if snapshot is None:
            snapshot = await get_postgres_pcb_snapshot(drawing_id)
        await _persist_job_snapshot(drawing_id, tenant_id, snapshot)
    except Exception as exc:
        logger.error("pcb.job_projection_failed", drawing_id=drawing_id, exc_info=True)
        await _mark_job_recoverable(
            drawing_id, tenant_id, f"状态投影失败：{exc}", projection_failed=True
        )


# ── 执行 ──────────────────────────────────────────────────────────


def _initial_state(params: dict) -> dict:
    return {
        "task": "generate_pcb_package",
        "mode": params.get("mode", "full"),
        "drawing_id": params.get("drawing_id", ""),
        "tenant_id": params.get("tenant_id", ""),
        "spec": params.get("spec", {}),
        "input_kind": params.get("input_kind", "spec"),
        "image_path": params.get("image_path", ""),
        "context_image_paths": params.get("context_image_paths", []),
        "source_document_paths":params.get('source_document_paths',[]),
        "context_source_info":params.get('context_source_info',[]),
        "message": params.get("message", ""),
        "work_dir": params.get("work_dir") or "",
        "allegro_exe": params.get("allegro_exe"),
        "allegro_timeout": params.get("allegro_timeout"),
        "execution_mode": params.get("execution_mode"),
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
    retry = params.get("accepted_retry")
    supplement=params.get('accepted_source')
    if supplement:
        snapshot=await get_postgres_pcb_snapshot(drawing_id)
        values=snapshot.get('values') or {}
        if values.get('source_revision_id')!=supplement['id']:
            matching=next((i for i in snapshot.get('interrupts',[]) if i['id']==supplement['interrupt_id'] and i['value'].get('stage')=='image_parameters'),None)
            if not matching or values.get('human_image_parameters'):
                await _sync_job_projection(drawing_id,tenant_id,snapshot);return
            updates={'image_path':supplement['image_path'],'context_image_paths':supplement['context_image_paths'],
                     'source_document_paths':supplement['source_document_paths'],'source_revision_id':supplement['id'],
                     'context_source_info':supplement.get('context_source_info',[]),
                     'spec':{},'image_source_spec':{},'image_reading':{},'extraction_issues':[],
                     'human_clarification_facts':{},'human_manufacturing_adjustments':[],
                     'human_history':[*values.get('human_history',[]),{'stage':'source_supplement',
                         **{k:supplement[k] for k in ('id','user_id','answered_at','role','filename','pages')}}],
                     'artifact_paths':{},'generated_files':{},'extraction_report_path':'','errors':[],
                     'status':'input_ready','human_image_parameters':None,'human_spec_confirmation':None}
            await resume_postgres_pcb_graph_at_node(drawing_id,updates,as_node='prepare_input')
            await _sync_job_projection(drawing_id,tenant_id);return
    if retry:
        snapshot = await get_postgres_pcb_snapshot(drawing_id)
        values = snapshot.get("values") or {}
        if values.get("extraction_retry_id") != retry["id"]:
            if not snapshot.get('checkpoint_id') and not values:
                updates={**_initial_state(params),'extraction_retry_id':retry['id']}
                await invoke_postgres_pcb_graph(drawing_id,updates)
                await _sync_job_projection(drawing_id,tenant_id)
                return
            if not snapshot.get("checkpoint_id") or values.get("status") != "failed" or values.get("image_reading"):
                await _sync_job_projection(drawing_id, tenant_id, snapshot)
                return
            updates = {**_initial_state(params), "status": "input_ready", "extraction_retry_id": retry["id"],
                       "result": {}, "spec": {}, "image_reading": {}, "extraction_issues": [],
                       "artifact_paths": {}, "generated_files": {}, "extraction_report_path": "",
                       "human_image_parameters": None, "human_spec_confirmation": None,
                       "human_review": None, "needs_review": False}
            await resume_postgres_pcb_graph_at_node(drawing_id, updates, as_node="prepare_input")
            await _sync_job_projection(drawing_id, tenant_id)
            return
    if recovering or resume_input is not None or accepted or retry:
        try:
            snapshot = await get_postgres_pcb_snapshot(drawing_id)
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
            # 只消费已认证、已校验的持久化回答；过期回答不会误答后一个问题。
            graph_input: Any = Command(resume={matching["id"]: accepted["answer"]})
        elif interrupts or not snapshot.get("next_nodes"):
            await _sync_job_projection(drawing_id, tenant_id, snapshot)
            return
        else:
            # None 续跑 checkpoint 的下一批节点；重新塞初始 state 会重放人工阶段。
            graph_input = None
    else:
        graph_input = _initial_state(params)

    try:
        await invoke_postgres_pcb_graph(drawing_id, graph_input)
    except Exception as exc:
        logger.error("pcb.job_exception", drawing_id=drawing_id, exc_info=True)
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
    """持 per-job advisory lock 执行；拿不到锁说明别的 worker 在跑。"""
    async with open_postgres_pcb_job_lock(drawing_id, wait=not recovering) as acquired:
        if not acquired:
            logger.info("pcb.recovery_active_worker_skipped", drawing_id=drawing_id)
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
            logger.error("pcb.job_unhandled", drawing_id=drawing_id, exc_info=True)
            await _update_job_failed(drawing_id, tenant_id, str(exc))


async def _submit_job(request: GenerateRequest, current_user: dict, *, image_input: dict | None = None) -> dict:
    drawing_id = str(uuid.uuid4())
    tenant_id = str(current_user["tenant_id"])
    work_dir = PCB_JOB_ROOT / drawing_id
    work_dir.mkdir(parents=True, exist_ok=True)

    params = {
        "drawing_id": drawing_id,
        "tenant_id": tenant_id,
        "mode": request.mode,
        "task": "generate_pcb_package",
        "spec": request.spec,
        "allegro_exe": request.allegro_exe,
        "allegro_timeout": request.allegro_timeout,
        "execution_mode": request.execution_mode,
        "work_dir": str(work_dir.resolve()),
        "retryable": False,
    }
    image_path = None
    uploaded_paths = []
    if image_input:
        image_path = work_dir / "input.png"
        image_path.write_bytes(image_input["content"])
        uploaded_paths.append(image_path)
        context_paths = []
        for i, context in enumerate(image_input.get("contexts", [])):
            context_path = work_dir / f"context_{i+1}.png"
            context_path.write_bytes(context)
            uploaded_paths.append(context_path)
            context_paths.append(str(context_path.resolve()))
        params.update({"input_kind": "image", "image_path": str(image_path.resolve()),
                       "context_image_paths": context_paths,
                       "image_meta": image_input["meta"], "message": image_input["message"]})
        params['context_source_info']=image_input.get('context_source_info',[])
        documents=[]
        for i,document in enumerate(image_input.get('documents',[])):
            path=work_dir/f'source_document_{i+1}.pdf';path.write_bytes(document);uploaded_paths.append(path);documents.append(str(path.resolve()))
        params['source_document_paths']=documents
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO pcb_drawings (id, tenant_id, status, package_params)
                    VALUES (CAST(:id AS uuid), :tenant_id, 'ai_processing',
                            CAST(:package_params AS jsonb))
                """),
                {
                    "id": drawing_id,
                    "tenant_id": tenant_id,
                    "package_params": json.dumps(params, ensure_ascii=False, default=_json_default),
                },
            )
            await session.commit()
    except Exception:
        for path in uploaded_paths:
            path.unlink(missing_ok=True)
        logger.error("pcb.job_insert_failed", drawing_id=drawing_id, exc_info=True)
        raise HTTPException(status_code=503, detail="任务暂时无法登记，请稍后重试")

    task = asyncio.create_task(_run_job(drawing_id, tenant_id))
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    logger.info("pcb.job_submitted", drawing_id=drawing_id, tenant_id=tenant_id)
    return {
        "drawing_id": drawing_id,
        "status": "ai_processing",
        "status_url": f"/api/v1/pcb/packages/{drawing_id}",
    }


# ── 路由 ──────────────────────────────────────────────────────────


@router.get("/ui", include_in_schema=False)
async def pcb_generation_ui():
    return FileResponse(FRONTEND_ROOT / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/ui/assets/{asset_name}", include_in_schema=False)
async def pcb_ui_asset(asset_name: str):
    if asset_name not in UI_ASSETS:
        raise HTTPException(status_code=404, detail="页面资源不存在")
    path, media_type = UI_ASSETS[asset_name]
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@router.get("/devices")
async def pcb_devices(current_user: dict = Depends(get_current_user)):
    return device_status(str(current_user["tenant_id"]))


@router.post("/generate-image", status_code=status.HTTP_202_ACCEPTED)
async def generate_package_from_image(
    file: UploadFile = File(..., description="封装尺寸图或推荐焊盘图"),
    message: str = Form(default="", max_length=4000),
    mode: Literal["full", "emit_only"] = Form(default="full"),
    execution_mode: Literal["local", "ssh"] | None = Form(default=None),
    allegro_timeout: float | None = Form(default=None, gt=0, le=1800),
    context_images: list[UploadFile] | None = File(default=None, description="最多4张辅助名称/本体尺寸/编号图；结果参考截图不应作为输入"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """图片 → 参数与证据 → 人工补充/确认 → Allegro 封装。"""
    uploads = [file, *(context_images or [])]
    normalized_images = []
    documents=[]
    try:
        if len(uploads) > 5:
            raise HTTPException(status_code=422, detail="最多支持四张辅助图片")
        for index, upload in enumerate(uploads):
            label = "主图" if index == 0 else f"第{index}张辅助图"
            filename = Path(upload.filename or "未命名文件").name[:200]
            content = await upload.read(MAX_IMAGE_BYTES + 1)
            if len(content) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail=f"{label}“{filename}”超过 32 MB")
            if content.startswith(b"%PDF-") or filename.lower().endswith(".pdf"):
                if index==0:raise HTTPException(status_code=422, detail=f"主图“{filename}”是 PDF。主图请使用 PNG、JPG、WEBP、BMP，PDF手册请放在辅助资料中上传。")
                from backend.agents.pcb.source_inputs import decode_supplement
                try:supplement=await asyncio.to_thread(decode_supplement,content,filename,message)
                except ValueError as exc:raise HTTPException(status_code=422,detail=f'{label}“{filename}”：{exc}') from exc
                normalized_images.extend((image,{**supplement['meta'],'filename':filename,'page':page}) for image,page in zip(supplement['images'],supplement['pages']))
                documents.append(content)
                continue
            try:
                normalized, meta = await asyncio.to_thread(normalize_image, content)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{label}“{filename}”无法读取：{exc}") from exc
            normalized_images.append((normalized, {**meta,'filename':filename}))
    finally:
        for upload in uploads:
            await upload.close()
    if len(normalized_images)>5:raise HTTPException(status_code=422,detail='辅助资料渲染后最多4页，请减少图片数量或仅上传对应器件的手册')
    return await _submit_job(GenerateRequest(spec={}, mode=mode, execution_mode=execution_mode,
                                           allegro_timeout=allegro_timeout), current_user,
                             image_input={"content": normalized_images[0][0], "meta": normalized_images[0][1],
                                          "contexts": [content for content, _ in normalized_images[1:]],
                                          'context_source_info':[{k:meta[k] for k in ('filename','page','sha256') if k in meta} for _,meta in normalized_images[1:]],
                                          "documents":documents,"message": message})


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_package(
    request: GenerateRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """提交一次 PCB 封装生成。

    参数不合规不会走到这里失败 —— 它会在图中被拦到 ``stopped_invalid_spec``
    终态，并通过 ``pending_input`` / ``result.issues`` 把问题原样交回。
    """
    return await _submit_job(request, current_user)


@router.get("/packages/{drawing_id}")
async def get_package(
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
                FROM pcb_drawings
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
        "spec_name": params.get("spec_name", ""),
        "input_kind": params.get("input_kind", "spec"),
        "spec": params.get("spec", {}),
        "image_meta": params.get("image_meta", {}),
        # Older completed checkpoints retain extraction-time errors. Keep these
        # in the source report, not as unresolved errors after a valid submit.
        "extraction_issues": [] if params.get('human_image_parameters') else params.get("extraction_issues", []),
        "source_extraction_issues": params.get("source_extraction_issues", params.get("extraction_issues", [])),
        "evidence": (params.get("image_reading") or {}).get("evidence", {}),
        "summary": params.get("derived_summary", ""),
        "issues": params.get("clearance_issues", []),
        "warnings": params.get("warnings", []),
        "verdict": params.get("verdict", {}),
        "execution": params.get("execution", {}),
        "runs": {stage: params.get(f"{stage}_run", {}) for stage in ("build", "verify", "props")},
        "artifacts": sorted((params.get("artifacts") or {}).keys()),
        "artifact_urls": {name: f"/api/v1/pcb/packages/{drawing_id}/artifacts/{name}"
                          for name in sorted((params.get("artifacts") or {}).keys()) if name in ARTIFACT_KEYS},
        "human_input_url": f"/api/v1/pcb/packages/{drawing_id}/human-input",
        "queue": next((entry for entry in cadence_scheduler.snapshot(tenant_id)
                       if entry["kind"] == "pcb" and entry["drawing_id"] == drawing_id), None),
        "human_history": params.get("human_history", []),
        "source_revision_id":params.get('source_revision_id',''),
        "pending_input": _public_pending_input(params.get("pending_input")),
        "error_msg": row["error_msg"],
        "extraction_retryable": row["status"] == "failed" and params.get("input_kind") == "image"
            and not params.get("image_reading"),
        "needs_review": row["needs_review"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/packages/{drawing_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_image_extraction(drawing_id: uuid.UUID, current_user: dict = Depends(get_current_user)) -> dict:
    """Retry failed image reading in the same job, preserving source and audit history."""
    drawing_id = str(drawing_id)
    tenant_id = str(current_user["tenant_id"])
    async with open_postgres_pcb_job_lock(drawing_id, wait=False) as acquired:
        if not acquired:
            raise HTTPException(status_code=409, detail="任务仍在执行，请稍后重试")
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT id, status, package_params FROM pcb_drawings
                WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant_id FOR UPDATE
            """), {"id": drawing_id, "tenant_id": tenant_id})
            row = result.mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            params = _package_params(row)
            if row["status"] != "failed" or params.get("input_kind") != "image" or params.get("image_reading"):
                raise HTTPException(status_code=409, detail="只支持重新识别尚未提取成功的图片任务")
            source = Path(params.get("image_path") or "").resolve()
            if not source.is_relative_to((PCB_JOB_ROOT / drawing_id).resolve()) or not source.is_file():
                raise HTTPException(status_code=409, detail="原图不存在，无法重新识别")
            retry = {"id": str(uuid.uuid4()), "user_id": str(current_user["user_id"]),
                     "requested_at": datetime.now(timezone.utc).isoformat()}
            patch = {"accepted_retry": retry, "retry_history": [*(params.get("retry_history") or []), retry]}
            await session.execute(text("""
                UPDATE pcb_drawings SET status='ai_processing', error_msg=NULL, needs_review=FALSE,
                    package_params=COALESCE(package_params,'{}'::jsonb) || CAST(:patch AS jsonb), updated_at=NOW()
                WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant_id
            """), {"id": drawing_id, "tenant_id": tenant_id, "patch": json.dumps(patch)})
            await session.commit()
    task = asyncio.create_task(_run_job(drawing_id, tenant_id, recovering=True))
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    return {"drawing_id": drawing_id, "status": "ai_processing",
            "status_url": f"/api/v1/pcb/packages/{drawing_id}"}


@router.post('/packages/{drawing_id}/sources',status_code=status.HTTP_202_ACCEPTED)
async def supplement_sources(drawing_id:uuid.UUID,file:UploadFile=File(...),interrupt_id:str=Form(...),
                             role:Literal['primary','context']=Form('context'),current_user:dict=Depends(get_current_user)):
    """Accept a source revision only for this tenant's current unconfirmed image task."""
    from backend.agents.pcb.source_inputs import decode_supplement
    try:
        content=await file.read(MAX_IMAGE_BYTES+1)
        filename=file.filename or '补充资料'
        if role=='primary' and (content.startswith(b'%PDF-') or filename.lower().endswith('.pdf')):
            raise HTTPException(status_code=422,detail='推荐焊盘主图请使用图片；PDF请选择辅助手册')
        try:decoded=await asyncio.to_thread(decode_supplement,content,filename)
        except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc
    finally:await file.close()
    drawing_id=str(drawing_id);tenant_id=str(current_user['tenant_id']);written=[]
    async with open_postgres_pcb_job_lock(drawing_id,wait=False) as acquired:
        if not acquired:raise HTTPException(status_code=409,detail='任务正在执行，请等到追问出现后补充资料')
        try:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    result=await session.execute(text('SELECT status,package_params FROM pcb_drawings WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant_id FOR UPDATE'),{'id':drawing_id,'tenant_id':tenant_id})
                    row=result.mappings().first()
                    if row is None:raise HTTPException(status_code=404,detail='任务不存在')
                    params=_package_params(row);pending=params.get('pending_input') or {}
                    if row['status']!='awaiting_input' or pending.get('stage')!='image_parameters' or pending.get('interrupt_id')!=interrupt_id:
                        raise HTTPException(status_code=409,detail='当前问题已变化或已生成封装，请刷新后再补充')
                    if len(params.get('source_upload_history') or [])>=8:raise HTTPException(status_code=422,detail='本任务已补充8次资料，请核对来源后重新提交任务')
                    folder=(PCB_JOB_ROOT/drawing_id).resolve()
                    primary=Path(params.get('image_path') or '').resolve()
                    if not primary.is_relative_to(folder) or not primary.is_file():raise HTTPException(status_code=409,detail='原图不存在，不能继续补充')
                    revision=uuid.uuid4().hex;paths=[]
                    for i,image in enumerate(decoded['images']):
                        path=folder/f'supplement_{revision}_{i+1}.png';path.write_bytes(image);written.append(path);paths.append(str(path))
                    contexts=list(params.get('context_image_paths') or []);documents=list(params.get('source_document_paths') or [])
                    info=list(params.get('context_source_info') or [{} for _ in contexts])
                    if role=='primary':
                        contexts=[str(primary),*contexts][-4:];info=[{'filename':'此前主图'},*info][-4:];primary=Path(paths[0])
                    else:
                        contexts=[*contexts,*paths][-4:]
                        additions=[{'filename':decoded['filename'],'sha256':decoded['meta']['sha256'],**({'page':page} if page else {})} for page in (decoded['pages'] or [None])]
                        info=[*info,*additions][-4:]
                    if decoded['document']:
                        path=folder/f'supplement_{revision}.pdf';path.write_bytes(decoded['document']);written.append(path);documents.append(str(path))
                    accepted={'id':revision,'interrupt_id':interrupt_id,'image_path':str(primary),
                              'context_image_paths':contexts,'source_document_paths':documents,'role':role,
                              'context_source_info':info,
                              'filename':decoded['filename'],'pages':decoded['pages'],'sha256':decoded['meta']['sha256'],
                              'user_id':str(current_user['user_id']),'answered_at':datetime.now(timezone.utc).isoformat()}
                    params.update(accepted_source=accepted,source_upload_history=[*params.get('source_upload_history',[]),{k:accepted[k] for k in ('id','role','filename','pages','sha256','user_id','answered_at')}])
                    await session.execute(text("UPDATE pcb_drawings SET status='ai_processing',error_msg=NULL,package_params=CAST(:params AS jsonb),updated_at=NOW() WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant_id"),{'id':drawing_id,'tenant_id':tenant_id,'params':json.dumps(params,ensure_ascii=False)})
        except BaseException:
            for path in written:path.unlink(missing_ok=True)
            raise
    task=asyncio.create_task(_run_job(drawing_id,tenant_id,recovering=True));_background_tasks.add(task);task.add_done_callback(_discard_background_task)
    return {'drawing_id':drawing_id,'status':'ai_processing','status_url':f'/api/v1/pcb/packages/{drawing_id}'}


@router.post("/packages/{drawing_id}/human-input")
async def submit_human_input(
    drawing_id: str,
    request: HumanInputRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """回答当前的人工询问并续跑任务。

    compare-and-set：只有把任务从 ``awaiting_input``/``pending_review``
    抢过来的那一次回答会被接受，重复提交返回 409。
    """
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
                    SELECT status, package_params FROM pcb_drawings
                    WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
                    FOR UPDATE
                """),
                {"id": drawing_id, "tenant_id": tenant_id},
            )
            row = result.mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            if row["status"] not in {"awaiting_input", "pending_review"}:
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
                    UPDATE pcb_drawings
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
                    "package_params": json.dumps(params, ensure_ascii=False, default=_json_default),
                },
            )
            if claimed.scalar_one_or_none() is None:
                raise HTTPException(status_code=409, detail="该问题已被回答")

    task = asyncio.create_task(_run_job(drawing_id, tenant_id, resume_input={"accepted": True}))
    _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    logger.info("pcb.human_input_accepted", drawing_id=drawing_id)
    return {"drawing_id": drawing_id, "status": "ai_processing"}


@router.get("/packages")
async def list_packages(
    limit: int = 20,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """按租户列出最近的任务。"""
    tenant_id = str(current_user["tenant_id"])
    limit = max(1, min(limit, 100))
    offset = max(offset, 0)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, status, package_params, created_at
                FROM pcb_drawings
                WHERE tenant_id = :tenant_id
                ORDER BY created_at DESC, id DESC
                LIMIT :limit OFFSET :offset
            """),
            {"tenant_id": tenant_id, "limit": limit + 1, "offset": offset},
        )
        rows = result.mappings().all()
    return {
        "items": [
            {
                "drawing_id": str(row["id"]),
                "status": row["status"],
                "spec_name": _package_params(row).get("spec_name", ""),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows[:limit]
        ],
        "has_more": len(rows) > limit,
        "limit": limit,
        "offset": offset,
    }


@router.get("/packages/{drawing_id}/artifacts/{artifact_name}")
async def download_artifact(
    drawing_id: str,
    artifact_name: str,
    current_user: dict = Depends(get_current_user),
) -> FileResponse:
    """下载一份产物（SKILL 脚本、剧本或 Allegro 生成的二进制）。

    产物名走白名单；路径必须落在该任务的目录内（防目录穿越）。
    """
    if artifact_name not in ARTIFACT_KEYS:
        raise HTTPException(status_code=404, detail="产物不存在")

    tenant_id = str(current_user["tenant_id"])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT package_params FROM pcb_drawings
                WHERE id = CAST(:id AS uuid) AND tenant_id = :tenant_id
            """),
            {"id": drawing_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    params = _package_params(row)
    recorded = (params.get("artifacts") or {}).get(artifact_name)
    if not recorded:
        raise HTTPException(status_code=404, detail="该产物尚未生成")

    job_root = (PCB_JOB_ROOT / str(drawing_id)).resolve()
    artifact_path = Path(recorded).resolve()
    if not artifact_path.is_relative_to(job_root) or not artifact_path.is_file():
        raise HTTPException(status_code=404, detail="产物文件不存在")

    media_type = ARTIFACT_MEDIA_TYPES.get(artifact_path.suffix.lower(), "application/octet-stream")
    return FileResponse(artifact_path, media_type=media_type, filename=artifact_path.name)


# ── 启动恢复 ──────────────────────────────────────────────────────


async def recover_pcb_jobs() -> None:
    """启动时接回被中断的任务。

    扫 ``ai_processing`` 与"投影失败"两类；每个任务在 per-job 锁下恢复，
    别的进程正在跑就跳过（不重放）。
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, tenant_id
                FROM pcb_drawings
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
        logger.info("pcb.recovery_scheduled", drawing_id=drawing_id)
