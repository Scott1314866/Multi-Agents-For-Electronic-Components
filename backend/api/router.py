"""当前已实现的认证与 STEP API 路由。"""

from fastapi import APIRouter

from backend.api.v1 import auth, step


api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(step.router, prefix="/step", tags=["STEP 数模生成"])
