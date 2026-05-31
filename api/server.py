"""FastAPI 应用工厂 + uvicorn 入口。

导出 create_app() 工厂函数，便于测试和灵活配置。
作为独立进程运行时连接同一份 collision_results.db。

使用方式:
    # 直接运行
    python -m api.server

    # 或嵌入到其他进程
    from api import create_app
    app = create_app()
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

# 将项目根加入 sys.path（与 collision_engine.py 一致）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── 日志 ──────────────────────────────────────────────────────
logger = logging.getLogger("api.server")

# ── 共享状态（托管在 state.py 中消除交叉导入） ──────────────
from . import state as _state  # noqa: E402
from .metrics import get_registry  # noqa: E402


# ── 应用工厂 ────────────────────────────────────────────────
def create_app(enable_grpc_master: bool = False, grpc_port: int = 50051) -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""

    # ── 生命周期事件 ──────────────────────────────────────────
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """应用生命周期管理（替代已弃用的 on_event）。"""
        # startup
        logger.info("API 服务启动中...")
        target_info = _state.load_target_sets()
        logger.info(
            "目标集: Hash160=%s(%d), XOnly=%s(%d)",
            "✓" if target_info["hash160_loaded"] else "✗",
            target_info["hash160_count"],
            "✓" if target_info["xonly_loaded"] else "✗",
            target_info["xonly_count"],
        )

        # 可选启动 gRPC Master
        if enable_grpc_master:
            try:
                from concurrent import futures
                import grpc
                from distributed.master import MasterService, WorkerRegistry
                from distributed.protocol_pb2_grpc import (
                    add_MasterServiceServicer_to_server,
                )
                from .routes import _set_worker_registry

                _state._worker_registry = WorkerRegistry()
                _set_worker_registry(_state._worker_registry)

                _state._grpc_server = grpc.server(
                    futures.ThreadPoolExecutor(max_workers=10)
                )
                add_MasterServiceServicer_to_server(
                    MasterService(_state._worker_registry), _state._grpc_server
                )
                _state._grpc_server.add_insecure_port(f"[::]:{grpc_port}")
                _state._grpc_server.start()
                logger.info("gRPC Master 服务已启动 (port=%d)", grpc_port)
            except Exception as exc:
                logger.warning("gRPC Master 启动失败: %s", exc)

        yield

        # shutdown
        if _state._grpc_server is not None:
            try:
                _state._grpc_server.stop(grace=5)
                logger.info("gRPC 服务已停止")
            except Exception as exc:
                logger.warning("gRPC 服务停止异常: %s", exc)
        if _state._db is not None:
            _state._db.close()
        if _state._hash160_set is not None:
            _state._hash160_set.close()
        if _state._xonly_set is not None:
            _state._xonly_set.close()
        logger.info("API 服务关闭")

    app = FastAPI(
        title="Bitcoin Collision Engine Dashboard",
        version="1.0.0",
        description="私钥碰撞检测系统实时监控仪表盘",
        lifespan=lifespan,
    )

    # ── 挂载子路由 ────────────────────────────────────────────
    from .routes import router

    app.include_router(router)

    # ── Prometheus /metrics 端点（可选认证）─────────────────
    _ENGINE_API_KEY = os.environ.get("ENGINE_API_KEY", "")

    def _require_metrics_auth(request: Request) -> None:
        """如果设置了 ENGINE_API_KEY，则验证 X-API-Key 请求头。"""
        if _ENGINE_API_KEY:
            key = request.headers.get("X-API-Key", "")
            if key != _ENGINE_API_KEY:
                raise HTTPException(status_code=403, detail="Forbidden")

    @app.get("/metrics")
    async def metrics_endpoint(
        _: None = Depends(_require_metrics_auth),
    ) -> PlainTextResponse:
        """Prometheus /metrics 端点 (text/plain 格式, 零依赖)。"""

        registry = get_registry()
        return PlainTextResponse(
            registry.render(),
            media_type="text/plain; version=0.0.4",
        )

    # ── 静态文件 ──────────────────────────────────────────────
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


# ── 直接运行入口 ────────────────────────────────────────────
def main() -> None:
    """启动 uvicorn 服务器（可选同时启动 gRPC Master）。"""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="API Dashboard 服务器")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="HTTP 端口")
    parser.add_argument(
        "--with-grpc", action="store_true", help="同时启动 gRPC Master 服务"
    )
    parser.add_argument("--grpc-port", type=int, default=50051, help="gRPC 端口")
    args = parser.parse_args()

    # 配置日志格式
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    host = args.host
    port = args.port

    print("╔═══════════════════════════════════════════════════╗")
    print("║  Bitcoin Collision Engine Dashboard               ║")
    print(f"║  监听地址: http://{host}:{port}                   ║")
    print(f"║  REST API: http://{host}:{port}/api/              ║")
    print(f"║  WebSocket: ws://{host}:{port}/ws                 ║")
    if args.with_grpc:
        print(f"║  gRPC Master: :{args.grpc_port}                   ║")
    print("╚═══════════════════════════════════════════════════╝")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    app = create_app(enable_grpc_master=True)
    main()
