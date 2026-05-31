"""
分布式扫描模块单元测试

测试覆盖：
- WorkerRegistry 注册/心跳/过期检测
- WorkerRegistry 任务分配（assign / steal）
- MasterService gRPC service handler
- Worker 数据模型
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from distributed.models import WorkerInfo, Assignment


# ═══════════════════════════════════════════════════════════════
#  Data Model Tests
# ═══════════════════════════════════════════════════════════════


class TestWorkerInfo:
    def test_create_default(self) -> None:
        w = WorkerInfo(
            worker_id="test", address="addr", cpu_cores=4, gpu_count=0, version="1.0"
        )
        assert w.worker_id == "test"
        assert w.status == "idle"
        assert w.keys_checked == 0
        assert w.is_alive is False  # last_heartbeat = 0

    def test_is_alive_within_timeout(self) -> None:
        w = WorkerInfo(
            worker_id="a", address="", cpu_cores=2, gpu_count=1, version="1.0"
        )
        w.last_heartbeat = time.time()
        assert w.is_alive is True

    def test_is_alive_expired(self) -> None:
        w = WorkerInfo(
            worker_id="a", address="", cpu_cores=2, gpu_count=1, version="1.0"
        )
        w.last_heartbeat = time.time() - 31  # 超过 30s 超时
        assert w.is_alive is False

    def test_scan_rate_zero_uptime(self) -> None:
        w = WorkerInfo(
            worker_id="a", address="", cpu_cores=2, gpu_count=1, version="1.0"
        )
        assert w.scan_rate == 0.0

    def test_uptime_not_registered(self) -> None:
        w = WorkerInfo(
            worker_id="a", address="", cpu_cores=2, gpu_count=1, version="1.0"
        )
        assert w.uptime_seconds == 0.0


class TestAssignment:
    def test_create_range(self) -> None:
        a = Assignment(start_key=100, end_key=200, cursor=100)
        assert a.range_size == 100
        assert a.contains(150) is True
        assert a.contains(99) is False
        assert a.contains(200) is False  # end_key 不包含

    def test_infinite_range(self) -> None:
        a = Assignment(start_key=100, end_key=0, cursor=100)
        assert a.range_size == -1
        assert a.contains(999999) is True
        assert a.contains(99) is False

    def test_cursor_property(self) -> None:
        a = Assignment(start_key=10, end_key=20, cursor=15)
        assert a.cursor == 15


# ═══════════════════════════════════════════════════════════════
#  WorkerRegistry Tests
# ═══════════════════════════════════════════════════════════════


class TestWorkerRegistry:
    """WorkerRegistry 单元测试（mock gRPC，仅测试业务逻辑）。"""

    @pytest.fixture
    def registry(self) -> Any:
        from distributed.master import WorkerRegistry

        return WorkerRegistry(assignment_size=100)

    def test_register_new(self, registry: Any) -> None:
        info = WorkerInfo(
            worker_id="node-1", address="addr", cpu_cores=4, gpu_count=1, version="1.0"
        )
        accepted, msg = registry.register(info)
        assert accepted is True
        assert "注册成功" in msg
        assert registry.get_worker("node-1") is not None

    def test_register_reconnect(self, registry: Any) -> None:
        info = WorkerInfo(
            worker_id="node-1", address="addr", cpu_cores=4, gpu_count=1, version="1.0"
        )
        registry.register(info)
        info2 = WorkerInfo(
            worker_id="node-1",
            address="new-addr",
            cpu_cores=8,
            gpu_count=2,
            version="1.0",
        )
        accepted, msg = registry.register(info2)
        assert accepted is True
        assert "重新注册" in msg
        w = registry.get_worker("node-1")
        assert w is not None
        assert w.cpu_cores == 8  # 更新了

    def test_unregister(self, registry: Any) -> None:
        info = WorkerInfo(
            worker_id="node-1", address="addr", cpu_cores=4, gpu_count=1, version="1.0"
        )
        registry.register(info)
        assert registry.unregister("node-1") is True
        assert registry.get_worker("node-1") is None

    def test_update_heartbeat(self, registry: Any) -> None:
        info = WorkerInfo(
            worker_id="node-1", address="addr", cpu_cores=4, gpu_count=1, version="1.0"
        )
        registry.register(info)
        found = registry.update_heartbeat("node-1", 100, 50, "scanning")
        assert found is True
        w = registry.get_worker("node-1")
        assert w is not None
        assert w.keys_checked == 100
        assert w.current_start == 50
        assert w.status == "scanning"

    def test_update_heartbeat_unknown(self, registry: Any) -> None:
        found = registry.update_heartbeat("unknown", 0, 0, "idle")
        assert found is False

    def test_assign_range(self, registry: Any) -> None:
        info = WorkerInfo(
            worker_id="node-1", address="addr", cpu_cores=4, gpu_count=1, version="1.0"
        )
        registry.register(info)
        assignment = registry.assign_range("node-1")
        assert assignment is not None
        assert assignment.start_key == 1  # 从 cursor=1 开始
        assert assignment.end_key == 101  # cursor + assignment_size
        assert assignment.cursor == 1
        assert info.current_start == 1
        assert info.current_end == 101

    def test_assign_multiple_workers(self, registry: Any) -> None:
        r = registry
        r.register(WorkerInfo("w1", "", 1, 0, "1.0"))
        r.register(WorkerInfo("w2", "", 1, 0, "1.0"))

        a1 = r.assign_range("w1")
        a2 = r.assign_range("w2")
        assert a1 is not None
        assert a2 is not None
        assert a1.start_key == 1
        assert a1.end_key == 101
        assert a2.start_key == 101
        assert a2.end_key == 201

    def test_assign_unknown_worker(self, registry: Any) -> None:
        assert registry.assign_range("unknown") is None

    def test_steal_range(self, registry: Any) -> None:
        r = registry
        r.register(WorkerInfo("w1", "", 1, 0, "1.0"))
        r.register(WorkerInfo("w2", "", 1, 0, "1.0"))

        # w1 获取 range
        a1 = r.assign_range("w1")
        assert a1 is not None
        w1 = r.get_worker("w1")
        assert w1 is not None

        # 设置 w1 的上次心跳在 60 秒前（模拟超时）
        w1.last_heartbeat = time.time() - 60
        w1.status = "scanning"

        # w2 尝试 steal
        stolen = r.steal_range("w2")
        assert stolen is not None
        assert stolen.start_key >= 1

        # w1 被标记为 error
        assert w1.status == "error"
        assert "timeout" in w1.error_message

    def test_list_workers(self, registry: Any) -> None:
        r = registry
        r.register(WorkerInfo("w1", "", 1, 0, "1.0"))
        r.register(WorkerInfo("w2", "", 2, 0, "1.0"))
        workers = r.list_workers()
        assert len(workers) == 2

    def test_total_keys_checked(self, registry: Any) -> None:
        r = registry
        r.register(WorkerInfo("w1", "", 1, 0, "1.0"))
        r.register(WorkerInfo("w2", "", 1, 0, "1.0"))
        r.update_heartbeat("w1", 500, 100, "scanning")
        r.update_heartbeat("w2", 300, 50, "scanning")
        assert r.total_keys_checked == 800

    def test_global_cursor(self, registry: Any) -> None:
        r = registry
        r.register(WorkerInfo("w1", "", 1, 0, "1.0"))
        assert r.global_cursor == 1
        r.assign_range("w1")
        assert r.global_cursor == 101
        r.assign_range("w1")
        assert r.global_cursor == 201

    def test_thread_safety(self, registry: Any) -> None:
        """并发注册和分配测试。"""
        r = registry
        errors: list[Exception] = []
        lock = threading.Lock()

        def register_and_assign(worker_id: str) -> None:
            try:
                info = WorkerInfo(worker_id, "", 1, 0, "1.0")
                r.register(info)
                for _ in range(10):
                    r.assign_range(worker_id)
                    r.update_heartbeat(worker_id, 10, 0, "scanning")
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=register_and_assign, args=(f"w{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert r.global_cursor > 1


# ═══════════════════════════════════════════════════════════════
#  MasterService gRPC Handler Tests
# ═══════════════════════════════════════════════════════════════


class FakeContext:
    """模拟 grpc.ServicerContext"""

    def __init__(self) -> None:
        self.code: Any = None
        self.details: str = ""

    def set_code(self, code: Any) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details = details


class TestMasterService:
    """MasterService gRPC handler 单元测试（无需 gRPC 服务器）。"""

    @pytest.fixture
    def service(self) -> Any:
        from distributed.master import MasterService, WorkerRegistry

        return MasterService(WorkerRegistry(assignment_size=100))

    def test_register_handler(self, service: Any) -> None:
        from distributed.protocol_pb2 import RegisterRequest

        req = RegisterRequest(
            worker_id="node-1", cpu_cores=4, gpu_count=1, version="1.0"
        )
        resp = service.Register(req, FakeContext())
        assert resp.accepted is True
        assert resp.heartbeat_interval_sec == 5
        assert resp.assignment_size == 100

    def test_heartbeat_handler(self, service: Any) -> None:
        from distributed.protocol_pb2 import RegisterRequest, HeartbeatRequest

        # 先注册
        service.Register(
            RegisterRequest(worker_id="n1", cpu_cores=2, gpu_count=0, version="1.0"),
            FakeContext(),
        )

        # 心跳
        req = HeartbeatRequest(
            worker_id="n1", keys_checked=100, current_key=50, status="scanning"
        )
        resp = service.Heartbeat(req, FakeContext())
        assert resp.acknowledged is True
        assert resp.cancel_requested is False

    def test_heartbeat_unknown(self, service: Any) -> None:
        from distributed.protocol_pb2 import HeartbeatRequest

        req = HeartbeatRequest(
            worker_id="unknown", keys_checked=0, current_key=0, status="idle"
        )
        resp = service.Heartbeat(req, FakeContext())
        assert resp.acknowledged is False
        assert resp.cancel_requested is True

    def test_get_assignment(self, service: Any) -> None:
        from distributed.protocol_pb2 import RegisterRequest, AssignmentRequest

        service.Register(
            RegisterRequest(worker_id="n1", cpu_cores=2, gpu_count=0, version="1.0"),
            FakeContext(),
        )

        req = AssignmentRequest(worker_id="n1")
        resp = service.GetAssignment(req, FakeContext())
        assert resp.has_work is True
        assert resp.start_key == 1
        assert resp.end_key == 101

    def test_get_assignment_no_worker(self, service: Any) -> None:
        from distributed.protocol_pb2 import AssignmentRequest

        resp = service.GetAssignment(
            AssignmentRequest(worker_id="unknown"), FakeContext()
        )
        assert resp.has_work is False

    def test_report_hit(self, service: Any) -> None:
        from distributed.protocol_pb2 import HitReport

        req = HitReport(worker_id="n1", privkey_hex="abcdef", key_value=123)
        resp = service.ReportHit(req, FakeContext())
        assert resp.accepted is True
        assert "hit-1" in resp.collision_id

    def test_get_target_info(self, service: Any) -> None:
        from distributed.protocol_pb2 import TargetInfoRequest

        req = TargetInfoRequest(worker_id="n1")
        resp = service.GetTargetInfo(req, FakeContext())
        # 不会失败，hash160_available 取决于文件是否存在
        assert resp.download_url == "/api/target/download/hash160"


# ═══════════════════════════════════════════════════════════════
#  Integration Smoke Tests (requires running gRPC server)
# ═══════════════════════════════════════════════════════════════


@pytest.mark.skip(reason="需要运行中的 gRPC Master")
class TestIntegration:
    """集成测试：启动临时 gRPC 服务器并连接 Worker。"""

    def test_master_worker_flow(self) -> None:
        pass
