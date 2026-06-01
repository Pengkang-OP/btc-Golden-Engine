"""E2E 集成测试 — 模拟完整碰撞检测流水线。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest


def _hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data)) — 与 collision_engine.hash160() 一致。"""
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def _make_utxo(tmp_dir: Path, hash160s: list[bytes]) -> dict[str, Any]:
    """创建 mock utxo_hash160.bin / .idx / .bloom 文件。"""
    hash160s = sorted(hash160s)
    bin_p = tmp_dir / "utxo_hash160.bin"
    with open(bin_p, "wb") as f:
        for h in hash160s:
            f.write(h)
    pm: dict[int, list[int]] = {}
    for i, h in enumerate(hash160s):
        pm.setdefault(h[0], []).append(i)
    idx: dict[str, list[int | bool]] = {}
    for fb in range(256):
        ii = pm.get(fb, [])
        if not ii:
            idx[f"{fb:02x}"] = [0, -1, True]
        else:
            idx[f"{fb:02x}"] = [ii[0], ii[-1], False]
    idx_p = tmp_dir / "utxo_hash160.idx"
    idx_p.write_text(json.dumps({"total": len(hash160s), "index": idx}))
    return {
        "bin": str(bin_p),
        "idx": str(idx_p),
        "bloom": str(tmp_dir / "utxo_hash160.bloom"),
    }


def _make_xonly(tmp_dir: Path, xonlys: list[bytes]) -> dict[str, Any]:
    """创建 mock utxo_xonly.bin / .idx 文件（32 字节）。"""
    xonlys = sorted(xonlys)
    bin_p = tmp_dir / "utxo_xonly.bin"
    with open(bin_p, "wb") as f:
        for x in xonlys:
            f.write(x)
    pm: dict[int, list[int]] = {}
    for i, x in enumerate(xonlys):
        pm.setdefault(x[0], []).append(i)
    idx: dict[str, list[int | bool]] = {}
    for fb in range(256):
        ii = pm.get(fb, [])
        if not ii:
            idx[f"{fb:02x}"] = [0, -1, True]
        else:
            idx[f"{fb:02x}"] = [ii[0], ii[-1], False]
    idx_p = tmp_dir / "utxo_xonly.idx"
    idx_p.write_text(json.dumps({"total": len(xonlys), "index": idx}))
    return {
        "bin": str(bin_p),
        "idx": str(idx_p),
        "bloom": str(tmp_dir / "utxo_xonly.bloom"),
    }


def _mk_cfg(tmp_dir: Path) -> Path:
    """创建 EngineConfig JSON 文件并返回路径。"""
    cfg = {
        "results_db": str(tmp_dir / "results.db"),
        "checkpoint_file": str(tmp_dir / "checkpoint.json"),
        "log_file": str(tmp_dir / "logs" / "e2e.log"),
        "log_dir": str(tmp_dir / "logs"),
        "log_level": "CRITICAL",
    }
    p = tmp_dir / "e2e.conf"
    p.write_text(json.dumps(cfg))
    return p


def _patch_all(
    monkeypatch: pytest.MonkeyPatch,
    ce: Any,
    ct: Any,
    tmp_dir: Path,
    mock: dict[str, Any],
    mock_xonly: dict[str, Any] | None = None,
):
    """monkeypatch collision_target 和 collision_engine 的路径常量。"""
    monkeypatch.setattr(ct.Hash160Set, "BIN_DEFAULT", Path(mock["bin"]))
    monkeypatch.setattr(ct.Hash160Set, "IDX_DEFAULT", Path(mock["idx"]))
    monkeypatch.setattr(ct.Hash160Set, "BLOOM_DEFAULT", Path(mock["bloom"]))
    if mock_xonly:
        monkeypatch.setattr(ct.XOnlySet, "BIN_DEFAULT", Path(mock_xonly["bin"]))
        monkeypatch.setattr(ct.XOnlySet, "IDX_DEFAULT", Path(mock_xonly["idx"]))
        monkeypatch.setattr(ct.XOnlySet, "BLOOM_DEFAULT", Path(mock_xonly["bloom"]))
    monkeypatch.setattr(ce, "RESULTS_FILE", tmp_dir / "collision_results.json")
    monkeypatch.setattr(ce, "CHECKPOINT_FILE", tmp_dir / "checkpoint.json")


