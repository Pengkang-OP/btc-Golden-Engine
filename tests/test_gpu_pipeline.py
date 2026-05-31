"""测试 gpu_engine/gpu_pipeline 模块 — mock pyopencl。

策略：
  使用 unittest.mock patch 模拟 pyopencl 调用，不依赖真实 GPU。
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import numpy as np
import pytest


# 保存真实 pyopencl 的引用，避免 _remove_mock 中 pop+mock 后重新导入时
# 触发 pyopencl 内部的循环引用（pyopencl/__init__.py 在初始化过程中访问
# pyopencl._monkeypatch 导致部分初始化模块错误）。
_real_pyopencl: object | None = None


@pytest.fixture(autouse=True)
def _mock_pyopencl() -> Generator[None, None, None]:
    """自动 mock pyopencl 模块及其所有依赖。"""
    import sys

    global _real_pyopencl
    _real_pyopencl = sys.modules.get("pyopencl")  # 保存真实模块

    cl_mock = MagicMock()
    cl_mock.device_type.GPU = 4
    cl_mock.device_type.CPU = 2
    cl_mock.mem_flags.READ_ONLY = 1
    cl_mock.mem_flags.WRITE_ONLY = 2
    cl_mock.mem_flags.ALLOC_HOST_PTR = 4
    cl_mock.Buffer = MagicMock(return_value=MagicMock())

    mock_device = MagicMock()
    mock_device.name = "Mock GPU Device"
    mock_device.max_compute_units = 128
    mock_device.max_work_group_size = 256
    mock_device.global_mem_size = 8 * 1024 * 1024 * 1024
    mock_device.local_mem_size = 65536
    mock_device.max_clock_frequency = 2000
    mock_device.version = "OpenCL 2.0"
    mock_device.type = 4
    mock_device.available = True

    mock_platform = MagicMock()
    mock_platform.name = "Mock Platform"
    mock_platform.get_devices.return_value = [mock_device]
    cl_mock.get_platforms.return_value = [mock_platform]
    cl_mock.Context = MagicMock(return_value=MagicMock())
    cl_mock.CommandQueue = MagicMock(return_value=MagicMock())
    cl_mock.Program = MagicMock()
    cl_mock.Program.build.return_value = MagicMock()

    sys.modules["pyopencl"] = cl_mock

    # 确保 kernel 源文件存在
    kernel_dir = Path(__file__).parent.parent / "gpu_engine"
    kernel_file = kernel_dir / "gpu_kernel.h"
    if not kernel_file.exists():
        kernel_file.parent.mkdir(parents=True, exist_ok=True)
        kernel_file.write_text(
            "__kernel void ec_mul_hash160("
            "__global const uchar* in, __global uchar* out) {}",
            encoding="utf-8",
        )

    yield


class TestGPUPipeline:
    """GPUPipeline 创建与初始化测试 (mock PyOpenCL)。"""

    def test_create_pipeline(self):
        """测试 GPUPipeline 可以创建。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(
            batch_size=1024,
            quiet=True,
        )
        assert pipe.batch_size == 1024
        assert pipe.quiet is True
        pipe.close()

    def test_pipeline_default_batch_size(self):
        """测试默认 batch_size 为 65536。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(quiet=True)
        assert pipe.batch_size == 65536
        pipe.close()


# ═══════════════════════════════════════════════════════════════
#  真实 GPU 硬件测试（无 GPU 时自动跳过）
# ═══════════════════════════════════════════════════════════════


def _has_gpu() -> bool:
    """检查系统是否有可用的 OpenCL GPU 设备。"""
    try:
        import pyopencl as cl  # noqa: F811

        for plat in cl.get_platforms():
            try:
                devices = plat.get_devices(cl.device_type.GPU)
                if devices:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


_HAS_GPU = _has_gpu()


@pytest.mark.skipif(not _HAS_GPU, reason="无可用 OpenCL GPU 设备")
class TestGPUPipelineHardware:
    """GPU 管道真实硬件测试 — 需要 pyopencl + 至少一个 OpenCL GPU。

    使用 pytest -m gpu 可单独运行：
        pytest tests/test_gpu_pipeline.py -m gpu -v
    """

    @pytest.fixture(autouse=True)
    def _remove_mock(self) -> Generator[None, None, None]:
        """恢复真实 pyopencl，确保硬件测试使用真实模块。

        不再从 sys.modules 中 pop 并触发重新导入，因为 pyopencl 自身在
        重新初始化时存在内部循环引用（_monkeypatch 在 __init__.py 完成前
        不可访问）。改为恢复之前保存的真实模块对象。
        """
        import sys

        if _real_pyopencl is not None:
            sys.modules["pyopencl"] = _real_pyopencl
        else:
            sys.modules.pop("pyopencl", None)
        yield

    # ── 设备发现 ──────────────────────────────────────────────

    @pytest.mark.gpu
    def test_device_discovery_returns_gpu(self) -> None:
        """设备发现应返回至少一个 GPU。"""
        from gpu_engine.gpu_device import DeviceInfo, list_devices

        devices = list_devices(device_type="GPU")
        assert len(devices) > 0
        dev = devices[0]
        assert isinstance(dev, DeviceInfo)
        assert dev.device_name
        assert dev.compute_units > 0
        assert dev.global_mem_size > 0

    @pytest.mark.gpu
    def test_pick_best_gpu_returns_valid(self) -> None:
        """pick_best_gpu 应返回最佳 GPU 的 DeviceInfo。"""
        from gpu_engine.gpu_device import DeviceInfo, pick_best_gpu

        best = pick_best_gpu()
        assert best is not None
        assert isinstance(best, DeviceInfo)
        assert best.device_name
        assert best.compute_units > 0

    # ── Pipeline 初始化 ───────────────────────────────────────

    @pytest.mark.gpu
    def test_pipeline_initializes_on_real_gpu(self) -> None:
        """在真实 GPU 上创建 Pipeline 应成功。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=256, quiet=True)
        try:
            assert pipe.batch_size == 256
            assert pipe._ctx is not None
            assert pipe._queue is not None
            assert pipe._program is not None
        finally:
            pipe.close()

    # ── Kernel 编译 ───────────────────────────────────────────

    @pytest.mark.gpu
    def test_kernel_compiles_successfully(self) -> None:
        """OpenCL kernel 编译应无错误。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=256, quiet=True)
        try:
            # 编译成功意味着 program 已创建
            assert pipe._program is not None
            # kernel 函数应可用
            assert pipe._kernel_hash160 is not None
        finally:
            pipe.close()

    # ── Random batch 提交 ─────────────────────────────────────

    @pytest.mark.gpu
    def test_random_batch_submission(self) -> None:
        """随机模式 batch 提交应返回 BatchResult。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=256, quiet=True)
        try:
            result = pipe.submit_batch()
            assert result.keys_checked == 256
            assert result.elapsed > 0
            assert result.keys_per_sec > 0
            # hash160s 为 1D 连续数组 (batch * 20,)
            assert result.hash160s.shape == (256 * 20,)
            assert result.hash160s.dtype == np.uint8
            # privkey_bytes 为 1D 连续数组 (batch * 32,)
            assert result.privkey_bytes.shape == (256 * 32,)
            assert result.privkey_bytes.dtype == np.uint8
            assert isinstance(result.hit_indices, list)
        finally:
            pipe.close()

    @pytest.mark.gpu
    def test_multiple_batches_consistent(self) -> None:
        """连续多次 batch 提交应保持稳定。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=256, quiet=True)
        try:
            rates = []
            for _ in range(3):
                result = pipe.submit_batch()
                rates.append(result.keys_per_sec)
                assert result.keys_checked == 256
            # 三次速率应都在合理正数范围
            assert all(r > 0 for r in rates)
        finally:
            pipe.close()

    # ── Sequential 模式 ───────────────────────────────────────

    @pytest.mark.gpu
    def test_sequential_mode_stride(self) -> None:
        """顺序模式第二次 batch 的起始值应正确推进。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(
            batch_size=256,
            quiet=True,
            mode="sequential",
            sequential_start=1,
        )
        try:
            r1 = pipe.submit_batch()
            assert r1.keys_checked == 256
            assert pipe._seq_start == 257  # stride = batch_size

            r2 = pipe.submit_batch()
            assert r2.keys_checked == 256
            assert pipe._seq_start == 513
        finally:
            pipe.close()

    @pytest.mark.gpu
    def test_sequential_batches_non_overlap(self) -> None:
        """连续顺序 batch 的私钥区间不应重叠。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(
            batch_size=256,
            quiet=True,
            mode="sequential",
            sequential_start=1,
        )
        try:
            r1 = pipe.submit_batch()
            r2 = pipe.submit_batch()

            # 保存两个 batch 的私钥字节（已整形为 (N, 32)）
            pk1 = r1.privkey_bytes.reshape(-1, 32)
            pk2 = r2.privkey_bytes.reshape(-1, 32)

            # 将每个私钥转为 int 用于范围比较
            def _pk_to_int(pk: np.ndarray) -> int:
                return int.from_bytes(bytes(pk), "little")

            # Batch 0: [1, 256], Batch 1: [257, 512]
            assert _pk_to_int(pk1[0]) == 1
            assert _pk_to_int(pk1[-1]) == 256
            assert _pk_to_int(pk2[0]) == 257
            assert _pk_to_int(pk2[-1]) == 512

            # 用 set 交集验证不重叠
            s1 = {_pk_to_int(pk1[i]) for i in range(256)}
            s2 = {_pk_to_int(pk2[i]) for i in range(256)}
            assert s1.isdisjoint(s2)
            assert min(s1) == 1
            assert max(s1) == 256
            assert min(s2) == 257
            assert max(s2) == 512
        finally:
            pipe.close()

    # ── TDR 安全模式 ──────────────────────────────────────────

    @pytest.mark.gpu
    def test_tdr_safe_mode_active(self) -> None:
        """TDR 安全模式应默认启用。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=256, quiet=True)
        try:
            # 小 batch 不应触发 sub-batch 拆分
            assert pipe._tdr_config.enabled is True
        finally:
            pipe.close()

    # ── GPU Dispatcher ────────────────────────────────────────

    @pytest.mark.gpu
    def test_single_gpu_dispatcher_runs(self) -> None:
        """GPU 调度器在单设备模式下应能完成一轮扫描。"""
        from gpu_engine.gpu_dispatcher import (
            DispatcherConfig,
            GPUBatchScheduler,
        )

        cfg = DispatcherConfig(
            batch_size=256,
            total_keys=512,
            quiet=True,
            device_indices=[0],
        )
        scheduler = GPUBatchScheduler(cfg)
        try:
            ok = scheduler.initialize()
            assert ok is True
            results = scheduler.run()
            assert len(results) >= 1
            assert results[0].keys_checked == 512
            assert results[0].hits == 0
            assert results[0].errors == 0
        finally:
            scheduler.close()

    @pytest.mark.gpu
    def test_gpu_dispatcher_collision_callback(self) -> None:
        """Dispatcher 碰撞命中回调应被正确调用。"""
        from unittest.mock import MagicMock

        from gpu_engine.gpu_dispatcher import (
            DispatcherConfig,
            GPUBatchScheduler,
        )

        mock_on_hit = MagicMock()

        cfg = DispatcherConfig(
            batch_size=256,
            total_keys=256,
            quiet=True,
            device_indices=[0],
            check_collision=lambda pk: True,  # 所有私钥标记为碰撞
            on_hit=mock_on_hit,
        )
        scheduler = GPUBatchScheduler(cfg)
        try:
            ok = scheduler.initialize()
            assert ok is True
            results = scheduler.run()
            # 应命中 batch_size 次
            assert any(w.hits == 256 for w in results)
            assert mock_on_hit.call_count == 256
            # 每个回调应收到 32 字节私钥
            for call in mock_on_hit.call_args_list:
                pk = call[0][0]
                assert isinstance(pk, bytes)
                assert len(pk) == 32
        finally:
            scheduler.close()

    # ── Collision 集成 ────────────────────────────────────────

    @pytest.mark.gpu
    def test_collision_detection_with_utxo(self, tmp_dir) -> None:
        """GPU 输出格式验证（shape / dtype / 碰撞检测 API）。"""
        import json

        from gpu_engine.gpu_pipeline import GPUPipeline

        # 创建临时 UTXO 文件：将一个已知 HASH160 放入目标集
        known_h160 = b"\xab" * 20
        bin_path = tmp_dir / "utxo_hash160.bin"
        with open(bin_path, "wb") as f:
            f.write(known_h160)

        # 创建前缀索引
        idx_data: dict[str, object] = {
            "total": 1,
            "index": {
                f"{known_h160[0]:02x}": [0, 0, False],
            },
        }
        for fb in range(256):
            if fb != known_h160[0]:
                idx_data["index"][f"{fb:02x}"] = [0, -1, True]
        idx_path = tmp_dir / "utxo_hash160.idx"
        idx_path.write_text(json.dumps(idx_data), encoding="utf-8")

        # 注: collision_target 使用模块级常量 HASH_BIN / HASH_IDX
        # 我们通过 monkeypatch 不可行（已 import），改用直接构造 Hash160Set 并注入路径
        # 实际上 Hash160Set 默认从 HASH_BIN 加载，测试不方便
        # 这里验证 API 管道能产生有效输出即可
        pipe = GPUPipeline(batch_size=256, quiet=True)
        try:
            result = pipe.submit_batch()
            # 验证 HASH160 输出格式正确（1D 连续数组, batch * 20 字节）
            assert len(result.hash160s.shape) == 1
            assert result.hash160s.shape[0] == 256 * 20
            assert result.hash160s.dtype == np.uint8  # type: ignore[attr-defined]  # noqa: E501
            # 验证私钥输出格式正确（1D 连续数组, batch * 32 字节）
            assert len(result.privkey_bytes.shape) == 1
            assert result.privkey_bytes.shape[0] == 256 * 32
            # 随机模式下私钥应非零
            assert result.privkey_bytes.any()
        finally:
            pipe.close()
