#!/usr/bin/env python3
"""
对撞匹配工具 — Hash160 目标集加载模块

从 extract_utxo_hash160.py 输出的文件中加载所有带余额地址的 Hash160。
使用 mmap + 前缀索引 + 二分查找 + Bloom Filter，无需将 3.3 GB 数据全部加载到内存。

Bloom Filter:
  - 假阳性率 ~1%，~99% 的 miss 查询在 O(1) 时间内被过滤
  - 首次构建保存到磁盘缓存 (utxo_hash160.bloom)，后续启动即时加载
  - 使用 Kirsch-Mitzenmacher 优化: 2 次 SHA-256 -> 7 个哈希位置

用法:
    from collision_target import Hash160Set

    target = Hash160Set()
    target.load()   # 加载数据

    # 检查一个 Hash160 是否在目标集中
    if b'\x00\x01\x02...' in target:
        print("命中！")
"""

import hashlib
import json
import logging
import mmap
import os
import struct
import threading
import time
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

BASE = Path(__file__).parent
HASH_BIN = BASE / "utxo_hash160.bin"
HASH_IDX = BASE / "utxo_hash160.idx"
BLOOM_FILE = BASE / "utxo_hash160.bloom"
XONLY_BIN = BASE / "utxo_xonly.bin"
XONLY_IDX = BASE / "utxo_xonly.idx"
XONLY_BLOOM = BASE / "utxo_xonly.bloom"

# Bloom Filter 参数
_BLOOM_MAGIC = b"BFLM"
_BLOOM_VERSION = 1
_BLOOM_BITS_PER_ENTRY = 10  # 假阳性率 ~1%
_BLOOM_NUM_HASHES = 7  # k = (m/n) * ln(2) ≈ 7


