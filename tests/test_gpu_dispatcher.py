"""测试 gpu_engine.gpu_dispatcher 模块。.

策略：
  - DispatcherConfig / WorkerResult dataclass 直接测试。
  - GPUBatchScheduler 的 _resolve_device_indices / initialize / run / close
    通过 monkeypatch 隔离 pyopencl 和 GPUPipeline。
"""

from __future__ import annotations

import logging

# ── 项目根 ────────────────────────────────────────────────
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

_engine_path = Path(__file__).resolve().parent.parent
if str(_engine_path) not in sys.path:
    sys.path.insert(0, str(_engine_path))

from gpu_engine.gpu_device import DeviceInfo
from gpu_engine.gpu_dispatcher import (
    DispatcherConfig,
    GPUBatchScheduler,
    WorkerResult,
)

# ═══════════════════════════════════════════════════════════
# 辅助：构造伪造的 DeviceInfo
# ═══════════════════════════════════════════════════════════


def _fake_device_info(name: str = "Mock GPU", units: int = 16) -> DeviceInfo:
    return DeviceInfo(
        platform_name="MockPlatform",
        device_name=name,
        device_type="GPU",
        compute_units=units,
        max_work_group_size=256,
        global_mem_size=8 * 1024**3,
        local_mem_size=32768,
        max_clock_frequency=1500,
        opencl_version="OpenCL 3.0",
        driver_version="1.0",
        available=True,
        _raw_device=None,
    )


# ═══════════════════════════════════════════════════════════
# DispatcherConfig
# ═══════════════════════════════════════════════════════════


class TestDispatcherConfig:
    def test_defaults(self) -> None:
        cfg = DispatcherConfig()
        assert cfg.batch_size == 65536
        assert cfg.device_indices is None
        assert cfg.total_keys == 0
        assert cfg.quiet is False
        assert cfg.check_collision is None
        assert cfg.on_hit is None
        assert cfg.mode == "random"
        assert cfg.sequential_start == 1
        assert cfg.tdr_safe is True
        assert cfg.max_kernel_time == 1.5

    def test_custom_values(self) -> None:
        cfg = DispatcherConfig(
            batch_size=131072,
            device_indices=[0, 1],
            total_keys=1000000,
            quiet=True,
            check_collision=lambda _: True,
            on_hit=lambda _: None,
            mode="sequential",
            sequential_start=42,
            tdr_safe=False,
            max_kernel_time=0.5,
        )
        assert cfg.batch_size == 131072
        assert cfg.device_indices == [0, 1]
        assert cfg.total_keys == 1000000
        assert cfg.quiet is True
        assert cfg.mode == "sequential"
        assert cfg.sequential_start == 42
        assert cfg.tdr_safe is False
        assert cfg.max_kernel_time == 0.5
        assert callable(cfg.check_collision)
        assert callable(cfg.on_hit)


# ═══════════════════════════════════════════════════════════
# WorkerResult
# ═══════════════════════════════════════════════════════════


class TestWorkerResult:
    def test_defaults(self) -> None:
        w = WorkerResult(device_name="Test GPU")
        assert w.device_name == "Test GPU"
        assert w.keys_checked == 0
        assert w.total_elapsed == 0.0
        assert w.hits == 0
        assert w.errors == 0

    def test_custom_values(self) -> None:
        w = WorkerResult(
            device_name="RTX 4090",
            keys_checked=100000,
            total_elapsed=10.5,
            hits=3,
            errors=1,
        )
        assert w.keys_checked == 100000
        assert w.total_elapsed == 10.5
        assert w.hits == 3
        assert w.errors == 1


# ═══════════════════════════════════════════════════════════
# GPUBatchScheduler — _resolve_device_indices 静态方法
# ═══════════════════════════════════════════════════════════


class TestResolveDeviceIndices:
    """_resolve_device_indices 静态方法（需 mock pyopencl）。."""

    def test_no_indices_selects_all_gpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=2)
        result = GPUBatchScheduler._resolve_device_indices(None)
        assert len(result) == 2
        for _pi, _di, info in result:
            assert info.device_type == "GPU"

    def test_specific_indices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=3)
        result = GPUBatchScheduler._resolve_device_indices([0, 2])
        assert len(result) == 2
        assert result[0][1] == 0  # local device index
        assert result[1][1] == 2

    def test_no_gpu_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=0)
        result = GPUBatchScheduler._resolve_device_indices(None)
        assert result == []

    def test_out_of_range_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=1)
        result = GPUBatchScheduler._resolve_device_indices([99])
        assert result == []


