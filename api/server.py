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

import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import jinja2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

# 将项目根加入 sys.path（与 collision_engine.py 一致）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.metrics import get_registry  # noqa: E402
from core.database import ResultDB  # noqa: E402

# ── 日志 ──────────────────────────────────────────────────────
logger = logging.getLogger("api.server")


# ── 全局共享状态 ──────────────────────────────────────────────
class EngineStatus:
    """引擎运行时状态（跨进程共享 via JSON 文件）。"""

    STATUS_FILE = PROJECT_ROOT / "collision_engine_status.json"

    def __init__(self) -> None:
        self._last_read: float = 0.0
        self._cache_timeout: float = 1.0  # 缓存 1 秒
        self._cached: dict[str, Any] = {}
        self._cached_ok: bool = False

    def read(self) -> dict[str, Any]:
        """从状态文件读取引擎当前运行状态。"""
        now = time.monotonic()
        if self._cached_ok and (now - self._last_read) < self._cache_timeout:
            return self._cached

        self._last_read = now
        try:
            data = json.loads(self.STATUS_FILE.read_text(encoding="utf-8"))
            self._cached = data
            self._cached_ok = True
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._cached = {
                "running": False,
                "mode": "unknown",
                "keys_per_second": 0.0,
                "total_keys": 0,
                "elapsed_seconds": 0.0,
                "error": "引擎未运行或状态文件不可用",
            }
            self._cached_ok = True
        return self._cached

    def write(self, data: dict[str, Any]) -> None:
        """写入引擎状态（由引擎进程调用）。"""
        try:
            self.STATUS_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写入状态文件失败: %s", exc)


# 单例
_engine_status = EngineStatus()


def get_engine_status() -> dict[str, Any]:
    return _engine_status.read()


# ── Hash160 目标集加载 ────────────────────────────────────────
_hash160_set: Any = None
_xonly_set: Any = None


def load_target_sets() -> dict[str, Any]:
    """尝试加载 Hash160 和 x-only 目标集，返回描述信息。"""
    global _hash160_set, _xonly_set
    result: dict[str, Any] = {
        "hash160_loaded": False,
        "hash160_count": 0,
        "xonly_loaded": False,
        "xonly_count": 0,
    }

    try:
        from collision_target import Hash160Set, XOnlySet
    except ImportError:
        return result

    # 加载 Hash160
    try:
        _hash160_set = Hash160Set()
        _hash160_set.load(quiet=True)
        result["hash160_loaded"] = True
        result["hash160_count"] = len(_hash160_set)
    except (FileNotFoundError, Exception) as exc:
        logger.info("Hash160Set 未加载: %s", exc)

    # 加载 x-only
    try:
        _xonly_set = XOnlySet()
        _xonly_set.load(quiet=True)
        result["xonly_loaded"] = True
        result["xonly_count"] = len(_xonly_set)
    except (FileNotFoundError, Exception) as exc:
        logger.info("XOnlySet 未加载: %s", exc)

    return result


# ── WebSocket 连接管理 ───────────────────────────────────────
_websocket_clients: set[WebSocket] = set()


async def broadcast_stats(stats: dict[str, Any]) -> None:
    """广播统计信息到所有已连接的 WebSocket 客户端。"""
    dead: list[WebSocket] = []
    for ws in _websocket_clients:
        try:
            await ws.send_json(stats)
        except WebSocketDisconnect:
            dead.append(ws)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _websocket_clients.discard(ws)

    # ── 更新 Prometheus 指标 ──
    registry = get_registry()
    registry.gauge("keys_per_second", stats.get("keys_per_second", 0.0))
    registry.gauge("total_keys_scanned", float(stats.get("total_keys", 0)))
    registry.gauge("collision_count", float(stats.get("total_collisions", 0)))
    elapsed = stats.get("elapsed_seconds", 0.0)
    registry.gauge("uptime_seconds", elapsed)


# ── Jinja2 模板 ─────────────────────────────────────────────
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)

# ── 数据库 ──────────────────────────────────────────────────
_db: Optional[ResultDB] = None


def get_db() -> ResultDB:
    global _db
    if _db is None:
        _db = ResultDB(str(PROJECT_ROOT / "collision_results.db"))
    return _db


# ── 应用工厂 ────────────────────────────────────────────────
def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""

    # ── 生命周期事件 ──────────────────────────────────────────
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """应用生命周期管理（替代已弃用的 on_event）。"""
        # startup
        logger.info("API 服务启动中...")
        target_info = load_target_sets()
        logger.info(
            "目标集: Hash160=%s(%d), XOnly=%s(%d)",
            "✓" if target_info["hash160_loaded"] else "✗",
            target_info["hash160_count"],
            "✓" if target_info["xonly_loaded"] else "✗",
            target_info["xonly_count"],
        )

        yield

        # shutdown
        global _db
        if _db is not None:
            _db.close()
        if _hash160_set is not None:
            _hash160_set.close()
        if _xonly_set is not None:
            _xonly_set.close()
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

    # ── Prometheus /metrics 端点 ─────────────────────────────
    @app.get("/metrics")
    async def metrics_endpoint() -> PlainTextResponse:
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
    """启动 uvicorn 服务器。"""
    import uvicorn

    # 配置日志格式
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    host = "127.0.0.1"
    port = 8080

    print("╔══════════════════════════════════════════╗")
    print("║  Bitcoin Collision Engine Dashboard      ║")
    print(f"║  监听地址: http://{host}:{port}          ║")
    print(f"║  REST API: http://{host}:{port}/api/     ║")
    print(f"║  WebSocket: ws://{host}:{port}/ws        ║")
    print("╚══════════════════════════════════════════╝")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    app = create_app()
    main()