class _BaseTargetSet:
    """基于 mmap + 前缀索引 + 二分查找 + Bloom Filter 的目标集查询基类。

    子类通过类级常量参数化差异项：
      - ``RECORD_SIZE``: 每条记录的字节数
      - ``RECORD_NAME``: 记录的人类可读名称（用于日志）
      - ``BIN_DEFAULT``: 数据二进制文件默认路径
      - ``IDX_DEFAULT``: 前缀索引文件默认路径
      - ``BLOOM_DEFAULT``: Bloom Filter 缓存文件默认路径
      - ``EXTRACT_SCRIPT``: 对应的提取脚本文件名
    """

    RECORD_SIZE: int = 0
    RECORD_NAME: str = ""
    BIN_DEFAULT: os.PathLike[str] | str = ""
    IDX_DEFAULT: os.PathLike[str] | str = ""
    BLOOM_DEFAULT: os.PathLike[str] | str = ""
    EXTRACT_SCRIPT: str = ""

    __slots__ = (
        "_mmap",
        "_idx",
        "_empty",
        "_total",
        "_count",
        "_fd",
        "_bloom",
        "_bloom_m",
        "_bin_path",
        "_idx_path",
    )

    def __init__(self) -> None:
        """初始化查询集合（未加载）。调用 load() 加载数据。"""
        self._bloom: bytes | None = None
        self._bloom_m = 0
        self._count: int = 0
        self._total: int = 0
        self._bin_path = ""
        self._idx_path = ""

    def load(
        self,
        bin_path: str | None = None,
        idx_path: str | None = None,
        quiet: bool = False,
    ) -> None:
        """加载二进制文件和前缀索引到 mmap。

        Args:
            bin_path: .bin 文件路径。None 则使用默认路径。
            idx_path: .idx 索引文件路径。None 则使用默认路径。
            quiet: True 时抑制信息输出。
        """
        self._bin_path = bin_path or str(self.BIN_DEFAULT)
        self._idx_path = idx_path or str(self.IDX_DEFAULT)
        bin_path = self._bin_path
        idx_path = self._idx_path

        if not os.path.exists(bin_path) or not os.path.exists(idx_path):
            raise FileNotFoundError(
                f"缺少 {self.RECORD_NAME} 数据文件。请先运行:\n"
                f"  python {self.EXTRACT_SCRIPT}\n"
                f"需要: {bin_path}\n"
                f"      {idx_path}"
            )

        with open(idx_path) as f:
            meta = json.load(f)

        self._total = meta["total"]
        self._idx = {}  # first_byte -> (start_idx, end_idx)
        self._empty = set()  # first_bytes with zero entries
        for k, v in meta["index"].items():
            fb = int(k, 16)
            if len(v) == 3 and v[2]:  # [lo, hi, True] = empty
                self._empty.add(fb)
                self._idx[fb] = (v[0], v[1])
            else:  # [lo, hi, False] = has data
                self._idx[fb] = (v[0], v[1])

        self._fd = open(bin_path, "rb")
        self._mmap = mmap.mmap(self._fd.fileno(), 0, access=mmap.ACCESS_READ)
        self._count = self._total

        # Bloom Filter: 尝试加载缓存，失败则构建
        bloom_loaded = self._try_load_bloom(bin_path)
        if not bloom_loaded:
            self._build_bloom(bin_path, quiet=quiet)

        if not quiet:
            logger.info("已加载 %s 个 %s", f"{self._total:,}", self.RECORD_NAME)
            bin_gb = os.path.getsize(bin_path) / 1e9
            logger.info("文件: %s (%.2f GB, mmap)", bin_path, bin_gb)
            if bloom_loaded:
                bloom_mb = self._bloom_m / 8 / 1_048_576
                logger.info("Bloom Filter: %.0f MB (缓存)", bloom_mb)
            else:
                bloom_mb = self._bloom_m / 8 / 1_048_576
                logger.info("Bloom Filter: %.0f MB (已构建)", bloom_mb)

    # ── Bloom Filter 加载/构建 ──────────────────────────────────

    def _bloom_hash_positions(self, h: bytes) -> list[int]:
        """计算 7 个 Bloom Filter 位位置（Kirsch-Mitzenmacher 优化）。"""
        # 只需 2 次 SHA-256 派生所有 k 个位置
        h1 = struct.unpack_from(
            "<I",
            hashlib.sha256(b"\x01" + h).digest(),
        )[0]
        h2 = struct.unpack_from(
            "<I",
            hashlib.sha256(b"\x02" + h).digest(),
        )[0]
        m = self._bloom_m
        return [(h1 + i * h2) % m for i in range(_BLOOM_NUM_HASHES)]

    def _try_load_bloom(self, bin_path: str) -> bool:
        """尝试从磁盘加载缓存的 Bloom Filter。"""
        if not os.path.exists(self.BLOOM_DEFAULT):
            return False

        try:
            with open(self.BLOOM_DEFAULT, "rb") as f:
                magic = f.read(4)
                if magic != _BLOOM_MAGIC:
                    return False
                version = struct.unpack("<B", f.read(1))[0]
                if version != _BLOOM_VERSION:
                    return False
                cached_total = struct.unpack("<Q", f.read(8))[0]
                if cached_total != self._total:
                    return False

                hdr = struct.unpack("<Q32sQ", f.read(8 + 32 + 8))
                # hdr: [bloom_m (8B), bin_digest (32B), byte_size (8B)]
                bloom_m = hdr[0]
                bin_digest = hdr[1]
                actual_digest = _file_sha256(bin_path)
                if bin_digest != actual_digest:
                    return False

                bloom_bytes = f.read()
                expected_bytes = (bloom_m + 7) // 8
                if len(bloom_bytes) != expected_bytes:
                    return False

            self._bloom_m = bloom_m
            self._bloom = bloom_bytes
            return True

        except (OSError, struct.error, ValueError) as e:
            logger.warning("%s Bloom Filter 缓存加载失败: %s", self.RECORD_NAME, e)
            return False

    def _build_bloom(self, bin_path: str, quiet: bool = False) -> None:
        """从 mmap 数据构建 Bloom Filter 并保存到磁盘缓存。"""
        t0 = time.perf_counter()
        rs = self.RECORD_SIZE

        m = self._total * _BLOOM_BITS_PER_ENTRY
        byte_size = (m + 7) // 8
        self._bloom_m = m
        bloom = bytearray(byte_size)

        if not quiet:
            mb = byte_size / 1_048_576
            logger.info(
                "构建 Bloom Filter (%.0f MB, %s 条目)...",
                mb,
                f"{self._total:,}",
            )

        # 批次处理：每 100K 条报告一次进度
        batch_size = 100_000
        total = self._total
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)
            for i in range(batch_start, batch_end):
                h = self._mmap[i * rs : (i + 1) * rs]
                for pos in self._bloom_hash_positions(h):
                    byte_idx = pos >> 3
                    bit_idx = pos & 7
                    bloom[byte_idx] |= 1 << bit_idx

            if not quiet and batch_start > 0 and batch_start % 1_000_000 == 0:
                pct = batch_start / total * 100
                elapsed = time.perf_counter() - t0
                logger.debug(
                    "  [%.0f%%] %s/%s (%.1fs)",
                    pct,
                    f"{batch_start:,}",
                    f"{total:,}",
                    elapsed,
                )

        self._bloom = bytes(bloom)  # 转为不可变 bytes 以节省内存

        # 保存到磁盘
        self._save_bloom(bin_path, bloom)

        elapsed = time.perf_counter() - t0
        if not quiet:
            logger.info(
                "  [100%%] %s/%s (%.1fs) ✓", f"{total:,}", f"{total:,}", elapsed
            )

    def _save_bloom(self, bin_path: str, bloom: bytearray) -> None:
        """保存 Bloom Filter 到磁盘缓存。"""
        bin_digest = _file_sha256(bin_path)
        byte_size = len(bloom)

        header = bytearray()
        header.extend(_BLOOM_MAGIC)  # 4B magic
        header.extend(struct.pack("<B", _BLOOM_VERSION))  # 1B version
        header.extend(struct.pack("<Q", self._total))  # 8B total
        header.extend(struct.pack("<Q", self._bloom_m))  # 8B bit count
        header.extend(bin_digest)  # 32B SHA-256 of data file
        header.extend(struct.pack("<Q", byte_size))  # 8B byte count

        try:
            with open(self.BLOOM_DEFAULT, "wb") as f:
                f.write(header)
                f.write(bloom)
        except OSError as e:
            logger.warning(
                "%s Bloom Filter 缓存写入失败（不影响运行）: %s",
                self.RECORD_NAME,
                e,
            )

    # ── 碰撞查询 ──────────────────────────────────────────────

    def __contains__(self, h: bytes) -> bool:
        """检查指定字节串是否在目标集中。"""
        rs = self.RECORD_SIZE
        if len(h) != rs:
            return False

        fb = h[0]
        if fb in self._empty:
            return False

        bounds = self._idx.get(fb)
        if bounds is None:
            return False
        start, end = bounds
        if start > end:
            return False

        # Bloom Filter 预筛（跳过 ~99% 的 miss）
        if self._bloom is not None:
            bloom_chk = self._bloom  # local ref for speed
            m = self._bloom_m
            h1 = struct.unpack_from(
                "<I",
                hashlib.sha256(b"\x01" + h).digest(),
            )[0]
            h2 = struct.unpack_from(
                "<I",
                hashlib.sha256(b"\x02" + h).digest(),
            )[0]
            for i in range(_BLOOM_NUM_HASHES):
                pos = (h1 + i * h2) % m
                byte_idx = pos >> 3
                bit_idx = pos & 7
                if not (bloom_chk[byte_idx] & (1 << bit_idx)):
                    return False  # 确定不存在

        # 二分查找
        lo = start * rs
        hi = (end + 1) * rs
        mview = self._mmap

        while lo < hi:
            mid = (lo + hi) // 2
            mid = mid - (mid % rs)  # 对齐到字节边界
            chunk = mview[mid : mid + rs]
            if chunk < h:
                lo = mid + rs
            elif chunk > h:
                hi = mid
            else:
                return True
        return False

    def __len__(self) -> int:
        """返回已加载的条目总数。"""
        return self._count

    def close(self) -> None:
        """关闭 mmap、文件描述符并释放 Bloom Filter 资源。"""
        if hasattr(self, "_mmap") and self._mmap:
            try:
                self._mmap.close()
            except Exception:
                pass
            self._mmap = None  # type: ignore[assignment]
        if hasattr(self, "_fd") and self._fd:
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None  # type: ignore[assignment]
        self._bloom = None

    def reload(self, quiet: bool = False) -> None:
        """关闭当前 mmap 并从相同路径重新加载数据。

        用于 UTXO 自动刷新：保持实例 ID 不变，但指向新数据。
        """
        self.close()
        self.load(
            bin_path=self._bin_path,
            idx_path=self._idx_path,
            quiet=quiet,
        )

    def __enter__(self) -> "_BaseTargetSet":
        """上下文管理器入口，返回自身。"""
        return self

    def __exit__(self, *args: object) -> None:
        """上下文管理器出口，关闭 mmap 和文件句柄。"""
        self.close()


