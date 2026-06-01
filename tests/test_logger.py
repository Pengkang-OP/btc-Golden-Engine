"""测试 core.logger 模块。."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from conftest import *


@pytest.fixture(autouse=True)
def _reset_logging() -> Generator[None, None, None]:
    """每个测试后清理 logging handlers（确保隔离）。."""
    yield
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    for name in list(logging.root.manager.loggerDict.keys()):
        logger = logging.getLogger(name)
        logger.handlers.clear()


class TestSetupLogger:
    """setup_logger 功能测试。."""

    def test_basic_setup(self):
        """测试基本日志设置 — 返回 Logger 实例。."""
        from core.logger import setup_logger

        logger = setup_logger(name="test_basic")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_basic"
        assert logger.level == logging.INFO

    def test_file_handler_created(self, tmp_dir: Path):
        """测试文件日志 Handler 正确创建（测试完成后关闭 handler）。."""
        from core.logger import setup_logger

        log_path = tmp_dir / "logs" / "test.log"
        logger = setup_logger(
            name="test_file",
            log_path=log_path,
            level="DEBUG",
        )
        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        assert file_handlers[0].baseFilename == str(log_path)
        # 关闭 handler 以允许 Windows 删除临时目录
        for h in logger.handlers:
            h.close()

    def test_console_handler_created(self):
        """测试控制台 Handler 正确创建。."""
        from core.logger import setup_logger

        logger = setup_logger(name="test_console")
        stream_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(stream_handlers) == 1
        # 默认 console_level = WARNING
        assert stream_handlers[0].level == logging.WARNING

    def test_logger_writes_to_file(self, tmp_dir: Path):
        """测试日志实际写入文件。."""
        from core.logger import setup_logger

        log_path = tmp_dir / "write_test.log"
        logger = setup_logger(
            name="test_write",
            log_path=log_path,
            level="DEBUG",
        )
        logger.info("test info message")
        logger.warning("test warning message")

        # 关闭 logger 确保 flush
        for h in logger.handlers:
            h.close()

        content = log_path.read_text(encoding="utf-8")
        assert "test info message" in content
        assert "test warning message" in content

    def test_log_file_rotation(self, tmp_dir: Path):
        """测试 RotatingFileHandler 的 max_bytes 限制。."""
        from core.logger import setup_logger

        log_path = tmp_dir / "rotation.log"
        logger = setup_logger(
            name="test_rotation",
            log_path=log_path,
            level="DEBUG",
            max_bytes=500,
            backup_count=2,
        )
        # 写入足够数据触发 rotation
        for i in range(200):
            logger.warning("log line %04d - %s", i, "x" * 50)

        for h in logger.handlers:
            h.close()

        # 至少应有原始文件和 1 个备份
        files = list(tmp_dir.glob("rotation.log*"))
        assert len(files) >= 2

    def test_custom_levels(self, tmp_dir: Path):
        """测试自定义日志级别。."""
        from core.logger import setup_logger

        log_path = tmp_dir / "level.log"
        logger = setup_logger(
            name="test_level",
            log_path=log_path,
            level="WARNING",
        )
        assert logger.level == logging.WARNING

        # info 不应该被记录
        logger.info("should not appear")
        logger.warning("should appear")

        for h in logger.handlers:
            h.close()

        content = log_path.read_text(encoding="utf-8")
        assert "should not appear" not in content
        assert "should appear" in content

    def test_get_logger_existing(self):
        """测试 get_logger 获取已存在的日志器。."""
        from core.logger import get_logger, setup_logger

        original = setup_logger(name="test_get")
        fetched = get_logger("test_get")
        assert fetched is original

    def test_get_logger_auto_create(self):
        """测试 get_logger 在日志器不存在时自动创建。."""
        from core.logger import get_logger

        logger = get_logger("test_auto")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_auto"
