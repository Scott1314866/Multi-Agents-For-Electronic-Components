# backend/api/router.py
# API 路由总入口

from fastapi import APIRouter
from backend.api.v1 import auth, qa, pcb, step, symbol, unified_chat  # 六个子模块

api_router = APIRouter()                              # 总路由

# 把每个子 router 带前缀 + 标签聚合进来
api_router.include_router(auth.router,          prefix="/auth",      tags=["认证"])
api_router.include_router(unified_chat.router,  prefix="/chat",      tags=["AI助手"])   # 本章统一入口
api_router.include_router(qa.router,            prefix="/qa",        tags=["智能问答"])  # 第 5 章
api_router.include_router(pcb.router,          prefix="/pcb",      tags=["试卷批改"])  # 第 6 章
api_router.include_router(step.router,        prefix="/step",    tags=["简历审查"])  # 第 4 章
api_router.include_router(symbol.router,     prefix="/symbol", tags=["模拟面试"])  # 第 7 章