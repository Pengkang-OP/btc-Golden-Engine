"""API 共享状态 — 分离自 server.py，消除与 routes.py 的交叉导入。

所有全局单例、getter 函数、WebSocket 管理、Jinja 模板环境集中于此。
server.py 和 routes.py 均从此模块导入所需符号。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional

import jinja2
from fastapi import WebSocket, WebSocketDisconnect

# 将项目根加入 sys.path（与 collision_engine.py 一致）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.database import ResultDB  # noqa: E402

logger = logging.getLogger("api.state")


# ── 引擎运行状态 ──────────────────────────────────────────────


class EngineStatus:
    """引擎运行时状态（跨进程共享 via JSON 文件）。"""

    STATUS_FILE = PROJECT_ROOT / "collision_engine_status.json"

    def __init__(self) -> None:
        """初始化引擎状态缓存，设置缓存超时时间。"""
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

    def write(self, data: dict[str, Any]) -> None:  # pragma: no cover
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
    """返回引擎当前运行状态（含缓存）。"""
    return _engine_status.read()


# ── Hash160 目标集 ────────────────────────────────────────────
_hash160_set: Any = None
_xonly_set: Any = None


def get_hash160_set() -> Any:
    """获取全局 Hash160 目标集引用。"""
    return _hash160_set


def get_xonly_set() -> Any:
    """获取全局 x-only pubkey 目标集引用。"""
    return _xonly_set


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
    from api.metrics import get_registry

    dead: list[WebSocket] = []
    for ws in list(_websocket_clients):
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
    """返回全局 ResultDB 单例（惰性初始化）。"""
    global _db
    if _db is None:
        _db = ResultDB(str(PROJECT_ROOT / "collision_results.db"))
    return _db


# ── 分布式 gRPC 引用 ────────────────────────────────────────
_grpc_server: Any = None
_worker_registry: Any = None


def get_worker_registry() -> Any:
    """返回全局 WorkerRegistry 实例。"""
    return _worker_registry
