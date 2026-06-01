"""测试 collision_target 模块 — Hash160Set 和 XOnlySet。.

策略：
  使用 monkeypatch 模拟 mmap 和文件 I/O，避免依赖真实的 3.3 GB 数据文件。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

# ── 辅助函数 ──────────────────────────────────────────────


def _make_bloom_header(
    total: int,
    bin_path: str,
    byte_size: int,
) -> bytes:
    """构造 Bloom Filter 缓存文件头（与 _save_bloom 兼容）。."""
    import hashlib

    header = bytearray()
    header.extend(b"BFLM")  # magic
    header.extend(struct.pack("<B", 1))  # version
    header.extend(struct.pack("<Q", total))
    header.extend(struct.pack("<Q", total * 10))  # bloom_m
    header.extend(hashlib.sha256(b"fake_bin").digest())  # bin_digest
    header.extend(struct.pack("<Q", byte_size))
    return bytes(header)


def _make_idx(total: int = 1000) -> dict[str, Any]:
    """构造前缀索引，与 _make_bin_data 产生的一致分布精确匹配。.

    调用者须保证 total 和 record_size 与 _make_bin_data 的实参一致；
    此处不取 record_size 参数，因为索引只关心记录序号而非字节偏移。
    """
    records_per_prefix = total // 256
    remainder = total % 256

    index: dict[str, Any] = {}
    for fb in range(256):
        count = records_per_prefix + 1 if fb < remainder else records_per_prefix

        if count == 0:
            index[f"{fb:02x}"] = [0, -1, True]  # empty
        else:
            if fb < remainder:
                start = fb * (records_per_prefix + 1)
            else:
                start = (
                    remainder * (records_per_prefix + 1)
                    + (fb - remainder) * records_per_prefix
                )
            end = start + count - 1
            index[f"{fb:02x}"] = [start, end, False]

    return {"total": total, "index": index}


def _make_bin_data(total: int, record_size: int = 20) -> bytes:
    """生成排序的二进制数据，第一字节均匀分布（匹配前缀索引假设）。."""
    records = []
    for i in range(total):
        # 让第一字节在 0..255 间均匀分布（与 _make_idx 的假设一致）
        fb = i % 256
        rec = bytes([fb]) + b"\x00" * (record_size - 1)
        rec = rec[:record_size]
        records.append(rec)
    records.sort()
    return b"".join(records)


# ── mock 类 ───────────────────────────────────────────────


class _MockMmap:
    """模拟 mmap.mmap 对象 — 支持 __getitem__ 切片。."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def close(self) -> None:
        pass

    def __getitem__(self, key):
        return self._data[key]

    def __len__(self) -> int:
        return len(self._data)


@pytest.fixture
def mock_hash160_files(tmp_dir: Path) -> dict[str, Any]:
    """创建模拟的 Hash160 数据文件并返回路径。.

    返回值包括:
        bin_path, idx_path, bloom_path, data_size
    """
    total = 128  # 前缀 0x00-0x7f 各一条，0x80-0xff 为空
    bin_data = _make_bin_data(total, 20)
    idx_data = _make_idx(total)

    bin_path = tmp_dir / "utxo_hash160.bin"
    bin_path.write_bytes(bin_data)

    idx_path = tmp_dir / "utxo_hash160.idx"
    idx_path.write_text(json.dumps(idx_data), encoding="utf-8")

    return {
        "bin_path": str(bin_path),
        "idx_path": str(idx_path),
        "total": total,
        "data": bin_data,
    }


