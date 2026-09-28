"""认证与 STEP 的独立 API 入口：python -m backend.step_main。

用于仅运行 STEP 功能的部署，不要求 QA、PCB 等其他 Agent 已安装。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import sys

if sys.platform == "win32":
    # psycopg 异步连接在 Windows 上需要 Selector 事件循环。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.v1 import auth, step
from backend.db.migrations import run_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 迁移失败时阻止启动，避免接收不能保存暂停状态的任务。
    await run_migrations()
    # Durable answers and graph checkpoints are recovered under a per-job PG
    # lock; another live worker's job is skipped rather than executed twice.
    await step.recover_step_jobs()
    yield
    # 正常关闭时等待已接收的任务保存暂停点或执行结果。
    pending = tuple(step._background_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


app = FastAPI(title="STEP Agent API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://{host}:{port}"
        for host in ("localhost", "127.0.0.1")
        for port in (3000, 5173, 8080)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router, prefix="/api/v1/auth", tags=["认证"])
app.include_router(step.router, prefix="/api/v1/step", tags=["STEP 数模生成"])


@app.get("/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "mode": "step"}


def run_server() -> None:
    """所有模块入口共用此启动函数，Windows 显式使用 Selector 循环。"""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=8000, loop="asyncio")
    # 新版 Uvicorn 的 loop_factory 会覆盖 Windows 事件循环策略。
    # 显式创建 Selector 循环并运行 serve，确保 psycopg 能使用异步连接。
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(uvicorn.Server(config).serve())
    else:
        uvicorn.Server(config).run()


if __name__ == "__main__":
    run_server()
