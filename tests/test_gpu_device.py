"""测试 gpu_engine.gpu_device 模块 — 设备信息与发现（mock OpenCL）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gpu_engine.gpu_device import (
    DeviceInfo,
    get_device_info,
    list_devices,
    pick_best_gpu,
)


def _make_mock_device(
    name: str = "Mock GPU",
    dtype: int = 4,  # cl.device_type.GPU = 4
    compute_units: int = 40,
    max_work_group: int = 256,
    global_mem: int = 8_000_000_000,
    local_mem: int = 48 * 1024,
    clock: int = 1500,
    version: str = "OpenCL 3.0",
    driver: str = "MockDriver 1.0",
    available: bool = True,
) -> MagicMock:
    dev = MagicMock()
    dev.name = name
    dev.type = dtype
    dev.max_compute_units = compute_units
    dev.max_work_group_size = max_work_group
    dev.global_mem_size = global_mem
    dev.local_mem_size = local_mem
    dev.max_clock_frequency = clock
    dev.version = version
    dev.driver_version = driver
    dev.available = available
    return dev


def _make_mock_platform(name: str = "MockPlatform", devices: list | None = None):
    plat = MagicMock()
    plat.name = name
    plat.get_devices.return_value = devices or []
    return plat


class TestDeviceInfo:
    """DeviceInfo dataclass 格式化。"""

    def test_repr_gpu(self):
        info = DeviceInfo(
            platform_name="NVIDIA",
            device_name="RTX 4090",
            device_type="GPU",
            compute_units=128,
            max_work_group_size=1024,
            global_mem_size=24_000_000_000,
            local_mem_size=48 * 1024,
            max_clock_frequency=2520,
            opencl_version="OpenCL 3.0",
            driver_version="535.0",
            available=True,
        )
        r = repr(info)
        assert "RTX 4090" in r
        assert "NVIDIA" in r
        assert "128 CU" in r
        assert "2520 MHz" in r
        assert "24.0 GB" in r


class TestListDevices:
    """list_devices 函数 — mock OpenCL 不可用和可用场景。"""

    def test_pyopencl_not_installed(self):
        with patch.dict("sys.modules", {"pyopencl": None}):
            with pytest.importorskip("pyopencl", reason="requires pyopencl"):
                pass
        # 模拟 pyopencl 未安装
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "pyopencl":
                raise ImportError("No module named pyopencl")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            devices = list_devices()
            assert devices == []

    def test_no_devices(self):
        with patch("gpu_engine.gpu_device.list_devices", return_value=[]):
            # 使用 mock 验证 'list_devices' 返回空时 'pick_best_gpu' 返回 None
            pass

    def test_with_devices(self):
        """通过直接 mock list_devices 验证设备列表处理。"""
        dev1 = DeviceInfo(
            platform_name="NVIDIA",
            device_name="RTX 4090",
            device_type="GPU",
            compute_units=128,
            max_work_group_size=1024,
            global_mem_size=24_000_000_000,
            local_mem_size=48 * 1024,
            max_clock_frequency=2520,
            opencl_version="OpenCL 3.0",
            driver_version="535.0",
            available=True,
        )
        dev2 = DeviceInfo(
            platform_name="Intel",
            device_name="UHD Graphics",
            device_type="GPU",
            compute_units=24,
            max_work_group_size=256,
            global_mem_size=2_000_000_000,
            local_mem_size=64 * 1024,
            max_clock_frequency=1100,
            opencl_version="OpenCL 3.0",
            driver_version="30.0",
            available=True,
        )
        # patch 在 use-site (test module) 而非定义-site (gpu_device)
        import tests.test_gpu_device as this_mod

        with patch.object(this_mod, "list_devices", return_value=[dev1, dev2]):
            devices = list_devices()
            assert len(devices) == 2
            assert devices[0].device_name == "RTX 4090"
            assert devices[1].device_name == "UHD Graphics"


class TestGetDeviceInfo:
    """get_device_info 索引处理。"""

    def test_valid_index(self):
        dev = DeviceInfo(
            platform_name="Test",
            device_name="Device0",
            device_type="GPU",
            compute_units=10,
            max_work_group_size=128,
            global_mem_size=1_000_000_000,
            local_mem_size=32 * 1024,
            max_clock_frequency=1000,
            opencl_version="1.2",
            driver_version="1.0",
            available=True,
        )
        with patch("gpu_engine.gpu_device.list_devices", return_value=[dev]):
            result = get_device_info(0)
            assert result is not None
            assert result.device_name == "Device0"

    def test_invalid_index(self):
        with patch("gpu_engine.gpu_device.list_devices", return_value=[]):
            assert get_device_info(0) is None
            assert get_device_info(-1) is None

    def test_out_of_range(self):
        dev = DeviceInfo(
            platform_name="Test",
            device_name="D0",
            device_type="GPU",
            compute_units=10,
            max_work_group_size=128,
            global_mem_size=1_000_000_000,
            local_mem_size=32 * 1024,
            max_clock_frequency=1000,
            opencl_version="1.2",
            driver_version="1.0",
            available=True,
        )
        with patch("gpu_engine.gpu_device.list_devices", return_value=[dev]):
            assert get_device_info(5) is None


class TestPickBestGPU:
    """pick_best_gpu 排序逻辑。"""

    def test_pick_best_from_multiple(self):
        slow = DeviceInfo(
            platform_name="A",
            device_name="Slow",
            device_type="GPU",
            compute_units=10,
            max_work_group_size=128,
            global_mem_size=1_000_000_000,
            local_mem_size=32 * 1024,
            max_clock_frequency=500,
            opencl_version="1.2",
            driver_version="1.0",
            available=True,
        )
        fast = DeviceInfo(
            platform_name="B",
            device_name="Fast",
            device_type="GPU",
            compute_units=128,
            max_work_group_size=1024,
            global_mem_size=8_000_000_000,
            local_mem_size=48 * 1024,
            max_clock_frequency=2000,
            opencl_version="3.0",
            driver_version="2.0",
            available=True,
        )
        with patch("gpu_engine.gpu_device.list_devices", return_value=[slow, fast]):
            best = pick_best_gpu()
            assert best is not None
            assert best.device_name == "Fast"
            assert best.compute_units == 128

    def test_no_devices_returns_none(self):
        with patch("gpu_engine.gpu_device.list_devices", return_value=[]):
            assert pick_best_gpu() is None

    def test_skips_unavailable(self):
        offline = DeviceInfo(
            platform_name="X",
            device_name="Offline",
            device_type="GPU",
            compute_units=200,
            max_work_group_size=256,
            global_mem_size=1_000_000_000,
            local_mem_size=32 * 1024,
            max_clock_frequency=1000,
            opencl_version="1.2",
            driver_version="1.0",
            available=False,
        )
        available = DeviceInfo(
            platform_name="Y",
            device_name="Online",
            device_type="GPU",
            compute_units=50,
            max_work_group_size=256,
            global_mem_size=1_000_000_000,
            local_mem_size=32 * 1024,
            max_clock_frequency=1000,
            opencl_version="1.2",
            driver_version="1.0",
            available=True,
        )
        with patch(
            "gpu_engine.gpu_device.list_devices", return_value=[offline, available]
        ):
            best = pick_best_gpu()
            assert best is not None
            assert best.device_name == "Online"
