"""API 路由自动化测试 — 使用 FastAPI TestClient。

涵盖全部 6 个 REST 端点 + WebSocket + /metrics 端点。
所有外部依赖（数据库、目标集、引擎状态）均通过 monkeypatch mock。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Generator
from unittest import mock

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
        api_server,
        "get_engine_status",
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
        api_server,
        "load_target_sets",
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
        assert any(
            kw in lower
            for kw in (
                "keys/s",
                "keys_per_second",
                "keys per second",
            )
        )


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
        self,
        client: TestClient,
        mock_db: Any,
    ) -> None:
        """插入 3 条结果后默认返回全部。"""
        self._seed_results(mock_db, 3)
        resp = client.get("/api/results")
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_limit_offset(
        self,
        client: TestClient,
        mock_db: Any,
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
        self,
        client: TestClient,
        mock_db: Any,
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
        self,
        client: TestClient,
        mock_db: Any,
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


# ═══════════════════════════════════════════════════════════════
#  EngineStatus 单元测试（不依赖 app/client fixture）
# ═══════════════════════════════════════════════════════════════


class TestEngineStatusReadWrite:
    """EngineStatus.read/write 的缓存、文件 IO 和异常路径。"""

    # ── read() 路径 ────────────────────────────────────────────

    def test_read_cache_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """缓存命中（_cached_ok 且未超时）→ 直接返回缓存值。"""
        import time
        import api.server as api_server

        es = api_server.EngineStatus()
        cached = {"running": True, "mode": "gpu"}
        es._cached = cached
        es._cached_ok = True
        es._last_read = time.monotonic()  # 在 1 秒缓存窗口内

        result = es.read()
        assert result == cached

    @staticmethod
    def _mock_status_file(es: Any, read_text_ret: Any | None = None, write_text_side_effect: Any = None) -> mock.MagicMock:
        """用 MagicMock 替换 EngineStatus 实例的 STATUS_FILE。"""
        mock_file = mock.MagicMock()
        if read_text_ret is not None:
            mock_file.read_text.return_value = read_text_ret
        es.STATUS_FILE = mock_file
        return mock_file

    def test_read_cache_miss_file_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """缓存过期 + 文件读取成功 → 解析 JSON 并更新缓存。"""
        import api.server as api_server
        import time

        es = api_server.EngineStatus()
        es._cached_ok = False
        es._last_read = time.monotonic() - 10.0  # 超时
        file_data = {"running": True, "keys_per_second": 500.0}
        self._mock_status_file(es, read_text_ret=json.dumps(file_data))

        result = es.read()
        assert result == file_data
        assert es._cached == file_data
        assert es._cached_ok is True

    def test_read_file_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """文件不存在 → 返回默认状态字典。"""
        import api.server as api_server

        es = api_server.EngineStatus()
        es._cached_ok = False
        self._mock_status_file(es)
        es.STATUS_FILE.read_text.side_effect = FileNotFoundError

        result = es.read()
        assert result["running"] is False
        assert result["mode"] == "unknown"
        assert "error" in result

    def test_read_json_decode_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JSON 解析失败 → 返回默认状态字典。"""
        import api.server as api_server

        es = api_server.EngineStatus()
        es._cached_ok = False
        self._mock_status_file(es)
        es.STATUS_FILE.read_text.side_effect = json.JSONDecodeError("bad token", "", 0)

        result = es.read()
        assert result["running"] is False
        assert "error" in result

    def test_read_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError → 返回默认状态字典。"""
        import api.server as api_server

        es = api_server.EngineStatus()
        es._cached_ok = False
        self._mock_status_file(es)
        es.STATUS_FILE.read_text.side_effect = OSError("permission denied")

        result = es.read()
        assert result["running"] is False
        assert "error" in result

    # ── write() 路径 ───────────────────────────────────────────

    def test_write_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """写入状态文件成功。"""
        import api.server as api_server

        es = api_server.EngineStatus()
        mock_file = self._mock_status_file(es)
        data = {"running": True, "mode": "cpu"}
        es.write(data)

        mock_file.write_text.assert_called_once()
        written = json.loads(mock_file.write_text.call_args[0][0])
        assert written == data

    def test_write_os_error(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """写入状态文件失败 → 记录警告日志。"""
        import logging
        import api.server as api_server

        caplog.set_level(logging.WARNING)
        es = api_server.EngineStatus()
        mock_file = self._mock_status_file(es)
        mock_file.write_text.side_effect = OSError("disk full")

        es.write({"running": True})
        assert any("写入状态文件失败" in rec.message for rec in caplog.records)


# ═══════════════════════════════════════════════════════════════
#  load_target_sets 路径覆盖（通过 sys.modules mock）
# ═══════════════════════════════════════════════════════════════


class TestLoadTargetSets:
    """load_target_sets 的 ImportError / FileNotFound / 成功路径。"""

    def test_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """collision_target 模块不可导入 → 返回全 False 描述。"""
        import api.server as api_server

        monkeypatch.setitem(sys.modules, "collision_target", None)

        result = api_server.load_target_sets()
        assert result["hash160_loaded"] is False
        assert result["xonly_loaded"] is False

    def test_hash160_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hash160Set.load() 抛出 FileNotFoundError → hash160 部分标记未加载。"""
        import api.server as api_server

        ct_mock = mock.MagicMock()
        ct_mock.Hash160Set.return_value.load.side_effect = FileNotFoundError("no utxo file")
        ct_mock.XOnlySet.return_value.load.return_value = None
        ct_mock.XOnlySet.return_value.__len__.return_value = 50
        monkeypatch.setitem(sys.modules, "collision_target", ct_mock)

        result = api_server.load_target_sets()
        assert result["hash160_loaded"] is False
        assert result["xonly_loaded"] is True
        assert result["xonly_count"] == 50

    def test_xonly_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """XOnlySet.load() 抛出 FileNotFoundError → xonly 部分标记未加载。"""
        import api.server as api_server

        ct_mock = mock.MagicMock()
        ct_mock.Hash160Set.return_value.load.return_value = None
        ct_mock.Hash160Set.return_value.__len__.return_value = 100
        ct_mock.XOnlySet.return_value.load.side_effect = FileNotFoundError("no xonly file")
        monkeypatch.setitem(sys.modules, "collision_target", ct_mock)

        result = api_server.load_target_sets()
        assert result["hash160_loaded"] is True
        assert result["hash160_count"] == 100
        assert result["xonly_loaded"] is False

    def test_success_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """两个目标集均成功加载 → 全部标记为已加载。"""
        import api.server as api_server

        ct_mock = mock.MagicMock()
        ct_mock.Hash160Set.return_value.load.return_value = None
        ct_mock.Hash160Set.return_value.__len__.return_value = 100
        ct_mock.XOnlySet.return_value.load.return_value = None
        ct_mock.XOnlySet.return_value.__len__.return_value = 50
        monkeypatch.setitem(sys.modules, "collision_target", ct_mock)

        result = api_server.load_target_sets()
        assert result["hash160_loaded"] is True
        assert result["hash160_count"] == 100
        assert result["xonly_loaded"] is True
        assert result["xonly_count"] == 50


