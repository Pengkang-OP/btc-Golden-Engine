"""SQLite 结果持久化模块 — 替代 JSON O(n) 读写模式。

取代 collision_engine.py 中 save_result() 函数 (第 202-216 行)，
该函数每次命中需读取整个 JSON 文件再写回，时间复杂度 O(n)。

使用 SQLite WAL 模式，INSERT 操作为 O(1)，支持并发读/写。

用法:
    from core.database import ResultDB

    db = ResultDB("collision_results.db")
    db.save_result(collision_result)
    count = db.count_results()
    results = db.list_results(limit=10)
    db.export_json("collision_results.json")  # 向后兼容
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .errors import DatabaseError


class ResultDB:
    """SQLite 结果持久化 — 线程安全，O(1) 写入。

    Attributes:
        db_path: SQLite 数据库文件路径。
        _lock: 线程锁，确保写入安全。
        _conn: SQLite 连接。
    """

    def __init__(self, db_path: str | Path = Path("collision_results.db")):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection
        self._closed = False
        self._initialize()

    def _initialize(self) -> None:
        """创建数据库表 (如不存在)。"""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,  # 由外部锁保护
            )
            # WAL 模式: 显著提升并发性能
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 同步模式: NORMAL 兼顾性能与安全性
            self._conn.execute("PRAGMA synchronous=NORMAL")

            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS collisions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    privkey_hex TEXT NOT NULL,
                    wif_compressed TEXT NOT NULL,
                    wif_uncompressed TEXT NOT NULL,
                    p2pkh_address_comp TEXT DEFAULT '',
                    p2wpkh_address TEXT DEFAULT '',
                    p2pkh_address_uncomp TEXT DEFAULT '',
                    h160_hex TEXT DEFAULT '',
                    address_type TEXT DEFAULT '',
                    found_via TEXT DEFAULT '',
                    timestamp TEXT DEFAULT '',
                    p2tr_address TEXT DEFAULT '',
                    xonly_hex TEXT DEFAULT '',
                    p2sh_address TEXT DEFAULT '',
                    created_at  REAL NOT NULL DEFAULT (julianday('now'))
                )
            """)
            # 索引: 按时间戳排序查询、按地址类型过滤
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON collisions(timestamp)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_address_type"
                " ON collisions(address_type)"
            )
            self._conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseError(f"数据库初始化失败: {exc}", original=exc) from exc

    def save_result(self, result: Any) -> int:
        """保存碰撞结果到数据库。

        Args:
            result: CollisionResult dataclass 实例 (或任意有相同字段的对象)。

        Returns:
            新插入记录的 ID。

        Raises:
            DatabaseError: 写入失败。
        """
        # 自动设置时间戳
        timestamp = getattr(result, "timestamp", "")
        if not timestamp:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        try:
            with self._lock:
                cursor = self._conn.execute(
                    """INSERT INTO collisions (
                        privkey_hex, wif_compressed, wif_uncompressed,
                        p2pkh_address_comp, p2wpkh_address,
                        p2pkh_address_uncomp, h160_hex, address_type,
                        found_via, timestamp, p2tr_address,
                        xonly_hex, p2sh_address
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        getattr(result, "privkey_hex", ""),
                        getattr(result, "wif_compressed", ""),
                        getattr(result, "wif_uncompressed", ""),
                        getattr(result, "p2pkh_address_comp", ""),
                        getattr(result, "p2wpkh_address", ""),
                        getattr(result, "p2pkh_address_uncomp", ""),
                        getattr(result, "h160_hex", ""),
                        getattr(result, "address_type", ""),
                        getattr(result, "found_via", ""),
                        timestamp,
                        getattr(result, "p2tr_address", ""),
                        getattr(result, "xonly_hex", ""),
                        getattr(result, "p2sh_address", ""),
                    ),
                )
                self._conn.commit()
                return cursor.lastrowid or 0
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存碰撞结果失败: {exc}", original=exc) from exc

    def get_result(self, result_id: int) -> Optional[dict[str, Any]]:
        """根据 ID 查询单条碰撞结果。

        Args:
            result_id: 碰撞结果 ID。

        Returns:
            结果字典，或 None (不存在)。
        """
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT * FROM collisions WHERE id = ?", (result_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return self._row_to_dict(row, cursor)
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"查询碰撞结果失败 (id={result_id}): {exc}", original=exc
            ) from exc

    def list_results(
        self,
        limit: int = 100,
        offset: int = 0,
        address_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """列出碰撞结果，支持分页和类型过滤。

        Args:
            limit: 返回条数上限。
            offset: 偏移量。
            address_type: 地址类型过滤 (None = 全部)。

        Returns:
            结果字典列表。
        """
        try:
            with self._lock:
                if address_type:
                    cursor = self._conn.execute(
                        """SELECT * FROM collisions
                           WHERE address_type = ?
                           ORDER BY id DESC LIMIT ? OFFSET ?""",
                        (address_type, limit, offset),
                    )
                else:
                    cursor = self._conn.execute(
                        """SELECT * FROM collisions
                           ORDER BY id DESC LIMIT ? OFFSET ?""",
                        (limit, offset),
                    )
                return [self._row_to_dict(row, cursor) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise DatabaseError(f"列出碰撞结果失败: {exc}", original=exc) from exc

    def count_results(self, address_type: Optional[str] = None) -> int:
        """返回碰撞结果总数（可选按地址类型过滤）。

        Args:
            address_type: 可选的地址类型过滤（如 "P2PKH"、"P2WPKH"）。

        Returns:
            碰撞结果数量。
        """
        try:
            with self._lock:
                if address_type:
                    cursor = self._conn.execute(
                        "SELECT COUNT(*) FROM collisions WHERE address_type = ?",
                        (address_type,),
                    )
                else:
                    cursor = self._conn.execute("SELECT COUNT(*) FROM collisions")
                row = cursor.fetchone()
                return row[0] if row else 0
        except sqlite3.Error as exc:
            raise DatabaseError(f"统计碰撞结果失败: {exc}", original=exc) from exc

    def export_json(self, output_path: str | Path, chunk_size: int = 10_000) -> int:
        """导出全部碰撞结果为 JSON 文件 (向后兼容)，流式写入避免 OOM。

        Args:
            output_path: 输出 JSON 文件路径。
            chunk_size: 每批读取的行数 (默认 10_000)。

        Returns:
            导出的行数。
        """
        try:
            total = self.count_results()
            exported = 0
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("[\n")
                first_chunk = True
                for offset in range(0, total, chunk_size):
                    rows = self.list_results(limit=chunk_size, offset=offset)
                    for row in rows:
                        if not first_chunk:
                            f.write(",\n")
                        f.write(json.dumps(row, ensure_ascii=False, indent=2))
                        first_chunk = False
                        exported += 1
                f.write("\n]")
            return exported
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseError(f"导出 JSON 失败: {exc}", original=exc) from exc

    def import_json(self, json_path: str | Path) -> int:
        """从 JSON 文件导入碰撞结果 (迁移工具)。

        Args:
            json_path: JSON 文件路径。

        Returns:
            导入的行数。
        """
        try:
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            count = 0
            for item in data:
                # 从字典创建 dataclass 兼容对象
                class _ResultProxy:
                    pass

                proxy = _ResultProxy()
                for k, v in item.items():
                    setattr(proxy, k, v)
                self.save_result(proxy)
                count += 1
            return count
        except (json.JSONDecodeError, OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"导入 JSON 失败: {exc}", original=exc) from exc

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._closed:
            return
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            # 关闭失败不影响整体流程，但记录警告供排查
            import logging

            logging.getLogger("ResultDB").warning("数据库连接关闭异常: %s", exc)
        self._closed = True

    def __enter__(self) -> "ResultDB":
        """上下文管理器入口，返回自身。"""
        return self

    def __exit__(self, *args: Any) -> None:
        """上下文管理器出口，关闭数据库连接。"""
        self.close()

    @staticmethod
    def _row_to_dict(
        row: sqlite3.Row,
        cursor: sqlite3.Cursor,
    ) -> dict[str, Any]:
        """将 SQLite 行转换为字典。"""
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