class TestHash160Set:
    """Hash160Set 功能测试 — 使用 monkeypatch 模拟 mmap。."""

    def test_empty_bloom_contains(
        self,
        mock_hash160_files: dict[str, Any],
        monkeypatch,
    ):
        """无 Bloom Filter 时，二分查找应正常工作。."""
        import collision_target as ct

        # 移除 Bloom Filter 文件路径，避免自动加载
        monkeypatch.setattr(ct, "BLOOM_FILE", Path("/nonexistent_bloom.bloom"))

        hs = ct.Hash160Set()
        hs.load(
            bin_path=mock_hash160_files["bin_path"],
            idx_path=mock_hash160_files["idx_path"],
            quiet=True,
        )

        # 无 Bloom 缓存文件时，Bloom Filter 在 load() 内自动构建，最终 _bloom 非 None
        assert hs._bloom is not None

        # 取已知存在的记录
        data = mock_hash160_files["data"]
        existing = data[20:40]  # 第二条记录
        assert existing in hs

        # 不存在的记录
        nonexistent = b"\xff" + b"\x00" * 19
        assert nonexistent not in hs

        hs.close()

    def test_contains_invalid_length(self, mock_hash160_files, monkeypatch):
        """__contains__ 对非 20 字节输入应返回 False。."""
        import collision_target as ct

        monkeypatch.setattr(ct, "BLOOM_FILE", Path("/nonexistent_bloom.bloom"))

        hs = ct.Hash160Set()
        hs.load(
            bin_path=mock_hash160_files["bin_path"],
            idx_path=mock_hash160_files["idx_path"],
            quiet=True,
        )
        assert b"too_short" not in hs
        assert b"\x00" * 21 not in hs
        hs.close()

    def test_len_matches_total(self, mock_hash160_files, monkeypatch):
        """__len__ 应返回 total 条数。."""
        import collision_target as ct

        monkeypatch.setattr(ct, "BLOOM_FILE", Path("/nonexistent_bloom.bloom"))

        hs = ct.Hash160Set()
        hs.load(
            bin_path=mock_hash160_files["bin_path"],
            idx_path=mock_hash160_files["idx_path"],
            quiet=True,
        )
        assert len(hs) == mock_hash160_files["total"]
        hs.close()

    def test_close_cleans_up(self, mock_hash160_files, monkeypatch):
        """close() 应清理 _mmap 和 _bloom。."""
        import collision_target as ct

        monkeypatch.setattr(ct, "BLOOM_FILE", Path("/nonexistent_bloom.bloom"))

        hs = ct.Hash160Set()
        hs.load(
            bin_path=mock_hash160_files["bin_path"],
            idx_path=mock_hash160_files["idx_path"],
            quiet=True,
        )
        hs.close()
        assert hs._bloom is None

    def test_load_missing_file_raises(self, tmp_dir):
        """缺少数据文件应抛出 FileNotFoundError。."""
        from collision_target import Hash160Set

        hs = Hash160Set()
        with pytest.raises(FileNotFoundError):
            hs.load(
                bin_path=str(tmp_dir / "nonexistent.bin"),
                idx_path=str(tmp_dir / "nonexistent.idx"),
            )

    def test_bloom_filter_caching(self, mock_hash160_files, monkeypatch):
        """Bloom Filter 缓存文件被加载时不应重建。."""
        import collision_target as ct

        bloom_path = Path(mock_hash160_files["bin_path"]).with_suffix(".bloom")
        monkeypatch.setattr(ct, "BLOOM_FILE", bloom_path)

        # 依次调用 — 第一次构建，第二次从缓存加载
        hs1 = ct.Hash160Set()
        hs1.load(
            bin_path=mock_hash160_files["bin_path"],
            idx_path=mock_hash160_files["idx_path"],
            quiet=True,
        )
        hs1.close()

        # 第二次应加载缓存
        hs2 = ct.Hash160Set()
        hs2.load(
            bin_path=mock_hash160_files["bin_path"],
            idx_path=mock_hash160_files["idx_path"],
            quiet=True,
        )
        assert hs2._bloom is not None
        hs2.close()


class TestXOnlySet:
    """XOnlySet 功能测试。."""

    def test_xonly_contains(self, tmp_dir, monkeypatch):
        """XOnlySet 的基本二分查找查询。."""
        import collision_target as ct

        total = 128
        bin_data = _make_bin_data(total, 32)
        idx_data = _make_idx(total)

        bin_path = tmp_dir / "utxo_xonly.bin"
        bin_path.write_bytes(bin_data)

        idx_path = tmp_dir / "utxo_xonly.idx"
        idx_path.write_text(json.dumps(idx_data), encoding="utf-8")

        monkeypatch.setattr(ct, "XONLY_BLOOM", Path("/nonexistent_bloom.bloom"))

        xs = ct.XOnlySet()
        xs.load(bin_path=str(bin_path), idx_path=str(idx_path), quiet=True)

        # 第一条记录应存在
        existing = bin_data[32:64]
        assert existing in xs

        # 不存在的
        nonexistent = b"\xfe" + b"\x00" * 31
        assert nonexistent not in xs

        xs.close()

    def test_xonly_invalid_length(self, tmp_dir, monkeypatch):
        """__contains__ 对非 32 字节输入返回 False。."""
        import collision_target as ct

        total = 16
        bin_data = _make_bin_data(total, 32)
        idx_data = _make_idx(total)

        bin_path = tmp_dir / "utxo_xonly.bin"
        bin_path.write_bytes(bin_data)
        idx_path = tmp_dir / "utxo_xonly.idx"
        idx_path.write_text(json.dumps(idx_data), encoding="utf-8")
        monkeypatch.setattr(ct, "XONLY_BLOOM", Path("/nonexistent_bloom.bloom"))

        xs = ct.XOnlySet()
        xs.load(bin_path=str(bin_path), idx_path=str(idx_path), quiet=True)
        assert b"short" not in xs
        xs.close()
