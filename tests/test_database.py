"""测试 core.database 模块。.

使用 SQLite :memory: 测试，不写入磁盘。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from core.database import ResultDB
from core.errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@pytest.fixture
def db() -> Generator[ResultDB, None, None]:
    """内存数据库 fixture。."""
    _db = ResultDB(":memory:")
    yield _db
    _db.close()


@pytest.fixture
def sample_proxy(sample_result: dict[str, Any]) -> object:
    """创建 dataclass 兼容的代理对象。."""

    class _Proxy:
        pass

    obj = _Proxy()
    for k, v in sample_result.items():
        setattr(obj, k, v)
    return obj


class TestResultDB:
    """ResultDB 功能测试。."""

    def test_init_creates_table(self, db: ResultDB):
        """测试初始化时创建表。."""
        cursor = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='collisions'",
        )
        assert cursor.fetchone() is not None

    def test_init_wal_mode(self, db: ResultDB):
        """测试 WAL 模式已启用。."""
        cursor = db._conn.execute("PRAGMA journal_mode")
        row = cursor.fetchone()
        assert row is not None
        # :memory: 数据库可能返回 memory，文件数据库才是 wal
        assert row[0] in ("wal", "memory")

    def test_save_result_returns_id(self, db: ResultDB, sample_proxy: object):
        """测试 save_result 返回自增 ID。."""
        result_id = db.save_result(sample_proxy)
        assert isinstance(result_id, int)
        assert result_id > 0

    def test_save_and_retrieve(self, db: ResultDB, sample_proxy: object):
        """测试保存后能正确查询。."""
        result_id = db.save_result(sample_proxy)
        retrieved = db.get_result(result_id)
        assert retrieved is not None
        assert retrieved["privkey_hex"] == sample_proxy.privkey_hex  # type: ignore[attr-defined]
        assert retrieved["address_type"] == "P2PKH"
        assert retrieved["id"] == result_id

    def test_get_nonexistent(self, db: ResultDB):
        """测试查询不存在的 ID 返回 None。."""
        assert db.get_result(99999) is None

    def test_count_results(self, db: ResultDB, sample_proxy: object):
        """测试 count_results 返回正确数量。."""
        assert db.count_results() == 0
        db.save_result(sample_proxy)
        assert db.count_results() == 1
        db.save_result(sample_proxy)
        assert db.count_results() == 2

    def test_list_results_pagination(self, db: ResultDB, sample_proxy: object):
        """测试 list_results 分页功能。."""
        ids = []
        for i in range(10):
            proxy = sample_proxy
            if hasattr(proxy, "privkey_hex"):
                proxy.privkey_hex = f"{i:064x}"
            ids.append(db.save_result(sample_proxy))

        # limit=5, offset=0 应返回前 5 条
        page1 = db.list_results(limit=5, offset=0)
        assert len(page1) == 5

        # limit=5, offset=5 应返回后 5 条
        page2 = db.list_results(limit=5, offset=5)
        assert len(page2) == 5

        # 第二条的第一条 id 应小于第一条的最后一条 id (DESC 排序)
        assert page1[0]["id"] >= page2[0]["id"]

    def test_list_results_filter_by_type(
        self,
        db: ResultDB,
        sample_proxy: object,
    ):
        """测试 list_results 按地址类型过滤。."""
        p2pkh = sample_proxy
        # 需要不同对象以避免修改共享引用
        import copy

        p2wpkh_copy = copy.copy(sample_proxy)
        p2wpkh_copy.address_type = "P2WPKH"  # type: ignore[attr-defined]
        p2wpkh_copy.privkey_hex = "b" * 64

        db.save_result(p2pkh)
        db.save_result(p2wpkh_copy)

        p2pkh_list = db.list_results(address_type="P2PKH")
        assert len(p2pkh_list) == 1
        assert p2pkh_list[0]["address_type"] == "P2PKH"

        p2wpkh_list = db.list_results(address_type="P2WPKH")
        assert len(p2wpkh_list) == 1
        assert p2wpkh_list[0]["address_type"] == "P2WPKH"

    def test_export_json(self, db: ResultDB, sample_proxy: object, tmp_dir: Path):
        """测试 export_json 导出为 JSON 文件。."""
        db.save_result(sample_proxy)
        db.save_result(sample_proxy)

        json_path = tmp_dir / "exported.json"
        count = db.export_json(json_path)
        assert count == 2
        assert json_path.exists()

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["privkey_hex"] == sample_proxy.privkey_hex  # type: ignore[attr-defined]

    def test_import_json(self, db: ResultDB, tmp_dir: Path):
        """测试 import_json 从 JSON 导入数据。."""
        records = [
            {
                "privkey_hex": "a" * 64,
                "wif_compressed": "test_wif_1",
                "wif_uncompressed": "test_uwif_1",
            },
            {
                "privkey_hex": "b" * 64,
                "wif_compressed": "test_wif_2",
                "wif_uncompressed": "test_uwif_2",
            },
        ]
        json_path = tmp_dir / "import.json"
        json_path.write_text(json.dumps(records), encoding="utf-8")

        count = db.import_json(json_path)
        assert count == 2
        assert db.count_results() == 2

    def test_auto_timestamp(self, db: ResultDB):
        """测试 save_result 自动设置时间戳。."""

        class _Minimal:
            privkey_hex = "ff" * 32
            wif_compressed = "test"
            wif_uncompressed = "test"
            p2pkh_address_comp = ""
            p2wpkh_address = ""
            p2pkh_address_uncomp = ""
            h160_hex = ""
            address_type = ""
            found_via = ""
            timestamp = ""
            p2tr_address = ""
            xonly_hex = ""

        result_id = db.save_result(_Minimal())
        retrieved = db.get_result(result_id)
        assert retrieved is not None
        # 时间戳应为非空字符串
        assert retrieved["timestamp"]

    def test_database_error_on_bad_path(self):
        """测试无效路径抛出 DatabaseError（使用 NUL 设备绕过 mkdir）。."""
        import os

        bad_path = (
            os.path.join("\\\\.\\NUL", "test.db")
            if os.name == "nt"
            else "/dev/null/test.db"
        )
        with pytest.raises(DatabaseError):
            ResultDB(bad_path)

    def test_close_releases_connection(self, db: ResultDB):
        """测试 close() 释放连接（标记关闭标志）。."""
        assert db._closed is False
        db.close()
        assert db._closed is True

    def test_context_manager(self, sample_proxy: object):
        """测试 context manager (: 和 : 退出)。."""
        with ResultDB(":memory:") as _db:
            rid = _db.save_result(sample_proxy)
            assert rid > 0
        # 退出 context 后连接已关闭
        assert _db._closed is True
