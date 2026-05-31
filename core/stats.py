"""性能统计追踪模块 — 实时 keys/s 滑动窗口。

使用 collections.deque 作为滑动窗口，O(1) 追加，内存占用固定。
线程安全，支持从多个扫描线程同时记录。

用法:
    from core.stats import StatsTracker

    stats = StatsTracker(window_seconds=300)
    stats.record_keys(65536)      # 记录一批扫描的 key 数
    kps = stats.keys_per_second() # 当前速率
    total = stats.total_keys()    # 累计
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional


class StatsTracker:
    """基于滑动窗口的性能统计追踪器。

    使用 (timestamp, key_count) 二元组组成的 deque 作为滑动窗口。
    窗口大小固定（按秒计），过期的数据点会被自动修剪。

    Attributes:
        window_seconds: 滑动窗口大小（秒）。
        total: 累计扫描的 key 总数。
        _start_time: 追踪器开始时间（Unix 时间戳）。
        _window: 滑动窗口 deque，元素为 (timestamp, count)。
        _lock: 线程锁。
    """

    __slots__ = (
        "window_seconds",
        "total",
        "_start_time",
        "_window",
        "_lock",
    )

    def __init__(self, window_seconds: int = 3600):
        self.window_seconds = max(window_seconds, 1)
        self.total: int = 0
        self._start_time: float = time.monotonic()
        self._window: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    # ── 记录 ──────────────────────────────────────────────

    def record_keys(self, count: int = 1) -> None:
        """记录一批扫描的 key 数量。

        Args:
            count: 本次扫描的 key 数（通常为 batch_size）。
        """
        if count <= 0:
            return

        now = time.monotonic()
        with self._lock:
            self.total += count
            self._window.append((now, count))
            self._trim(now)

    def _trim(self, now: float) -> None:
        """修剪窗口：移除超出滑动窗口的数据点。"""
        cutoff = now - self.window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    # ── 查询 ──────────────────────────────────────────────

    def keys_per_second(self) -> float:
        """返回当前滑动窗口内的平均 keys/s。

        Returns:
            每秒 key 数。无数据返回 0.0。
        """
        now = time.monotonic()
        with self._lock:
            self._trim(now)
            if not self._window:
                return 0.0

            # 窗口实际时间跨度
            span = now - self._window[0][0]
            if span < 0.001:
                return 0.0

            total_in_window = sum(c for _, c in self._window)
            return total_in_window / span

    def keys_per_minute(self) -> float:
        """返回每分钟 keys 数（方便阅读的速率）。"""
        return self.keys_per_second() * 60.0

    def total_keys(self) -> int:
        """返回累计扫描的 key 总数。"""
        return self.total

    def elapsed_seconds(self) -> float:
        """返回自创建以来经过的秒数。"""
        return time.monotonic() - self._start_time

    def window_count(self) -> int:
        """返回当前窗口内的数据点数量。"""
        now = time.monotonic()
        with self._lock:
            self._trim(now)
            return len(self._window)

    def window_total(self) -> int:
        """返回当前窗口内的累计 key 数。"""
        now = time.monotonic()
        with self._lock:
            self._trim(now)
            return sum(c for _, c in self._window)

    def get_snapshot(self) -> dict[str, object]:
        """返回当前状态的快照字典。"""
        kps = self.keys_per_second()
        return {
            "total_keys": self.total,
            "keys_per_second": round(kps, 1),
            "keys_per_minute": round(kps * 60, 1),
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
            "window_seconds": self.window_seconds,
            "window_count": self.window_count(),
            "window_total": self.window_total(),
        }

    # ── 重置 ──────────────────────────────────────────────

    def reset(self) -> None:
        """重置所有统计数据和窗口。"""
        with self._lock:
            self.total = 0
            self._window.clear()
            self._start_time = time.monotonic()
