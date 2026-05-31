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


class MetricsRegistry:
    """Simple Prometheus-compatible metrics registry.

    支持的指标类型:
        - gauge: 可增可减的瞬时值
        - counter: 只增不减的累计值
        - histogram: 暂未实现完整分位数计算, 仅预留接口
    """

    def __init__(self) -> None:
        self._gauges: dict[str, float] = {}
        self._counters: dict[str, int] = {}

    def gauge(
        self, name: str, value: float, labels: dict[str, str] | None = None
    ) -> None:
        """记录 gauge 指标。

        Args:
            name: 指标名称 (如 "keys_per_second")。
            value: 当前值。
            labels: 可选的标签字典 (当前仅用于兼容 future 使用, 渲染时暂未展开)。
        """
        self._gauges[name] = value

    def counter(self, name: str, value: int = 1) -> None:
        """递增 counter 指标。

        Args:
            name: 指标名称。
            value: 增量 (默认 1)。
        """
        self._counters[name] = self._counters.get(name, 0) + value

    def render(self) -> str:
        """将所有指标渲染为 Prometheus text/plain 格式 (版本 0.0.4)。

        Returns:
            符合 Prometheus exposition 格式的字符串。
        """
        lines: list[str] = []
        for name, value in self._gauges.items():
            lines.append(f"# HELP {name} Gauge metric")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        for name, value in self._counters.items():
            lines.append(f"# HELP {name} Counter metric")
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


def get_registry() -> MetricsRegistry:
    """获取全局 MetricsRegistry 单例。"""
    global _registry
    if _registry is None:
        _registry = MetricsRegistry()
    return _registry


def reset_registry() -> None:
    """重置全局注册表 (测试用)。"""
    global _registry
    _registry = MetricsRegistry()
