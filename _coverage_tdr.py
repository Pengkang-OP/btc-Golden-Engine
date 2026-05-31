"""在 coverage 下直接运行 tdr_handler 测试，绕过 numpy 进程冲突。

用法: coverage run _coverage_tdr.py && coverage report -m
"""

import builtins
import importlib.util
import logging
import os
import sys
from unittest import mock

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 直接通过 importlib 加载 tdr_handler，不从 gpu_engine 包导入
# 这样可绕过 gpu_engine/__init__.py → gpu_pipeline → numpy 的导入链
spec = importlib.util.spec_from_file_location(
    "tdr_handler_standalone", "gpu_engine/tdr_handler.py"
)
tdr = importlib.util.module_from_spec(spec)
sys.modules["tdr_handler_standalone"] = tdr
spec.loader.exec_module(tdr)

_TDR_DELAY_KEY = "TdrDelay"
_TDR_DEFAULT_TIMEOUT = 2.0


def _build_winreg(tdr_delay_value=None):
    winreg_mock = mock.MagicMock()
    winreg_mock.HKEY_LOCAL_MACHINE = 0x80000002
    winreg_mock.KEY_READ = 0x20019
    key_handle = mock.sentinel.key_handle
    winreg_mock.OpenKey.return_value = key_handle

    def _qve(key, name):
        if name == _TDR_DELAY_KEY and tdr_delay_value is not None:
            return (tdr_delay_value, 4)
        raise FileNotFoundError(f"No such: {name}")

    winreg_mock.QueryValueEx = mock.MagicMock(side_effect=_qve)
    return winreg_mock


def _setup_logger(tdr_mod):
    records = []
    handler = logging.Handler()
    handler.handle = lambda r: records.append(r)
    tdr_mod.logger.addHandler(handler)
    tdr_mod.logger.setLevel(logging.INFO)
    return records


def test_default_timeout():
    sys.modules.pop("winreg", None)
    records = _setup_logger(tdr)
    sys.modules["winreg"] = _build_winreg(2)
    result = tdr.warn_tdr_settings(quiet=False)
    assert result == 2.0
    msgs = [r.getMessage() for r in records]
    assert any("Windows TDR" in m for m in msgs)
    print("  OK: default_timeout")


def test_optimized_timeout():
    sys.modules.pop("winreg", None)
    records = _setup_logger(tdr)
    sys.modules["winreg"] = _build_winreg(8)
    result = tdr.warn_tdr_settings(quiet=False)
    assert result == 8.0
    msgs = [r.getMessage() for r in records]
    assert any("已优化" in m for m in msgs)
    print("  OK: optimized_timeout")


def test_missing_delay():
    sys.modules.pop("winreg", None)
    records = _setup_logger(tdr)
    sys.modules["winreg"] = _build_winreg(None)
    result = tdr.warn_tdr_settings(quiet=False)
    assert result == _TDR_DEFAULT_TIMEOUT
    msgs = [r.getMessage() for r in records]
    assert any("Windows TDR" in m for m in msgs)
    print("  OK: missing_delay")


def test_import_error():
    sys.modules.pop("winreg", None)
    records = _setup_logger(tdr)
    orig_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "winreg":
            raise ImportError("no winreg")
        return orig_import(name, *args, **kwargs)

    builtins.__import__ = mock_import
    try:
        result = tdr.warn_tdr_settings(quiet=False)
        assert result is None
        msgs = [r.getMessage() for r in records]
        assert any("非 Windows" in m for m in msgs)
    finally:
        builtins.__import__ = orig_import
    print("  OK: import_error")


def test_quiet_mode():
    sys.modules.pop("winreg", None)
    sys.modules["winreg"] = _build_winreg(2)
    result = tdr.warn_tdr_settings(quiet=True)
    assert result == 2.0
    print("  OK: quiet_mode")


if __name__ == "__main__":
    test_default_timeout()
    test_optimized_timeout()
    test_missing_delay()
    test_import_error()
    test_quiet_mode()
    print("\nAll 5 warn_tdr_settings tests PASSED")