def _make_platforms(
    monkeypatch: pytest.MonkeyPatch,
    n_platforms: int = 1,
    n_gpu: int = 2,
) -> None:
    """Mock pyopencl 并注入 sys.modules，提供指定数量的平台和 GPU 设备。.

    _resolve_device_indices 在函数体内部 import pyopencl as cl，
    所以无法靠常规 import mocking，必须注入 sys.modules。
    """
    import sys as _sys

    class FakeDev:
        type = 4  # cl.device_type.GPU 的整数值
        name = "MockDevice"
        vendor = "Mock Vendor"
        max_compute_units = 16
        max_work_group_size = 256
        global_mem_size = 8 * 1024**3
        local_mem_size = 32768
        max_clock_frequency = 1500
        max_mem_alloc_size = 2 * 1024**3  # 2 GB
        version = "OpenCL 3.0"
        driver_version = "1.0"
        available = True
        platform = mock.MagicMock()

        def __repr__(self) -> str:
            return "FakeDev"

    class FakePlatform:
        name = "MockPlatform"

        def __init__(self, n_gpu: int) -> None:
            self._devices = [FakeDev() for _ in range(n_gpu)]

        def get_devices(self) -> list[FakeDev]:
            return self._devices

    # 构建 FakeDev 类型引用
    FakeDev.type = 4  # GPU = 4 in pyopencl

    mock_cl = mock.MagicMock()
    mock_cl.get_platforms.return_value = [
        FakePlatform(n_gpu) for _ in range(n_platforms)
    ]
    mock_cl.device_type.GPU = 4
    mock_cl.RuntimeError = RuntimeError
    _sys.modules["pyopencl"] = mock_cl


# ═══════════════════════════════════════════════════════════
# GPUBatchScheduler — initialize
# ═══════════════════════════════════════════════════════════


class TestInitialize:
    def test_initialize_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=2)
        cfg = DispatcherConfig(quiet=True)
        scheduler = GPUBatchScheduler(cfg)
        assert scheduler.initialize() is True
        assert len(scheduler._pipelines) == 2
        assert len(scheduler._workers) == 2

    def test_initialize_no_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=0)
        cfg = DispatcherConfig(quiet=True)
        scheduler = GPUBatchScheduler(cfg)
        assert scheduler.initialize() is False
        assert len(scheduler._pipelines) == 0

    def test_initialize_sequential_partition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=3)
        cfg = DispatcherConfig(
            quiet=True,
            mode="sequential",
            sequential_start=100,
            batch_size=64,
        )
        scheduler = GPUBatchScheduler(cfg)
        assert scheduler.initialize() is True
        assert len(scheduler._pipelines) == 3
        # 顺序模式分区：GPU[i] 起始 = 100 + i*64
        for i, pipe in enumerate(scheduler._pipelines):
            assert pipe.sequential_start == 100 + i * 64, f"GPU {i} wrong start"

    def test_initialize_pipeline_failure_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _make_platforms(monkeypatch, n_platforms=1, n_gpu=2)
        # 让第一个 GPUPipeline 创建失败
        from gpu_engine import gpu_dispatcher as gd

        call_count = 0

        class FailingPipeline:
            def __init__(self, **kwargs: Any) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    msg = "Pipeline init failed"
                    raise RuntimeError(msg)

        monkeypatch.setattr(gd, "GPUPipeline", FailingPipeline)
        cfg = DispatcherConfig(quiet=True)
        scheduler = GPUBatchScheduler(cfg)
        with caplog.at_level(logging.WARNING):
            result = scheduler.initialize()
        assert result is True  # 1/2 成功
        assert len(scheduler._pipelines) == 1


# ═══════════════════════════════════════════════════════════
# GPUBatchScheduler — run / stop / close
# ═══════════════════════════════════════════════════════════


