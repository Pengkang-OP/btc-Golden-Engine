"""core — 碰撞引擎基础设施包。

提供配置管理、日志系统、结构化异常、结果持久化、
通知发送、性能统计等生产级基础设施。

模块:
    config   — EngineConfig 配置管理 (dataclass + JSON 持久化)
    logger   — 日志系统 (RotatingFileHandler + 控制台输出)
    errors   — 结构化异常体系
    database — SQLite 结果持久化 (WAL 模式, 线程安全)
    notifier — 通知发送 (邮件 + Webhook + Telegram, 异步)
    stats    — 性能统计 (滑动窗口 keys/s)
"""

from .config import EngineConfig, load_config, save_config
from .logger import setup_logger, get_logger
from .errors import (
    CollisionEngineError,
    ConfigError,
    DatabaseError,
    GPUSetupError,
    NotifierError,
)
from .database import ResultDB
from .notifier import Notifier
from .stats import StatsTracker

__all__ = [
    "EngineConfig",
    "load_config",
    "save_config",
    "setup_logger",
    "get_logger",
    "CollisionEngineError",
    "ConfigError",
    "DatabaseError",
    "GPUSetupError",
    "NotifierError",
    "ResultDB",
    "Notifier",
    "StatsTracker",
]
