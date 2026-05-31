"""测试 api.metrics 模块 — 零依赖 Prometheus metrics 导出。"""

from __future__ import annotations

import pytest

from api.metrics import MetricsRegistry, get_registry, reset_registry


class TestMetricsRegistry:
    """MetricsRegistry 基础功能。"""

    def test_gauge_set_and_render(self):
        registry = MetricsRegistry()
        registry.gauge("keys_per_second", 12345.6)
        output = registry.render()
        assert "# HELP keys_per_second" in output
        assert "# TYPE keys_per_second gauge" in output
        assert "keys_per_second 12345.6" in output

    def test_gauge_overwrite(self):
        registry = MetricsRegistry()
        registry.gauge("temp", 100.0)
        registry.gauge("temp", 200.0)
        output = registry.render()
        lines = [l for l in output.splitlines() if l.startswith("temp ")]
        assert len(lines) == 1
        assert lines[0] == "temp 200.0"

    def test_counter_increment_default(self):
        registry = MetricsRegistry()
        registry.counter("requests")
        assert registry._counters["requests"] == 1

    def test_counter_increment_custom(self):
        registry = MetricsRegistry()
        registry.counter("requests", 5)
        registry.counter("requests", 3)
        assert registry._counters["requests"] == 8

    def test_counter_render(self):
        registry = MetricsRegistry()
        registry.counter("total_hits", 42)
        output = registry.render()
        assert "# HELP total_hits" in output
        assert "# TYPE total_hits counter" in output
        assert "total_hits 42" in output

    def test_empty_registry(self):
        """空 registry 渲染应包含 python_info 但无其他指标。"""
        registry = MetricsRegistry()
        output = registry.render()
        lines = output.strip().split("\n")
        assert len(lines) >= 1
        # 只应有 python_info 行
        metric_lines = [l for l in lines if not l.startswith("#") and l]
        assert any(l.startswith("python_info") for l in metric_lines)

    def test_python_info_present(self):
        registry = MetricsRegistry()
        output = registry.render()
        assert "# HELP python_info" in output
        assert "# TYPE python_info gauge" in output
        assert 'python_info{version="' in output

    def test_gauge_and_counter_together(self):
        registry = MetricsRegistry()
        registry.gauge("rate", 99.9)
        registry.counter("total", 7)
        output = registry.render()
        assert "rate 99.9" in output
        assert "total 7" in output

    def test_labels_parameter_accepted(self):
        """labels 参数应被接受（当前渲染未展开，至少不报错）。"""
        registry = MetricsRegistry()
        registry.gauge("with_labels", 1.0, labels={"env": "test"})
        assert registry._gauges["with_labels"] == 1.0


class TestGetRegistry:
    """get_registry 全局单例。"""

    def test_singleton(self):
        reset_registry()
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_reset(self):
        reset_registry()
        r = get_registry()
        r.gauge("test", 1.0)
        reset_registry()
        r2 = get_registry()
        assert r2 is not r
        assert len(r2._gauges) == 0
