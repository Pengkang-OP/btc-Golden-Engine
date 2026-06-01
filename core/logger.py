"""日志系统模块 - 统一日志输出..

取代 collision_engine.py 中约 40 处 print() 调用.
提供 RotatingFileHandler + 控制台 Handler,支持日志级别控制.
支持 LOG_FORMAT=json 环境变量,输出结构化 JSON 日志.

用法:
    from core.logger import setup_logger, get_logger

    logger = setup_logger()
    logger.info("引擎启动")
    logger.warning("配置文件未找到,使用默认值")
    logger.error("GPU 初始化失败", exc_info=True)

    # JSON 格式 (设置环境变量 LOG_FORMAT=json)
    import os
    os.environ["LOG_FORMAT"] = "json"
    logger = setup_logger()  # 输出为 JSON 格式
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path


class JsonFormatter(logging.Formatter):
    """JSON 日志格式器 - 输出机器可读的结构化日志..

    格式:
        {"timestamp": "...", "level": "INFO", "logger": "...", "message": "..."}

    异常信息会自动包含 exception.type 和 exception.message.
    通过 record.extra_fields 字典可附加额外字段.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """使用 datetime.strftime 替代 time.strftime (支持 %f 微秒).."""
        ct = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]  # 默认格式: 毫秒精度

    def format(self, record: logging.LogRecord) -> str:
        """将日志记录格式化为 JSON 字符串..

        包含时间戳,日志级别,记录器名称,消息,
        以及异常信息和 extra_fields(如存在).

        Args:
            record: 日志记录对象.

        Returns:
            JSON 格式的日志字符串.

        """
        log_entry: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
            }
        if hasattr(record, "extra_fields"):
            log_entry.update(cast("dict[str, object]", record.extra_fields))
        return json.dumps(log_entry, ensure_ascii=False)


def setup_logger(
    name: str = "collision_engine",
    log_path: Path | None = None,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    console_level: str = "WARNING",
) -> logging.Logger:
    """配置并返回应用日志器..

    Args:
        name: 日志器名称.
        log_path: 日志文件路径.None 则不写入文件.
        level: 日志器全局级别.
        max_bytes: 单个日志文件最大字节数.
        backup_count: 保留的备份文件数.
        console_level: 控制台输出级别 (默认 WARNING, 仅显示警告和错误).

    Returns:
        配置好的 logging.Logger 实例.

    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 关闭并移除已有 handler (避免文件句柄泄漏)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    # ── 文件 Handler (RotatingFileHandler) ──
    # 检查 LOG_FORMAT 环境变量,支持 json 格式输出
    log_format = os.environ.get("LOG_FORMAT", "").strip().lower()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
        if log_format == "json":
            file_handler.setFormatter(JsonFormatter())
        else:
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                ),
            )
        logger.addHandler(file_handler)

    # ── 控制台 Handler (stderr) ──
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(getattr(logging, console_level.upper(), logging.WARNING))
    console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(console_handler)

    # 记录日志格式信息
    fmt_name = "JSON" if log_format == "json" else "text"
    logger.info("Logger initialized: format=%s, file=%s", fmt_name, log_path)

    return logger


def get_logger(name: str = "collision_engine") -> logging.Logger:
    """获取已存在的日志器,或创建默认配置的日志器..

    用于在模块级别获取日志器的便捷方式.

    Args:
        name: 日志器名称.

    Returns:
        logging.Logger 实例.

    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        # 无 handler → 未初始化,使用默认配置
        return setup_logger(name=name)
    return logger