class Hash160Set(_BaseTargetSet):
    """基于 mmap + 前缀索引 + 二分查找 + Bloom Filter 的 Hash160 查询集合。

    约 1.65 亿条 × 20 字节 = 3.3 GB，mmap 只占用虚拟地址空间，
    物理内存只加载实际访问的页面（约为扫描时的一小部分）。

    Bloom Filter 额外占用 ~116 MB 内存 + ~1.87 GB 磁盘缓存。
    """

    RECORD_SIZE = 20
    RECORD_NAME = "Hash160"
    BIN_DEFAULT = HASH_BIN
    IDX_DEFAULT = HASH_IDX
    BLOOM_DEFAULT = BLOOM_FILE
    EXTRACT_SCRIPT = "extract_utxo_hash160.py"


def _file_sha256(path: os.PathLike[str] | str) -> bytes:
    """快速计算文件的 SHA-256 摘要（64 KB 块读取）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.digest()


# ── XOnlySet（32 字节 x-only pubkey 查询，用于 P2TR 匹配） ────


class XOnlySet(_BaseTargetSet):
    """基于 mmap + 前缀索引 + 二分查找 + Bloom Filter 的 x-only pubkey 查询集合。

    约 5400 万条 × 32 字节 = 1.7 GB，mmap 只占用虚拟地址空间。
    Bloom Filter 额外占用 ~68 MB 内存。
    """

    RECORD_SIZE = 32
    RECORD_NAME = "x-only pubkey"
    BIN_DEFAULT = XONLY_BIN
    IDX_DEFAULT = XONLY_IDX
    BLOOM_DEFAULT = XONLY_BLOOM
    EXTRACT_SCRIPT = "extract_utxo_xonly.py"


# ── TargetProtocol ────────────────────────────────────────────────


class TargetProtocol(Protocol):
    """碰撞引擎对目标集的 duck-type 接口协议。

    Hash160Set / XOnlySet / SwappableTarget 均实现此协议，
    使 collision_engine.py 中可使用静态类型而非 ``object``。
    """

    def __contains__(self, item: object) -> bool:
        """检查目标是否包含指定项（Protocol stub）。"""
        ...

    def __len__(self) -> int:
        """返回目标集的条目数（Protocol stub）。"""
        ...

    def close(self) -> None:
        """关闭并释放资源（Protocol stub）。"""
        ...


# ── SwappableTarget ─────────────────────────────────────────────


class SwappableTarget:
    """线程安全的单目标集容器，支持原子交换底层数据集。

    ``__contains__`` 和 ``__len__`` 透明代理到当前活跃的集合，
    刷新线程可通过 ``swap()`` 原子替换为新的已加载集合。

    用法::

        wrapper = SwappableTarget(Hash160Set())
        wrapper.target.load(quiet=True)
        # 在碰撞循环中使用: ``h160 in wrapper``
        new_set = Hash160Set()
        new_set.load(...)
        wrapper.swap(new_set)  # 原子交换，旧集自动关闭
    """

    def __init__(self, initial_set: object | None = None):
        """初始化可交换目标集容器，可选设置初始集合。"""
        self._lock = threading.Lock()
        self._set: object | None = initial_set

    def __contains__(self, item: object) -> bool:
        """代理到当前活跃集合的 __contains__ 查询。"""
        s = self._set
        return item in s if s is not None else False  # type: ignore[operator]

    def __len__(self) -> int:
        """返回当前活跃集合的条目数。"""
        s = self._set
        return len(s) if s is not None else 0  # type: ignore[arg-type]

    @property
    def target(self) -> object | None:
        """当前活跃的底层集合（用于需要直接引用集合的场景）。"""
        return self._set

    def swap(self, new_set: object | None = None) -> None:
        """原子替换底层集合。旧集合在锁外关闭。"""
        old: object | None = None
        with self._lock:
            old = self._set
            self._set = new_set
        if old is not None:
            try:
                if hasattr(old, "close"):
                    old.close()
            except Exception as e:
                logger.error("关闭旧目标集时出错: %s", e)

    def close(self) -> None:
        """关闭当前活跃的底层集合并置空。"""
        self.swap(new_set=None)


# ── 快速测试 ────────────────────────────────────────────────────

if __name__ == "__main__":
    s = Hash160Set()
    s.load()
    print(f"共 {len(s):,} 个 Hash160")

    # 测试几个已知有余额的地址
    test_addrs = [
        (
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
            "0000000000000000000000000000000000000000",
        ),
        ("bc1q8kjxlkffnrpja09g5z3sj5pmrqtaz0f6cdx7lh", None),
    ]
    for label, hexhash in test_addrs:
        if hexhash:
            h = bytes.fromhex(hexhash)
            print(f"  {label}: {'命中！' if h in s else '未命中'}")
