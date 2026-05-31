"""
Worker 节点 — gRPC 客户端 + 扫描编排

负责：
- 向 Master 注册、心跳、获取作业范围
- 在本地执行 CPU/GPU 扫描
- 碰撞结果通过 gRPC 上报
- 本地 checkpoint 双保险
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import threading
from pathlib import Path
from typing import Optional

import grpc

# 将项目根加入 sys.path（与 collision_engine.py 一致）
_src_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_src_root))
_local_pkg = _src_root / ".local-packages"
if _local_pkg.is_dir():
    sys.path.insert(0, str(_local_pkg))

from distributed.protocol_pb2 import (  # noqa: E402
    RegisterRequest,
    HeartbeatRequest,
    AssignmentRequest,
    HitReport,
)
from distributed.protocol_pb2_grpc import MasterServiceStub  # noqa: E402

_logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────
HEARTBEAT_INTERVAL = 5  # 心跳间隔（秒）
CHECKPOINT_FILE = _src_root / "distributed_checkpoint.json"


class DistributedScanner:
    """分布式扫描器：与 Master 协作执行远程扫描任务。"""

    def __init__(
        self,
        master_addr: str,
        worker_id: str,
        cpu_cores: int = 0,
        gpu_enabled: bool = True,
        gpu_devices: str = "",
        gpu_batch_size: int = 65536,
        gpu_start: int = 0,
        count: int = 0,
        p2tr: bool = False,
        xonly_file: str = "",
        master_http_port: int = 8080,
    ):
        self._master_addr = master_addr
        self._master_http_port = master_http_port
        self._worker_id = worker_id
        self._cpu_cores = cpu_cores or os.cpu_count() or 4
        self._gpu_enabled = gpu_enabled
        self._gpu_devices = gpu_devices
        self._gpu_batch_size = gpu_batch_size
        self._gpu_start = gpu_start
        self._count = count
        self._p2tr = p2tr
        self._xonly_file = xonly_file

        self._channel: Optional[grpc.Channel] = None
        self._stub: Optional[MasterServiceStub] = None
        self._stop_event = threading.Event()

        # 运行时状态
        self._keys_checked = 0
        self._current_key = 0
        self._assignment_cursor = 0
        self._heartbeat_interval = HEARTBEAT_INTERVAL
        self._last_heartbeat_time = 0.0

        # 重试退避
        self._retry_delay = 1.0  # 初始退避秒数
        self._max_retry_delay = 60.0  # 最大退避秒数

    # ── 生命周期 ──────────────────────────────────────────

    def connect(self) -> bool:
        """连接到 Master 并注册。返回是否成功。"""
        try:
            self._channel = grpc.insecure_channel(self._master_addr)
            self._stub = MasterServiceStub(self._channel)
            _logger.info(
                "[Worker %s] 连接到 Master: %s", self._worker_id, self._master_addr
            )

            # 注册
            resp = self._stub.Register(
                RegisterRequest(
                    worker_id=self._worker_id,
                    cpu_cores=self._cpu_cores,
                    gpu_count=self._detect_gpu_count(),
                    address=f"worker-{self._worker_id}",
                    version="2.0.1-distributed",
                )
            )
            if not resp.accepted:
                _logger.error("[Worker %s] 注册被拒: %s", self._worker_id, resp.message)
                return False

            self._heartbeat_interval = resp.heartbeat_interval_sec or HEARTBEAT_INTERVAL
            _logger.info(
                "[Worker %s] 注册成功 (master_id=%s, hb=%ds, assign_size=%d)",
                self._worker_id,
                resp.master_id,
                self._heartbeat_interval,
                resp.assignment_size,
            )
            return True

        except Exception as exc:
            _logger.error("[Worker %s] 连接失败: %s", self._worker_id, exc)
            return False

    def close(self) -> None:
        """关闭连接。"""
        self._stop_event.set()
        if self._channel:
            self._channel.close()
            _logger.info("[Worker %s] 连接已关闭", self._worker_id)

    # ── 主循环 ──────────────────────────────────────────

    def run(self) -> None:
        """主扫描循环：获取 range → 扫描 → 上报。"""
        _logger.info("[Worker %s] 开始分布式扫描", self._worker_id)

        while not self._stop_event.is_set():
            try:
                # 1. 获取下一个作业范围
                assignment = self._get_assignment()
                if assignment is None or not assignment.has_work:
                    _logger.info(
                        "[Worker %s] 无可用作业，等待 %.1fs 后重试...",
                        self._worker_id,
                        self._retry_delay,
                    )
                    self._send_heartbeat(status="idle")
                    time.sleep(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)
                    continue

                self._retry_delay = 1.0  # 成功获取作业，重置退避
                start_key = assignment.start_key
                end_key = assignment.end_key
                self._current_key = start_key
                self._assignment_cursor = assignment.cursor

                _logger.info(
                    "[Worker %s] 获取作业范围: [%d, %d) (cursor=%d)",
                    self._worker_id,
                    start_key,
                    end_key,
                    self._assignment_cursor,
                )

                # 2. 加载目标集 TODO: 支持远程下载
                target, xonly_target = self._load_targets()
                if target is None:
                    _logger.error(
                        "[Worker %s] 目标集加载失败，跳过作业", self._worker_id
                    )
                    self._send_heartbeat(status="error", error_message="目标集加载失败")
                    time.sleep(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)
                    continue

                # 3. 执行扫描
                self._scan_range(start_key, end_key, target, xonly_target)

                # 4. 清理
                target.close()
                if xonly_target is not None:
                    xonly_target.close()

            except KeyboardInterrupt:
                _logger.warning("[Worker %s] 用户中断", self._worker_id)
                break
            except Exception as exc:
                _logger.error(
                    "[Worker %s] 扫描异常: %s", self._worker_id, exc, exc_info=True
                )
                self._send_heartbeat(status="error", error_message=str(exc))
                time.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)

        self._save_local_checkpoint()
        _logger.info(
            "[Worker %s] 扫描结束，共检查 %d 个私钥",
            self._worker_id,
            self._keys_checked,
        )

    # ── 核心扫描 ──────────────────────────────────────────

    def _scan_range(
        self,
        start_key: int,
        end_key: int,
        target,
        xonly_target,
    ) -> None:
        """扫描指定 key 范围。尝试 GPU 模式，回退到 CPU 模式。

        若指定了 gpu_start（非零），CPU 回退从此值而非 start_key 开始，
        避免重复扫描 GPU 已处理的部分。
        """
        from collision_engine import _GPU_AVAILABLE as gpu_available

        use_gpu = self._gpu_enabled and gpu_available

        actual_start = max(start_key, self._gpu_start) if self._gpu_start else start_key

        if use_gpu:
            self._scan_gpu(actual_start, end_key, target, xonly_target)
        else:
            self._scan_cpu(actual_start, end_key, target, xonly_target)

    def _scan_cpu(self, start_key: int, end_key: int, target, xonly_target) -> None:
        """CPU 模式扫描：多线程 + 点加法链加速。

        每个线程管理自己的 stride 序列（等差数列），线程 i 扫描
        start_key + i + n * cpu_cores（n=0,1,2,...），通过点加法连
        加速连续 key（stride=cpu_cores）的推导，消除重复的完整 EC 乘法。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from collision_engine import check_single_key_chain, save_result

        limit = (end_key - start_key) if end_key > 0 else self._count
        total_keys = limit if limit > 0 else self._count
        n_threads = min(self._cpu_cores, max(1, total_keys))
        stride_bytes = n_threads.to_bytes(32, "big")
        acc_lock = threading.Lock()

        def _worker(thread_id: int) -> None:
            """线程级扫描：stride-based 等差数列 + 点加法链加速。"""
            local_checked = 0
            pubkey_point = None
            last_report = time.time()

            # 此线程的起始 key
            k = start_key + thread_id
            first = True

            while k < end_key:
                if self._stop_event.is_set():
                    break

                # 首次完整 EC 乘法，后续点加法链加速
                result, pubkey_point = check_single_key_chain(
                    k,
                    target,
                    stride_bytes if not first else None,
                    pubkey_point if not first else None,
                    xonly_target,
                )
                first = False

                if result:
                    self._report_hit(result)
                    try:
                        save_result(result)
                    except Exception:
                        pass

                local_checked += 1
                k += n_threads

                # 心跳
                now = time.time()
                if now - last_report > self._heartbeat_interval:
                    with acc_lock:
                        self._current_key = k
                        self._keys_checked += local_checked
                    local_checked = 0
                    self._send_heartbeat(status="scanning")
                    last_report = now

            # 最终刷新
            if local_checked > 0:
                with acc_lock:
                    self._current_key = k
                    self._keys_checked += local_checked

        with ThreadPoolExecutor(max_workers=n_threads) as executor:
            futures = [executor.submit(_worker, i) for i in range(n_threads)]
            for f in as_completed(futures):
                exc = f.exception()
                if exc:
                    _logger.error(
                        "[Worker %s] CPU 扫描线程异常: %s", self._worker_id, exc
                    )

    def _scan_gpu(self, start_key: int, end_key: int, target, xonly_target) -> None:
        """GPU 模式扫描。"""
        try:
            from gpu_engine import GPUBatchScheduler, DispatcherConfig

            limit = (end_key - start_key) if end_key > 0 else self._count

            def on_hit(privkey_bytes: bytes) -> None:
                from collision_engine import check_single_key, save_result

                privkey_int = int.from_bytes(privkey_bytes, "little")
                result = check_single_key(privkey_int, target, xonly_target)
                if result is not None:
                    self._report_hit(result)
                    try:
                        save_result(result)
                    except Exception:
                        pass
                # 碰撞回调中不需要检查 stop_event（由 GPU pipeline 负责）
                pass

            config = DispatcherConfig(
                batch_size=self._gpu_batch_size,
                device_indices=None,
                total_keys=limit,
                quiet=True,
                check_collision=lambda h160: h160 in target,
                on_hit=on_hit,
                mode="sequential",
                sequential_start=start_key,
                tdr_safe=True,
                max_kernel_time=1.5,
            )

            scheduler = GPUBatchScheduler(config)
            if not scheduler.initialize():
                _logger.error(
                    "[Worker %s] GPU 初始化失败，回退到 CPU 模式", self._worker_id
                )
                self._scan_cpu(start_key, end_key, target, xonly_target)
                return

            scheduler.run()

        except ImportError:
            _logger.warning(
                "[Worker %s] GPU 引擎不可用，回退到 CPU 模式", self._worker_id
            )
            self._scan_cpu(start_key, end_key, target, xonly_target)

    # ── 目标集 ──────────────────────────────────────────

    def _load_targets(self):
        """加载目标集。支持本地预部署或从 Master 下载。"""
        try:
            from collision_target import Hash160Set, XOnlySet, SwappableTarget

            h160_path = _src_root / "utxo_hash160.bin"
            if not h160_path.exists():
                _logger.warning(
                    "[Worker %s] 本地目标集不存在: %s，尝试从 Master 下载...",
                    self._worker_id,
                    h160_path,
                )
                if not self._download_target():
                    return None, None

            target = Hash160Set()
            target.load(quiet=True)
            _logger.info(
                "[Worker %s] 已加载 %s 个 HASH160", self._worker_id, f"{len(target):,}"
            )

            xonly_target = None
            if self._p2tr:
                xonly_path = self._xonly_file if self._xonly_file else None
                xonly_set = XOnlySet()
                xonly_set.load(bin_path=xonly_path, quiet=True)
                xonly_target = SwappableTarget(initial_set=xonly_set)

            return SwappableTarget(initial_set=target), xonly_target

        except Exception as exc:
            _logger.error("[Worker %s] 目标集加载异常: %s", self._worker_id, exc)
            return None, None

    def _download_target(self) -> bool:
        """从 Master HTTP 端点下载目标集文件。返回是否成功。"""
        import urllib.request

        # 获取目标集信息
        try:
            from distributed.protocol_pb2 import TargetInfoRequest

            resp = self._stub.GetTargetInfo(
                TargetInfoRequest(worker_id=self._worker_id)
            )
            if not resp.hash160_available:
                _logger.error("[Worker %s] Master 无可用目标集", self._worker_id)
                return False
        except Exception as exc:
            _logger.error("[Worker %s] 获取目标集信息失败: %s", self._worker_id, exc)
            return False

        # 通过 HTTP 下载（Master FastAPI 提供服务）
        master_host = self._master_addr.split(":")[0]
        base_url = f"http://{master_host}:{self._master_http_port}"

        files_to_download = [
            "utxo_hash160.bin",
            "utxo_hash160.idx",
            "utxo_hash160.bloom",
        ]
        success = True

        for fname in files_to_download:
            local_path = _src_root / fname
            if local_path.exists():
                _logger.info(
                    "[Worker %s] 文件已存在: %s，跳过下载", self._worker_id, fname
                )
                continue

            url = f"{base_url}/api/target/download/{fname}"
            try:
                _logger.info("[Worker %s] 下载: %s ...", self._worker_id, url)
                urllib.request.urlretrieve(url, local_path)
                _logger.info("[Worker %s] 下载完成: %s", self._worker_id, fname)
            except Exception as exc:
                _logger.error(
                    "[Worker %s] 下载失败 %s: %s", self._worker_id, fname, exc
                )
                success = False

        return success

    # ── gRPC 通信 ──────────────────────────────────────────

    def _get_assignment(self):
        """获取下一个作业范围。"""
        if self._stub is None:
            return None
        return self._stub.GetAssignment(AssignmentRequest(worker_id=self._worker_id))

    def _send_heartbeat(
        self, status: str = "scanning", error_message: str = ""
    ) -> bool:
        """发送心跳。返回是否成功。"""
        if self._stub is None:
            return False
        try:
            resp = self._stub.Heartbeat(
                HeartbeatRequest(
                    worker_id=self._worker_id,
                    keys_checked=self._keys_checked,
                    current_key=self._current_key,
                    status=status,
                    error_message=error_message,
                )
            )
            if resp.cancel_requested:
                _logger.warning("[Worker %s] Master 要求停止当前范围", self._worker_id)
                self._stop_event.set()
            return resp.acknowledged
        except Exception as exc:
            _logger.warning("[Worker %s] 心跳失败: %s", self._worker_id, exc)
            return False

    def _report_hit(self, result):
        """上报碰撞结果到 Master。"""
        if self._stub is None:
            return
        try:
            resp = self._stub.ReportHit(
                HitReport(
                    worker_id=self._worker_id,
                    privkey_hex=getattr(result, "privkey_hex", ""),
                    key_value=int(result.privkey_hex, 16) & 0x7FFFFFFFFFFFFFFF,  # protobuf int64 有符号
                )
            )
            _logger.info(
                "[Worker %s] 碰撞已上报: %s (verified=%s)",
                self._worker_id,
                resp.collision_id,
                resp.verified,
            )
        except Exception as exc:
            _logger.warning("[Worker %s] 上报碰撞失败: %s", self._worker_id, exc)

    def _save_local_checkpoint(self) -> None:
        """保存本地 checkpoint 作为双保险。"""
        import json

        try:
            data = {
                "worker_id": self._worker_id,
                "master_addr": self._master_addr,
                "keys_checked": self._keys_checked,
                "last_key": self._current_key,
                "cursor": self._assignment_cursor,
                "timestamp": time.time(),
            }
            CHECKPOINT_FILE.write_text(json.dumps(data, indent=2))
            _logger.info("[Worker %s] 本地 checkpoint 已保存", self._worker_id)
        except Exception as exc:
            _logger.warning(
                "[Worker %s] 保存 checkpoint 失败: %s", self._worker_id, exc
            )

    # ── 辅助 ──────────────────────────────────────────

    @staticmethod
    def _detect_gpu_count() -> int:
        """检测本机 GPU 数量（通过 pyopencl）。"""
        try:
            import pyopencl as cl

            platforms = cl.get_platforms()
            return sum(len(p.get_devices(cl.device_type.GPU)) for p in platforms)
        except Exception:
            return 0


