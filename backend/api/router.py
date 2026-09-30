"""当前已实现的路由聚合入口。

这里列出的每一项都必须**真的实现了** —— 仓库历史上曾把 exam / interview /
qa / resume 等未落地模块的路由一并挂上，启动即 ImportError。
`tests/test_step_entrypoints.py` 会核对本目录与主应用的路径集合完全一致。
"""

from fastapi import APIRouter

from backend.api.v1 import auth, pcb, step, symbol

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(step.router, prefix="/step", tags=["STEP 数模生成"])
api_router.include_router(pcb.router, prefix="/pcb", tags=["PCB 封装生成"])
api_router.include_router(symbol.router, prefix="/symbol", tags=["OrCAD 符号生成"])
