"""API - Web UI / REST API 包..

提供基于 FastAPI 的 Web 仪表盘和 REST API,
用于实时监控碰撞引擎的运行状态,查询碰撞结果.

模块:
    server   - FastAPI 应用工厂 + uvicorn 入口
    routes   - REST 路由 + WebSocket 端点
"""

from .routes import router
from .server import create_app

__all__ = [
    "create_app",
    "router",
]
