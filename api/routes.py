"""API 路由 - REST 端点 + WebSocket 实时推送..

端点列表:
    GET  /               - Dashboard 页面
    GET  /api/health     - 健康检查
    GET  /api/stats      - 引擎统计数据
    GET  /api/results    - 碰撞结果分页查询
    GET  /api/status     - 引擎运行状态
    WS   /ws             - 实时 WebSocket 推送
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.responses import HTMLResponse, JSONResponse, Response

from .state import (
    _websocket_clients,
    _ws_lock,
    get_db,
    get_engine_status,
    get_hash160_set,
    get_xonly_set,
    jinja_env,
    logger,
)

router = APIRouter()


# ═══════════════════════════════════════════════════════════════
#  内部辅助
# ═══════════════════════════════════════════════════════════════


def _build_stats() -> dict[str, Any]:
    """构建完整的统计信息字典.."""
    es = get_engine_status()
    db = get_db()
    try:
        total_collisions = db.count_results()
    except Exception:  # noqa: BLE001
        total_collisions = 0

    target_info = {
        "hash160": 0,
        "xonly": 0,
        "hash160_loaded": False,
        "xonly_loaded": False,
    }
    hs = get_hash160_set()
    if hs is not None:
        try:
            target_info["hash160"] = len(hs)
            target_info["hash160_loaded"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取 Hash160 目标集大小失败: %s", exc)
    xs = get_xonly_set()
    if xs is not None:
        try:
            target_info["xonly"] = len(xs)
            target_info["xonly_loaded"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取 XOnly 目标集大小失败: %s", exc)

    return {
        "keys_per_second": es.get("keys_per_second", 0.0),
        "total_keys": es.get("total_keys", 0),
        "elapsed_seconds": es.get("elapsed_seconds", 0.0),
        "total_collisions": total_collisions,
        "engine_running": es.get("running", False),
        "engine_mode": es.get("mode", "unknown"),
        "target_count": target_info,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ═══════════════════════════════════════════════════════════════
#  页面路由
# ═══════════════════════════════════════════════════════════════


@router.get("/", response_class=HTMLResponse, response_model=None)
async def dashboard() -> str | HTMLResponse:
    """渲染 Dashboard 页面.."""
    try:
        template = jinja_env.get_template("dashboard.html")
        stats = _build_stats()
        return template.render(stats=stats)
    except Exception as exc:  # noqa: BLE001
        logger.error("渲染 Dashboard 失败: %s", exc)
        return HTMLResponse(
            content=f"<h1>Dashboard 渲染错误</h1><pre>{exc}</pre>",
            status_code=500,
        )


# ═══════════════════════════════════════════════════════════════
#  REST API 端点
# ═══════════════════════════════════════════════════════════════


@router.get("/api/health")
async def health_check() -> dict[str, Any]:
    """健康检查端点.."""
    db = get_db()
    db_ok = True
    try:
        db.count_results()
    except Exception:  # noqa: BLE001
        db_ok = False

    return {
        "status": "ok",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "database": "connected" if db_ok else "error",
        "version": "2.3.0",
    }


@router.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    """获取实时统计信息.."""
    return _build_stats()


_VALID_ADDRESS_TYPES = frozenset(
    {
        "P2PKH (Legacy)",
        "P2WPKH/P2PKH",
        "P2TR (Taproot)",
        "P2PKH",
        "P2WPKH",
    },
)


@router.get("/api/results", response_model=None)
async def get_results(
    limit: Annotated[int, Query(ge=1, le=500, description="每页条数")] = 50,
    offset: Annotated[int, Query(ge=0, description="偏移量")] = 0,
    address_type: Annotated[str | None, Query(description="地址类型过滤")] = None,
) -> dict[str, Any] | JSONResponse:
    """分页查询碰撞结果.."""
    if address_type is not None and address_type not in _VALID_ADDRESS_TYPES:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"无效的地址类型: {address_type!r}.有效值: {sorted(_VALID_ADDRESS_TYPES)}",
                "total": 0,
                "items": [],
            },
        )
    db = get_db()
    try:
        total = db.count_results(address_type=address_type)
        items = db.list_results(limit=limit, offset=offset, address_type=address_type)
    except Exception as exc:  # noqa: BLE001
        logger.error("查询碰撞结果失败: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": "内部查询错误", "total": 0, "items": []},
        )

    _short_display_limit = 16
    # 截断私钥显示
    for item in items:
        pk = item.get("privkey_hex", "")
        if len(pk) > _short_display_limit:
            item["privkey_hex_short"] = pk[:8] + "..." + pk[-8:]
        else:
            item["privkey_hex_short"] = pk
        # 截断 WIF
        wif = item.get("wif_compressed", "") or item.get("wif_uncompressed", "")
        if len(wif) > _short_display_limit:
            item["wif_short"] = wif[:8] + "..." + wif[-8:]
        else:
            item["wif_short"] = wif

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/api/status")
async def get_status() -> dict[str, Any]:
    """获取引擎运行状态.."""
    es = get_engine_status()
    db = get_db()
    try:
        collision_count = db.count_results()
    except Exception:  # noqa: BLE001
        collision_count = 0

    # 分布式 worker 状态
    workers = _get_worker_stats()

    return {
        "running": es.get("running", False),
        "mode": es.get("mode", "unknown"),
        "keys_per_second": es.get("keys_per_second", 0.0),
        "total_keys": es.get("total_keys", 0),
        "elapsed_seconds": es.get("elapsed_seconds", 0.0),
        "collision_count": collision_count,
        "gpu_info": es.get("gpu_info", {}),
        "error": es.get("error", None),
        "distributed_workers": workers,
    }


# ═══════════════════════════════════════════════════════════════
#  分布式扫描端点
# ═══════════════════════════════════════════════════════════════

_WORKER_REGISTRY: Any = None


def _set_worker_registry(registry: Any) -> None:
    """设置 WorkerRegistry 引用(由 server.py 启动时注入).."""
    global _WORKER_REGISTRY
    _WORKER_REGISTRY = registry


def _get_worker_stats() -> list[dict[str, Any]]:
    """获取所有 worker 的摘要信息.."""
    if _WORKER_REGISTRY is None:
        return []
    try:
        workers = _WORKER_REGISTRY.list_workers()
        return [
            {
                "worker_id": w.worker_id,
                "address": w.address,
                "cpu_cores": w.cpu_cores,
                "gpu_count": w.gpu_count,
                "status": w.status,
                "keys_checked": w.keys_checked,
                "current_key": w.current_start,
                "last_heartbeat": w.last_heartbeat,
                "alive": w.is_alive,
            }
            for w in workers
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取 worker 状态失败: %s", exc)
        return []


@router.get("/api/workers")
async def get_workers() -> dict[str, Any]:
    """获取所有注册 worker 的状态.."""
    workers = _get_worker_stats()
    return {
        "total": len(workers),
        "alive": sum(1 for w in workers if w.get("alive")),
        "workers": workers,
    }


@router.get("/api/target/download/{filename}")
async def download_target_file(filename: str) -> Response:
    """提供目标集文件下载(供 Worker 远程拉取).."""
    from pathlib import Path

    from fastapi.responses import FileResponse

    allowed = {
        "utxo_hash160.bin",
        "utxo_hash160.idx",
        "utxo_hash160.bloom",
        "utxo_xonly.bin",
        "utxo_xonly.idx",
        "utxo_xonly.bloom",
    }

    if filename not in allowed:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=404,
            content={"error": f"不允许的文件名: {filename}"},
        )

    root = Path(__file__).resolve().parent.parent
    file_path = root / filename
    if not file_path.exists():
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=404,
            content={"error": f"文件不存在: {filename}"},
        )

    return FileResponse(path=str(file_path), filename=filename)


# ═══════════════════════════════════════════════════════════════
#  WebSocket 端点
# ═══════════════════════════════════════════════════════════════


MAX_WS_CLIENTS = 50


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket 端点 - 实时推送统计信息(最多 50 个连接).."""
    await websocket.accept()
    async with _ws_lock:
        if len(_websocket_clients) >= MAX_WS_CLIENTS:
            await websocket.send_json({"error": "连接数已达上限"})
            await websocket.close()
            return
        _websocket_clients.add(websocket)
    logger.info("WebSocket 客户端已连接 (共 %d 个)", len(_websocket_clients))

    try:
        # 每 2 秒推送一次最新统计
        while True:
            stats = _build_stats()
            await websocket.send_json(stats)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("WebSocket 连接异常: %s", exc)
    finally:
        async with _ws_lock:
            _websocket_clients.discard(websocket)
            logger.info("WebSocket 客户端断开 (剩余 %d 个)", len(_websocket_clients))
