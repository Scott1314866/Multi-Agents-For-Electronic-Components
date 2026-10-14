"""统一后端入口：python -s -m backend.main。

提供认证、STEP、PCB、Symbol 和工作台 API，负责迁移及任务恢复。
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

from backend.api.v1 import auth, pcb, step, symbol, workbench
from backend.db.migrations import run_migrations
from backend.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 迁移失败时阻止启动，避免接收不能保存暂停状态的任务。
    await run_migrations()
    # Durable answers and graph checkpoints are recovered under a per-job PG
    # lock; another live worker's job is skipped rather than executed twice.
    await step.recover_step_jobs()
    await pcb.recover_pcb_jobs()
    await symbol.recover_symbol_jobs()
    yield
    # 正常关闭时等待已接收的任务保存暂停点或执行结果。
    pending = tuple(step._background_tasks | pcb._background_tasks | symbol._background_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


app = FastAPI(title="Component Foundry 工作台", version="1.0.0-pilot.1", lifespan=lifespan)
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
app.include_router(pcb.router, prefix="/api/v1/pcb", tags=["PCB 封装生成"])
app.include_router(symbol.router, prefix="/api/v1/symbol", tags=["OrCAD 符号生成"])
app.include_router(workbench.router, prefix="/api/v1/workbench", tags=["统一工作台"])


@app.get("/", include_in_schema=False)
async def home():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/v1/workbench/ui")


@app.get("/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "mode": "step", "version": app.version}


def run_server() -> None:
    """启动统一应用，Windows 显式使用 Selector 循环。"""
    import uvicorn

    settings = get_settings()
    config = uvicorn.Config(app, host=settings.app_host, port=settings.app_port, loop="asyncio")
    # 新版 Uvicorn 的 loop_factory 会覆盖 Windows 事件循环策略。
    # 显式创建 Selector 循环并运行 serve，确保 psycopg 能使用异步连接。
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(uvicorn.Server(config).serve())
    else:
        uvicorn.Server(config).run()


if __name__ == "__main__":
    run_server()
