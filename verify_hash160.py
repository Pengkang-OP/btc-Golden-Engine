"""Verify utxo_hash160.bin data integrity"""

import json
import time
import hashlib
from pathlib import Path

BASE = Path(__file__).parent
BIN = BASE / "utxo_hash160.bin"
IDX = BASE / "utxo_hash160.idx"
STATS = BASE / "utxo_hash160_stats.json"


def check_size():
    """验证数据文件大小是否为 20 字节对齐并返回条目数。"""
    size = BIN.stat().st_size
    n = size // 20
    if size % 20 != 0:
        print(f"[FAIL] File size {size} not divisible by 20")
        return None
    print(f"[OK]   File size: {size:,} bytes = {n:,} entries")
    return n


def check_sorted(n):
    """通过等间隔采样检查数据是否全局排序。"""
    print("[...]  Checking sort order (sampling every 1000)...")
    t0 = time.time()
    step = 1000
    prev = b"\x00" * 20
    with open(BIN, "rb") as f:
        for i in range(0, n, step):
            f.seek(i * 20)
            cur = f.read(20)
            if cur <= prev:
                print(f"  [FAIL] Entry {i} ({cur.hex()} <= {prev.hex()})")
                return False
            prev = cur
    print(f"[OK]   Sorted (sampled {n // step:,}, {time.time() - t0:.1f}s)")
    return True


def check_bounds(n):
    """读取第一个和最后一个 Hash160 值作为基准。"""
    with open(BIN, "rb") as f:
        first = f.read(20)
        f.seek((n - 1) * 20)
        last = f.read(20)
    print(f"[OK]   First: {first.hex()}")
    print(f"[OK]   Last:  {last.hex()}")
    return first, last


def check_first_byte_distribution(n):
    """统计首字节分布，用于快速检查数据平衡性。"""
    print("[...]  Counting first-byte distribution...")
    t0 = time.time()
    dist = [0] * 256
    with open(BIN, "rb") as f:
        while True:
            chunk = f.read(20 * 100000)
            if not chunk:
                break
            for i in range(0, len(chunk), 20):
                dist[chunk[i]] += 1
    total = sum(dist)
    print(f"[OK]   First-byte distribution: {total:,} total ({time.time() - t0:.1f}s)")
    for b in range(256):
        if dist[b]:
            print(f"       0x{b:02x}: {dist[b]:>10,}")
    return dist


def check_stats_match(n):
    """验证条目数与 stats.json 中的记录一致。"""
    if not STATS.exists():
        print(f"[WARN] {STATS} not found, skipping")
        return
    stats = json.loads(STATS.read_text())
    expected = stats.get("TOTAL_HASH160")
    if expected is None:
        expected = stats.get("P2PKH", 0) + stats.get("P2WPKH", 0)
    if n == expected:
        print(f"[OK]   Count matches stats.json: {n:,}")
    else:
        print(f"[FAIL] Count mismatch: file={n:,}, stats={expected:,}")


