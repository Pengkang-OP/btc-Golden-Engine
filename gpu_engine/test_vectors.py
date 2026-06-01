#!/usr/bin/env python3
"""GPU Kernel 验证测试向量 — 用于 Linux 实际运行验证。.

使用方法:
  python gpu_engine/test_vectors.py      # 打印期望输出
  # 在有 GPU 的 Linux 上运行 GPU pipeline 后对比输出

HASH160 链路: RIPEMD160(SHA256(compressed_pubkey))
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coincurve import PrivateKey

TEST_CASES = [
    # (name, privkey_int_or_hex)
    ("privkey=1", 1),
    ("privkey=2", 2),
    ("privkey=42", 42),
    (
        "privkey=SECP256K1_ORDER-1",
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364140,
    ),
    ("privkey=EFF", 0xEFF),
]


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def main() -> None:
    results = []
    for name, keyval in TEST_CASES:
        pk = PrivateKey.from_int(keyval if isinstance(keyval, int) else int(keyval, 16))
        pub_comp = pk.public_key.format(compressed=True)
        h160_hex = hash160(pub_comp).hex()
        sha_mid = hashlib.sha256(pub_comp).hexdigest()
        results.append((name, pub_comp.hex(), sha_mid, h160_hex))

    for name, _pub, _sha, h160_hex in results:
        pass

    # 输出为 JSON 供自动比对
    import json

    vec = [{"name": n, "pubkey": p, "sha256": s, "hash160": h} for n, p, s, h in results]
    vec_file = Path(__file__).parent / "kernel_test_vectors.json"
    vec_file.write_text(json.dumps(vec, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
