#!/usr/bin/env python3
"""多 GPU 调度器 — 管理多个 GPU 管道的并行提交与结果编排。

支持：
  - 多 GPU 并行执行 batch
  - 自动负载均衡（按计算单元数）
  - 进度报告和碰撞统计
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .gpu_pipeline import GPUPipeline
from .gpu_device import DeviceInfo

logger = logging.getLogger(__name__)


@dataclass
class DispatcherConfig:
    """调度器配置。"""

    batch_size: int = 65536
    device_indices: list[int] | None = None  # None = 所有可用 GPU
    total_keys: int = 0  # 0 = 无限
    quiet: bool = False
    check_collision: Callable[[bytes], bool] | None = None
    on_hit: Callable[[bytes], None] | None = None  # 命中回调: privkey_bytes(32B)
    mode: str = "random"  # "random" 或 "sequential"
    sequential_start: int = 1  # 顺序扫描起始私钥
    tdr_safe: bool = True  # 启用 TDR 安全 sub-batch 拆分
    max_kernel_time: float = 1.5  # 单个 sub-batch 最大执行时间（秒）
    # P2-10: GPU 侧碰撞检测 bloom 参数（通过调度器透传）
    bloom_data: bytes | None = None
    bloom_m: int = 0


@dataclass
class WorkerResult:
    """单个 GPU 工作线程的累计结果。"""

    device_name: str
    keys_checked: int = 0
    total_elapsed: float = 0.0
    hits: int = 0
    errors: int = 0


class GPUBatchScheduler:
    """多 GPU batch 调度器。"""

    def __init__(self, config: DispatcherConfig):
        """初始化调度器，设置配置和内部状态。"""
        self.config = config
        self._pipelines: list[GPUPipeline] = []
        self._workers: list[WorkerResult] = []
        self._stop_event = threading.Event()
        self._total_checked = 0
        self._total_hits = 0
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_device_indices(
        device_indices: list[int] | None,
    ) -> list[tuple[int, int, DeviceInfo]]:
        """将全局扁平的设备索引解析为 (platform_index, local_device_index, DeviceInfo) 元组。

        `device_indices` 是 `list_devices()` 返回的全局扁平列表中的索引。
        """
        try:
            import pyopencl as cl
        except ImportError:
            logger.warning("[GPU] pyopencl 未安装，无法解析设备索引。")
            return []

        result: list[tuple[int, int, DeviceInfo]] = []
        global_idx = 0
        for pi, platform in enumerate(cl.get_platforms()):
            try:
                devs = platform.get_devices()
            except cl.RuntimeError:
                devs = []
            for di, dev in enumerate(devs):
                type_str = (
                    "GPU"
                    if dev.type == cl.device_type.GPU
                    else "CPU"
                    if dev.type == cl.device_type.CPU
                    else "Accelerator"
                )
                dev_info = DeviceInfo(
                    platform_name=platform.name,
                    device_name=dev.name.strip(),
                    device_type=type_str,
                    compute_units=dev.max_compute_units,
                    max_work_group_size=dev.max_work_group_size,
                    global_mem_size=dev.global_mem_size,
                    local_mem_size=dev.local_mem_size,
                    max_clock_frequency=dev.max_clock_frequency,
                    opencl_version=dev.version,
                    driver_version=dev.driver_version,
                    available=bool(dev.available),
                    _raw_device=dev,
                )
                # 筛选：指定索引 → 精确匹配；未指定 → 可用 GPU
                if device_indices is not None:
                    if global_idx in device_indices:
                        result.append((pi, di, dev_info))
                else:
                    if dev_info.available and dev_info.device_type == "GPU":
                        result.append((pi, di, dev_info))
                global_idx += 1
        return result

    def initialize(self) -> bool:
        """初始化所有 GPU 管道。成功返回 True。"""
        selected = self._resolve_device_indices(self.config.device_indices)
        if not selected:
            logger.warning(
                "%s",
                "[GPU] 没有可用的 GPU 设备。"
                if self.config.device_indices is None
                else f"[GPU] 指定的设备索引 {self.config.device_indices} 无效。",
            )
            return False

        if not self.config.quiet:
            logger.info("[GPU] 初始化 %d 个 GPU 管道...", len(selected))

        # 顺序模式下，多 GPU 需要分区起始值
        n_gpu = len(selected)
        base_start = self.config.sequential_start
        # stride = n_gpu * batch_size（每个 GPU 轮转一次后整体推进量）
        gpu_stride = n_gpu * self.config.batch_size

        for i, (pi, di, dev_info) in enumerate(selected):
            # P2-8: 按设备能力计算独立 batch_size
            dev_batch = self.config.batch_size
            raw_dev = dev_info._raw_device
            if raw_dev is not None:
                # 根据计算单元数比例缩放 batch_size
                ref_cu = selected[0][2].compute_units
                if ref_cu > 0:
                    ratio = dev_info.compute_units / ref_cu
                    dev_batch = max(int(self.config.batch_size * ratio), 16384)
                # 受 max_mem_alloc_size 限制
                max_alloc = raw_dev.max_mem_alloc_size
                alloc_limit = int(max_alloc // (32 + 20))
                dev_batch = min(dev_batch, alloc_limit)

            try:
                pipe = GPUPipeline(
                    platform_index=pi,
                    device_index=di,
                    batch_size=dev_batch,
                    quiet=self.config.quiet,
                    mode=self.config.mode,
                    sequential_start=(
                        base_start + i * self.config.batch_size
                        if self.config.mode == "sequential"
                        else base_start
                    ),
                    sequential_stride=(
                        gpu_stride if self.config.mode == "sequential" else None
                    ),
                    tdr_safe=self.config.tdr_safe,
                    max_kernel_time=self.config.max_kernel_time,
                    bloom_data=self.config.bloom_data,
                    bloom_m=self.config.bloom_m,
                )
                self._pipelines.append(pipe)
                self._workers.append(WorkerResult(device_name=dev_info.device_name))
                if not self.config.quiet:
                    logger.info("  [%d] %s [OK]", i, dev_info.device_name)
            except Exception as e:
                logger.error("  [%d] %s [FAIL]: %s", i, dev_info.device_name, e)
                self._workers.append(WorkerResult(device_name=dev_info.device_name))

        return len(self._pipelines) > 0

    def _worker_loop(self, pipe_index: int):
        """单个 GPU 管道的工作循环。"""
        pipe = self._pipelines[pipe_index]
        worker = self._workers[pipe_index]

        # P2-10: 当 bloom 数据已设置且未传入 check_collision 时，
        # 自动启用 GPU 侧碰撞检测（submit_batch 根据 check_collision=None 触发）
        use_gpu_collision = (
            self.config.bloom_data is not None and self.config.check_collision is None
        )

        while not self._stop_event.is_set():
            try:
                check_fn = None if use_gpu_collision else self.config.check_collision
                result = pipe.submit_batch(check_collision=check_fn)

                # 保存碰撞命中
                if result.hit_indices and self.config.on_hit is not None:
                    for idx in result.hit_indices:
                        pk_bytes = pipe.get_privkey_for_index(result, idx)
                        self.config.on_hit(pk_bytes)

                with self._lock:
                    worker.keys_checked += result.keys_checked
                    worker.total_elapsed += result.elapsed
                    worker.hits += len(result.hit_indices)
                    self._total_checked += result.keys_checked
                    self._total_hits += len(result.hit_indices)

                # 检查是否达到总量限制
                if self.config.total_keys > 0:
                    with self._lock:
                        if self._total_checked >= self.config.total_keys:
                            self._stop_event.set()

            except Exception as e:
                with self._lock:
                    worker.errors += 1
                if not self.config.quiet:
                    logger.error("\n[GPU][%d] 错误: %s", pipe_index, e)
                # 出错时短暂暂停
                self._stop_event.wait(1.0)
                if self._stop_event.is_set():
                    break

    def run(self) -> list[WorkerResult]:
        """启动所有 GPU worker 并等待完成。"""
        if not self._pipelines:
            raise RuntimeError("调度器未初始化，请先调用 initialize()")

        t_start = time.perf_counter()
        threads = []

        for i in range(len(self._pipelines)):
            t = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                daemon=True,
            )
            threads.append(t)
            t.start()

        # 主线程等待或处理 Ctrl+C
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            logger.warning("\n[GPU] 收到中断信号，正在停止...")
            self._stop_event.set()
            for t in threads:
                t.join(timeout=5.0)

        total_time = time.perf_counter() - t_start

        if not self.config.quiet:
            self._print_summary(total_time)

        return self._workers

    def stop(self) -> None:
        """停止所有 GPU worker。"""
        self._stop_event.set()

    def _print_summary(self, total_time: float):
        """记录运行汇总。"""
        logger.info(
            "\n%s\n  GPU 碰撞扫描结果\n%s",
            "=" * 60,
            "=" * 60,
        )
        for i, w in enumerate(self._workers):
            rate = w.keys_checked / w.total_elapsed if w.total_elapsed > 0 else 0
            logger.info(
                "  [%d] %s\n"
                "       检查: %s | 命中: %d\n"
                "       速率: %s keys/s | 错误: %d",
                i,
                w.device_name,
                f"{w.keys_checked:,}",
                w.hits,
                f"{rate:,.0f}",
                w.errors,
            )

        total_rate = self._total_checked / total_time if total_time > 0 else 0
        logger.info(
            "\n  总计\n"
            "    检查: %s 个私钥\n"
            "    命中: %d\n"
            "    耗时: %.1fs\n"
            "    速率: %s keys/s (合计)\n"
            "%s",
            f"{self._total_checked:,}",
            self._total_hits,
            total_time,
            f"{total_rate:,.0f}",
            "=" * 60,
        )

    def close(self) -> None:
        """释放所有管道。"""
        self._stop_event.set()
        for pipe in self._pipelines:
            try:
                pipe.close()
            except Exception:
                pass
        self._pipelines.clear()

    def __enter__(self) -> GPUBatchScheduler:
        """上下文管理器入口，返回自身。"""
        return self

    def __exit__(self, *args: Any) -> None:
        """上下文管理器出口，释放所有 GPU 管道。"""
        self.close()