def main() -> None:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="分布式扫描 Worker 节点")
    parser.add_argument(
        "--master-addr",
        type=str,
        required=True,
        help="Master 地址 (host:port)",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=f"worker-{os.urandom(4).hex()}",
        help="Worker 唯一标识 (default: auto)",
    )
    parser.add_argument(
        "--master-http-port",
        type=int,
        default=8080,
        help="Master HTTP 端口 (FastAPI, default: 8080)",
    )
    parser.add_argument(
        "--cpu-cores", type=int, default=0, help="CPU 核心数 (default: auto)"
    )
    parser.add_argument("--no-gpu", action="store_true", help="禁用 GPU 扫描")
    parser.add_argument(
        "--gpu-devices", type=str, default="", help="GPU 设备索引 (逗号分隔)"
    )
    parser.add_argument(
        "--gpu-batch-size", type=int, default=65536, help="GPU batch 大小"
    )
    parser.add_argument(
        "--gpu-first",
        type=int,
        default=0,
        help="GPU 起始扫描值（非零时 GPU 从此开始，CPU 回退时也使用此值避免重复）",
    )
    parser.add_argument("--count", type=int, default=0, help="扫描上限 (0=无限)")
    parser.add_argument("--p2tr", action="store_true", help="启用 P2TR 匹配")
    parser.add_argument(
        "--xonly-file", type=str, default="", help="P2TR x-only pubkey 文件路径"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    scanner = DistributedScanner(
        master_addr=args.master_addr,
        worker_id=args.worker_id,
        master_http_port=args.master_http_port,
        cpu_cores=args.cpu_cores,
        gpu_enabled=not args.no_gpu,
        gpu_devices=args.gpu_devices,
        gpu_batch_size=args.gpu_batch_size,
        gpu_start=args.gpu_first,
        count=args.count,
        p2tr=args.p2tr,
        xonly_file=args.xonly_file,
    )

    if scanner.connect():
        try:
            scanner.run()
        except KeyboardInterrupt:
            _logger.info("[Worker %s] 用户中断", args.worker_id)
        finally:
            scanner.close()
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
