"""测试 gpu_engine.tdr_handler 模块 — TDR 安全 sub-batch 拆分逻辑。"""

from __future__ import annotations

from unittest import mock

import pytest

from gpu_engine.tdr_handler import (
    TDRConfig,
    KernelTimer,
    is_tdr_error,
    warn_tdr_settings,
    _TDR_DELAY_KEY,
    _TDR_DEFAULT_TIMEOUT,
    _TDR_DDI_DELAY_KEY,
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
        cfg = TDRConfig(
            enabled=False, max_kernel_time=0.5, min_sub_batch=32, calibration_keys=128
        )
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


# ═══════════════════════════════════════════════════════════════
#  warn_tdr_settings 注册表诊断路径
# ═══════════════════════════════════════════════════════════════


class TestWarnTDRSettings:
    """warn_tdr_settings 的注册表读取和日志输出。"""

    @staticmethod
    def _build_winreg_mock(
        monkeypatch: pytest.MonkeyPatch,
        tdr_delay_value: object = None,  # None means key doesn't exist
        tdr_ddi_exists: bool = False,
    ) -> mock.MagicMock:
        """创建 winreg mock 并注册到 sys.modules。"""
        import sys

        winreg_mock = mock.MagicMock()
        winreg_mock.HKEY_LOCAL_MACHINE = 0x80000002
        winreg_mock.KEY_READ = 0x20019

        key_handle = mock.sentinel.key_handle
        winreg_mock.OpenKey.return_value = key_handle

        def _query_value_ex(key: object, name: str) -> tuple:
            if name == _TDR_DELAY_KEY and tdr_delay_value is not None:
                return (tdr_delay_value, 4)
            if name == _TDR_DDI_DELAY_KEY and not tdr_ddi_exists:
                raise FileNotFoundError(f"No such value: {name}")
            raise FileNotFoundError(f"No such value: {name}")

        winreg_mock.QueryValueEx = mock.MagicMock(side_effect=_query_value_ex)

        monkeypatch.setitem(sys.modules, "winreg", winreg_mock)
        return winreg_mock

    def test_default_timeout_not_quiet(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """TdrDelay=2 (默认), not quiet → 打印诊断建议。"""
        import logging

        caplog.set_level(logging.INFO)
        self._build_winreg_mock(monkeypatch, tdr_delay_value=2)

        result = warn_tdr_settings(quiet=False)

        assert result == 2.0
        assert any(
            "Windows TDR 超时 = 2.0s (默认)" in rec.message for rec in caplog.records
        )
        assert any(
            "引擎已启用自动 sub-batch 拆分" in rec.message for rec in caplog.records
        )

    def test_optimized_timeout_not_quiet(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """TdrDelay=8 (已优化), not quiet → 打印优化状态。"""
        import logging

        caplog.set_level(logging.INFO)
        self._build_winreg_mock(monkeypatch, tdr_delay_value=8)

        result = warn_tdr_settings(quiet=False)

        assert result == 8.0
        assert any("TDR 超时 = 8.0s (已优化)" in rec.message for rec in caplog.records)

    def test_tdr_delay_missing_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """TdrDelay 注册表值不存在 → 使用默认值 2.0。"""
        import logging

        caplog.set_level(logging.INFO)
        self._build_winreg_mock(monkeypatch, tdr_delay_value=None)

        result = warn_tdr_settings(quiet=False)

        assert result == _TDR_DEFAULT_TIMEOUT
        assert any(
            "Windows TDR 超时 = 2.0s (默认)" in rec.message for rec in caplog.records
        )

    def test_import_error_non_windows(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ImportError (非 Windows) → 返回 None 并记录提示。"""
        import sys
        import logging

        caplog.set_level(logging.INFO)
        # 阻止 winreg 导入
        monkeypatch.setitem(sys.modules, "winreg", None)

        result = warn_tdr_settings(quiet=False)

        assert result is None
        assert any("非 Windows 平台" in rec.message for rec in caplog.records)

    def test_os_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OpenKey 抛出 OSError → 返回 None。"""
        import sys
        import logging

        caplog.set_level(logging.INFO)
        winreg_mock = mock.MagicMock()
        winreg_mock.OpenKey.side_effect = OSError("access denied")
        winreg_mock.HKEY_LOCAL_MACHINE = 0x80000002
        winreg_mock.KEY_READ = 0x20019
        monkeypatch.setitem(sys.modules, "winreg", winreg_mock)

        result = warn_tdr_settings(quiet=False)

        assert result is None
        assert any("非 Windows 平台" in rec.message for rec in caplog.records)

    def test_quiet_mode_suppresses_logging(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """quiet=True → 不输出诊断日志。"""
        import logging

        caplog.set_level(logging.INFO)
        self._build_winreg_mock(monkeypatch, tdr_delay_value=2)

        result = warn_tdr_settings(quiet=True)

        assert result == 2.0
        # quiet=True 时不应有任何日志输出
        assert len(caplog.records) == 0

    def test_quiet_import_error_no_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """quiet=True + ImportError → 无日志, 返回 None。"""
        import sys
        import logging

        caplog.set_level(logging.INFO)
        monkeypatch.setitem(sys.modules, "winreg", None)

        result = warn_tdr_settings(quiet=True)

        assert result is None
        assert len(caplog.records) == 0
