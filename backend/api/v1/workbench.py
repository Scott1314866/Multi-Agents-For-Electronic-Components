"""Authenticated unified adapters; existing Agents own durable jobs and artifacts."""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
import shutil
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.api.v1 import pcb, step, symbol
from backend.api.cadence import device_status
from backend.api.source_attachments import read_attachments, public_attachments, attachment_response
from backend.config import get_settings
from backend.core.cadence_scheduler import cadence_scheduler, resolve_mode
from backend.dependencies import AsyncSessionLocal, get_current_user, engine

router = APIRouter()
Kind = Literal["step", "symbol", "pcb"]
Mode = Literal["local", "ssh"]
FRONTEND_ROOT = Path(__file__).resolve().parents[3] / "frontend/workbench"
ASSETS = {"app.js": "text/javascript", "api.js": "text/javascript", "styles.css": "text/css"}
SHARED_ASSETS = {"workspace.js": "text/javascript", "workspace.css": "text/css"}


@router.get("/ready", tags=["系统"])
async def readiness():
    async def probe():
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    try:
        await asyncio.wait_for(probe(), timeout=5)
    except Exception:
        raise HTTPException(503, "数据库暂不可用，请稍后重试或联系管理员") from None
    return {"status": "ready", "database": "ok"}


def _base(kind: str, drawing_id: str) -> str:
    return f"/api/v1/{kind}/{'packages' if kind == 'pcb' else 'drawings'}/{drawing_id}"


def _normalized(kind: str, job: dict) -> dict:
    drawing_id = str(job["drawing_id"])
    base = _base(kind, drawing_id)
    execution = job.get("capture_execution") if kind == "symbol" else job.get("execution")
    artifacts = dict(job.get("artifact_urls") or {})
    if kind == "pcb":
        artifacts = {name: base + f"/artifacts/{name}" for name in job.get("artifacts", []) if name in pcb.ARTIFACT_KEYS}
    if kind == "step":
        if job.get("step_file_url"):
            artifacts["step"] = job["step_file_url"]
        if job.get("preview_url"):
            artifacts["preview"] = job["preview_url"]
        artifacts.update({"preview_" + view: url for view, url in (job.get("preview_urls") or {}).items() if isinstance(url, str)})
    return {"drawing_id": drawing_id, "kind": kind,
            "title": job.get("spec_name") or job.get("device") or (job.get("layout") or {}).get("part_name") or f"{kind.upper()} · {drawing_id[:8]}",
            "status": job["status"], "pending_input": job.get("pending_input"),
            "execution": execution or {}, "artifacts": artifacts,
            "human_input_url": f"/api/v1/workbench/jobs/{kind}/{drawing_id}/human-input",
            "error_msg": job.get("error_msg"), "created_at": job.get("created_at"),
            "detail": job}


