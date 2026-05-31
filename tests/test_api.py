"""API 路由自动化测试 — 使用 FastAPI TestClient。

涵盖全部 6 个 REST 端点 + WebSocket + /metrics 端点。
所有外部依赖（数据库、目标集、引擎状态）均通过 monkeypatch mock。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient

# ── 路径准备 ──────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_PKG = PROJECT_ROOT / ".local-packages"
for _p in (str(PROJECT_ROOT), str(_LOCAL_PKG)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ═══════════════════════════════════════════════════════════════
#  辅助 mock 类（需在类上定义 __len__ 才能被 len() 找到）
# ═══════════════════════════════════════════════════════════════


class _MockTargetSet:
    """模拟 Hash160Set / XOnlySet。"""
    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size

    def close(self) -> None:
        pass


# ═══════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_db() -> Generator:
    """内存 SQLite 数据库，替代真实 collision_results.db。"""
    from core.database import ResultDB

    _db = ResultDB(":memory:")
    yield _db
    _db.close()


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch,
    mock_db: Any,
) -> Generator:
    """创建 patched 后的 FastAPI app 实例。

    在调用 create_app() 前 monkeypatch api.server 的全变量，
    使得 routes.py 首次 import 时拿到 patched 引用。
    """
    import api.server as api_server

    # ── 强制 routes.py 重新导入（清除 sys.modules 缓存）──
    #    确保 routes.py 的 from .server import _hash160_set 拿到 patched 值
    sys.modules.pop("api.routes", None)

    # ── 数据库 ──
    monkeypatch.setattr(api_server, "get_db", lambda: mock_db)
    monkeypatch.setattr(api_server, "_db", mock_db)

    # ── 目标集 mock ──
    monkeypatch.setattr(api_server, "_hash160_set", _MockTargetSet(82_469_589))
    monkeypatch.setattr(api_server, "_xonly_set", _MockTargetSet(54_000_000))

    # ── 引擎状态 mock（避免文件 IO，且确保返回默认值）──
    monkeypatch.setattr(
        api_server, "get_engine_status",
        lambda: {
            "running": False,
            "mode": "unknown",
            "keys_per_second": 0.0,
            "total_keys": 0,
            "elapsed_seconds": 0.0,
        },
    )

    # ── 阻止 startup 事件加载 1.65 GB UTXO 数据 ──
    monkeypatch.setattr(
        api_server, "load_target_sets",
        lambda: {
            "hash160_loaded": True,
            "hash160_count": 82_469_589,
            "xonly_loaded": True,
            "xonly_count": 54_000_000,
        },
    )

    # ── 创建应用（routes.py 在此刻导入，拿到 patched 值）──
    _app = api_server.create_app()
    yield _app


@pytest.fixture
def client(app) -> Generator[TestClient, None, None]:
    """FastAPI TestClient。"""
    with TestClient(app) as c:
        yield c


# ═══════════════════════════════════════════════════════════════
#  测试类
# ═══════════════════════════════════════════════════════════════


class TestDashboard:
    """GET / — Dashboard 页面。"""

    def test_returns_html(self, client: TestClient) -> None:
        """应返回 200 和 HTML 内容。"""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Bitcoin Collision Engine" in resp.text

    def test_contains_key_stats(self, client: TestClient) -> None:
        """HTML 中包含引擎状态关键字段。"""
        resp = client.get("/")
        assert resp.status_code == 200
        # 使用低层级存在的渲染标记验证
        lower = resp.text.lower()
        assert any(kw in lower for kw in (
            "keys/s", "keys_per_second", "keys per second",
        ))


class TestHealthCheck:
    """GET /api/health — 健康检查。"""

    def test_health_ok(self, client: TestClient) -> None:
        """返回 status=ok。"""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["database"] in ("connected", "error")

    def test_health_has_version(self, client: TestClient) -> None:
        """包含版本字段。"""
        resp = client.get("/api/health")
        data = resp.json()
        assert data["version"] == "1.0.0"

    def test_health_timestamp(self, client: TestClient) -> None:
        """时间戳为 UTC ISO 格式。"""
        resp = client.get("/api/health")
        data = resp.json()
        assert "timestamp" in data
        assert data["timestamp"].endswith("Z")


class TestStats:
    """GET /api/stats — 引擎统计数据。"""

    def test_returns_expected_fields(self, client: TestClient) -> None:
        """应包含所有关键统计字段。"""
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "keys_per_second" in data
        assert "total_keys" in data
        assert "elapsed_seconds" in data
        assert "total_collisions" in data
        assert "engine_running" in data
        assert "engine_mode" in data
        assert "target_count" in data
        assert "timestamp" in data

    def test_target_count_fields(self, client: TestClient) -> None:
        """target_count 包含 hash160 和 xonly 统计。"""
        resp = client.get("/api/stats")
        data = resp.json()
        tc = data["target_count"]
        assert tc["hash160"] == 82_469_589
        assert tc["xonly"] == 54_000_000
        assert tc["hash160_loaded"] is True
        assert tc["xonly_loaded"] is True

    def test_total_collisions_zero_initially(self, client: TestClient) -> None:
        """初始碰撞数为 0。"""
        resp = client.get("/api/stats")
        assert resp.json()["total_collisions"] == 0


class TestResults:
    """GET /api/results — 碰撞结果查询。"""

    def test_empty_results(self, client: TestClient) -> None:
        """无碰撞结果时返回空列表。"""
        resp = client.get("/api/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []
        assert data["limit"] == 50
        assert data["offset"] == 0

    def test_default_pagination(
        self, client: TestClient, mock_db: Any,
    ) -> None:
        """插入 3 条结果后默认返回全部。"""
        self._seed_results(mock_db, 3)
        resp = client.get("/api/results")
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_limit_offset(
        self, client: TestClient, mock_db: Any,
    ) -> None:
        """分页参数正确生效。"""
        self._seed_results(mock_db, 10)
        resp = client.get("/api/results?limit=3&offset=5")
        data = resp.json()
        assert data["total"] == 10
        assert data["limit"] == 3
        assert data["offset"] == 5
        assert len(data["items"]) == 3

    def test_limit_clamped(self, client: TestClient) -> None:
        """limit 超过 500 应被拒绝。"""
        resp = client.get("/api/results?limit=999")
        # FastAPI Query 校验失败返回 422
        assert resp.status_code == 422

    def test_address_type_filter(
        self, client: TestClient, mock_db: Any,
    ) -> None:
        """address_type 参数过滤有效。"""
        self._seed_results(mock_db, 1, address_type="P2PKH")
        self._seed_results(mock_db, 1, address_type="P2WPKH")

        p2pkh = client.get("/api/results?address_type=P2PKH").json()
        assert p2pkh["total"] == 1
        assert p2pkh["items"][0]["address_type"] == "P2PKH"

        p2wpkh = client.get("/api/results?address_type=P2WPKH").json()
        assert p2wpkh["total"] == 1
        assert p2wpkh["items"][0]["address_type"] == "P2WPKH"

    def test_privkey_truncation(
        self, client: TestClient, mock_db: Any,
    ) -> None:
        """私钥应被截断显示 (前8+后8)。"""
        self._seed_results(mock_db, 1)
        resp = client.get("/api/results")
        item = resp.json()["items"][0]
        assert "privkey_hex_short" in item
        short = item["privkey_hex_short"]
        assert "..." in short
        # 验证格式: 前8...后8
        parts = short.split("...")
        assert len(parts) == 2
        assert len(parts[0]) == 8
        assert len(parts[1]) == 8

    @staticmethod
    def _seed_results(db: Any, n: int, address_type: str = "P2PKH") -> None:
        """向 mock_db 插入 n 条碰撞结果。"""

        class _Proxy:
            pass

        for i in range(n):
            obj = _Proxy()
            obj.privkey_hex = f"{i:064x}"
            obj.wif_compressed = f"wif_comp_{i}"
            obj.wif_uncompressed = f"wif_uncomp_{i}"
            obj.p2pkh_address_comp = f"1Addr{i}"
            obj.p2wpkh_address = f"bc1qAddr{i}"
            obj.p2pkh_address_uncomp = f"1UAddr{i}"
            obj.h160_hex = f"{i:040x}"
            obj.address_type = address_type
            obj.found_via = "cpu_test"
            obj.timestamp = "2026-01-01T00:00:00Z"
            obj.p2tr_address = ""
            obj.xonly_hex = ""
            db.save_result(obj)


class TestEngineStatus:
    """GET /api/status — 引擎运行状态。"""

    def test_returns_status_fields(self, client: TestClient) -> None:
        """应包含引擎运行状态所有字段。"""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "mode" in data
        assert "keys_per_second" in data
        assert "total_keys" in data
        assert "elapsed_seconds" in data
        assert "collision_count" in data
        assert "gpu_info" in data

    def test_not_running_by_default(self, client: TestClient) -> None:
        """默认引擎未运行。"""
        resp = client.get("/api/status")
        data = resp.json()
        assert data["running"] is False
        assert data["mode"] == "unknown"


class TestMetrics:
    """GET /metrics — Prometheus 指标端点。"""

    def test_metrics_prometheus_format(self, client: TestClient) -> None:
        """应返回 text/plain Prometheus 格式。"""
        resp = client.get("/metrics")
        assert resp.status_code == 200
        content_type = resp.headers["content-type"]
        assert "text/plain" in content_type
        body = resp.text
        # Prometheus 格式特征: # HELP / # TYPE 行
        assert "# HELP" in body
        assert "# TYPE" in body
        assert "python_info" in body


class TestWebSocket:
    """WS /ws — WebSocket 实时推送。"""

    def test_websocket_receives_stats(self, client: TestClient) -> None:
        """连接后可收到统计消息。"""
        with client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
            assert "keys_per_second" in data
            assert "total_keys" in data
            assert "total_collisions" in data
            assert "timestamp" in data
