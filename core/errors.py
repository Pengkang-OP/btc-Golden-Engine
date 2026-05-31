"""结构化异常体系 — 碰撞引擎专用异常。

取代原先的 `except: pass` 静默吞异常模式。
所有引擎异常继承自 CollisionEngineError，便于上层统一捕获。

异常层次:
    CollisionEngineError
    ├── ConfigError         — 配置加载/解析错误
    ├── DatabaseError       — 数据库读写错误
    ├── GPUSetupError       — GPU 初始化/编译错误
    ├── NotifierError       — 通知发送失败
    └── CheckpointError     — Checkpoint 读写错误
"""

from __future__ import annotations


class CollisionEngineError(Exception):
    """碰撞引擎所有异常的基类。"""

    def __init__(self, message: str, *, original: Exception | None = None):
        """初始化碰撞引擎异常。

        Args:
            message: 错误描述信息。
            original: 触发此异常的原始异常对象（可选）。
        """
        super().__init__(message)
        self.original = original


class ConfigError(CollisionEngineError):
    """配置相关错误 — 配置文件缺失、格式错误、字段无效。"""


class DatabaseError(CollisionEngineError):
    """数据库相关错误 — 连接失败、查询失败、写入失败。"""


class GPUSetupError(CollisionEngineError):
    """GPU 初始化错误 — pyopencl 不可用、内核编译失败、设备选择失败。"""


class NotifierError(CollisionEngineError):
    """通知发送失败 — SMTP 连接失败、Webhook 返回错误状态码。"""


class CheckpointError(CollisionEngineError):
    """Checkpoint 读写错误 — 文件损坏、写入失败。"""
