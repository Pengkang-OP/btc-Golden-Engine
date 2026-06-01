"""Bitcoin UTXO snapshot -> P2TR x-only pubkey extractor.

从 dumptxoutset 快照中提取所有 P2TR 输出的 32 字节 x-only pubkey，
排序后输出为紧凑二进制文件 + 前缀索引。

P2TR 格式:
  1. Compact code 0x04: 32 字节 x-only pubkey
  2. Raw script 0x51 0x20 [32B]: OP_1 + 32B push (BIP 341)

输出:
  utxo_xonly.bin   - 排序的 32 字节 x-only pubkey 数组
  utxo_xonly.idx   - 前缀索引 JSON（首字节 → [lo, hi, is_empty]）
"""

import json
import struct
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
SNAPSHOT = BASE / "utxo_snapshot.dat"
XONLY_BIN = BASE / "utxo_xonly.bin"
XONLY_IDX = BASE / "utxo_xonly.idx"
CHUNK = 100_000
MAX_PARSE_ERRORS = 1000

SNAPSHOT_MAGIC = b"utxo\xff"
NETWORK_MAGIC = bytes.fromhex("f9beb4d9")


def read_compact_size(data: bytes, off: int) -> tuple[int, int]:
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


def read_varint(data: bytes, off: int) -> tuple[int, int]:
    """MSB base-128 VarInt (Bitcoin Core internal)."""
    n = 0
    while True:
        b = data[off]
        off += 1
        n = (n << 7) | (b & 0x7F)
        if (b & 0x80) == 0:
            return n, off
        n += 1


def decompress_amount(x: int) -> int:
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


