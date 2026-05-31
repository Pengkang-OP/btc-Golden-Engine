#!/usr/bin/env python3
"""GPU 管道 — pyopencl 实际调用链。

流程:
  1. 生成 batch_size 个随机私钥 (32B × batch)
  2. 写入 GPU 显存
  3. 启动 ec_mul_hash160 kernel → 直接得到 HASH160(20B × batch)
  4. 回读结果到 Host
  5. 在 Host 端二分查找碰撞
"""

from __future__ import annotations

import os
import time
import numpy as np
from pathlib import Path
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

from .gpu_device import DeviceInfo  # noqa: E402
from .tdr_handler import TDRConfig, KernelTimer, is_tdr_error  # noqa: E402

KERNEL_SRC_FILE = Path(__file__).parent / "gpu_kernel.h"


@dataclass
class BatchResult:
    """单个 GPU batch 的执行结果。"""

    keys_checked: int
    elapsed: float  # seconds
    keys_per_sec: float
    hash160s: np.ndarray  # (batch*20,) flattened uint8 array
    privkey_bytes: np.ndarray  # (batch*32,) flattened uint8 array
    hit_indices: list[int] = field(default_factory=list)


class GPUPipeline:
    """GPU 计算管道：管理 OpenCL 上下文、内核和执行。"""

    def __init__(
        self,
        device_index: int | None = None,
        platform_index: int | None = None,
        batch_size: int = 65536,
        profile: bool = False,
        quiet: bool = False,
        mode: str = "random",
        sequential_start: int = 1,
        sequential_stride: int | None = None,
        tdr_safe: bool = True,
        max_kernel_time: float = 1.5,
    ):
        self.batch_size = batch_size
        self.quiet = quiet
        self.profile = profile
        self.mode = mode
        self._seq_start = sequential_start
        # stride: 多 GPU 时每个 worker 每批推进量（单 GPU = batch_size）
        self._seq_stride = (
            sequential_stride if sequential_stride is not None else batch_size
        )
        # TDR 安全设置
        self._tdr_safe = tdr_safe
        self._max_kernel_time = max_kernel_time
        self._timer = KernelTimer()
        self._tdr_config = TDRConfig(
            enabled=tdr_safe,
            max_kernel_time=max_kernel_time,
        )
        self._ctx: Any = None
        self._queue: Any = None
        self._program: Any = None
        self._kernel_hash160: Any = None
        self._kernel_pubkey: Any = None
        self._d_privkeys: Any = None
        self._d_hash160s: Any = None
        self._d_pubkeys: Any = None
        self._h_privkeys = np.zeros(batch_size * 32, dtype=np.uint8)
        self._h_hash160s = np.zeros(batch_size * 20, dtype=np.uint8)

        self._init_opencl(device_index, platform_index)

    def _init_opencl(
        self, device_index: int | None, platform_index: int | None
    ) -> None:
        """初始化 OpenCL 上下文和命令队列。"""
        try:
            import pyopencl as cl
        except ImportError:
            logger.error(
                "[GPU] pyopencl 未安装。请先安装: pip install pyopencl>=2024.1"
            )
            raise

        platforms = cl.get_platforms()
        if not platforms:
            raise RuntimeError("未找到任何 OpenCL 平台")

        if platform_index is not None:
            pf = platforms[platform_index]
        else:
            # 自动选择含 GPU 的平台
            for p in platforms:
                try:
                    devs = p.get_devices(device_type=cl.device_type.GPU)
                    if devs:
                        pf = p
                        break
                except cl.RuntimeError:
                    continue
            else:
                pf = platforms[0]  # 回退到第一个平台

        if device_index is not None:
            devices = pf.get_devices()
            self._device = devices[device_index]
        else:
            # 选择最佳 GPU
            try:
                gpus = pf.get_devices(device_type=cl.device_type.GPU)
                self._device = gpus[0]
            except cl.RuntimeError:
                self._device = pf.get_devices()[0]

        if not self.quiet:
            info = DeviceInfo(
                platform_name=pf.name,
                device_name=self._device.name.strip(),
                device_type="GPU",
                compute_units=self._device.max_compute_units,
                max_work_group_size=self._device.max_work_group_size,
                global_mem_size=self._device.global_mem_size,
                local_mem_size=self._device.local_mem_size,
                max_clock_frequency=self._device.max_clock_frequency,
                opencl_version=self._device.version,
                driver_version=self._device.driver_version,
                available=bool(self._device.available),
            )
            logger.info("[GPU] 设备: %s", info)

        # 创建 context 和 queue
        self._ctx = cl.Context([self._device])
        props = []
        if self.profile:
            props.append(cl.command_queue_properties.PROFILING_ENABLE)
        self._queue = cl.CommandQueue(
            self._ctx,
            self._device,
            properties=props,  # type: ignore[arg-type]
        )

        # 编译 kernel
        if not KERNEL_SRC_FILE.exists():
            raise FileNotFoundError(f"GPU kernel 源文件未找到: {KERNEL_SRC_FILE}")
        src = KERNEL_SRC_FILE.read_text(encoding="utf-8")
        self._program = cl.Program(self._ctx, src).build(
            options=["-cl-std=CL1.2"],
        )

        # 获取 kernel 函数
        self._kernel_hash160 = self._program.ec_mul_hash160
        self._kernel_pubkey = self._program.ec_mul_pubkey

        # 分配设备端缓冲区
        mf = cl.mem_flags
        self._d_privkeys = cl.Buffer(
            self._ctx,
            mf.READ_ONLY | mf.ALLOC_HOST_PTR,
            size=self.batch_size * 32,
        )
        self._d_hash160s = cl.Buffer(
            self._ctx,
            mf.WRITE_ONLY | mf.ALLOC_HOST_PTR,
            size=self.batch_size * 20,
        )
        self._d_pubkeys = cl.Buffer(
            self._ctx,
            mf.WRITE_ONLY | mf.ALLOC_HOST_PTR,
            size=self.batch_size * 32,
        )

        if not self.quiet:
            logger.info("[GPU] 内核编译完成 | batch=%s", f"{self.batch_size:,}")

    def _fill_privkeys(self, privkey_bytes: np.ndarray | None = None):
        """填充私钥缓冲区。可以传入指定私钥，否则根据 mode 自动生成。

        Args:
            privkey_bytes: (batch*32,) uint8 array 或 None（按 mode 自动生成）
        """
        if privkey_bytes is not None:
            self._h_privkeys[: len(privkey_bytes)] = privkey_bytes
            return

        if self.mode == "sequential":
            self._fill_sequential_privkeys()
        else:
            self._fill_random_privkeys()

    def _fill_random_privkeys(self) -> None:
        """生成 batch_size 个随机私钥并确保在 [1, N-1] 范围内。"""
        rand_bytes = os.urandom(self.batch_size * 32)
        self._h_privkeys[:] = np.frombuffer(rand_bytes, dtype=np.uint8)

        # 限制首字节 ≤ 0xFE 确保值 < N（浪费 ~0.4% keyspace）
        first_bytes = self._h_privkeys[::32]
        clamp_mask = first_bytes >= 0xFE
        n_clamp = int(np.sum(clamp_mask))
        if n_clamp > 0:
            self._h_privkeys[::32][clamp_mask] = np.random.randint(
                1,
                0xFE,
                size=n_clamp,
                dtype=np.uint8,
            )

        # 修复全零私钥（概率 ~2^-248，安全兜底）
        view = self._h_privkeys.reshape(-1, 32)
        zero_mask = np.all(view == 0, axis=1)
        if np.any(zero_mask):
            view[zero_mask, 0] = 1

    def _fill_sequential_privkeys(self) -> None:
        """填充 batch_size 个顺序递增的私钥（小端编码），从 _seq_start 开始。"""
        start = self._seq_start
        # numpy uint64 支持 64 位索引计算
        batch = self.batch_size
        data = np.zeros(batch * 32, dtype=np.uint8)
        view = data.reshape(batch, 32)

        # 使用 int.to_bytes 逐私钥填充小端 32 字节
        for i in range(batch):
            val = start + i
            if val >= 0xFFFFFFFFFFFFFFFF:
                # 超过 uint64 范围需用 Python int
                view[i] = np.frombuffer(
                    int.to_bytes(val, 32, "little"),
                    dtype=np.uint8,
                )
            else:
                # 常用范围：直接用小端编码
                buf = val.to_bytes(8, "little")
                view[i, :8] = np.frombuffer(buf, dtype=np.uint8)
                # 剩余 24 字节已初始化为 0

        self._h_privkeys[:] = data

    @property
    def sequential_start(self) -> int:
        """当前顺序扫描起始值（外部用于 checkpoint）。"""
        return self._seq_start

    @sequential_start.setter
    def sequential_start(self, val: int):
        self._seq_start = val

    def submit_batch(
        self,
        check_collision: Callable[[bytes], bool] | None = None,
        mode: str | None = None,
        sequential_start: int | None = None,
        privkey_bytes: np.ndarray | None = None,
    ) -> BatchResult:
        """提交一个 batch 到 GPU 执行。

        若启用了 TDR 安全模式 (tdr_safe=True)，会自动将大 batch
        拆分为多个安全的 sub-batch，确保每个 sub-batch 内核执行
        时间不超过 max_kernel_time 秒。

        Args:
            check_collision: 可选的回调函数，用于逐个检查 HASH160 是否命中目标集
            mode: 覆盖实例的 mode（"random" 或 "sequential"）
            sequential_start: 覆盖实例的起始值（顺序模式下）

        Returns:
            BatchResult 包含执行统计和命中索引
        """
        import pyopencl as cl

        if mode is not None:
            self.mode = mode
        if sequential_start is not None:
            self._seq_start = sequential_start

        t0 = time.perf_counter()

        # 生成所有私钥（根据 mode，先填满 host 缓冲区）
        self._fill_privkeys(privkey_bytes)

        # 将全部私钥写入设备
        write_evt = cl.enqueue_copy(self._queue, self._d_privkeys, self._h_privkeys)
        write_evt.wait()

        # --- 确定 sub-batch 大小（TDR 安全） ---
        if self._tdr_safe and self._timer.is_calibrated:
            sub_batch = self._timer.safe_sub_batch_size(
                max_time_ms=self._max_kernel_time * 1000,
                min_size=self._tdr_config.min_sub_batch,
            )
            sub_batch = min(sub_batch, self.batch_size)
        else:
            sub_batch = self.batch_size

        # 设置 kernel 参数（指向完整缓冲区，batch_size 传给 kernel 用于范围检查）
        self._kernel_hash160.set_args(
            self._d_privkeys, self._d_hash160s, np.uint32(self.batch_size)
        )

        # 如果没有校准且启用了 TDR 安全，运行一次小校准
        _ran_calib = False
        calib_size = 0
        if self._tdr_safe and not self._timer.is_calibrated and self.batch_size > 256:
            _ran_calib = True
            calib_size = min(self._tdr_config.calibration_keys, self.batch_size)
            try:
                self._run_sub_batch(0, calib_size)
                calib_elapsed = time.perf_counter() - t0
                self._timer.update(calib_elapsed, calib_size)
                # 校准后重新计算 sub-batch 大小
                sub_batch = self._timer.safe_sub_batch_size(
                    max_time_ms=self._max_kernel_time * 1000,
                    min_size=self._tdr_config.min_sub_batch,
                )
                sub_batch = min(sub_batch, self.batch_size)
            except Exception:
                _ran_calib = False  # 校准失败，回退到全 batch

        # --- 拆分执行 sub-batches ---
        processed = calib_size if _ran_calib else 0
        while processed < self.batch_size:
            remaining = self.batch_size - processed
            cur_size = min(sub_batch, remaining)
            try:
                self._run_sub_batch(processed, cur_size)
            except Exception as e:
                tdr_err = is_tdr_error(e)
                if tdr_err and self._tdr_safe:
                    # TDR 错误：激进降低 sub-batch 大小并重试
                    new_size = max(64, cur_size // 4)
                    if not self.quiet:
                        logger.warning(
                            "[GPU][TDR] 检测到 TDR 超时 (sub_batch=%s)，"
                            "降低到 %s 并重试",
                            cur_size,
                            new_size,
                        )
                    # 等待 GPU 恢复
                    time.sleep(2.0)
                    # 重试
                    sub_batch = new_size
                    continue  # 不推进 processed，重试当前范围
                elif tdr_err:
                    # TDR 但未启用安全模式
                    if not self.quiet:
                        logger.error("[GPU][TDR] TDR 超时: %s", e)
                    raise
                else:
                    # 非 TDR 错误：直接抛出
                    raise
            processed += cur_size

        # 回读全部结果
        read_evt = cl.enqueue_copy(
            self._queue,
            self._h_hash160s,
            self._d_hash160s,
        )
        read_evt.wait()

        t1 = time.perf_counter()
        elapsed = t1 - t0

        # 更新计时器（全校准，排除校准本身的批次）
        if not _ran_calib:
            # 用完整的批次时间更新
            self._timer.update(elapsed, self.batch_size)
        elif not self._timer.is_calibrated:
            # 校准运行过但失败了？忽略
            pass

        # 检查碰撞
        hit_indices: list[int] = []
        if check_collision is not None:
            for i in range(self.batch_size):
                h160 = bytes(self._h_hash160s[i * 20 : (i + 1) * 20])
                if check_collision(h160):
                    hit_indices.append(i)

        # 顺序模式下自动推进起始值（按 stride 步进支持多 GPU 分区）
        if self.mode == "sequential":
            self._seq_start += self._seq_stride

        keys_checked = self.batch_size
        return BatchResult(
            keys_checked=keys_checked,
            elapsed=elapsed,
            keys_per_sec=keys_checked / elapsed if elapsed > 0 else 0,
            hash160s=self._h_hash160s.copy(),
            privkey_bytes=self._h_privkeys.copy(),
            hit_indices=hit_indices,
        )

    def _run_sub_batch(self, offset: int, count: int) -> None:
        """执行单个 sub-batch 的内核。

        利用 global_work_offset 让每个工作项读取正确的私钥并写入
        正确的 HASH160 输出位置，无需修改内核或创建子缓冲区。

        Args:
            offset: 该 sub-batch 在完整 batch 中的起始索引
            count: 该 sub-batch 的工作项数
        """
        import pyopencl as cl

        kernel_evt = cl.enqueue_nd_range_kernel(
            self._queue,
            self._kernel_hash160,
            (count,),  # global_work_size
            None,  # local_work_size
            (offset,),  # global_work_offset
            None,  # wait_for
        )
        kernel_evt.wait()

    def get_privkey_for_index(
        self,
        batch_result: BatchResult,
        index: int,
    ) -> bytes:
        """从 batch 结果中提取指定索引的私钥字节。"""
        return bytes(batch_result.privkey_bytes[index * 32 : (index + 1) * 32])

    def close(self) -> None:
        """释放 GPU 资源。"""
        for buf in [self._d_privkeys, self._d_hash160s, self._d_pubkeys]:
            if buf is not None:
                buf = None
        if self._queue is not None:
            self._queue.finish()
        if self._ctx is not None:
            self._ctx = None

    def __enter__(self) -> GPUPipeline:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _demo():
    """快速测试：初始化 GPU 管道并运行 1 个 batch。"""
    print("GPU Pipeline 快速测试\n")
    pipe = GPUPipeline(batch_size=4096, quiet=False)
    try:
        result = pipe.submit_batch()
        print(f"\n[结果] {result.keys_checked:,} keys in {result.elapsed:.2f}s")
        print(f"       速率: {result.keys_per_sec:,.0f} keys/s")
    finally:
        pipe.close()


if __name__ == "__main__":
    _demo()