@router.get("/ui", include_in_schema=False)
async def ui():
    return FileResponse(FRONTEND_ROOT / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/ui/assets/{name}", include_in_schema=False)
async def asset(name: str):
    if name in SHARED_ASSETS:
        return FileResponse(FRONTEND_ROOT.parent / "shared" / name, media_type=SHARED_ASSETS[name], headers={"Cache-Control": "no-cache"})
    if name not in ASSETS:
        raise HTTPException(status_code=404, detail="页面资源不存在")
    return FileResponse(FRONTEND_ROOT / name, media_type=ASSETS[name], headers={"Cache-Control": "no-cache"})


@router.get("/devices")
async def devices(current_user: dict = Depends(get_current_user)):
    return device_status(str(current_user["tenant_id"]), settings=get_settings(),
                         defaults={kind: resolve_mode(kind) for kind in ("symbol", "pcb")})


@router.post("/jobs", status_code=202)
async def submit(
    kind: Kind = Form(...), file: UploadFile = File(...),
    message: str = Form(default="", max_length=4000),
    execution_mode: Mode | None = Form(default=None),
    device: str = Form(default="", max_length=200), package: str = Form(default="", max_length=200),
    mode: Literal["full", "emit_only"] = Form(default="full"),
    strict_pages: bool = Form(default=False),
    context_images: list[UploadFile] | None = File(default=None),
    current_user: dict = Depends(get_current_user),
):
    try:
        if context_images and kind != "pcb":
            raise HTTPException(status_code=422, detail="辅助尺寸图仅用于 PCB 任务")
        if kind == "step" and execution_mode:
            raise HTTPException(status_code=422, detail="STEP 使用后端 OpenCascade，不需要选择 Cadence 设备")
        if kind == "step":
            response = await step.generate_step_from_user_input(message=message or "生成这张工程图对应的 STEP 模型", file=file, current_user=current_user)
        elif kind == "symbol":
            response = await symbol.generate_symbol(file=file, device=device, package=package,
                                                    strict_pages=strict_pages, execution_mode=execution_mode, current_user=current_user)
        else:
            response = await pcb.generate_package_from_image(file=file, message=message, mode=mode,
                execution_mode=execution_mode, allegro_timeout=None, context_images=context_images, current_user=current_user)
        return {**response, "kind": kind, "status_url": f"/api/v1/workbench/jobs/{kind}/{response['drawing_id']}"}
    finally:
        for upload in [file, *(context_images or [])]:
            await upload.close()


@router.get("/jobs")
async def jobs(kind: Kind | None = None, limit: int = Query(30, ge=1, le=100),
               offset: int = Query(0, ge=0), current_user: dict = Depends(get_current_user)):
    # One globally sorted, tenant-scoped page. No duplicate task table or polling
    # side effects; Symbol soft-deleted tasks remain available in its detail UI.
    query = text("""
        SELECT * FROM (
          SELECT 'step' AS kind, id, status, package_params, created_at FROM step_drawings WHERE tenant_id=:tenant_id
          UNION ALL
          SELECT 'symbol' AS kind, id, status, package_params, created_at FROM symbol_drawings WHERE tenant_id=:tenant_id AND deleted_at IS NULL
          UNION ALL
          SELECT 'pcb' AS kind, id, status, package_params, created_at FROM pcb_drawings WHERE tenant_id=:tenant_id
        ) AS tasks
        WHERE (CAST(:kind AS text) IS NULL OR kind=CAST(:kind AS text))
        ORDER BY created_at DESC, kind, id DESC LIMIT :limit OFFSET :offset
    """)
    async with AsyncSessionLocal() as session:
        result = await session.execute(query, {"tenant_id": str(current_user["tenant_id"]), "kind": kind, "limit": limit + 1, "offset": offset})
        rows = result.mappings().all()
    items = []
    for row in rows[:limit]:
        params = row["package_params"] or {}
        if isinstance(params, str):
            params = json.loads(params)
        drawing_id = str(row["id"])
        items.append({"kind": row["kind"], "drawing_id": drawing_id, "status": row["status"],
            "title": params.get("spec_name") or params.get("device") or params.get("original_filename") or params.get("human_request") or row["kind"].upper(),
            "created_at": row["created_at"], "execution_mode": params.get("execution_mode"),
            "status_url": f"/api/v1/workbench/jobs/{row['kind']}/{drawing_id}"})
    return {"items": items, "has_more": len(rows) > limit, "limit": limit, "offset": offset}


@router.get("/jobs/{kind}/{drawing_id}")
async def job(kind: Kind, drawing_id: uuid.UUID, current_user: dict = Depends(get_current_user)):
    if kind == "pcb":
        data = await pcb.get_package(str(drawing_id), current_user)
    elif kind == "symbol":
        data = await symbol.get_drawing(drawing_id, current_user)
    else:
        data = await step.get_step_drawing(drawing_id, current_user, include_checkpoint=False)
    normalized = _normalized(kind, data)
    normalized["queue"] = next((entry for entry in cadence_scheduler.snapshot(str(current_user["tenant_id"]))
                                if entry["kind"] == kind and entry["drawing_id"] == str(drawing_id)), None)
    return normalized


async def _attachments(kind: Kind, drawing_id: uuid.UUID, current_user: dict) -> list[dict]:
    root = {"step": step.STEP_JOB_ROOT, "symbol": symbol.SYMBOL_JOB_ROOT, "pcb": pcb.PCB_JOB_ROOT}[kind]
    return await read_attachments(kind, drawing_id, current_user, root=root, session_factory=AsyncSessionLocal)


@router.get("/jobs/{kind}/{drawing_id}/attachments")
async def attachments(kind: Kind, drawing_id: uuid.UUID, current_user: dict = Depends(get_current_user)):
    files = await _attachments(kind, drawing_id, current_user)
    return public_attachments(files, f"/api/v1/workbench/jobs/{kind}/{drawing_id}/attachments")


@router.get("/jobs/{kind}/{drawing_id}/attachments/{name}")
async def attachment(kind: Kind, drawing_id: uuid.UUID, name: str, current_user: dict = Depends(get_current_user)):
    if name not in {'source',*(f'context_{i}' for i in range(1,5)),*(f'document_{i}' for i in range(1,9))}:
        raise HTTPException(status_code=404, detail="附件不存在")
    files = await _attachments(kind, drawing_id, current_user)
    return attachment_response(files, name)


class Answer(BaseModel):
    interrupt_id: str = Field(min_length=1)
    answer: dict


@router.post("/jobs/{kind}/{drawing_id}/human-input", status_code=202)
async def answer(kind: Kind, drawing_id: uuid.UUID, request: Answer, current_user: dict = Depends(get_current_user)):
    # Existing adapters inject the authenticated actor and enforce current
    # interrupt / compare-and-set checks, so this endpoint cannot bypass review.
    if kind == "pcb":
        return await pcb.submit_human_input(str(drawing_id), pcb.HumanInputRequest(**request.model_dump()), current_user)
    if kind == "symbol":
        return await symbol.submit_human_input(drawing_id, symbol.HumanInputRequest(**request.model_dump()), current_user)
    return await step.submit_step_human_input(drawing_id, step.HumanInputRequest(**request.model_dump()), current_user)


@router.get("/jobs/{kind}/{drawing_id}/artifacts/{name}")
async def download(kind: Kind, drawing_id: uuid.UUID, name: str, current_user: dict = Depends(get_current_user)):
    if kind == "pcb":
        return await pcb.download_artifact(str(drawing_id), name, current_user)
    if kind == "symbol":
        return await symbol.download_artifact(drawing_id, name, current_user)
    return await step.get_step_artifact(drawing_id, name, current_user)
