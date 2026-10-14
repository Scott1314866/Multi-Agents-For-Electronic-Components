"""Tenant-owned upload access shared by workbench and dedicated agent pages."""
import json
from pathlib import Path
import uuid
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import text

SOURCE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".bmp": "image/bmp", ".pdf": "application/pdf"}

async def read_attachments(kind: str, drawing_id: uuid.UUID, current_user: dict, *, root: Path, session_factory) -> list[dict]:
    # Table names and directories are application constants, never request paths.
    table = {"step": "step_drawings", "symbol": "symbol_drawings", "pcb": "pcb_drawings"}[kind]
    columns = "source_image_path, package_params" if kind == "step" else "package_params"
    active = " AND deleted_at IS NULL" if kind == "symbol" else ""
    async with session_factory() as session:
        result = await session.execute(
            text(f"SELECT {columns} FROM {table} WHERE id=:id AND tenant_id=:tenant_id{active}"),
            {"id": drawing_id, "tenant_id": str(current_user["tenant_id"])},
        )
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    params = row["package_params"] or {}
    if isinstance(params, str):
        params = json.loads(params)
    source = row["source_image_path"] if kind == "step" else params.get("source_path") or params.get("image_path") or params.get("pdf_path")
    entries = [("source", "上传原图" if kind != "symbol" or params.get("input_kind") != "pdf" else "原始手册", source)]
    if kind == "pcb":
        entries.extend((f"context_{i+1}", f"辅助尺寸图 {i+1}", path)
                       for i, path in enumerate((params.get("context_image_paths") or [])[:4]))
        entries.extend((f'document_{i+1}',f'补充手册 {i+1}',path) for i,path in enumerate((params.get('source_document_paths') or [])[:8]))
    job_root = (root / str(drawing_id)).resolve()
    files = []
    for name, label, stored in entries:
        if not stored:
            continue
        path = Path(stored).resolve()
        media_type = SOURCE_TYPES.get(path.suffix.lower())
        if not path.is_relative_to(job_root) or not path.is_file() or not media_type:
            continue
        if media_type == "application/pdf" and kind == "step":
            continue
        filename = Path(str(params.get("original_filename") or path.name).replace("\\", "/")).name if name == "source" else path.name
        # PCB uploads are normalized to PNG; downloads must match their actual format.
        if Path(filename).suffix.lower() != path.suffix.lower():
            filename = Path(filename).stem + path.suffix.lower()
        files.append({"name": name, "label": label, "filename": filename, "media_type": media_type,
                      "size": path.stat().st_size, "path": path})
    return files


def public_attachments(files: list[dict], base: str) -> dict:
    return {"items": [{**{key: value for key, value in item.items() if key != "path"},
                       "url": f"{base}/{item['name']}"} for item in files]}


def attachment_response(files: list[dict], name: str):
    item = next((item for item in files if item["name"] == name), None)
    if not item:
        raise HTTPException(status_code=404, detail="原始附件文件不存在")
    return FileResponse(item["path"], media_type=item["media_type"], filename=item["filename"],
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
