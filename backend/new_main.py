import uvicorn
from fastapi import FastAPI
# from backend.api.v1 import resume
from backend.api.v1 import qa
from backend.api.v1 import auth
from backend.api.v1 import pcb
from backend.api.v1 import symbol
from backend.mcp.knowledge_base_server import mcp as kb_mcp
from backend.mcp.web_search_server import mcp as ws_mcp
from contextlib import asynccontextmanager

# mcp 2.x：stateless_http / json_response 从 MCPServer 构造参数挪到了 streamable_http_app()
_kb_app = kb_mcp.streamable_http_app(stateless_http=True, json_response=True)
_ws_app = ws_mcp.streamable_http_app(stateless_http=True, json_response=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # app.mount() 不会自动传播子应用 lifespan，必须在这里手动启动
    # 2.x 中 session_manager 由 streamable_http_app() 懒创建，随子应用 lifespan 一起初始化
    async with _ws_app.router.lifespan_context(_ws_app):
        async with _kb_app.router.lifespan_context(_kb_app):
            yield
app = FastAPI(lifespan=lifespan)
app.include_router(auth.router,   prefix="/api/v1/auth")
# app.include_router(qa.router,   prefix="/api/v1/qa")
app.include_router(exam.router, prefix="/api/v1/exam")
app.include_router(interview.router, prefix="/api/v1/interview")
# app.include_router(resume.router,   prefix="/api/v1/resume")
app.mount("/mcp/kb",     _kb_app)
app.mount("/mcp/web-search", _ws_app)


if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=8006)

