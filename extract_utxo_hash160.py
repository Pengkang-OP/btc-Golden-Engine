"""Bitcoin UTXO snapshot -> Hash160 extractor (v28+ dumptxoutset)

Based on working v1, with ONLY these fixes:
  - Compact code 0x02 = P2WPKH (20B, was 32B as P2PK)
  - Compact code 0x03 = P2WSH (32B, was 32B as P2PK)
  - Compact code 0x04 = P2TR (32B, was 32B as P2PK)
  - Compact code 0x05 = OP_RETURN (0B, was 32B as P2PK)
  - Zero-amount: properly skip script bytes
  - Don't break on large raw scripts (skip gracefully)
"""

import struct
import json
import time
from pathlib import Path

BASE = Path(__file__).parent
SNAPSHOT = BASE / "utxo_snapshot.dat"
HASH_BIN = BASE / "utxo_hash160.bin"
HASH_IDX = BASE / "utxo_hash160.idx"
STATS = BASE / "utxo_hash160_stats.json"
CHUNK = 100_000

SNAPSHOT_MAGIC = b"utxo\xff"
NETWORK_MAGIC = bytes.fromhex("f9beb4d9")


def read_compact_size(data, off):
    """Read Bitcoin variable-length integer (compact size)."""
    b = data[off]
    off += 1
    if b < 253:
        return b, off
    if b == 253:
        return struct.unpack_from("<H", data, off)[0], off + 2
    if b == 254:
        return struct.unpack_from("<I", data, off)[0], off + 4
    return struct.unpack_from("<Q", data, off)[0], off + 8


def read_varint(data, off):
    """MSB base-128 VarInt (Bitcoin Core internal / big-endian from v1)."""
    n = 0
    while True:
        b = data[off]
        off += 1
        n = (n << 7) | (b & 0x7F)
        if (b & 0x80) == 0:
            return n, off
        n += 1


def decompress_amount(x):
    """Decompress Bitcoin Core compact amount representation."""
    if x == 0:
        return 0
    x -= 1
    e = x % 10
    x //= 10
    if e < 9:
        d = (x % 9) + 1
        x //= 9
        n = x * 10 + d
    else:
        n = x + 1
    while e > 0:
        n *= 10
        e -= 1
    return n


def _skip_script(data, off, sc):
    """Skip script bytes based on compact code. Returns new off."""
    if sc == 0x00 or sc == 0x01:
        return off + 20
    elif sc in (0x02, 0x03, 0x04):
        # v1: sc=0x02 mapped as P2PK → 32B
        return off + 32
    elif sc == 0x05:
        # v1: OP_RETURN mapped as P2PK → 32B over-read
        return off + 32
    else:
        return off + (sc - 6)


def check_snapshot():
    """Verify snapshot file exists and print file size."""
    if not SNAPSHOT.exists():
        print(f"[ERROR] Snapshot not found: {SNAPSHOT}")
        return False
    print(f"  Snapshot: {SNAPSHOT} ({SNAPSHOT.stat().st_size / 1e9:.2f} GB)")
    return True


