# backend/main.py
# uvicorn backend.main:app --reload --port 8000
import os
import sys

# Windows conda 专用：把 base 的 Library/bin 加入 DLL 搜索路径，
# 让 _lzma.pyd 能找到 liblzma.dll（非 Windows 跳过，不影响 Linux/Mac）
if sys.platform == "win32":
    _conda_base_lib_bin = os.path.normpath(
        os.path.join(os.path.dirname(sys.executable), "..", "..", "Library", "bin")
    )
    if os.path.isdir(_conda_base_lib_bin):
        os.add_dll_directory(_conda_base_lib_bin)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings                          # 配置（第 3 章）
from backend.core.logger import configure_logging, get_logger    # 日志（3.3）
from backend.api.router import api_router                        # 8.6.1 聚合的总路由

settings = get_settings()

# MCP Sub-Apps 在 lifespan 定义前创建，
# 这样 lifespan 与下面的 mount 共享同一实例
from backend.mcp.knowledge_base_server import mcp as kb_mcp      # 知识库 MCP（5.11）
from backend.mcp.web_search_server import mcp as ws_mcp          # 联网搜索 MCP（5.11）

# mcp 2.x：stateless_http / json_response 从 MCPServer 构造参数挪到了 streamable_http_app()
_kb_app = kb_mcp.streamable_http_app(stateless_http=True, json_response=True)   # 把 MCP 变成 ASGI 子应用
_ws_app = ws_mcp.streamable_http_app(stateless_http=True, json_response=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()                                  # 初始化结构化日志（3.3）
    logger = get_logger(__name__)
    logger.info("app.starting | env=%s port=%s", settings.app_env, settings.app_port)

    # ① DB Schema 自动迁移（幂等，每次启动执行）
    try:
        from backend.db.migrations import run_migrations
        await run_migrations()
    except Exception as e:
        logger.warning("app.migrations_failed | error=%s", e)   # 迁移失败只告警，不拦启动

    # ② 并行预热三个本地模型（首次加载慢，提前热好，避免首个请求卡顿）
    import asyncio
    try:
        from backend.core.reranker import BGEReranker            # 重排序模型（5.8）
        from backend.core.query_classifier import QueryClassifier # 意图分类器（5.9）
        from backend.core.knowledge_base import BGEMEmbedder      # BGE-M3 嵌入（5.4）

        loop = asyncio.get_running_loop()
        await asyncio.gather(                                    # 三个模型并行加载（各跑在线程池里）
            loop.run_in_executor(None, BGEReranker.get_instance),
            loop.run_in_executor(None, QueryClassifier.get_instance),
            loop.run_in_executor(None, BGEMEmbedder.get_instance),
        )
        logger.info("app.local_models_warmed_up")
    except Exception as e:
        logger.warning("app.local_models_warmup_failed | error=%s", e)  # 预热失败也不拦启动

    # ③ 驱动 MCP Server 的 lifespan（mount 不会自动调用子应用的 lifespan，需手动嵌套进入）
    async with _kb_app.router.lifespan_context(_kb_app):
        async with _ws_app.router.lifespan_context(_ws_app):
            logger.info("app.started")
            yield                                               # ← 应用运行期间停在这里

            # ── 关闭时执行 ──
            logger.info("app.shutting_down")
            from backend.core.llm_factory import LLMFactory      # LLM 工厂（3.4）
            LLMFactory.clear_cache()                            # 清缓存
            logger.info("app.shutdown_complete")

app = FastAPI(
    title="Agent API",
    description="AI 工程制图 API",
    version="1.0.0",
    docs_url="/docs",                                    # Swagger 文档
    redoc_url="/redoc",                                  # ReDoc 文档
    lifespan=lifespan,                                   # 挂上面的生命周期钩子
)

# CORS：允许前端开发端口跨域访问（3000/5173/8080）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://localhost:5173", "http://localhost:8080",
        "http://127.0.0.1:3000", "http://127.0.0.1:5173", "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")        # 挂总路由（8.6.1），所有业务接口在 /api/v1 下

app.mount("/mcp/kb",         _kb_app)                   # 挂知识库 MCP 子应用（5.11）
app.mount("/mcp/web-search", _ws_app)                   # 挂联网搜索 MCP 子应用（5.11）


@app.get("/health", tags=["系统"])                       # 健康检查（运维探活用）
async def health_check():
    return {"status": "ok", "env": settings.app_env}