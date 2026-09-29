#!/usr/bin/env python3
"""验证 STEP API、PostgreSQL state/checkpoint 与下载；人工问题默认保留暂停。

    python -m backend.api.v1.verify_step_e2e --image sample/picture/TR-00002.png
    python -m backend.api.v1.verify_step_e2e --drawing-id <UUID> --interactive

退出码：0=生成及持久化验证通过，1=失败/拒绝/停止，2=等待实际使用者输入。
生成记录和文件保留，供事后检查。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sys
import time
import uuid

import httpx


SUCCESS_STATUSES = {"completed", "reviewed"}
PAUSED_STATUSES = {"awaiting_input", "pending_review"}
STOPPED_STATUSES = {"failed", "rejected", "stopped", "cancelled"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image", help="工程图路径，未指定时使用 STEP_IMAGE_PATH")
    source.add_argument("--drawing-id", type=uuid.UUID, help="继续检查/回答既有任务，不重复上传")
    parser.add_argument("--base-url", default=os.getenv("STEP_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--username", default=os.getenv("STEP_USERNAME"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("STEP_POLL_TIMEOUT", "900")))
    parser.add_argument("--interval", type=float, default=float(os.getenv("STEP_POLL_INTERVAL", "3")))
    parser.add_argument("--interactive", action="store_true", help="在终端用自然语言回答任务中的问题")
    args = parser.parse_args(argv)
    if not args.drawing_id and not args.image:
        args.image = os.getenv("STEP_IMAGE_PATH")
    if args.timeout <= 0 or args.interval <= 0:
        parser.error("--timeout 和 --interval 必须大于 0")
    return args


async def verify_database_records(drawing_id: str) -> tuple[int, dict]:
    """仅查询本任务的业务记录与 checkpoint 数量。"""
    from sqlalchemy import text
    from backend.dependencies import AsyncSessionLocal

    thread_id = f"step:image:{uuid.UUID(drawing_id)}"
    async with AsyncSessionLocal() as session:
        checkpoint_result = await session.execute(
            text("SELECT COUNT(*) FROM checkpoints WHERE thread_id = :thread_id AND checkpoint_ns = ''"),
            {"thread_id": thread_id},
        )
        checkpoint_count = int(checkpoint_result.scalar_one())
        job_result = await session.execute(
            text("""
                SELECT status, source_image_path, output_path, preview_path,
                       needs_review, package_params
                FROM step_drawings WHERE id = CAST(:id AS uuid)
            """),
            {"id": drawing_id},
        )
        row = job_result.mappings().fetchone()
        if row is None:
            raise RuntimeError(f"数据库中找不到 step_drawings 记录：{drawing_id}")
        return checkpoint_count, dict(row)


def check_persisted_state(job: dict, db_job: dict, snapshot: dict, checkpoint_count: int) -> None:
    """核对同一任务的 API、业务表和实际 LangGraph 快照。"""
    if checkpoint_count < 1 or not snapshot.get("checkpoint_id"):
        raise RuntimeError("PostgreSQL 没有对应 thread_id 的 checkpoint。")
    if db_job["status"] != job["status"]:
        raise RuntimeError(f"数据库/API 状态不一致：{db_job['status']} / {job['status']}")
    values = snapshot.get("values") or {}
    if not values or values.get("image_path") != db_job["source_image_path"]:
        raise RuntimeError("checkpoint state 的 image_path 与业务记录不一致。")
    if job.get("checkpoint_id") and job["checkpoint_id"] != snapshot["checkpoint_id"]:
        raise RuntimeError("API 和数据库回读的 checkpoint_id 不一致。")
    db_params = db_job.get("package_params") or {}
    if db_params.get("human_history", []) != values.get("human_history", []):
        raise RuntimeError("业务表与 checkpoint state 的人工操作历史不一致。")
    pending = job.get("pending_input")
    interrupts = snapshot.get("interrupts") or []
    if job["status"] in PAUSED_STATUSES:
        if not pending or not pending.get("interrupt_id"):
            raise RuntimeError("任务暂停，但 API 未返回真实 pending_input/interrupt_id。")
        db_pending = db_params.get("pending_input") or {}
        if db_pending.get("interrupt_id") != pending["interrupt_id"]:
            raise RuntimeError("业务表和 API 的待答 interrupt_id 不一致。")
        matched = [item for item in interrupts if item["id"] == pending["interrupt_id"]]
        if not matched or not snapshot.get("next_nodes"):
            raise RuntimeError("API 显示暂停，但 checkpoint 中无对应待答 interrupt/待执行节点。")
        if matched[0]["value"].get("stage") != pending.get("stage"):
            raise RuntimeError("checkpoint 与 API 的人工阶段不一致。")
        expected_stages = (
            {"review"}
            if job["status"] == "pending_review"
            else {"package", "routing", "dimensions"}
        )
        if pending.get("stage") not in expected_stages:
            raise RuntimeError("待答阶段与 API 暂停状态不一致。")
        needs_review = job["status"] == "pending_review"
        if bool(db_job.get("needs_review")) != needs_review or bool(job.get("needs_review")) != needs_review:
            raise RuntimeError("暂停阶段与 needs_review 标记不一致。")
    elif job["status"] in SUCCESS_STATUSES:
        if pending or interrupts or snapshot.get("next_nodes"):
            raise RuntimeError("API 显示完成，但 checkpoint 仍有待处理节点或人工问题。")
        if values.get("status") != job["status"]:
            raise RuntimeError("checkpoint state 与 API 的最终状态不一致。")
        if db_job.get("needs_review") or job.get("needs_review"):
            raise RuntimeError("任务显示完成但仍标记需要人工审核。")

    if job["status"] in SUCCESS_STATUSES | {"pending_review"}:
        if values.get("verification", {}).get("passed") is not True:
            raise RuntimeError("候选产物缺少 checkpoint 中通过的几何验证结果。")
        result = values.get("result") or {}
        step_path = result.get("step_file") or (values.get("artifact_paths") or {}).get("step")
        previews = result.get("previews") or values.get("preview_paths") or {}
        preview_path = previews.get("isometric") or next(iter(previews.values()), None)
        if step_path != db_job.get("output_path") or preview_path != db_job.get("preview_path"):
            raise RuntimeError("checkpoint state 的产物路径与业务表不一致。")
        db_previews = (
            db_params.get("preview_paths")
            or (db_params.get("pending_input") or {}).get("previews")
            or (db_params.get("golden_comparison") or {}).get("previews")
            or {}
        )
        for view in ("isometric", "front", "top", "right"):
            if not previews.get(view) or previews[view] != db_previews.get(view):
                raise RuntimeError(f"checkpoint state 与业务表的 {view} 视图路径不一致或缺失。")


async def verify_persisted_job(drawing_id: str, job: dict) -> tuple[int, dict]:
    from backend.agents.step.persistence import get_postgres_step_snapshot

    checkpoint_count, db_job = await verify_database_records(drawing_id)
    snapshot = await get_postgres_step_snapshot("image", drawing_id)
    check_persisted_state(job, db_job, snapshot, checkpoint_count)
    print(f"数据库核验通过：checkpoints={checkpoint_count}, checkpoint_id={snapshot['checkpoint_id']}")
    print(f"state.status={snapshot['values'].get('status')}, next_nodes={snapshot['next_nodes']}")
    return checkpoint_count, db_job


def prompt_answer(pending: dict) -> dict | None:
    """Prompt in ordinary language and adapt the answer to the internal API shape."""
    stage = pending.get("stage", "unknown")
    print(f"\n需要你补充信息：{pending.get('question') or stage}")
    fields = pending.get("fields") or {}
    if stage == "dimensions":
        print("请直接填写参数名和数值，例如：housing_height=1.2 mm。多个参数用分号隔开。")
        for name, spec in fields.items():
            print(f"  - {spec.get('description', name)} ({name}, {spec.get('unit', 'mm')})")
    elif stage == "package":
        print("输入封装名称（如 SOP8），或输入“自动识别”；空行保留暂停。")
    elif stage == "routing":
        candidates = [item.get("family_id") for item in pending.get("candidates", []) if item.get("implemented")]
        if candidates:
            print("可改选已实现模板：" + "、".join(candidates))
        print("输入“确认”采用建议，输入模板编号改选，或输入“取消”；空行保留暂停。")
    elif stage == "review":
        print("输入“批准”完成审核，或输入“拒绝”；空行保留暂停。")
    while True:
        try:
            raw = input("你的回答：").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        normalized = raw.strip().casefold()
        if normalized in {"取消", "取消任务", "停止", "cancel", "退出"}:
            return {"action": "cancel"}
        if stage == "package":
            if normalized in {"自动", "自动识别", "auto", "不确定"}:
                return {"action": "auto"}
            return {"action": "provide", "package_type": raw}
        if stage == "dimensions":
            return {"text": raw}
        if stage == "routing":
            if normalized in {"确认", "使用建议", "confirm", "yes"}:
                if "confirm" in pending.get("options", []):
                    return {"action": "confirm"}
                print("当前没有可确认的建议模板，请改选一个已实现的模板或取消。")
                continue
            candidates = {
                item.get("family_id") for item in pending.get("candidates", [])
                if item.get("implemented")
            }
            if raw in candidates:
                return {"action": "change", "family_id": raw}
            print("请输入“确认”、一个已实现的模板编号，或“取消”。")
            continue
        if stage == "review":
            if normalized in {"批准", "通过", "approve", "yes"}:
                return {"action": "approve"}
            if normalized in {"拒绝", "不通过", "reject", "no"}:
                return {"action": "reject"}
            print("请输入“批准”或“拒绝”。")
            continue
        print(f"暂不支持的人工问题阶段：{stage}")
        return None


def require_response(response: httpx.Response, expected: int, action: str) -> dict:
    if response.status_code != expected:
        raise RuntimeError(f"{action}失败 ({response.status_code}): {response.text[:2000]}")
    return response.json()


def verify_artifact_downloads(client: httpx.Client, headers: dict, job: dict, db_job: dict) -> None:
    """Verify the candidate STEP and every review view without approving it."""
    params = db_job.get("package_params") or {}
    if isinstance(params, str):
        params = json.loads(params)
    previews = (
        params.get("preview_paths")
        or (params.get("pending_input") or {}).get("previews")
        or (params.get("golden_comparison") or {}).get("previews")
        or {}
    )
    preview_urls = job.get("preview_urls") or {}
    downloads = [(job.get("step_file_url"), db_job.get("output_path"), "STEP")]
    for view in ("isometric", "front", "top", "right"):
        downloads.append((preview_urls.get(view), previews.get(view), f"{view} 预览图"))
    for url, stored_path, label in downloads:
        if not url or not stored_path:
            raise RuntimeError(f"候选结果缺少 {label} 的下载地址或数据库文件记录。")
        download = client.get(url, headers=headers)
        if download.status_code != 200 or not download.content:
            raise RuntimeError(f"{label} 下载失败 ({download.status_code})。")
        local_path = Path(stored_path)
        if not local_path.is_file():
            raise RuntimeError(f"数据库引用的 {label} 文件在本机不存在；该脚本应在后端所在机器运行。")
        if hashlib.sha256(download.content).digest() != hashlib.sha256(local_path.read_bytes()).digest():
            raise RuntimeError(f"{label} 下载内容与数据库引用文件的 SHA-256 不一致。")
        print(f"{label} 下载及 SHA-256 核验通过：{url}")


def run(args: argparse.Namespace) -> int:
    # psycopg AsyncConnection cannot use Windows' default ProactorEventLoop.
    # Configure this before Runner creates the loop used for checkpoint reads.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    image_path = None
    if not args.drawing_id:
        if not args.image:
            raise RuntimeError("请使用 --image / STEP_IMAGE_PATH，或 --drawing-id 继续既有任务。")
        image_path = Path(args.image).expanduser().resolve()
        if not image_path.is_file():
            raise RuntimeError(f"图片不存在：{image_path}")
    username = args.username or input("STEP API 用户名: ").strip()
    password = os.getenv("STEP_PASSWORD") or getpass.getpass("STEP API 密码: ")
    if not username or not password:
        raise RuntimeError("用户名和密码不能为空。")

    # 多次暂停检查使用同一事件循环，避免 SQLAlchemy 连接池跨 loop 复用。
    with asyncio.Runner() as runner, httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        login = require_response(client.post("/api/v1/auth/login", json={"username": username, "password": password}), 200, "登录")
        if not login.get("access_token"):
            raise RuntimeError("登录响应中没有 access_token。")
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        if args.drawing_id:
            drawing_id = str(args.drawing_id)
        else:
            content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
            with image_path.open("rb") as image_file:
                submitted = require_response(client.post("/api/v1/step/drawings", headers=headers, files={"file": (image_path.name, image_file, content_type)}), 202, "提交任务")
            drawing_id = submitted["drawing_id"]
        status_url = f"/api/v1/step/drawings/{drawing_id}"
        print(f"drawing_id={drawing_id}")
        deadline = time.monotonic() + args.timeout
        last_status = None
        job = {}
        while time.monotonic() < deadline:
            job = require_response(client.get(status_url, headers=headers), 200, "查询任务")
            current_status = job["status"]
            if current_status != last_status:
                print(f"状态：{current_status}")
                last_status = current_status
            if current_status in PAUSED_STATUSES:
                _, db_job = runner.run(verify_persisted_job(drawing_id, job))
                if current_status == "pending_review":
                    verify_artifact_downloads(client, headers, job, db_job)
                pending = job["pending_input"]
                if not args.interactive:
                    print(json.dumps(pending, ensure_ascii=False, indent=2))
                    print(f"BLOCKED：等待人工输入，退出码 2。继续：--drawing-id {drawing_id} --interactive")
                    return 2
                asked_at = time.monotonic()
                answer = prompt_answer(pending)
                deadline += time.monotonic() - asked_at
                if answer is None:
                    print(f"BLOCKED：保留暂停，退出码 2；drawing_id={drawing_id}")
                    return 2
                response = client.post(f"{status_url}/human-input", headers=headers, json={"interrupt_id": pending["interrupt_id"], "answer": answer})
                if response.status_code == 422:
                    print(f"回答未接受，请按问题要求重新输入：{response.text[:2000]}")
                    continue
                require_response(response, 202, "提交人工回答")
            elif current_status == "failed":
                print(f"执行失败：{job.get('error_msg') or '未提供错误详情'}")
                if args.interactive:
                    try:
                        retry = input("是否在原任务上重试？输入 y 重试，其他输入结束：").strip().casefold()
                    except (EOFError, KeyboardInterrupt):
                        retry = ""
                    if retry in {"y", "yes", "是", "重试"}:
                        retried = client.post(f"{status_url}/retry", headers=headers)
                        require_response(retried, 202, "重试任务")
                        print("已提交重试，继续轮询同一 drawing_id。")
                        last_status = None
                        continue
                return 1
            elif current_status in STOPPED_STATUSES or current_status.startswith("stopped_"):
                print(f"任务未完成生成：{current_status}；{job.get('error_msg') or ''}")
                return 1
            elif current_status in SUCCESS_STATUSES:
                break
            time.sleep(args.interval)
        else:
            raise RuntimeError(f"等待超时（{args.timeout:.0f}s）；status={job.get('status')}；drawing_id={drawing_id}")

        checkpoint_count, db_job = runner.run(verify_persisted_job(drawing_id, job))
        verify_artifact_downloads(client, headers, job, db_job)
        print(f"STEP Agent E2E 验证通过：drawing_id={drawing_id}，checkpoints={checkpoint_count}")
        return 0


def main() -> int:
    try:
        return run(parse_args())
    except (RuntimeError, httpx.HTTPError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