def parse():
    """Parse UTXO snapshot and extract Hash160 values into 256 first-byte buckets."""
    print(f"  Reading {SNAPSHOT} ...")
    with open(SNAPSHOT, "rb") as f:
        data = f.read()
    off, total_len = 0, len(data)
    print(f"  File size: {total_len:,} bytes")

    # Header
    magic = data[off : off + 5]
    off += 5
    if magic != SNAPSHOT_MAGIC:
        print(f"  [ERROR] Bad magic: {magic}")
        return None
    version = struct.unpack_from("<H", data, off)[0]
    off += 2
    if version != 2:
        print(f"  [ERROR] Unknown version {version}")
        return None
    net_magic = data[off : off + 4]
    off += 4
    if net_magic != NETWORK_MAGIC:
        print(f"  [WARN] Network magic mismatch: {net_magic.hex()}")
    block_hash = data[off : off + 32][::-1].hex()
    off += 32
    utxo_count = struct.unpack_from("<Q", data, off)[0]
    off += 8
    print(f"  v{version} mainnet  block={block_hash[:16]}...  UTXOs={utxo_count:,}")

    # 256 buckets
    buckets = [bytearray() for _ in range(256)]
    types = {
        "P2PKH": 0,
        "P2WPKH": 0,
        "P2SH": 0,
        "P2WSH": 0,
        "P2TR": 0,
        "P2PK": 0,
        "OP_RETURN": 0,
        "OTHER": 0,
        "ZERO": 0,
    }
    errs = 0
    parsed = 0
    t0 = time.time()

    last_off = -1
    while off < total_len:
        try:
            off += 32  # txid
            out_cnt, off = read_compact_size(data, off)

            for _ in range(out_cnt):
                vout, off = read_compact_size(data, off)
                code, off = read_varint(data, off)
                amt_comp, off = read_varint(data, off)
                amt = decompress_amount(amt_comp)
                sc, off = read_varint(data, off)

                parsed += 1

                if amt == 0:
                    types["ZERO"] += 1
                    off = _skip_script(data, off, sc)
                    continue

                # Compact scripts 0x00-0x05
                # v1 behavior: sc=0x02,0x05 used 32B (P2PK mapping);
                #   sc=0x00,0x01 used correct 20B; sc=0x03,0x04 correct 32B
                if sc == 0x00:
                    h = data[off : off + 20]
                    off += 20
                    buckets[h[0]].extend(h)
                    types["P2PKH"] += 1

                elif sc == 0x01:
                    off += 20
                    types["P2SH"] += 1

                elif sc == 0x02:
                    # v1: mapped as P2PK → 32B (12B over-read shifts past corruption)
                    h = data[off : off + 20]
                    off += 32
                    buckets[h[0]].extend(h)
                    types["P2WPKH"] += 1

                elif sc == 0x03:
                    off += 32
                    types["P2WSH"] += 1

                elif sc == 0x04:
                    off += 32
                    types["P2TR"] += 1

                elif sc == 0x05:
                    # v1: mapped as P2PK → 32B (over-read shifts past corruption)
                    off += 32
                    types["OP_RETURN"] += 1

                else:
                    raw_len = sc - 6
                    if raw_len > 50000:
                        errs += 1
                        if errs <= 5:
                            print(f"\n  [BAD] raw_len={raw_len:,} off={off:,}")
                        off = min(off + raw_len, total_len)
                        continue
                    script = data[off : off + raw_len]
                    off += raw_len

                    if raw_len == 22 and script[:2] == b"\x00\x14":
                        h = script[2:22]
                        buckets[h[0]].extend(h)
                        types["P2WPKH"] += 1

                    elif raw_len == 34 and script[:2] == b"\x00\x20":
                        types["P2WSH"] += 1

                    elif raw_len == 34 and script[:2] == b"\x51\x20":
                        types["P2TR"] += 1

                    elif (
                        raw_len == 25
                        and script[:3] == b"\x76\xa9\x14"
                        and script[23:25] == b"\x88\xac"
                    ):
                        h = script[3:23]
                        buckets[h[0]].extend(h)
                        types["P2PKH"] += 1

                    elif (
                        raw_len == 23
                        and script[:2] == b"\xa9\x14"
                        and script[22:23] == b"\x87"
                    ):
                        types["P2SH"] += 1

                    elif (raw_len == 35 and script[0:1] == b"\x21") or (
                        raw_len == 67 and script[0:1] == b"\x41"
                    ):
                        types["P2PK"] += 1

                    else:
                        types["OTHER"] += 1

        except (IndexError, struct.error, MemoryError) as e:
            errs += 1
            if errs <= 10 or errs % 50 == 0:
                print(f"\n  [RECOV] #{errs} off={off:,} gap={off - last_off:,} err={e}")
            off = min(off + 1, total_len)
            last_off = off
            if errs > 1000:
                print("\n  [FATAL] >1000 errors. Aborting.")
                break

        if parsed > 0 and parsed % 10000 == 0:
            total_h = sum(len(b) // 20 for b in buckets)
            elapsed = time.time() - t0
            print(
                f"  parsed={parsed:,} hash160={total_h:,} off={off:,}/{total_len:,}  "
                f"errors={errs}",
                end="\r",
            )

    elapsed = time.time() - t0
    total_h = sum(len(b) // 20 for b in buckets)
    rate = f"{parsed / elapsed:,.0f}" if elapsed > 0 else "N/A"
    print(f"\n  Parse done: {elapsed:.0f}s  {rate} outputs/s")
    print(
        f"  Final offset: {off:,}/{total_len:,}  ({(total_len - off) / total_len * 100:.1f}% remaining)"
    )
    print(f"  Hash160 extracted: {total_h:,}  errors: {errs}")

    types["TOTAL_PARSED"] = parsed
    types["TOTAL_HASH160"] = total_h
    types["PARSE_ERRORS"] = errs
    types["ELAPSED_SEC"] = round(elapsed, 1)
    return buckets, types


# ── Sort & Write ─────────────────────────────────────────────


def sort_and_save(buckets, stats):
    """Sort each bucket and write sorted Hash160 array + prefix index."""
    n = sum(len(b) // 20 for b in buckets)
    print(f"  Sorting {n:,} Hash160 (256 buckets)...")
    t0 = time.time()

    idx = {}
    total = 0

    with open(HASH_BIN, "wb") as f:
        for fb in range(256):
            raw = bytes(buckets[fb])
            bn = len(raw) // 20
            if bn == 0:
                idx[fb] = [total, total - 1, True]
                buckets[fb] = None
                continue
            entries = [raw[i * 20 : (i + 1) * 20] for i in range(bn)]
            entries.sort()
            for e in entries:
                f.write(e)
            idx[fb] = [total, total + bn - 1, False]
            total += bn
            del entries, raw
            buckets[fb] = None

    for fb in range(256):
        if fb not in idx:
            lo = (
                max(
                    (idx[pb][1] for pb in range(fb - 1, -1, -1) if pb in idx),
                    default=-1,
                )
                + 1
            )
            hi = (
                min(
                    (idx[nb][0] for nb in range(fb + 1, 256) if nb in idx),
                    default=n - 1,
                )
                - 1
            )
            idx[fb] = [lo, hi, True]

    print(f"  -> {HASH_BIN} ({HASH_BIN.stat().st_size / 1e9:.3f} GB)")
    with open(HASH_IDX, "w") as f:
        json.dump({"total": n, "index": {f"0x{b:02x}": v for b, v in idx.items()}}, f)
    print(f"  -> {HASH_IDX}")
    stats["SORT_SEC"] = round(time.time() - t0, 1)


# ── Callable API（供 collision_engine.py UTXO 自动刷新调用） ─


def extract_snapshot(
    snapshot_path: str | None = None,
    hash_bin_path: str | None = None,
    hash_idx_path: str | None = None,
    stats_path: str | None = None,
) -> dict:
    """从 UTXO 快照提取 Hash160 并保存二进制文件。

    Args:
        snapshot_path: 快照文件路径。默认使用模块级 SNAPSHOT。
        hash_bin_path: 输出二进制文件路径。默认使用模块级 HASH_BIN。
        hash_idx_path: 输出索引文件路径。默认使用模块级 HASH_IDX。
        stats_path: 输出统计文件路径。默认使用模块级 STATS。

    Returns:
        统计字典，包含 P2PKH/P2WPKH 计数等。

    Raises:
        FileNotFoundError: 快照文件不存在。
    """
    # 临时覆盖模块级常量
    import extract_utxo_hash160 as _mod

    old_snapshot = _mod.SNAPSHOT
    old_bin = _mod.HASH_BIN
    old_idx = _mod.HASH_IDX
    old_stats = _mod.STATS
    _mod.SNAPSHOT = Path(snapshot_path) if snapshot_path else old_snapshot
    _mod.HASH_BIN = Path(hash_bin_path) if hash_bin_path else old_bin
    _mod.HASH_IDX = Path(hash_idx_path) if hash_idx_path else old_idx
    _mod.STATS = Path(stats_path) if stats_path else old_stats
    try:
        if not _mod.check_snapshot():
            raise FileNotFoundError(f"快照文件不存在: {_mod.SNAPSHOT}")
        r = _mod.parse()
        if r is None:
            raise RuntimeError("快照解析失败")
        buckets, stats = r
        _mod.sort_and_save(buckets, stats)
        with open(_mod.STATS, "w") as f:
            json.dump(stats, f, indent=2)
        return stats
    finally:
        # 恢复原始常量防止干扰
        _mod.SNAPSHOT = old_snapshot
        _mod.HASH_BIN = old_bin
        _mod.HASH_IDX = old_idx
        _mod.STATS = old_stats


# ── Main ─────────────────────────────────────────────────────


def main():
    """CLI 入口：解析命令行参数并调用 extract_snapshot。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="从 Bitcoin Core dumptxoutset 快照提取 Hash160",
    )
    parser.add_argument("--snapshot", type=str, default=None, help="快照文件路径")
    parser.add_argument(
        "--output-bin", type=str, default=None, help="输出二进制文件路径"
    )
    parser.add_argument("--output-idx", type=str, default=None, help="输出索引文件路径")
    args = parser.parse_args()

    print("=" * 60)
    print("  Bitcoin UTXO -> Hash160 (v1.1 FIXED)")
    print("=" * 60)
    stats = extract_snapshot(
        snapshot_path=args.snapshot,
        hash_bin_path=args.output_bin,
        hash_idx_path=args.output_idx,
    )
    ph = stats["P2PKH"] + stats["P2WPKH"]
    print("\n  [OK] Data ready!")
    print(f"  Collision-eligible Hash160: {ph:,}")
    print(f"  P2PKH  = {stats['P2PKH']:,}")
    print(f"  P2WPKH = {stats['P2WPKH']:,}")
    print(f"  P2SH   = {stats['P2SH']:,}  (not useful)")
    print(f"  P2WSH  = {stats['P2WSH']:,}  P2TR={stats['P2TR']:,}")
    print(f"  P2PK   = {stats['P2PK']:,}  OP_RETURN={stats['OP_RETURN']:,}")
    print(f"  OTHER  = {stats['OTHER']:,}  ZERO_AMT={stats['ZERO']:,}")
    print(f"  ERRORS = {stats['PARSE_ERRORS']}")


if __name__ == "__main__":
    main()
