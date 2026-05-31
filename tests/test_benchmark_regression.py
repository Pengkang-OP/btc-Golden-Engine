"""性能基准回归测试 — 使用 pytest-benchmark 自动对比基线。

该文件中的测试由 pytest-benchmark 管理，自动记录性能指标并与
 tests/benchmark_baseline.json 基线对比。退化超过阈值时 CI 告警。

与 tests/test_benchmark.py 的区别：
- test_benchmark.py: 手动 time.perf_counter 计时，仅观察记录
- test_benchmark_regression.py: pytest-benchmark 框架，自动 JSON 基线对比
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ── 辅助函数 ──────────────────────────────────────────────────


def _make_utxo(tmp_dir: Path, n: int = 10) -> dict[str, Any]:
    """创建 n 条模拟 utxo_hash160 文件（与 test_benchmark.py 一致）。"""
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


# ── CPU 基准回归测试 ──────────────────────────────────────────


class TestBenchmarkRegressionCPU:
    """CPU 模式自动回归基准测试。

    测得的吞吐量将自动与 tests/benchmark_baseline.json 中的基线对比。
    退化 >10% 时将触发 CI 告警。
    """

    def test_cpu_single_key(
        self, benchmark, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """基准: CPU check_single_key 吞吐量 (ops/s)。"""
        import collision_engine as ce
        import collision_target as ct

        mock = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct, "HASH_BIN", Path(mock["bin"]))
        monkeypatch.setattr(ct, "HASH_IDX", Path(mock["idx"]))
        monkeypatch.setattr(ct, "BLOOM_FILE", Path(mock["bloom"]))

        target = ct.Hash160Set()
        target.load(quiet=True)

        def bench() -> None:
            ce.check_single_key(42, target, None)

        try:
            benchmark.pedantic(bench, rounds=10, iterations=100)
        finally:
            target.close()

    def test_cpu_chain_sequential(
        self, benchmark, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """基准: CPU worker_sequential 点加法链吞吐量。"""
        import collision_engine as ce
        import collision_target as ct

        mock = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct, "HASH_BIN", Path(mock["bin"]))
        monkeypatch.setattr(ct, "HASH_IDX", Path(mock["idx"]))
        monkeypatch.setattr(ct, "BLOOM_FILE", Path(mock["bloom"]))

        cfg = {
            "results_db": str(tmp_dir / "results.db"),
            "log_level": "CRITICAL",
        }
        cfg_p = tmp_dir / "bench.conf"
        cfg_p.write_text(json.dumps(cfg))
        ce._init_core(str(cfg_p))

        target = ct.Hash160Set()
        target.load(quiet=True)
        counter = ce.SequentialCounter(start=1, limit=1000)
        stride = (1).to_bytes(32, "big")

        def bench() -> None:
            ce.worker_sequential(counter, target, 0, stride, None)

        try:
            benchmark.pedantic(bench, rounds=5, iterations=50)
        finally:
            target.close()
            if ce._db is not None:
                ce._db.close()


# ── GPU 基准回归测试 (mock) ───────────────────────────────────


@pytest.fixture
def _mock_pyopencl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock pyopencl — 与 test_benchmark.py 一致。"""
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
    kdir = Path(__file__).resolve().parent.parent / "gpu_engine"
    kfile = kdir / "gpu_kernel.h"
    if not kfile.exists():
        kfile.parent.mkdir(parents=True, exist_ok=True)
        kfile.write_text(
            "__kernel void ec_mul_hash160("
            "__global const uchar* in, __global uchar* out) {}"
        )


class TestBenchmarkRegressionGPU:
    """Mock GPU 管道自动回归基准测试。"""

    def test_gpu_pipeline_mock(
        self, benchmark, tmp_dir: Path, _mock_pyopencl: None
    ) -> None:
        """基准: mock GPU 管道 submit_batch 吞吐量。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=4096, quiet=True)

        def bench() -> None:
            pipe.submit_batch()

        try:
            benchmark.pedantic(bench, rounds=5, iterations=10)
        finally:
            pipe.close()