def parse() -> tuple[list[bytearray], dict] | None:
    """Parse UTXO snapshot and extract P2TR x-only pubkeys into 256 first-byte buckets."""
    print(f"[...] 读取快照 {SNAPSHOT} ...")
    with open(SNAPSHOT, "rb") as f:
        data = f.read()
    off, total_len = 0, len(data)
    print(f"  文件大小: {total_len:,} bytes")

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

    # 256 个首字节桶 (32 字节记录)
    buckets = [bytearray() for _ in range(256)]
    p2tr_compact = 0
    p2tr_raw = 0
    zero_amount = 0
    errs = 0
    parsed = 0
    t0 = time.time()

    while off < total_len:
        try:
            off += 32  # txid
            out_cnt, off = read_compact_size(data, off)

            for _ in range(out_cnt):
                _vout, off = read_compact_size(data, off)
                _code, off = read_varint(data, off)
                amt_comp, off = read_varint(data, off)
                amt = decompress_amount(amt_comp)
                sc, off = read_varint(data, off)

                parsed += 1

                if amt == 0:
                    zero_amount += 1
                    # skip script bytes
                    if sc in {0, 1}:
                        off += 20
                    elif sc in (0x02, 0x03, 0x04):
                        off += 32
                    elif sc == 0x05:
                        pass  # OP_RETURN: 0 bytes
                    else:
                        off += sc - 6
                    continue

                # P2TR compact code 0x04: 32 字节 x-only pubkey
                if sc == 0x04:
                    xonly = data[off : off + 32]
                    off += 32
                    buckets[xonly[0]].extend(xonly)
                    p2tr_compact += 1

                # Raw P2TR: 0x51 0x20 [32B]
                elif sc > 6:  # raw script
                    raw_len = sc - 6
                    if raw_len >= 50000:
                        errs += 1
                        off = min(off + raw_len, total_len)
                        continue
                    script = data[off : off + raw_len]
                    off += raw_len

                    if raw_len == 34 and script[:2] == b"\x51\x20":
                        xonly = script[2:34]
                        buckets[xonly[0]].extend(xonly)
                        p2tr_raw += 1
                    # 其他脚本类型跳过
                # P2PKH(0x00), P2SH(0x01), P2WPKH(0x02), P2WSH(0x03), OP_RETURN(0x05)
                # 非 P2TR 跳过
                elif sc in {0, 1}:
                    off += 20
                elif sc in (0x02, 0x03, 0x04):
                    off += 32
                else:  # 0x05 (OP_RETURN): 0 bytes
                    pass

        except (IndexError, struct.error, MemoryError) as e:
            errs += 1
            if errs <= 10 or errs % 50 == 0:
                print(f"\n  [RECOV] #{errs} off={off:,} err={e}")
            off = min(off + 1, total_len)
            if errs > MAX_PARSE_ERRORS:
                print(f"\n  [FATAL] >{MAX_PARSE_ERRORS} errors. Aborting.")
                break

        if parsed > 0 and parsed % 500000 == 0:
            total_x = sum(len(b) // 32 for b in buckets)
            elapsed = time.time() - t0
            print(
                f"  解析: {parsed:,}/{utxo_count:,}  xonly={total_x:,}  "
                f"错误={errs}  {elapsed:.0f}s",
                end="\r",
            )

    elapsed = time.time() - t0
    total_x = sum(len(b) // 32 for b in buckets)
    rate = f"{parsed / elapsed:,.0f}" if elapsed > 0 else "N/A"
    print(f"\n  解析完成: {elapsed:.0f}s  {rate} outputs/s")
    print(
        f"  P2TR x-only 提取: {total_x:,}  (compact={p2tr_compact:,}  raw={p2tr_raw:,})",
    )
    print(f"  Zero amount: {zero_amount:,}  错误: {errs}")

    stats = {
        "total": total_x,
        "p2tr_compact": p2tr_compact,
        "p2tr_raw": p2tr_raw,
        "zero_amount": zero_amount,
        "errors": errs,
        "elapsed_sec": round(elapsed, 1),
    }
    return buckets, stats


def sort_and_save(buckets: list[bytearray], stats: dict) -> None:
    """Sort each bucket by x-only pubkey and write sorted array + prefix index."""
    n = sum(len(b) // 32 for b in buckets)
    if n == 0:
        print("  没有 P2TR 数据可保存。")
        return
    print(f"  排序 {n:,} 个 x-only pubkey (256 first-byte buckets)...")
    t0 = time.time()

    idx = {}
    total = 0

    with open(XONLY_BIN, "wb") as f:
        for fb in range(256):
            raw = bytes(buckets[fb])
            bn = len(raw) // 32
            if bn == 0:
                idx[fb] = [total, total - 1, True]
                buckets[fb] = None  # type: ignore[call-overload]
                continue
            entries = [raw[i * 32 : (i + 1) * 32] for i in range(bn)]
            entries.sort()
            f.writelines(entries)
            idx[fb] = [total, total + bn - 1, False]
            total += bn
            del entries, raw
            buckets[fb] = None  # type: ignore[call-overload]

    # 填充空首字节的边界
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

    print(f"  -> {XONLY_BIN} ({XONLY_BIN.stat().st_size / 1e9:.3f} GB)")
    with open(XONLY_IDX, "w") as f:
        json.dump(
            {"total": n, "index": {f"0x{b:02x}": v for b, v in idx.items()}},
            f,
        )
    print(f"  -> {XONLY_IDX}")
    stats["sort_sec"] = round(time.time() - t0, 1)


def main() -> None:
    """CLI 入口：解析并提取 P2TR x-only pubkey 数据。."""
    print("=" * 60)
    print("  Bitcoin UTXO -> P2TR x-only pubkey 提取器")
    print("=" * 60)
    if not SNAPSHOT.exists():
        print(f"[ERROR] 快照文件不存在: {SNAPSHOT}")
        sys.exit(1)
    print(f"  快照: {SNAPSHOT} ({SNAPSHOT.stat().st_size / 1e9:.2f} GB)")

    r = parse()
    if r is None:
        sys.exit(1)
    buckets, stats = r
    sort_and_save(buckets, stats)

    n = stats["total"]
    if n > 0:
        print("\n  [OK] P2TR 数据准备就绪!")
        print(f"  P2TR x-only: {n:,}")
        print(f"  碰撞概率提升: +{n / 93_283_974 * 100:.0f}%")
    else:
        print("\n  [INFO] 未找到 P2TR 输出。")


if __name__ == "__main__":
    main()
