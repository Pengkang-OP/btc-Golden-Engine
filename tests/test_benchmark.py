"""性能基准测试 — 测量 keys/s 吞吐量。

注意: GPU 管道测试依赖 mock pyopencl，因此测的是 mock 速率而非真实 GPU 速率。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ── 辅助函数 ──────────────────────────────────────────────────


def _make_utxo(tmp_dir: Path, n: int = 10) -> dict[str, Any]:
    """创建 n 条模拟 utxo_hash160 文件。"""
    records = sorted([b"\x00" * 19 + bytes([i]) for i in range(n)])
    bin_p = tmp_dir / "utxo_hash160.bin"
    with open(bin_p, "wb") as f:
        for r in records:
            f.write(r)
    pm: dict[int, list[int]] = {}
    for i, r in enumerate(records):
        pm.setdefault(r[0], []).append(i)
    idx: dict[str, list[int | bool]] = {}
    for fb in range(256):
        ii = pm.get(fb, [])
        idx[f"{fb:02x}"] = [ii[0], ii[-1], False] if ii else [0, -1, True]
    idx_p = tmp_dir / "utxo_hash160.idx"
    idx_p.write_text(json.dumps({"total": n, "index": idx}))
    return {
        "bin": str(bin_p),
        "idx": str(idx_p),
        "bloom": str(tmp_dir / "utxo_hash160.bloom"),
    }


# ── CPU 基准测试 ──────────────────────────────────────────────


class TestBenchmarkCPU:
    """CPU 模式性能基准测试。"""

    def test_cpu_benchmark_keys_per_second(self, tmp_dir, monkeypatch):
        """测量 CPU 端 check_single_key 吞吐量 (keys/s)。"""
        import collision_engine as ce
        import collision_target as ct

        mock = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct, "HASH_BIN", Path(mock["bin"]))
        monkeypatch.setattr(ct, "HASH_IDX", Path(mock["idx"]))
        monkeypatch.setattr(ct, "BLOOM_FILE", Path(mock["bloom"]))

        target = ct.Hash160Set()
        target.load(quiet=True)

        try:
            # 预热
            ce.check_single_key(1, target, None)

            # 计时运行 ~1 秒
            n = 0
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 1.0:
                ce.check_single_key(n + 1, target, None)
                n += 1
            elapsed = time.perf_counter() - t0

            kps = n / elapsed
            assert kps > 0, f"零吞吐量 ({n} keys in {elapsed:.3f}s)"

            # 输出供手动记录
            print(f"\n[基准] CPU keys/s: {kps:,.0f} ({n} keys in {elapsed:.3f}s)")
        finally:
            target.close()

    def test_cpu_benchmark_point_addition_vs_full_mul(self, tmp_dir, monkeypatch):
        """比较 check_single_key_chain (点加法链) 与 check_single_key (全量 EC 乘) 的速率差异。"""
        import collision_engine as ce
        import collision_target as ct

        mock = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct, "HASH_BIN", Path(mock["bin"]))
        monkeypatch.setattr(ct, "HASH_IDX", Path(mock["idx"]))
        monkeypatch.setattr(ct, "BLOOM_FILE", Path(mock["bloom"]))
        monkeypatch.setattr(ce, "RESULTS_FILE", tmp_dir / "collision_results.json")
        monkeypatch.setattr(ce, "CHECKPOINT_FILE", tmp_dir / "checkpoint.json")

        cfg = {
            "results_db": str(tmp_dir / "results.db"),
            "log_level": "CRITICAL",
        }
        cfg_p = tmp_dir / "bench.conf"
        cfg_p.write_text(json.dumps(cfg))
        ce._init_core(str(cfg_p))

        target = ct.Hash160Set()
        target.load(quiet=True)

        try:
            # 全量 EC 乘基准
            t0 = time.perf_counter()
            for i in range(1000):
                ce.check_single_key(i + 1, target, None)
            full_elapsed = time.perf_counter() - t0
            full_kps = 1000 / full_elapsed

            # 点加法链基准 (1 线程, stride=1)
            counter = ce.SequentialCounter(start=1, limit=1000)
            stride = (1).to_bytes(32, "big")
            t0 = time.perf_counter()
            ce.worker_sequential(counter, target, 0, stride, None)
            chain_elapsed = time.perf_counter() - t0
            chain_kps = 1000 / chain_elapsed

            print(f"\n[基准] 全量 EC 乘:    {full_kps:,.0f} keys/s")
            print(f"[基准] 点加法链加速: {chain_kps:,.0f} keys/s")
            print(f"[基准] 加速比:        {chain_kps / full_kps:.1f}x")

            # 点加法链应比全量 EC 乘快（但 mock pyopencl 等不影响）
            # 不 assert 加速比，只做观察记录
            assert chain_kps > 0 and full_kps > 0
        finally:
            target.close()
            if ce._db is not None:
                ce._db.close()


# ── GPU 基准测试 (mock) ───────────────────────────────────────


@pytest.fixture
def _mock_pyopencl(monkeypatch) -> None:
    """Mock pyopencl 模块 — 与 test_gpu_pipeline.py 一致。"""
    import sys

    cl = MagicMock()
    cl.device_type.GPU = 4
    cl.device_type.CPU = 2
    cl.mem_flags.READ_ONLY = 1
    cl.mem_flags.WRITE_ONLY = 2
    cl.mem_flags.ALLOC_HOST_PTR = 4
    cl.Buffer = MagicMock(return_value=MagicMock())

    dev = MagicMock()
    dev.name = "Mock GPU"
    dev.max_compute_units = 128
    dev.max_work_group_size = 256
    dev.global_mem_size = 8 * 1024 * 1024 * 1024
    dev.local_mem_size = 65536
    dev.max_clock_frequency = 2000
    dev.version = "OpenCL 2.0"
    dev.type = 4
    dev.available = True
    dev.driver_version = "Mock 1.0"

    plat = MagicMock()
    plat.name = "Mock Platform"
    plat.get_devices.return_value = [dev]
    cl.get_platforms.return_value = [plat]
    cl.Context = MagicMock(return_value=MagicMock())
    cl.CommandQueue = MagicMock(return_value=MagicMock())
    cl.Program = MagicMock()
    cl.Program.build.return_value = MagicMock()

    sys.modules["pyopencl"] = cl

    # 确保 kernel 源文件存在
    kdir = Path(__file__).parent.parent / "gpu_engine"
    kfile = kdir / "gpu_kernel.h"
    if not kfile.exists():
        kfile.parent.mkdir(parents=True, exist_ok=True)
        kfile.write_text(
            "__kernel void ec_mul_hash160("
            "__global const uchar* in, __global uchar* out) {}"
        )


class TestBenchmarkGPU:
    """GPU 管道基准测试 (mock pyopencl)。"""

    def test_gpu_pipeline_mock_benchmark(self, tmp_dir, _mock_pyopencl):
        """Mock GPU 管道基本功能验证 + 吞吐量测量。

        pyopencl 被 mock 后，测的是 mock 层的调用速度，
        但可验证 BatchResult 输出格式。
        """
        from gpu_engine.gpu_pipeline import GPUPipeline, BatchResult

        pipe = GPUPipeline(batch_size=4096, quiet=True)
        try:
            t0 = time.perf_counter()
            result = pipe.submit_batch()
            elapsed = time.perf_counter() - t0

            # 验证 BatchResult 类型和字段
            assert isinstance(result, BatchResult)
            assert result.keys_checked == 4096
            assert result.elapsed >= 0
            assert result.keys_per_sec > 0
            assert result.hash160s is not None
            assert result.privkey_bytes is not None
            assert isinstance(result.hit_indices, list)

            print(f"\n[基准] GPU (mock): {result.keys_per_sec:,.0f} keys/s")
            print(f"  batch={result.keys_checked}, elapsed={elapsed:.4f}s")
        finally:
            pipe.close()

    def test_gpu_pipeline_sequential_mode(self, tmp_dir, _mock_pyopencl):
        """验证 GPU 顺序模式能正确推进起始值。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(
            batch_size=256,
            quiet=True,
            mode="sequential",
            sequential_start=1,
        )
        try:
            r1 = pipe.submit_batch()
            assert r1.keys_checked == 256
            assert pipe.sequential_start == 257  # stride = batch_size

            r2 = pipe.submit_batch()
            assert r2.keys_checked == 256
            assert pipe.sequential_start == 513
        finally:
            pipe.close()
