"""GPU 加速引擎 - OpenCL 加速的私钥碰撞模块..

为 collision_engine.py 提供 GPU 计算支持,利用 OpenCL 并行执行
EC 乘法和 HASH160 计算,大幅提升私钥碰撞搜索速度.

依赖:
    - pyopencl >= 2024.1  (仅 GPU 模式需要)
    - 支持 OpenCL 1.2+ 的设备 (GPU/CPU)

TDR 安全:
    Windows TDR (Timeout Detection and Recovery) 会在 GPU 内核执行
    超过 2 秒时重置驱动.引擎通过自动 sub-batch 拆分来避免此问题.
"""

from .gpu_device import get_device_info, list_devices
from .gpu_dispatcher import DispatcherConfig, GPUBatchScheduler
from .gpu_pipeline import BatchResult, GPUPipeline
from .tdr_handler import (
    KernelTimer,
    TDRConfig,
    is_tdr_error,
    warn_tdr_settings,
)

__all__ = [
    "BatchResult",
    "DispatcherConfig",
    "GPUBatchScheduler",
    "GPUPipeline",
    "KernelTimer",
    "TDRConfig",
    "get_device_info",
    "is_tdr_error",
    "list_devices",
    "warn_tdr_settings",
]