class TestRunStopClose:
    @pytest.fixture
    def initialized_scheduler(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> GPUBatchScheduler:
        """构建一个 initialize 成功的 scheduler（mock GPU 依赖）。."""
        import gpu_engine.gpu_dispatcher as gd

        _make_platforms(monkeypatch, n_platforms=1, n_gpu=2)

        class MockPipeline:
            sequential_start = 0
            sequential_stride = 0

            def __init__(self, **kwargs: Any) -> None:
                self.sequential_start = kwargs.get("sequential_start", 0)
                self.sequential_stride = kwargs.get("sequential_stride", 0)

            def close(self) -> None:
                pass

        monkeypatch.setattr(gd, "GPUPipeline", MockPipeline)

        cfg = DispatcherConfig(quiet=True, total_keys=512)
        sched = GPUBatchScheduler(cfg)
        assert sched.initialize() is True
        return sched

    def test_run_returns_worker_results(
        self,
        initialized_scheduler: GPUBatchScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mock _worker_loop 直接写入 keys_checked，避免线程时序问题。."""
        import gpu_engine.gpu_dispatcher as gd

        def mock_worker_loop(sched_self: Any, pipe_index: int) -> None:
            worker = sched_self._workers[pipe_index]
            worker.keys_checked = 128
            worker.total_elapsed = 1.0
            worker.hits = 0
            sched_self._total_checked += 128

        monkeypatch.setattr(gd.GPUBatchScheduler, "_worker_loop", mock_worker_loop)
        workers = initialized_scheduler.run()
        assert len(workers) == 2
        for w in workers:
            assert w.keys_checked > 0
            assert w.total_elapsed > 0

    def test_run_respects_total_keys(
        self,
        initialized_scheduler: GPUBatchScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mock _worker_loop 验证总调用次数。."""
        import gpu_engine.gpu_dispatcher as gd

        call_count = 0

        def mock_worker_loop(sched_self: Any, pipe_index: int) -> None:
            nonlocal call_count
            call_count += 1

        monkeypatch.setattr(gd.GPUBatchScheduler, "_worker_loop", mock_worker_loop)
        workers = initialized_scheduler.run()
        assert len(workers) == 2
        assert call_count == 2

    def test_run_uninitialized_raises(self) -> None:
        cfg = DispatcherConfig(quiet=True)
        scheduler = GPUBatchScheduler(cfg)
        with pytest.raises(RuntimeError, match="未初始化"):
            scheduler.run()

    def test_stop_sets_event(self) -> None:
        cfg = DispatcherConfig()
        scheduler = GPUBatchScheduler(cfg)
        assert scheduler._stop_event.is_set() is False
        scheduler.stop()
        assert scheduler._stop_event.is_set() is True

    def test_close_clears_pipelines(self) -> None:
        cfg = DispatcherConfig(quiet=True)
        scheduler = GPUBatchScheduler(cfg)
        scheduler._pipelines = [mock.MagicMock(), mock.MagicMock()]
        scheduler.close()
        for p in scheduler._pipelines:
            p.close.assert_called_once()  # type: ignore[attr-defined]
        assert len(scheduler._pipelines) == 0

    def test_context_manager(self) -> None:
        cfg = DispatcherConfig()
        with GPUBatchScheduler(cfg) as sched:
            assert isinstance(sched, GPUBatchScheduler)
        # close() 被自动调用


# ═══════════════════════════════════════════════════════════
# GPUBatchScheduler — _print_summary
# ═══════════════════════════════════════════════════════════


class TestPrintSummary:
    def test_logs_summary(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = DispatcherConfig(quiet=False)
        scheduler = GPUBatchScheduler(cfg)
        scheduler._workers = [
            WorkerResult(
                device_name="GPU-0",
                keys_checked=5000,
                total_elapsed=2.0,
                hits=1,
            ),
            WorkerResult(
                device_name="GPU-1",
                keys_checked=3000,
                total_elapsed=1.5,
                hits=0,
            ),
        ]
        scheduler._total_checked = 8000
        scheduler._total_hits = 1
        with caplog.at_level(logging.INFO):
            scheduler._print_summary(total_time=3.5)
        assert "GPU-0" in caplog.text
        assert "GPU-1" in caplog.text
        assert "5,000" in caplog.text or "5000" in caplog.text
        assert "8,000" in caplog.text or "8000" in caplog.text
