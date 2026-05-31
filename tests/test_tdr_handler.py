"""测试 gpu_engine.tdr_handler 模块 — TDR 安全 sub-batch 拆分逻辑。"""

from __future__ import annotations

from gpu_engine.tdr_handler import (
    TDRConfig,
    KernelTimer,
    is_tdr_error,
)


class TestTDRConfig:
    """TDRConfig dataclass 默认值和边界。"""

    def test_defaults(self):
        cfg = TDRConfig()
        assert cfg.enabled is True
        assert cfg.max_kernel_time == 1.5
        assert cfg.min_sub_batch == 64
        assert cfg.calibration_keys == 256

    def test_custom_values(self):
        cfg = TDRConfig(enabled=False, max_kernel_time=0.5,
                        min_sub_batch=32, calibration_keys=128)
        assert cfg.enabled is False
        assert cfg.max_kernel_time == 0.5
        assert cfg.min_sub_batch == 32
        assert cfg.calibration_keys == 128


class TestKernelTimer:
    """KernelTimer 校准和 sub-batch 大小计算。"""

    def test_initial_state(self):
        timer = KernelTimer()
        assert timer.is_calibrated is False
        assert timer.ns_per_key == 0.0
        assert timer.keys_per_sec == 0.0
        assert timer.calib_count == 0

    def test_first_update_calibrates(self):
        timer = KernelTimer()
        timer.update(elapsed=0.1, keys=1000)
        assert timer.is_calibrated is True
        assert timer.calib_count == 1
        # 100ms / 1000 keys = 0.0001s/key = 100,000 ns/key
        assert timer.ns_per_key == 100_000.0

    def test_ema_smoothing(self):
        timer = KernelTimer()
        timer.update(elapsed=0.1, keys=1000)  # 100,000 ns/key
        timer.update(elapsed=0.2, keys=1000)  # 200,000 ns/key
        # EMA: 0.3 * 200k + 0.7 * 100k = 60k + 70k = 130k
        assert timer.ns_per_key == 130_000.0
        assert timer.calib_count == 2

    def test_zero_keys_no_update(self):
        timer = KernelTimer()
        timer.update(elapsed=1.0, keys=0)
        assert timer.is_calibrated is False
        assert timer.ns_per_key == 0.0

    def test_zero_elapsed_no_update(self):
        timer = KernelTimer()
        timer.update(elapsed=0.0, keys=100)
        assert timer.is_calibrated is False

    def test_negative_elapsed_no_update(self):
        timer = KernelTimer()
        timer.update(elapsed=-1.0, keys=100)
        assert timer.is_calibrated is False

    def test_keys_per_sec(self):
        timer = KernelTimer()
        timer._calibrated = True
        timer._ns_per_key = 1000.0  # 1 us per key
        assert timer.keys_per_sec == 1_000_000.0

    def test_keys_per_sec_zero_when_uncalibrated(self):
        timer = KernelTimer()
        assert timer.keys_per_sec == 0.0

    def test_safe_sub_batch_uncalibrated_returns_min(self):
        timer = KernelTimer()
        assert timer.safe_sub_batch_size(100.0) == 64
        assert timer.safe_sub_batch_size(100.0, min_size=128) == 128

    def test_safe_sub_batch_calibrated(self):
        timer = KernelTimer()
        timer.update(elapsed=0.001, keys=1000)  # 1000 ns/key
        # max_time=10ms → 10,000,000ns / 1000ns = 10,000 keys
        size = timer.safe_sub_batch_size(max_time_ms=10.0)
        assert size == 10_000

    def test_safe_sub_batch_clamped_to_min(self):
        timer = KernelTimer()
        timer.update(elapsed=0.1, keys=1)  # 100,000,000 ns/key (slow)
        # 5ms → 5,000,000ns / 100,000,000ns = 0 → clamped to min
        size = timer.safe_sub_batch_size(max_time_ms=5.0)
        assert size == 64

    def test_safe_sub_batch_custom_min(self):
        timer = KernelTimer()
        timer.update(elapsed=0.001, keys=1000)  # 1000 ns/key
        # 5ms → 5,000,000ns / 1000ns = 5000, but min = 10000
        size = timer.safe_sub_batch_size(max_time_ms=5.0, min_size=10000)
        assert size == 10000

    def test_multiple_calibrations(self):
        timer = KernelTimer()
        for _ in range(10):
            timer.update(elapsed=0.1, keys=1000)
        assert timer.calib_count == 10
        # After many identical updates, ns_per_key should converge to 100k
        assert abs(timer.ns_per_key - 100_000.0) < 1.0


class TestIsTDRError:
    """is_tdr_error 异常匹配。"""

    def test_tdr_keyword_match_exec_status(self):
        assert is_tdr_error(RuntimeError("exec_status_error")) is True

    def test_tdr_keyword_match_resources(self):
        assert is_tdr_error(RuntimeError("out_of_resources")) is True

    def test_tdr_keyword_match_failure(self):
        assert is_tdr_error(RuntimeError("command_execution_failure")) is True

    def test_non_tdr_error(self):
        assert is_tdr_error(RuntimeError("segmentation fault")) is False

    def test_empty_message(self):
        assert is_tdr_error(RuntimeError()) is False

    def test_case_insensitive(self):
        assert is_tdr_error(RuntimeError("EXEC_STATUS_ERROR")) is True

    def test_custom_exception(self):
        class CustomError(Exception):
            pass

        assert is_tdr_error(CustomError("out_of_host_memory")) is True
        assert is_tdr_error(CustomError("unknown")) is False
