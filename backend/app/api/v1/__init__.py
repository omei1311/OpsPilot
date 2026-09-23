"""API v1 路由汇总。

所有 v1 的子路由在这里挂载，main.py 只需要 include 一次 api_router，
并统一加上 API_V1_PREFIX。

后续阶段按架构文档 §7 依次补充：
    api_router.include_router(auth.router)        # 阶段 2 用户权限
    api_router.include_router(users.router)
    api_router.include_router(tickets.router)     # 阶段 3 工单中心
    api_router.include_router(dashboard.router)
    api_router.include_router(agent.router)       # 阶段 5 Agent
"""

from fastapi import APIRouter

from app.api.v1 import agent, auth, dashboard, health, tickets

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(tickets.router)
api_router.include_router(dashboard.router)
api_router.include_router(agent.router)

__all__ = ["api_router"]
