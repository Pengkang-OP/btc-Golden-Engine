"""测试 core.errors 模块 — 结构化异常体系。"""

from __future__ import annotations

import pytest

from core.errors import (
    CheckpointError,
    CollisionEngineError,
    ConfigError,
    DatabaseError,
    GPUSetupError,
    NotifierError,
)


class TestCollisionEngineError:
    """CollisionEngineError 基类。"""

    def test_message(self):
        err = CollisionEngineError("test message")
        assert str(err) == "test message"

    def test_original_none(self):
        err = CollisionEngineError("msg")
        assert err.original is None

    def test_original_set(self):
        cause = ValueError("inner")
        err = CollisionEngineError("wrapped", original=cause)
        assert err.original is cause


class TestConfigError:
    def test_inheritance(self):
        assert issubclass(ConfigError, CollisionEngineError)

    def test_basic(self):
        err = ConfigError("config missing")
        assert str(err) == "config missing"
        assert err.original is None


class TestDatabaseError:
    def test_inheritance(self):
        assert issubclass(DatabaseError, CollisionEngineError)

    def test_with_original(self):
        cause = RuntimeError("connection refused")
        err = DatabaseError("db failed", original=cause)
        assert str(err) == "db failed"
        assert err.original is cause


class TestGPUSetupError:
    def test_inheritance(self):
        assert issubclass(GPUSetupError, CollisionEngineError)

    def test_basic(self):
        err = GPUSetupError("OpenCL not available")
        assert str(err) == "OpenCL not available"


class TestNotifierError:
    def test_inheritance(self):
        assert issubclass(NotifierError, CollisionEngineError)

    def test_basic(self):
        err = NotifierError("SMTP failed")
        assert str(err) == "SMTP failed"


class TestCheckpointError:
    def test_inheritance(self):
        assert issubclass(CheckpointError, CollisionEngineError)

    def test_with_original(self):
        cause = OSError("disk full")
        err = CheckpointError("write failed", original=cause)
        assert err.original is cause

    def test_raise_and_catch_base(self):
        """验证所有异常可被 CollisionEngineError 统一捕获。"""
        for exc_cls in [
            ConfigError,
            DatabaseError,
            GPUSetupError,
            NotifierError,
            CheckpointError,
        ]:
            try:
                raise exc_cls("test")
            except CollisionEngineError:
                pass  # expected
            else:
                pytest.fail(f"{exc_cls.__name__} 未被 CollisionEngineError 捕获")
