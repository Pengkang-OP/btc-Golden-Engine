"""Minimal Prometheus metrics exporter (zero external dependencies).

使用方式:
    from api.metrics import get_registry

    registry = get_registry()
    registry.gauge("keys_per_second", 12345.6)
    registry.counter("total_requests", 1)

    # 渲染为 Prometheus text/plain 格式
    print(registry.render())
"""

from __future__ import annotations

import threading


class MetricsRegistry:
    """Simple Prometheus-compatible metrics registry.

    支持的指标类型:
        - gauge: 可增可减的瞬时值
        - counter: 只增不减的累计值
        - histogram: 暂未实现完整分位数计算, 仅预留接口

    Thread-safe: 所有公共方法均受 _lock 保护.
    """

    def __init__(self) -> None:
        """初始化注册表,清空 gauge 和 counter 存储.."""
        self._lock = threading.Lock()
        self._gauges: dict[str, float] = {}
        self._counters: dict[str, int] = {}

    def gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        """记录 gauge 指标..

        Args:
            name: 指标名称 (如 "keys_per_second").
            value: 当前值.
            labels: 可选的标签字典 (当前仅用于兼容 future 使用, 渲染时暂未展开).

        """
        with self._lock:
            self._gauges[name] = value

    def counter(self, name: str, value: int = 1) -> None:
        """递增 counter 指标..

        Args:
            name: 指标名称.
            value: 增量 (默认 1).

        """
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def render(self) -> str:
        """将所有指标渲染为 Prometheus text/plain 格式 (版本 0.0.4)..

        Returns:
            符合 Prometheus exposition 格式的字符串.

        """
        with self._lock:
            gauges_snapshot = dict(self._gauges)
            counters_snapshot = dict(self._counters)

        lines: list[str] = []
        for name, value in gauges_snapshot.items():
            desc = name.replace("_", " ")
            lines.append(f"# HELP {name} {desc}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        for name, value in counters_snapshot.items():
            desc = name.replace("_", " ")
            lines.append(f"# HELP {name} {desc}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")
        # 内置 python_info 指标
        py_ver = __import__("sys").version.split()[0]
        lines.append("# HELP python_info Python runtime info")
        lines.append("# TYPE python_info gauge")
        lines.append(f'python_info{{version="{py_ver}"}} 1')
        return "\n".join(lines) + "\n"


# ── 全局单例 ──────────────────────────────────────────────────
_registry: MetricsRegistry | None = None
_registry_lock: threading.Lock = threading.Lock()


def get_registry() -> MetricsRegistry:
    """获取全局 MetricsRegistry 单例 (双重检查锁定).."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = MetricsRegistry()
    return _registry


def reset_registry() -> None:
    """重置全局注册表 (测试用).."""
    global _registry
    with _registry_lock:
        _registry = MetricsRegistry()