# ═══════════════════════════════════════════════════════════════
#  main() 入口测试
# ═══════════════════════════════════════════════════════════════


class TestMainEntry:
    """main() 启动 uvicorn。"""

    def test_main_calls_uvicorn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() 应调用 uvicorn.run 并传递正确参数。"""
        import api.server as api_server

        uvicorn_mock = mock.MagicMock()
        monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_mock)

        # main() 引用模块级 app，需要预先设置
        api_server.app = mock.MagicMock()
        api_server.main()

        uvicorn_mock.run.assert_called_once()
        args, kwargs = uvicorn_mock.run.call_args
        assert kwargs.get("host") == "127.0.0.1"
        assert kwargs.get("port") == 8080
        assert kwargs.get("log_level") == "info"

    def test_main_sets_logging(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """main() 设置 logging.basicConfig。"""
        import logging
        import api.server as api_server

        caplog.set_level(logging.INFO)
        uvicorn_mock = mock.MagicMock()
        monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_mock)
        api_server.app = mock.MagicMock()

        # 测试 logging.basicConfig 被调用
        basic_config_mock = mock.MagicMock()
        monkeypatch.setattr(logging, "basicConfig", basic_config_mock)

        api_server.main()

        basic_config_mock.assert_called_once()
        assert basic_config_mock.call_args[1]["level"] == logging.INFO


# ═══════════════════════════════════════════════════════════════
#  Routes 异常路径测试（通过 monkeypatch routes 模块级引用）
# ═══════════════════════════════════════════════════════════════


class TestRoutesErrorPaths:
    """api/routes.py 各端点异常/边界路径覆盖。"""

    def test_build_stats_db_exception(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """_build_stats 中 db.count_results 抛出异常 → total_collisions=0。"""
        import api.routes as routes

        bad_db = mock.MagicMock()
        bad_db.count_results.side_effect = Exception("db error")
        monkeypatch.setattr(routes, "get_db", lambda: bad_db)

        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json()["total_collisions"] == 0

    def test_build_stats_hash160_len_exception(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """_build_stats 中 len(_hash160_set) 抛出异常 → hash160_loaded=False。"""
        import api.routes as routes

        bad_set = mock.MagicMock()
        bad_set.__len__.side_effect = Exception("len error")
        monkeypatch.setattr(routes, "_hash160_set", bad_set)

        resp = client.get("/api/stats")
        tc = resp.json()["target_count"]
        assert tc["hash160_loaded"] is False

    def test_build_stats_xonly_len_exception(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """_build_stats 中 len(_xonly_set) 抛出异常 → xonly_loaded=False。"""
        import api.routes as routes

        bad_set = mock.MagicMock()
        bad_set.__len__.side_effect = Exception("len error")
        monkeypatch.setattr(routes, "_xonly_set", bad_set)

        resp = client.get("/api/stats")
        tc = resp.json()["target_count"]
        assert tc["xonly_loaded"] is False

    def test_dashboard_render_error(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dashboard 模板渲染失败 → 返回错误 HTML。"""
        import api.routes as routes

        bad_env = mock.MagicMock()
        bad_env.get_template.side_effect = Exception("template not found")
        monkeypatch.setattr(routes, "_jinja_env", bad_env)

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Dashboard 渲染错误" in resp.text

    def test_health_db_error(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """健康检查 db 失败 → database=error。"""
        import api.routes as routes

        bad_db = mock.MagicMock()
        bad_db.count_results.side_effect = Exception("db unavailable")
        monkeypatch.setattr(routes, "get_db", lambda: bad_db)

        resp = client.get("/api/health")
        assert resp.json()["database"] == "error"

    def test_get_results_db_error(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """查询碰撞结果时 db 失败 → 返回 error 字段。"""
        import api.routes as routes

        bad_db = mock.MagicMock()
        bad_db.count_results.side_effect = Exception("query failed")
        monkeypatch.setattr(routes, "get_db", lambda: bad_db)

        resp = client.get("/api/results")
        data = resp.json()
        assert "error" in data
        assert data["total"] == 0

    def test_get_results_short_privkey(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """私钥长度 <= 16 时不做截断。"""
        import api.routes as routes

        mock_db = mock.MagicMock()
        mock_db.count_results.return_value = 1
        mock_db.list_results.return_value = [
            {"privkey_hex": "abcd", "wif_compressed": "", "wif_uncompressed": ""}
        ]
        monkeypatch.setattr(routes, "get_db", lambda: mock_db)

        resp = client.get("/api/results")
        items = resp.json()["items"]
        assert items[0]["privkey_hex_short"] == "abcd"

    def test_get_results_short_wif(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """WIF 长度 <= 16 时不做截断。"""
        import api.routes as routes

        mock_db = mock.MagicMock()
        mock_db.count_results.return_value = 1
        mock_db.list_results.return_value = [
            {"privkey_hex": "a" * 64, "wif_compressed": "short_wif_12", "wif_uncompressed": ""}
        ]
        monkeypatch.setattr(routes, "get_db", lambda: mock_db)

        resp = client.get("/api/results")
        items = resp.json()["items"]
        assert items[0]["wif_short"] == "short_wif_12"

    def test_get_status_db_error(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """引擎状态查询 db 失败 → collision_count=0。"""
        import api.routes as routes

        bad_db = mock.MagicMock()
        bad_db.count_results.side_effect = Exception("db error")
        monkeypatch.setattr(routes, "get_db", lambda: bad_db)

        resp = client.get("/api/status")
        assert resp.json()["collision_count"] == 0
