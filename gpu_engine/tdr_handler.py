"""Windows TDR (Timeout Detection and Recovery) 处理模块。

用途:
  防止 GPU 内核执行超时导致驱动重置（TDR）。
  Windows 默认 TDR 超时 = 2 秒。

功能:
  - 每 key 执行时间校准 (KernelTimer)
  - 安全 sub-batch 大小计算
  - Windows TDR 注册表诊断
  - TDR 错误识别
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TDRConfig:
    """TDR 安全配置。"""

    enabled: bool = True
    max_kernel_time: float = 1.5  # 秒, 单个 sub-batch 内核执行上限
    min_sub_batch: int = 64  # 最小 sub-batch（至少一个 wavefront）
    calibration_keys: int = 256  # 校准使用的私钥数


_TDR_REG_KEY = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
_TDR_DELAY_KEY = "TdrDelay"
_TDR_DDI_DELAY_KEY = "TdrDdiDelay"
_TDR_DEFAULT_TIMEOUT = 2.0


class KernelTimer:
    """GPU 每 key 执行时间估算器（指数移动平均）。

    通过小批量校准运行测量 per-key 时间,
    然后计算安全的 sub-batch 大小以规避 Windows TDR 重置。
    """

    def __init__(self) -> None:
        """初始化计时器，未校准状态。"""
        self._calibrated = False
        self._ns_per_key: float = 0.0
        self._calib_count = 0

    def update(self, elapsed: float, keys: int) -> None:
        """根据一次运行更新每 key 时间估算。

        Args:
            elapsed: 内核执行时间（秒）
            keys: 本次处理的工作项数
        """
        if keys <= 0 or elapsed <= 0:
            return
        measured_ns = (elapsed * 1e9) / keys
        self._calib_count += 1
        if not self._calibrated:
            self._ns_per_key = measured_ns
            self._calibrated = True
        else:
            # 指数移动平均, α = 0.3, 快速适应实际性能变化
            self._ns_per_key = 0.3 * measured_ns + 0.7 * self._ns_per_key

    @property
    def is_calibrated(self) -> bool:
        """是否已完成至少一次校准测量。"""
        return self._calibrated

    @property
    def ns_per_key(self) -> float:
        """当前每 key 纳秒估算值（指数移动平均）。"""
        return self._ns_per_key

    @property
    def keys_per_sec(self) -> float:
        """基于校准数据的每秒 key 数估算。"""
        if self._ns_per_key <= 0:
            return 0.0
        return 1e9 / self._ns_per_key

    @property
    def calib_count(self) -> int:
        """已执行的校准测量次数。"""
        return self._calib_count

    def safe_sub_batch_size(self, max_time_ms: float, min_size: int = 64) -> int:
        """根据校准数据计算安全的 sub-batch 大小。

        Args:
            max_time_ms: 单次 sub-batch 最大执行时间（毫秒）
            min_size: 最小 sub-batch 大小

        Returns:
            安全的 sub-batch 大小（不小于 min_size）
        """
        if not self._calibrated or self._ns_per_key <= 0:
            return min_size
        max_time_ns = max_time_ms * 1e6
        size = int(max_time_ns / self._ns_per_key)
        return max(min_size, size)


def warn_tdr_settings(quiet: bool = False) -> Optional[float]:
    """检查 Windows TDR 设置并发出诊断信息。

    在非 Windows 平台或无法读取注册表时静默返回 None。

    Args:
        quiet: 若为 True，仅在实际读取到极短 TDR 超时时打印。

    Returns:
        当前 TDR 超时值（秒），或 None（无法读取）。
    """
    tdr_delay: Optional[float] = None
    try:
        import winreg

        key = winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
            _TDR_REG_KEY,
            0,
            winreg.KEY_READ,  # type: ignore[attr-defined]
        )
        try:
            raw_val, _ = winreg.QueryValueEx(key, _TDR_DELAY_KEY)  # type: ignore[attr-defined]
            tdr_delay = float(raw_val)
        except FileNotFoundError:
            tdr_delay = _TDR_DEFAULT_TIMEOUT

        try:
            winreg.QueryValueEx(key, _TDR_DDI_DELAY_KEY)  # type: ignore[attr-defined]
        except FileNotFoundError:
            pass

        winreg.CloseKey(key)  # type: ignore[attr-defined]

        if tdr_delay <= 2 and not quiet:
            logger.info("[GPU][TDR] Windows TDR 超时 = %ss (默认)。", tdr_delay)
            logger.info("         GPU 内核执行超过此时间将触发 TDR 重置。")
            logger.info("         引擎已启用自动 sub-batch 拆分 (--gpu-tdr-safe)。")
            extra = max(5, int(tdr_delay) + 3)
            logger.info(
                "         [建议] 若遇到 TDR 崩溃，可增加 TdrDelay:\n"
                "           reg add HKLM\\%s "
                "/v TdrDelay /t REG_DWORD /d %s /f\n"
                "         然后重启。",
                _TDR_REG_KEY,
                extra,
            )
        elif tdr_delay > 2 and not quiet:
            logger.info("[GPU][TDR] Windows TDR 超时 = %ss (已优化)", tdr_delay)

    except (ImportError, OSError):
        if not quiet:
            logger.info(
                "[GPU][TDR] 非 Windows 平台或无法读取 TDR 设置。 TDR 安全模式默认启用。"
            )

    return tdr_delay


def is_tdr_error(e: Exception) -> bool:
    """判断异常是否为 TDR 相关的 GPU 错误。

    检查异常消息中是否包含 TDR 相关的 OpenCL 错误关键词。
    无需依赖 pyopencl 错误码常量，具有平台兼容性。
    """
    msg = str(e).lower()
    keywords = [
        "exec_status_error",
        "out_of_resources",
        "command_execution_failure",
        "device_not_available",
        "out_of_host_memory",
    ]
    return any(k in msg for k in keywords)