def _init_and_scan(
    tmp_dir: Path,
    ce: Any,
    ct: Any,
    start: int = 1,
    limit: int = 10,
    xonly_target: Any = None,
) -> list[dict[str, Any]]:
    """初始化引擎、加载目标集、运行扫描、返回碰撞结果列表。"""
    cfg_path = _mk_cfg(tmp_dir)
    ce._init_core(str(cfg_path))
    target = ct.Hash160Set()
    target.load(quiet=True)
    try:
        counter = ce.SequentialCounter(start=start, limit=limit)
        stride = (1).to_bytes(32, "big")
        ce.worker_sequential(counter, target, 0, stride, xonly_target)
        assert ce._db is not None
        return ce._db.list_results()  # type: ignore[no-any-return]
    finally:
        target.close()
        if ce._db is not None:
            ce._db.close()


class TestE2ECollision:
    """端到端碰撞检测集成测试。"""

    def test_compressed_collision(self, tmp_dir, monkeypatch):
        """压缩公钥 Hash160 碰撞 — 覆盖 P2PKH/P2WPKH。"""
        import collision_engine as ce
        import collision_target as ct
        from coincurve import PrivateKey

        privkey_bytes = (1).to_bytes(32, "big")
        pub = PrivateKey(privkey_bytes).public_key
        h160 = _hash160(pub.format(compressed=True))
        mock = _make_utxo(tmp_dir, [h160])

        _patch_all(monkeypatch, ce, ct, tmp_dir, mock)
        results = _init_and_scan(tmp_dir, ce, ct)

        assert len(results) == 1
        r = results[0]
        assert r["privkey_hex"] == privkey_bytes.hex()
        assert r["address_type"] == "P2WPKH/P2PKH"
        assert r["found_via"] == "compressed"
        assert r["h160_hex"] == h160.hex()

    def test_uncompressed_collision(self, tmp_dir, monkeypatch):
        """非压缩公钥 Hash160 碰撞 — P2PKH (Legacy)。"""
        import collision_engine as ce
        import collision_target as ct
        from coincurve import PrivateKey

        privkey_bytes = (1).to_bytes(32, "big")
        pub = PrivateKey(privkey_bytes).public_key
        h160 = _hash160(pub.format(compressed=False))
        mock = _make_utxo(tmp_dir, [h160])

        _patch_all(monkeypatch, ce, ct, tmp_dir, mock)
        results = _init_and_scan(tmp_dir, ce, ct)

        assert len(results) == 1
        r = results[0]
        assert r["privkey_hex"] == privkey_bytes.hex()
        assert r["address_type"] == "P2PKH (Legacy)"
        assert r["found_via"] == "uncompressed"
        assert r["h160_hex"] == h160.hex()

    def test_p2tr_collision(self, tmp_dir, monkeypatch):
        """P2TR (Taproot) 碰撞 — tweaked x-only output key 匹配。

        只将 P2TR 输出 key 放入 XOnlySet，不在 Hash160Set 中放匹配项。
        """
        import collision_engine as ce
        import collision_target as ct
        from coincurve import PrivateKey, PublicKey

        privkey_bytes = (2).to_bytes(32, "big")
        pub = PrivateKey(privkey_bytes).public_key
        pub_copy = PublicKey(pub.format())
        xonly = ce.tweak_taproot(pub_copy)
        assert xonly is not None and len(xonly) == 32

        dummy = b"\x00" * 20
        mock_h = _make_utxo(tmp_dir, [dummy])
        mock_x = _make_xonly(tmp_dir, [xonly])

        _patch_all(monkeypatch, ce, ct, tmp_dir, mock_h, mock_x)

        cfg_path = _mk_cfg(tmp_dir)
        ce._init_core(str(cfg_path))
        target = ct.Hash160Set()
        target.load(quiet=True)
        xonly_target = ct.XOnlySet()
        xonly_target.load(quiet=True)
        try:
            counter = ce.SequentialCounter(start=2, limit=10)
            stride = (1).to_bytes(32, "big")
            ce.worker_sequential(counter, target, 0, stride, xonly_target)
            assert ce._db is not None
            results = ce._db.list_results()
        finally:
            target.close()
            xonly_target.close()
            if ce._db is not None:
                ce._db.close()

        assert len(results) == 1
        r = results[0]
        assert r["privkey_hex"] == privkey_bytes.hex()
        assert r["address_type"] == "P2TR (Taproot)"
        assert r["found_via"] == "tweaked"
        assert r["xonly_hex"] == xonly.hex()
        assert r["p2tr_address"].startswith("bc1p")

    def test_no_collision(self, tmp_dir, monkeypatch):
        """目标集中无匹配时不应报告碰撞。"""
        import collision_engine as ce
        import collision_target as ct

        dummy = b"\xff" + b"\x00" * 19
        mock = _make_utxo(tmp_dir, [dummy])
        _patch_all(monkeypatch, ce, ct, tmp_dir, mock)
        results = _init_and_scan(tmp_dir, ce, ct, start=100, limit=5)

        assert len(results) == 0
