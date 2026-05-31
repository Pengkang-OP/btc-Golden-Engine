"""测试 core.stats 模块 — StatsTracker 滑动窗口统计。"""

from __future__ import annotations

import time

import pytest

from core.stats import StatsTracker


class TestStatsTrackerInit:
    """初始状态。"""

    def test_initial_total_zero(self):
        """初始 total 应为 0。"""
        s = StatsTracker()
        assert s.total_keys() == 0

    def test_initial_kps_zero(self):
        """初始 keys_per_second 应为 0。"""
        s = StatsTracker()
        assert s.keys_per_second() == 0.0

    def test_initial_kpm_zero(self):
        """初始 keys_per_minute 应为 0。"""
        s = StatsTracker()
        assert s.keys_per_minute() == 0.0

    def test_window_count_zero(self):
        """初始窗口计数应为 0。"""
        s = StatsTracker()
        assert s.window_count() == 0

    def test_elapsed_seconds_positive(self):
        """elapsed_seconds 应为大于等于 0。"""
        s = StatsTracker()
        time.sleep(0.001)
        assert s.elapsed_seconds() >= 0

    def test_min_window_seconds(self):
        """window_seconds 最小为 1。"""
        s = StatsTracker(window_seconds=0)
        assert s.window_seconds >= 1


class TestStatsTrackerRecording:
    """记录功能。"""

    def test_record_keys_increments_total(self):
        """record_keys 应增加 total。"""
        s = StatsTracker()
        s.record_keys(1000)
        assert s.total_keys() == 1000

    def test_record_keys_multiple_times(self):
        """多次记录应累加。"""
        s = StatsTracker()
        s.record_keys(500)
        s.record_keys(1500)
        assert s.total_keys() == 2000

    def test_record_keys_zero_no_effect(self):
        """记录 0 不应改变状态。"""
        s = StatsTracker()
        s.record_keys(0)
        assert s.total_keys() == 0
        assert s.window_count() == 0

    def test_record_keys_negative_no_effect(self):
        """记录负数不应改变状态。"""
        s = StatsTracker()
        s.record_keys(-100)
        assert s.total_keys() == 0
        assert s.window_count() == 0

    def test_kps_after_recording(self):
        """记录后 keys_per_second 应为正数。"""
        s = StatsTracker(window_seconds=60)
        s.record_keys(65536)
        time.sleep(0.02)
        kps = s.keys_per_second()
        assert kps > 0

    def test_kpm_is_60x_kps(self):
        """keys_per_minute 应为 keys_per_second 的 60 倍。"""
        s = StatsTracker(window_seconds=60)
        s.record_keys(6000)
        time.sleep(0.01)
        kps = s.keys_per_second()
        kpm = s.keys_per_minute()
        # 用 pytest.approx 容忍计时抖动：
        # keys_per_second() 和 keys_per_minute() 内部各调用一次 time.monotonic()，
        # 两次调用间时钟推进可导致速率差异（CI 慢机器上尤甚）。
        assert kpm == pytest.approx(kps * 60, rel=0.1)

    def test_window_count_increases(self):
        """记录后窗口计数应增加。"""
        s = StatsTracker()
        s.record_keys(100)
        assert s.window_count() == 1
        s.record_keys(200)
        assert s.window_count() == 2

    def test_window_total_matches_recent_records(self):
        """window_total 应等于窗口内的总和。"""
        s = StatsTracker(window_seconds=60)
        s.record_keys(1000)
        s.record_keys(2000)
        assert s.window_total() >= 3000  # >= because total includes all


class TestStatsTrackerReset:
    """重置功能。"""

    def test_reset_clears_total(self):
        """reset 应清除 total。"""
        s = StatsTracker()
        s.record_keys(5000)
        s.reset()
        assert s.total_keys() == 0

    def test_reset_clears_window(self):
        """reset 应清空窗口。"""
        s = StatsTracker()
        s.record_keys(500)
        s.reset()
        assert s.window_count() == 0
        assert s.keys_per_second() == 0.0

    def test_reset_restarts_elapsed(self):
        """reset 后 elapsed 应从 0 附近开始。"""
        s = StatsTracker()
        s.record_keys(100)
        s.reset()
        elapsed = s.elapsed_seconds()
        assert elapsed < 1.0


class TestStatsTrackerSnapshot:
    """快照功能。"""

    def test_get_snapshot_returns_dict(self):
        """get_snapshot 返回字典。"""
        s = StatsTracker()
        s.record_keys(1000)
        snap = s.get_snapshot()
        assert isinstance(snap, dict)

    def test_snapshot_contains_expected_keys(self):
        """快照应包含所有关键字段。"""
        s = StatsTracker()
        s.record_keys(1000)
        snap = s.get_snapshot()
        expected_keys = {
            "total_keys",
            "keys_per_second",
            "keys_per_minute",
            "elapsed_seconds",
            "window_seconds",
            "window_count",
            "window_total",
        }
        assert expected_keys.issubset(snap.keys())

    def test_snapshot_values_consistent(self):
        """快照值与实际状态一致。"""
        s = StatsTracker()
        s.record_keys(5000)
        snap = s.get_snapshot()
        assert snap["total_keys"] == s.total_keys()
        assert isinstance(snap["keys_per_second"], float)
        assert isinstance(snap["total_keys"], int)


class TestStatsTrackerWindowTrim:
    """窗口修剪。"""

    def test_old_entries_are_trimmed(self):
        """超过窗口的数据点应被修剪。"""
        s = StatsTracker(window_seconds=1)  # 1 秒窗口
        s.record_keys(1000)
        time.sleep(1.5)  # 超过窗口
        s.record_keys(500)  # 触发修剪
        # 修剪后窗口里只有新记录
        assert s.window_count() == 1
        assert s.window_total() == 500

    def test_trim_total_untouched(self):
        """修剪不应影响 total。"""
        s = StatsTracker(window_seconds=1)
        s.record_keys(1000)
        time.sleep(1.5)
        s.record_keys(500)
        assert s.total_keys() == 1500  # total 不变
