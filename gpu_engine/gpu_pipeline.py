#!/usr/bin/env python3
"""GPU 管道 - pyopencl 实际调用链..

流程:
  1. 生成 batch_size 个随机私钥 (32B × batch)
  2. 写入 GPU 显存
  3. 启动 ec_mul_hash160 kernel → 直接得到 HASH160(20B × batch)
  4. 回读结果到 Host
  5. P2-10: 若提供 bloom 数据则在 GPU 侧碰撞检测,否则 Host 端二分查找
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import numpy as np

logger = logging.getLogger(__name__)

from .gpu_device import DeviceInfo
from .tdr_handler import KernelTimer, TDRConfig, is_tdr_error

if TYPE_CHECKING:
    from collections.abc import Callable

KERNEL_SRC_FILE = Path(__file__).parent / "gpu_kernel.h"


@dataclass
class BatchResult:
    """单个 GPU batch 的执行结果.."""

    keys_checked: int
    elapsed: float  # seconds
    keys_per_sec: float
    hash160s: np.ndarray  # (batch*20,) flattened uint8 array
    privkey_bytes: np.ndarray  # (batch*32,) flattened uint8 array
    hit_indices: list[int] = field(default_factory=list)


class GPUPipeline:
    """GPU 计算管道:管理 OpenCL 上下文,内核和执行.."""

    def __init__(
        self,
        device_index: int | None = None,
        platform_index: int | None = None,
        batch_size: int = 65536,
        profile: bool = False,  # noqa: FBT001, FBT002
        quiet: bool = False,  # noqa: FBT001, FBT002
        mode: str = "random",
        sequential_start: int = 1,
        sequential_stride: int | None = None,
        tdr_safe: bool = True,  # noqa: FBT001, FBT002
        max_kernel_time: float = 1.5,
        bloom_data: bytes | None = None,  # P2-10: GPU 侧碰撞检测 bloom 位数组
        bloom_m: int = 0,  # P2-10: bloom 总位数
    ) -> None:
        """初始化 GPU 管道:配置参数,创建 OpenCL 上下文,编译内核..

        Args:
            bloom_data: P2-10 GPU 侧碰撞检测的 bloom 位数组(None=使用 host 检测)
            bloom_m: bloom 位数组总位数

        """
        self.batch_size = batch_size
        self.quiet = quiet
        self.profile = profile
        self.mode = mode
        self._seq_start = sequential_start
        # stride: 多 GPU 时每个 worker 每批推进量(单 GPU = batch_size)
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
        # P2-10: GPU 侧碰撞检测 bloom 数据
        self._bloom_data = bloom_data
        self._bloom_m = bloom_m

        self._ctx: Any = None
        self._queue: Any = None
        self._program: Any = None
        self._kernel_hash160: Any = None
        self._kernel_pubkey: Any = None
        self._kernel_collision: Any = None  # P2-10: 碰撞检测 kernel
        self._d_privkeys: Any = None
        self._d_hash160s: Any = None
        self._d_pubkeys: Any = None
        # P2-10: bloom / hit 设备缓冲区
        self._d_bloom: Any = None
        self._d_hit_count: Any = None
        self._d_hit_buffer: Any = None
        self._h_hit_count = np.zeros(1, dtype=np.int32)
        self._h_hit_buffer: np.ndarray | None = None

        self._h_privkeys = np.empty(batch_size * 32, dtype=np.uint8)
        self._h_hash160s = np.zeros(batch_size * 20, dtype=np.uint8)
        # 按 vendor 优化(_init_opencl 中设置)
        self._local_ws: int | None = None
        self._kernel_build_options: list[str] = ["-cl-std=CL1.2"]

        self._init_opencl(device_index, platform_index)

    def _init_opencl(
        self,
        device_index: int | None,
        platform_index: int | None,
    ) -> None:
        """初始化 OpenCL 上下文和命令队列.."""
        try:
            import pyopencl as cl
        except ImportError:
            logger.exception(
                "[GPU] pyopencl 未安装.请先安装: pip install pyopencl>=2024.1",
            )
            raise

        platforms = cl.get_platforms()
        if not platforms:
            msg = "未找到任何 OpenCL 平台"
            raise RuntimeError(msg)

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
            logger.info("[GPU] 设备: %s | vendor=%s", info, self._device.vendor.strip())

        # --- P0: 按 vendor 自动优化 ---
        vendor = self._device.vendor.lower()
        if "nvidia" in vendor:
            self._local_ws = 128
            self._kernel_build_options = [
                "-cl-std=CL1.2",
                "-cl-mad-enable",
                "-cl-fast-relaxed-math",
            ]
        elif "intel" in vendor:
            self._local_ws = 64
            self._kernel_build_options = [
                "-cl-std=CL3.0",
                "-cl-mad-enable",
                "-DARC_OPT",
            ]
        else:
            self._local_ws = 64
            self._kernel_build_options = ["-cl-std=CL1.2"]

        # --- P0: 检查 max_mem_alloc_size 限制 ---
        max_alloc = self._device.max_mem_alloc_size
        max_safe_batch = int(max_alloc // (32 + 20))  # 每工作项 32B + 20B
        if self.batch_size > max_safe_batch:
            logger.warning(
                "[GPU] batch_size %s 超过设备最大分配 %s,降为 %s",
                f"{self.batch_size:,}",
                f"{max_alloc / 1e9:.1f}GB",
                f"{max_safe_batch:,}",
            )
            self.batch_size = int(max_safe_batch)
            # 重新分配 host 缓冲区
            self._h_privkeys = np.empty(self.batch_size * 32, dtype=np.uint8)
            self._h_hash160s = np.zeros(self.batch_size * 20, dtype=np.uint8)

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

        # 编译 kernel(使用按 vendor 优化的编译选项)
        if not KERNEL_SRC_FILE.exists():
            msg = f"GPU kernel 源文件未找到: {KERNEL_SRC_FILE}"
            raise FileNotFoundError(msg)
        src = KERNEL_SRC_FILE.read_text(encoding="utf-8")
        if not self.quiet:
            logger.info("[GPU] 编译选项: %s", " ".join(self._kernel_build_options))
        self._program = cl.Program(self._ctx, src).build(
            options=self._kernel_build_options,
        )

        # 获取 kernel 函数
        self._kernel_hash160 = self._program.ec_mul_hash160
        self._kernel_pubkey = self._program.ec_mul_pubkey
        try:
            self._kernel_collision = self._program.ec_mul_hash160_collision
        except Exception:  # noqa: BLE001
            self._kernel_collision = None  # 旧 kernel 文件不含碰撞检测

        # 分配设备端缓冲区(ALLOC_HOST_PTR 减少 PCIe 拷贝,不能与 USE_HOST_PTR 混用)
        mf = cl.mem_flags
        self._d_privkeys = cl.Buffer(
            self._ctx,
            mf.READ_ONLY | mf.ALLOC_HOST_PTR,
            size=self.batch_size * 32,
            hostbuf=self._h_privkeys,
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

        # --- P2-10: 分配 Bloom/命中缓冲区 ---
        if self._bloom_data is not None and self._kernel_collision is not None:
            bloom_bytes = (self._bloom_m + 7) // 8
            self._d_bloom = cl.Buffer(
                self._ctx,
                mf.READ_ONLY | mf.COPY_HOST_PTR,
                size=bloom_bytes,
                hostbuf=np.frombuffer(self._bloom_data, dtype=np.uint8),
            )
            self._d_hit_count = cl.Buffer(
                self._ctx,
                mf.READ_WRITE | mf.ALLOC_HOST_PTR,
                size=4,  # int32
            )
            self._d_hit_buffer = cl.Buffer(
                self._ctx,
                mf.WRITE_ONLY | mf.ALLOC_HOST_PTR,
                size=self.batch_size * 4,  # uint per hit
            )
            self._h_hit_buffer = np.zeros(self.batch_size, dtype=np.int32)

        if not self.quiet:
            logger.info("[GPU] 内核编译完成 | batch=%s", f"{self.batch_size:,}")

    def _fill_privkeys(self, privkey_bytes: np.ndarray | None = None) -> None:
        """填充私钥缓冲区.可以传入指定私钥,否则根据 mode 自动生成..

        Args:
            privkey_bytes: (batch*32,) uint8 array 或 None(按 mode 自动生成)

        """
        if privkey_bytes is not None:
            n = len(privkey_bytes)
            self._h_privkeys[:n] = privkey_bytes
            # 清除剩余缓冲区,防止旧数据残留
            if n < len(self._h_privkeys):
                self._h_privkeys[n:] = 0
            return

        if self.mode == "sequential":
            self._fill_sequential_privkeys()
        else:
            self._fill_random_privkeys()

    def _fill_random_privkeys(self) -> None:
        """生成 batch_size 个随机私钥并确保在 [1, N-1] 范围内.."""
        rand_bytes = os.urandom(self.batch_size * 32)
        self._h_privkeys[:] = np.frombuffer(rand_bytes, dtype=np.uint8)

        # 限制 LSB(小端第一字节)≤ 0xFE 以确保 256 位值 < 阶 N(浪费 ~0.4%)
        first_bytes = self._h_privkeys[::32]
        _clamp_limit = 0xFE
        clamp_mask = first_bytes >= _clamp_limit
        n_clamp = int(np.sum(clamp_mask))
        if n_clamp > 0:
            self._h_privkeys[::32][clamp_mask] = np.random.randint(
                1,
                0xFE,
                size=n_clamp,
                dtype=np.uint8,
            )

        # 修复全零私钥(概率 ~2^-248,安全兜底)
        view = self._h_privkeys.reshape(-1, 32)
        zero_mask = np.all(view == 0, axis=1)
        if np.any(zero_mask):
            logger.warning("修复 %d 个全零私钥 -> 设为 1", int(np.sum(zero_mask)))
            view[zero_mask, 0] = 1

    def _fill_sequential_privkeys(self) -> None:
        """填充 batch_size 个顺序递增的私钥(小端编码),从 _seq_start 开始.."""
        start = self._seq_start
        # numpy uint64 支持 64 位索引计算
        batch = self.batch_size
        data = np.zeros(batch * 32, dtype=np.uint8)
        view = data.reshape(batch, 32)

        # 使用 int.to_bytes 逐私钥填充小端 32 字节
        for i in range(batch):
            val = start + i
            _uint64_max = 0xFFFFFFFFFFFFFFFF
            if val >= _uint64_max:
                # 超过 uint64 范围需用 Python int
                view[i] = np.frombuffer(
                    int.to_bytes(val, 32, "little"),
                    dtype=np.uint8,
                )
            else:
                # 常用范围:直接用小端编码
                buf = val.to_bytes(8, "little")
                view[i, :8] = np.frombuffer(buf, dtype=np.uint8)
                # 剩余 24 字节已初始化为 0

        self._h_privkeys[:] = data

    @property
    def sequential_start(self) -> int:
        """当前顺序扫描起始值(外部用于 checkpoint).."""
        return self._seq_start

    @sequential_start.setter
    def sequential_start(self, val: int) -> None:
        """设置顺序扫描起始值(用于 checkpoint 恢复).."""
        self._seq_start = val

    def submit_batch(
        self,
        check_collision: Callable[[bytes], bool] | None = None,
        mode: str | None = None,
        sequential_start: int | None = None,
        privkey_bytes: np.ndarray | None = None,
    ) -> BatchResult:
        """提交一个 batch 到 GPU 执行..

        若启用了 TDR 安全模式 (tdr_safe=True),会自动将大 batch
        拆分为多个安全的 sub-batch,确保每个 sub-batch 内核执行
        时间不超过 max_kernel_time 秒.

        Args:
            check_collision: 可选的回调函数,用于逐个检查 HASH160 是否命中目标集
            mode: 覆盖实例的 mode("random" 或 "sequential")
            sequential_start: 覆盖实例的起始值(顺序模式下)

        Returns:
            BatchResult 包含执行统计和命中索引

        """
        import pyopencl as cl

        if mode is not None:
            self.mode = mode
        if sequential_start is not None:
            self._seq_start = sequential_start

        t0 = time.perf_counter()

        # 生成所有私钥(根据 mode,先填满 host 缓冲区)
        self._fill_privkeys(privkey_bytes)

        # 判断是否使用 GPU 侧碰撞检测
        use_gpu_collision = (
            self._bloom_data is not None
            and self._kernel_collision is not None
            and check_collision is None  # 外部未传 collision 回调时自动启用
        )

        # 将全部私钥写入设备
        write_evt = cl.enqueue_copy(self._queue, self._d_privkeys, self._h_privkeys)
        write_evt.wait()

        # --- P2-10: GPU 碰撞检测时初始化命中计数器 ---
        if use_gpu_collision:
            self._queue.enqueue_write_buffer(
                self._d_hit_count,
                np.zeros(1, dtype=np.int32),
            )

        # --- 确定 sub-batch 大小(TDR 安全) ---
        if self._tdr_safe and self._timer.is_calibrated:
            sub_batch = self._timer.safe_sub_batch_size(
                max_time_ms=self._max_kernel_time * 1000,
                min_size=self._tdr_config.min_sub_batch,
            )
            sub_batch = min(sub_batch, self.batch_size)
        else:
            sub_batch = self.batch_size

        # 设置 kernel 参数
        if use_gpu_collision:
            self._kernel_collision.set_args(
                self._d_privkeys,
                self._d_hash160s,
                np.uint32(self.batch_size),
                self._d_bloom,
                np.uint32(self._bloom_m),
                self._d_hit_count,
                self._d_hit_buffer,
            )
        else:
            self._kernel_hash160.set_args(
                self._d_privkeys,
                self._d_hash160s,
                np.uint32(self.batch_size),
            )

        # 如果没有校准且启用了 TDR 安全,运行一次小校准
        _ran_calib = False
        calib_size = 0
        _calib_min_batch = 256
        if (
            self._tdr_safe
            and not self._timer.is_calibrated
            and self.batch_size > _calib_min_batch
        ):
            _ran_calib = True
            calib_size = min(self._tdr_config.calibration_keys, self.batch_size)
            try:
                self._run_sub_batch(0, calib_size)
                calib_elapsed = time.perf_counter() - t0
                self._timer.update(calib_elapsed, calib_size)
                sub_batch = self._timer.safe_sub_batch_size(
                    max_time_ms=self._max_kernel_time * 1000,
                    min_size=self._tdr_config.min_sub_batch,
                )
                sub_batch = min(sub_batch, self.batch_size)
            except Exception:  # noqa: BLE001
                _ran_calib = False

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
                    new_size = max(64, cur_size // 4)
                    if not self.quiet:
                        logger.warning(
                            "[GPU][TDR] 检测到 TDR 超时 (sub_batch=%s),"
                            "降低到 %s 并重试",
                            cur_size,
                            new_size,
                        )
                    time.sleep(2.0)
                    sub_batch = new_size
                    continue
                if tdr_err:
                    if not self.quiet:
                        logger.exception("[GPU][TDR] TDR 超时")
                    raise
                raise
            processed += cur_size

        # 回读结果
        if use_gpu_collision:
            # P2-10: 读回命中计数和索引
            self._queue.enqueue_read_buffer(self._d_hit_count, self._h_hit_count).wait()
            n_hits = int(self._h_hit_count[0])
            n_hits = min(n_hits, self.batch_size)
            if n_hits > 0:
                if self._h_hit_buffer is None:
                    msg = "命中缓冲未初始化,但 n_hits>0"
                    raise RuntimeError(msg)
                hit_buf = self._h_hit_buffer[:n_hits]
                self._queue.enqueue_read_buffer(self._d_hit_buffer, hit_buf).wait()
                hit_indices_on_gpu = hit_buf.tolist()
            else:
                hit_indices_on_gpu = []

        # 回读 HASH160(GPU 碰撞模式也要回读用于验证)
        cl.enqueue_copy(self._queue, self._h_hash160s, self._d_hash160s).wait()

        t1 = time.perf_counter()
        elapsed = t1 - t0

        # 更新计时器
        if not _ran_calib:
            self._timer.update(elapsed, self.batch_size)
        elif not self._timer.is_calibrated:
            pass

        # 检查碰撞
        hit_indices: list[int] = []
        if use_gpu_collision:
            hit_indices = hit_indices_on_gpu
        elif check_collision is not None:
            h160_view = self._h_hash160s.reshape(-1, 20)
            for i in range(self.batch_size):
                if check_collision(bytes(h160_view[i])):
                    hit_indices.append(i)

        # 顺序模式下自动推进起始值(按 stride 步进支持多 GPU 分区)
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
        """执行单个 sub-batch 的内核..

        利用 global_work_offset 让每个工作项读取正确的私钥并写入
        正确的 HASH160 输出位置,无需修改内核或创建子缓冲区.
        自动使用碰撞检测 kernel(P2-10)或标准 kernel.

        Args:
            offset: 该 sub-batch 在完整 batch 中的起始索引
            count: 该 sub-batch 的工作项数

        """
        import pyopencl as cl

        # P2-10: 选择正确的 kernel
        kernel = (
            self._kernel_collision
            if self._bloom_data is not None and self._kernel_collision is not None
            else self._kernel_hash160
        )

        # 确保 global_size 能被 local_ws 整除
        lws = self._local_ws
        gws = (count + lws - 1) // lws * lws if lws is not None else count

        kernel_evt = cl.enqueue_nd_range_kernel(
            self._queue,
            kernel,
            (gws,),
            (lws,) if lws is not None else None,
            (offset,),
            None,
        )
        kernel_evt.wait()

    def get_privkey_for_index(
        self,
        batch_result: BatchResult,
        index: int,
    ) -> bytes:
        """从 batch 结果中提取指定索引的 32 字节私钥.."""
        return bytes(batch_result.privkey_bytes[index * 32 : (index + 1) * 32])

    def close(self) -> None:
        """释放 GPU 资源(显式调用 release() 确保底层 OpenCL 资源释放).."""
        for buf_name in (
            "_d_privkeys",
            "_d_hash160s",
            "_d_pubkeys",
            "_d_bloom",
            "_d_hit_count",
            "_d_hit_buffer",
        ):
            buf = getattr(self, buf_name, None)
            if buf is not None:
                try:
                    buf.release()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, buf_name, None)
        if self._queue is not None:
            try:
                self._queue.release()
            except Exception:  # noqa: BLE001
                self._queue.finish()
            self._queue = None
        if self._ctx is not None:
            try:
                self._ctx.release()
            except Exception:  # noqa: BLE001
                pass
            self._ctx = None

    def __enter__(self) -> Self:
        """上下文管理器入口,返回自身.."""
        return self

    def __exit__(self, *args: object) -> None:
        """上下文管理器出口,释放 GPU 资源.."""
        self.close()


def _demo() -> None:
    """快速测试:初始化 GPU 管道并运行 1 个 batch.."""
    pipe = GPUPipeline(batch_size=4096, quiet=False)
    try:
        pipe.submit_batch()
    finally:
        pipe.close()


if __name__ == "__main__":
    _demo()
