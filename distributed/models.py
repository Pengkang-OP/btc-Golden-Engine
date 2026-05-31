"""
分布式扫描数据模型

定义 WorkerInfo、Assignment 等跨模块使用的 dataclass。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class WorkerInfo:
    """Worker 注册信息和运行时状态。"""

    # ── 注册信息（静态 ──
    worker_id: str
    address: str
    cpu_cores: int
    gpu_count: int
    version: str

    # ── 运行时状态 ──
    current_start: int = 0        # 当前分配 range 起始（包含）
    current_end: int = 0          # 当前分配 range 结束（不包含）
    keys_checked: int = 0         # 累计检查 key 数
    last_heartbeat: float = 0.0   # 上次心跳 Unix 时间戳
    status: str = "idle"          # "registering" / "scanning" / "idle" / "error"
    error_message: str = ""       # 可选错误信息
    registered_at: float = 0.0    # 注册时间戳

    @property
    def is_alive(self) -> bool:
        """检查 worker 是否存活（30秒心跳超时）。"""
        return (time.time() - self.last_heartbeat) < 30.0

    @property
    def uptime_seconds(self) -> float:
        """运行时长。"""
        if self.registered_at == 0:
            return 0.0
        return time.time() - self.registered_at

    @property
    def scan_rate(self) -> float:
        """估算扫描速率 (keys/sec)，需运行超过 10 秒才有意义。"""
        uptime = self.uptime_seconds
        if uptime < 10:
            return 0.0
        return self.keys_checked / uptime


@dataclass
class Assignment:
    """分配给 Worker 的 key 范围。"""

    start_key: int    # 包含
    end_key: int      # 不包含；0 表示无限
    cursor: int       # 全局 cursor（统计用途）

    @property
    def range_size(self) -> int:
        """范围大小。"""
        if self.end_key == 0:
            return -1  # 无限
        return self.end_key - self.start_key

    def contains(self, key: int) -> bool:
        """检查 key 是否在当前范围内。"""
        if key < self.start_key:
            return False
        if self.end_key == 0:
            return True
        return key < self.end_key
