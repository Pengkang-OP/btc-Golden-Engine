"""
Master 节点 — gRPC 服务端

负责：
- Worker 注册与心跳管理
- async 安全的 WorkerRegistry
- 按 stride 分区的 key 范围分配
- 碰撞结果接收与持久化
- 超时 worker 检测与任务重分配
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent import futures
from typing import Optional

import grpc

from distributed.models import WorkerInfo, Assignment
from distributed.protocol_pb2 import (
    RegisterRequest,
    RegisterResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    AssignmentRequest,
    AssignmentResponse,
    HitReport,
    ReportResponse,
    TargetInfoRequest,
    TargetInfoResponse,
)
from distributed.protocol_pb2_grpc import (
    MasterServiceServicer,
    add_MasterServiceServicer_to_server,
)

_logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────
DEFAULT_PORT = 50051
HEARTBEAT_TIMEOUT = 30.0  # worker 心跳超时秒数
DEFAULT_ASSIGNMENT_SIZE = 10**12  # 每次分配的 range 大小
MAX_WORKERS = 100  # 最大 worker 数量
SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


# 连续超时次数阈值 — 超过此次数才回收 range（防止网络瞬断误回收）
MAX_CONSECUTIVE_TIMEOUT = 3


class WorkerRegistry:
    """线程安全的 Worker 注册表，管理所有已注册 worker 的状态。"""

    def __init__(self, assignment_size: int = DEFAULT_ASSIGNMENT_SIZE):
        self._lock = threading.Lock()
        self._workers: dict[str, WorkerInfo] = {}
        self._assignment_size = assignment_size
        self._global_cursor: int = 1  # 全局 key cursor
        self._master_id = f"master-{os.urandom(4).hex()}"
        self._consecutive_timeout: dict[str, int] = {}  # I03: 累计超时计数

    @property
    def master_id(self) -> str:
        """返回 Master 唯一标识。"""
        return self._master_id

    # ── 注册与注销 ──────────────────────────────────────────

    def register(self, info: WorkerInfo) -> tuple[bool, str]:
        """注册或更新 worker 信息。返回 (accepted, message)。"""
        with self._lock:
            if len(self._workers) >= MAX_WORKERS:
                return False, "Master 已达最大 worker 数量限制"
            if info.worker_id in self._workers:
                existing = self._workers[info.worker_id]
                # 更新运行时信息，保留已检查数
                existing.cpu_cores = info.cpu_cores
                existing.gpu_count = info.gpu_count
                existing.address = info.address
                existing.version = info.version
                existing.status = "registering"
                existing.last_heartbeat = time.time()
                existing.registered_at = time.time()
                msg = f"Worker {info.worker_id} 重新注册成功"
            else:
                info.last_heartbeat = time.time()
                info.registered_at = time.time()
                info.status = "registering"
                self._workers[info.worker_id] = info
                msg = f"Worker {info.worker_id} 注册成功"
            return True, msg

    def unregister(self, worker_id: str) -> bool:
        """注销 worker。返回是否成功。"""
        with self._lock:
            if worker_id in self._workers:
                del self._workers[worker_id]
                return True
            return False

    # ── 心跳更新 ──────────────────────────────────────────

    def update_heartbeat(
        self,
        worker_id: str,
        keys_checked: int,
        current_key: int,
        status: str,
        error_message: str = "",
    ) -> bool:
        """更新 worker 心跳。返回 worker 是否存在。

        I03: 收到有效心跳时重置累计超时计数。
        """
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return False
            worker.last_heartbeat = time.time()
            worker.keys_checked = keys_checked
            worker.status = status
            worker.error_message = error_message
            self._consecutive_timeout.pop(worker_id, None)  # 收到心跳，重置超时计数
            return True

    # ── 任务分配 ──────────────────────────────────────────

    def assign_range(self, worker_id: str) -> Optional[Assignment]:
        """分配下一个 key 范围给 worker。返回 Assignment 或 None（无可用范围）。

        I02: 分配前先清理掉线 worker 的 range。
        """
        self._cleanup_dead_workers()
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return None

            start = self._global_cursor
            end = start + self._assignment_size
            if end > SECP256K1_ORDER:
                # 接近私钥空间上限，缩小最后一段
                end = SECP256K1_ORDER
                if start >= end:
                    return None  # 私钥空间已耗尽

            self._global_cursor = end
            worker.current_start = start
            worker.current_end = end
            worker.status = "scanning"

            return Assignment(start_key=start, end_key=end, cursor=start)

    def steal_range(self, worker_id: str) -> Optional[Assignment]:
        """尝试取回超时 worker 的未完成 range 并重新分配。

        I03: 要求连续超时超过 MAX_CONSECUTIVE_TIMEOUT 次才回收，
        防止网络瞬断误回收。
        """
        with self._lock:
            now = time.time()
            for wid, w in list(self._workers.items()):
                if wid == worker_id:
                    continue
                if w.status != "scanning":
                    continue
                elapsed = now - w.last_heartbeat
                if elapsed > HEARTBEAT_TIMEOUT:
                    # 累计超时计数
                    cnt = self._consecutive_timeout.get(wid, 0) + 1
                    self._consecutive_timeout[wid] = cnt
                    if cnt < MAX_CONSECUTIVE_TIMEOUT:
                        _logger.warning(
                            "[Master] Worker %s 心跳超时 %ds (第 %d/%d 次，暂不回收)",
                            wid, elapsed, cnt, MAX_CONSECUTIVE_TIMEOUT,
                        )
                        continue
                    _logger.warning(
                        "[Master] Worker %s 连续 %d 次心跳超时 (%ds)，回收其 range [%d, %d)",
                        wid,
                        cnt,
                        HEARTBEAT_TIMEOUT,
                        w.current_start,
                        w.current_end,
                    )
                    w.status = "error"
                    w.error_message = f"heartbeat timeout ({cnt}x consec)"
                    # 仅当请求方 worker 无活跃范围时，才分配被回收的 range，
                    # 避免覆盖 worker 自身的当前作业范围
                    worker = self._workers.get(worker_id)
                    if worker is None:
                        return None
                    # 如果 worker 已经在扫描中，不覆盖其现有范围
                    if worker.status == "scanning" or worker.current_start > 0:
                        # 仅回收被收回的 range 到全局池
                        if w.current_start > 0:
                            mid = w.current_start + (w.current_end - w.current_start) // 2
                            if mid > self._global_cursor:
                                self._global_cursor = mid
                        w.current_start = 0
                        w.current_end = 0
                        self._consecutive_timeout.pop(wid, None)
                        return None  # 由 assign_range 正常分配
                    worker.current_start = w.current_start
                    mid = w.current_start + (w.current_end - w.current_start) // 2
                    self._global_cursor = (
                        mid if mid > self._global_cursor else self._global_cursor
                    )
                    worker.current_end = w.current_end
                    worker.status = "scanning"
                    w.current_start = 0
                    w.current_end = 0
                    self._consecutive_timeout.pop(wid, None)
                    return Assignment(
                        start_key=worker.current_start,
                        end_key=worker.current_end,
                        cursor=worker.current_start,
                    )
            return None

    def _cleanup_dead_workers(self) -> int:
        """I02: 清理掉线 worker 并回收其 range 到全局 pool。

        遍历所有 worker，将超过心跳超时且未在 steal_range 中被回收的
        掉线 worker 的 range 归还到 _global_cursor。
        返回清理掉的 worker 数量。
        """
        with self._lock:
            now = time.time()
            cleaned = 0
            for wid, w in list(self._workers.items()):
                if w.status != "scanning":
                    continue
                elapsed = now - w.last_heartbeat
                if elapsed <= HEARTBEAT_TIMEOUT:
                    continue
                cnt = self._consecutive_timeout.get(wid, 0) + 1
                self._consecutive_timeout[wid] = cnt
                if cnt < MAX_CONSECUTIVE_TIMEOUT:
                    continue
                _logger.warning(
                    "[Master] Worker %s 持续掉线 (%ds, 第%d次)，清理其 range [%d, %d)",
                    wid, elapsed, cnt, w.current_start, w.current_end,
                )
                # 回收未完成的 range
                if w.current_start > 0 and w.current_end > w.current_start:
                    mid = w.current_start + (w.current_end - w.current_start) // 2
                    if mid > self._global_cursor:
                        self._global_cursor = mid
                w.status = "error"
                w.error_message = f"cleaned up after {cnt}x consec timeout"
                w.current_start = 0
                w.current_end = 0
                self._consecutive_timeout.pop(wid, None)
                cleaned += 1
            return cleaned

    # ── 查询 ──────────────────────────────────────────────

    def get_worker(self, worker_id: str) -> Optional[WorkerInfo]:
        """获取指定 worker 信息。"""
        with self._lock:
            return self._workers.get(worker_id)

    def list_workers(self) -> list[WorkerInfo]:
        """列表所有 worker。"""
        with self._lock:
            return list(self._workers.values())

    def alive_workers(self) -> int:
        """存活 worker 数量。"""
        with self._lock:
            return sum(1 for w in self._workers.values() if w.is_alive)

    @property
    def total_keys_checked(self) -> int:
        """所有 worker 累计检查 key 数。"""
        with self._lock:
            return sum(w.keys_checked for w in self._workers.values())

    @property
    def global_cursor(self) -> int:
        """当前全局 cursor。"""
        with self._lock:
            return self._global_cursor

    @property
    def assignment_size(self) -> int:
        """每次分配的 range 大小。"""
        return self._assignment_size


# ── gRPC 服务实现 ──────────────────────────────────────────


class MasterService(MasterServiceServicer):
    """gRPC MasterService 服务端实现。"""

    def __init__(self, registry: WorkerRegistry):
        self._registry = registry
        self._hit_count = 0

    def Register(
        self, request: RegisterRequest, context: grpc.ServicerContext
    ) -> RegisterResponse:
        info = WorkerInfo(
            worker_id=request.worker_id,
            address=request.address,
            cpu_cores=request.cpu_cores,
            gpu_count=request.gpu_count,
            version=request.version,
        )
        accepted, msg = self._registry.register(info)
        _logger.info(
            "[Master] Register(%s): accepted=%s, cores=%d, gpus=%d, msg=%s",
            request.worker_id,
            accepted,
            request.cpu_cores,
            request.gpu_count,
            msg,
        )
        return RegisterResponse(
            accepted=accepted,
            master_id=self._registry.master_id,
            heartbeat_interval_sec=5,
            assignment_size=self._registry.assignment_size,
            message=msg,
        )

    def Heartbeat(
        self, request: HeartbeatRequest, context: grpc.ServicerContext
    ) -> HeartbeatResponse:
        found = self._registry.update_heartbeat(
            worker_id=request.worker_id,
            keys_checked=request.keys_checked,
            current_key=request.current_key,
            status=request.status,
            error_message=request.error_message,
        )
        if not found:
            return HeartbeatResponse(
                acknowledged=False, cancel_requested=True, message="worker 未注册"
            )
        return HeartbeatResponse(acknowledged=True, cancel_requested=False)

    def GetAssignment(
        self, request: AssignmentRequest, context: grpc.ServicerContext
    ) -> AssignmentResponse:
        # 优先尝试取回超时 worker 的 range
        assignment = self._registry.steal_range(request.worker_id)
        if assignment is None:
            assignment = self._registry.assign_range(request.worker_id)

        if assignment is None:
            return AssignmentResponse(has_work=False)

        _logger.info(
            "[Master] Assign(%s): start=%d, end=%d, cursor=%d",
            request.worker_id,
            assignment.start_key,
            assignment.end_key,
            assignment.cursor,
        )
        return AssignmentResponse(
            has_work=True,
            start_key=assignment.start_key,
            end_key=assignment.end_key,
            cursor=assignment.cursor,
        )

    def ReportHit(
        self, request: HitReport, context: grpc.ServicerContext
    ) -> ReportResponse:
        self._hit_count += 1
        _logger.info(
            "[Master] HIT(%s): privkey=%s, key_value=%d (total hits=%d)",
            request.worker_id,
            request.privkey_hex,
            request.key_value,
            self._hit_count,
        )
        return ReportResponse(
            accepted=True,
            verified=False,
            collision_id=f"hit-{self._hit_count}",
            message=f"碰撞 #{self._hit_count} 已记录",
        )

    def GetTargetInfo(
        self, request: TargetInfoRequest, context: grpc.ServicerContext
    ) -> TargetInfoResponse:
        """返回目标集基本信息。"""
        # 检查本地目标集文件
        import hashlib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        h160_path = root / "utxo_hash160.bin"
        xonly_path = root / "utxo_xonly.bin"

        h160_available = h160_path.exists()
        xonly_available = xonly_path.exists()

        resp = TargetInfoResponse(
            hash160_available=h160_available,
            hash160_count=0,
            hash160_size_bytes=h160_path.stat().st_size if h160_available else 0,
            xonly_available=xonly_available,
            xonly_count=0,
            xonly_size_bytes=xonly_path.stat().st_size if xonly_available else 0,
            download_url="/api/target/download/hash160",
            bloom_url="/api/target/download/bloom",
        )

        # 计算校验和（仅对 .idx 和 bloom 文件做轻量校验）
        bloom_path = root / "utxo_hash160.bloom"
        if bloom_path.exists():
            resp.checksum_sha256 = hashlib.sha256(bloom_path.read_bytes()).hexdigest()

        return resp


# ── 启动入口 ──────────────────────────────────────────────


CLEANUP_INTERVAL = 15.0  # 周期性清理间隔（秒）


def _run_cleanup_loop(registry: WorkerRegistry, stop_event: threading.Event) -> None:
    """I02: 后台线程周期性清理掉线 worker。"""
    while not stop_event.is_set():
        cleaned = registry._cleanup_dead_workers()
        if cleaned:
            _logger.info("[Master] 清理了 %d 个掉线 worker", cleaned)
        stop_event.wait(CLEANUP_INTERVAL)


def run_master(
    port: int = DEFAULT_PORT,
    assignment_size: int = DEFAULT_ASSIGNMENT_SIZE,
    max_workers: int = 10,
) -> tuple[grpc.Server, threading.Event]:
    """启动 Master gRPC 服务器。返回 (grpc.Server, stop_event) 供外部控制生命周期。

    同时启动后台线程周期性清理掉线 worker。
    """
    registry = WorkerRegistry(assignment_size=assignment_size)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    add_MasterServiceServicer_to_server(MasterService(registry), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    stop_event = threading.Event()
    cleanup_thread = threading.Thread(
        target=_run_cleanup_loop, args=(registry, stop_event), daemon=True
    )
    cleanup_thread.start()
    _logger.info(
        "[Master] gRPC 服务已启动 (port=%d, assignment_size=%d, cleanup_interval=%ds)",
        port,
        assignment_size,
        CLEANUP_INTERVAL,
    )
    return server, stop_event


def main() -> None:
    """CLI 入口：python -m distributed.master [--port PORT]"""
    import argparse

    parser = argparse.ArgumentParser(description="分布式扫描 Master 节点")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"gRPC 端口 (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--assignment-size",
        type=int,
        default=DEFAULT_ASSIGNMENT_SIZE,
        help="每次分配的 range 大小",
    )
    parser.add_argument("--max-workers", type=int, default=10, help="gRPC 线程池大小")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    server, stop_event = run_master(
        port=args.port,
        assignment_size=args.assignment_size,
        max_workers=args.max_workers,
    )
    _logger.info("[Master] 按 Ctrl+C 停止")
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        _logger.info("[Master] 用户中断，停止中...")
        stop_event.set()
        server.stop(grace=5)


if __name__ == "__main__":
    main()
