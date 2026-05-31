"""GPU 加速引擎 — OpenCL 加速的私钥碰撞模块。

为 collision_engine.py 提供 GPU 计算支持，利用 OpenCL 并行执行
EC 乘法和 HASH160 计算，大幅提升私钥碰撞搜索速度。

依赖:
    - pyopencl >= 2024.1  (仅 GPU 模式需要)
    - 支持 OpenCL 1.2+ 的设备 (GPU/CPU)

TDR 安全:
    Windows TDR (Timeout Detection and Recovery) 会在 GPU 内核执行
    超过 2 秒时重置驱动。引擎通过自动 sub-batch 拆分来避免此问题。
"""

from .gpu_pipeline import GPUPipeline, BatchResult
from .gpu_dispatcher import GPUBatchScheduler, DispatcherConfig
from .gpu_device import list_devices, get_device_info
from .tdr_handler import (
    TDRConfig,
    KernelTimer,
    warn_tdr_settings,
    is_tdr_error,
)

__all__ = [
    "GPUPipeline",
    "BatchResult",
    "GPUBatchScheduler",
    "DispatcherConfig",
    "list_devices",
    "get_device_info",
    "TDRConfig",
    "KernelTimer",
    "warn_tdr_settings",
    "is_tdr_error",
]