def check_index_match(n):
    """验证前缀索引与数据文件内容一致（首字节、边界）。"""
    if not IDX.exists():
        print(f"[WARN] {IDX} not found, skipping")
        return
    idx = json.loads(IDX.read_text())
    idx_total = idx["total"]
    if idx_total != n:
        print(f"[FAIL] Index total mismatch: idx={idx_total:,}, bin={n:,}")
        return
    print(f"[OK]   Index total matches: {n:,}")

    errors = 0
    with open(BIN, "rb") as f:
        for key, (lo, hi, empty) in idx["index"].items():
            fb = int(key, 16)
            if empty:
                if lo != hi + 1:
                    print(f"[WARN] Empty bucket 0x{fb:02x} index anomaly: [{lo}, {hi}]")
                continue
            if 0 <= lo < n:
                f.seek(lo * 20)
                first = f.read(20)
                if len(first) != 20:
                    errors += 1
                    print(f"  [FAIL] 0x{fb:02x} lo={lo} read failed")
                    continue
                if first[0] != fb:
                    errors += 1
                    if errors <= 3:
                        print(f"  [FAIL] 0x{fb:02x} first byte is 0x{first[0]:02x}")
                if 0 <= hi < n:
                    f.seek(hi * 20)
                    last = f.read(20)
                    if last and last[0] != fb:
                        errors += 1
                        if errors <= 3:
                            print(f"  [FAIL] 0x{fb:02x} last byte is 0x{last[0]:02x}")
            # Check boundary: prev bucket last < current bucket first
            prev_b = fb - 1
            while prev_b >= 0:
                prev_info = idx["index"].get(f"0x{prev_b:02x}")
                if prev_info and not prev_info[2]:
                    prev_hi = prev_info[1]
                    if lo > 0 and 0 <= prev_hi < n - 1:
                        f.seek(prev_hi * 20)
                        prev_last = f.read(20)
                        f.seek(lo * 20)
                        cur_first = f.read(20)
                        if prev_last and cur_first and prev_last >= cur_first:
                            errors += 1
                            if errors <= 3:
                                print(
                                    f"  [FAIL] Bucket boundary: 0x{prev_b:02x} last > 0x{fb:02x} first"
                                )
                    break
                prev_b -= 1
    if errors == 0:
        print("[OK]   Index consistent with data")
    else:
        print(f"[FAIL] {errors} index errors")


def check_known_targets(n):
    """测试几个已知地址（创世地址、假地址）的命中/未命中。"""
    from collision_target import Hash160Set

    s = Hash160Set()
    s.load()

    tests = [
        (
            bytes.fromhex("62e907b15cbf27d5425399ebf6f0fb50ebb88f18"),
            "Genesis 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
            True,
        ),
        (bytes.fromhex("deadbeef" * 5), "Fake deadbeef...", False),
        (b"\x00" * 20, "All zeros", False),
        (b"\xff" * 20, "All 0xff", False),
    ]

    all_ok = True
    for h160, desc, expected in tests:
        found = h160 in s
        ok = found == expected
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        tag = "hit" if found else "miss"
        print(f"[{status}] {desc} (expected: {'hit' if expected else 'miss'}) -> {tag}")

    s.close()
    return all_ok


def check_no_duplicates_sampling(n):
    """通过等间隔采样检查是否存在相邻重复条目。"""
    print("[...]  Checking duplicates (sampling every 10000)...")
    t0 = time.time()
    with open(BIN, "rb") as f:
        for i in range(1, n, 10000):
            f.seek((i - 1) * 20)
            a = f.read(20)
            b = f.read(20)
            if a == b:
                print(f"  [FAIL] Duplicate: entry {i} {a.hex()}")
                return False
    print(
        f"[OK]   No duplicates (sampled {n // 10000:,} pairs, {time.time() - t0:.1f}s)"
    )
    return True


def check_checksum():
    """计算完整数据文件的 SHA256 校验和。"""
    print("[...]  Computing SHA256 checksum...")
    t0 = time.time()
    sha = hashlib.sha256()
    with open(BIN, "rb") as f:
        while chunk := f.read(16 * 1024 * 1024):
            sha.update(chunk)
    h = sha.hexdigest()
    print(f"[OK]   SHA256: {h} ({time.time() - t0:.1f}s)")
    return h


def main():
    """入口：逐项执行数据完整性检查并输出汇总结果。"""
    print("=" * 60)
    print("utxo_hash160.bin Data Integrity Verification")
    print("=" * 60)

    if not BIN.exists():
        print(f"[FAIL] {BIN} does not exist!")
        return 1

    results = []

    n = check_size()
    if n is None:
        return 1
    results.append(("File size", True))

    check_stats_match(n)

    ok = check_sorted(n)
    results.append(("Sort", ok))

    ok = check_no_duplicates_sampling(n)
    results.append(("Dedup", ok))

    check_bounds(n)
    check_first_byte_distribution(n)
    check_index_match(n)

    ok = check_known_targets(n)
    results.append(("Known addrs", ok))

    sha = check_checksum()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"Result: {passed} passed, {failed} failed")
    print(f"SHA256: {sha}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    exit(main())
