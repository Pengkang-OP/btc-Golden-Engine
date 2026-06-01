#!/usr/bin/env python3
"""GPU 设备发现与信息查询..

提供列出系统中所有 OpenCL 设备以及获取设备详细信息的工具函数.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    """单个 GPU/OpenCL 设备的摘要信息.."""

    platform_name: str
    device_name: str
    device_type: str  # 'GPU', 'CPU', 'Accelerator'
    compute_units: int
    max_work_group_size: int
    global_mem_size: int  # bytes
    local_mem_size: int  # bytes
    max_clock_frequency: int  # MHz
    opencl_version: str
    driver_version: str
    available: bool
    _raw_device: Any = None  # pyopencl.Device (if available)

    def __repr__(self) -> str:
        """返回设备的单行摘要字符串.."""
        return (
            f"{self.device_name} | {self.platform_name} | "
            f"{self.compute_units} CU @ {self.max_clock_frequency} MHz | "
            f"{self.global_mem_size / 1e9:.1f} GB global"
        )


def list_devices(device_type: str | None = None) -> list[DeviceInfo]:
    """枚举系统中所有可用的 OpenCL 设备..

    Args:
        device_type: 设备类型筛选,'GPU', 'CPU', 'Accelerator' 或 None(全部)

    Returns:
        设备信息列表

    """
    try:
        import pyopencl as cl
    except ImportError:
        logger.warning("[GPU] pyopencl 未安装.使用 pip install pyopencl 安装.")
        return []

    type_map = {
        "GPU": cl.device_type.GPU,
        "CPU": cl.device_type.CPU,
        "Accelerator": cl.device_type.ACCELERATOR,
    }
    filter_type = type_map.get(device_type) if device_type else None

    devices: list[DeviceInfo] = []
    for platform in cl.get_platforms():
        try:
            all_devices = platform.get_devices()
        except cl.RuntimeError:
            all_devices = []

        for dev in all_devices:
            if filter_type is not None and (dev.type & filter_type) == 0:
                continue

            type_str = (
                "GPU"
                if dev.type == cl.device_type.GPU
                else ("CPU" if dev.type == cl.device_type.CPU else "Accelerator")
            )

            devices.append(
                DeviceInfo(
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
                ),
            )

    return devices


def get_device_info(device_index: int = 0) -> DeviceInfo | None:
    """按索引获取单个设备的详细信息..

    Args:
        device_index: 设备索引(在所有设备的扁列表中)

    Returns:
        设备信息,或 None(索引无效)

    """
    devices = list_devices()
    if 0 <= device_index < len(devices):
        return devices[device_index]
    return None


def pick_best_gpu() -> DeviceInfo | None:
    """从可用 GPU 中选出最佳设备(按计算单元数降序).."""
    gpus = [d for d in list_devices("GPU") if d.available]
    if not gpus:
        return None
    gpus.sort(key=lambda d: d.compute_units, reverse=True)
    return gpus[0]


def _demo() -> None:
    """CLI 入口:列出所有设备并打印信息.."""
    all_devices = list_devices()
    if not all_devices:
        return

    for _i, _dev in enumerate(all_devices):
        pass

    best = pick_best_gpu()
    if best:
        pass
    else:
        pass


if __name__ == "__main__":
    _demo()
