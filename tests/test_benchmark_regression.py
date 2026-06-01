"""性能基准回归测试 — 使用 pytest-benchmark 自动对比基线。.

设计文档: docs/benchmark_regression.md

此文件由 GitHub Actions 自动执行 benchmark.yml 调用，用于检测
每次提交与历史基线之间的性能退化（阈值 -10%）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── 辅助函数 ──────────────────────────────────────────────────


def _make_utxo(tmp_dir: Path, n: int = 10) -> dict[str, Any]:
    """创建 n 条模拟 utxo_hash160 文件，与 test_benchmark.py 一致。."""
    records = sorted([b"\x00" * 19 + bytes([i]) for i in range(n)])
    bin_p = tmp_dir / "utxo_hash160.bin"
    with open(bin_p, "wb") as f:
        f.writelines(records)
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


# ── pytest-benchmark 基准测试 ──────────────────────────────────


@pytest.mark.benchmark(
    min_rounds=5,
    max_time=2.0,
    warmup=True,
    warmup_iterations=2,
    disable_gc=True,
    calibration_precision=100,
)
class TestBenchmarkRegression:
    """自动回归基准测试类。.

    测得的吞吐量将自动与 tests/benchmark_baseline.json 中的基线对比。
    """

    def test_cpu_single_key(
        self,
        benchmark: Any,
        tmp_dir: Path,
        monkeypatch: Any,
    ) -> None:
        """基准: CPU single_key 吞吐量。."""
        import collision_engine as ce
        import collision_target as ct

        mock_utxo = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct.Hash160Set, "BIN_DEFAULT", Path(mock_utxo["bin"]))
        monkeypatch.setattr(ct.Hash160Set, "IDX_DEFAULT", Path(mock_utxo["idx"]))
        monkeypatch.setattr(ct.Hash160Set, "BLOOM_DEFAULT", Path(mock_utxo["bloom"]))

        target = ct.Hash160Set()
        target.load(quiet=True)

        def bench() -> None:
            ce.check_single_key(42, target, None)

        result = benchmark.pedantic(bench, rounds=10, iterations=100)
        target.close()
        return result  # type: ignore[no-any-return]

    def test_cpu_chain_sequential(
        self,
        benchmark: Any,
        tmp_dir: Path,
        monkeypatch: Any,
    ) -> None:
        """基准: CPU 点加法链吞吐量。."""
        import collision_engine as ce
        import collision_target as ct

        mock_utxo = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct.Hash160Set, "BIN_DEFAULT", Path(mock_utxo["bin"]))
        monkeypatch.setattr(ct.Hash160Set, "IDX_DEFAULT", Path(mock_utxo["idx"]))
        monkeypatch.setattr(ct.Hash160Set, "BLOOM_DEFAULT", Path(mock_utxo["bloom"]))
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
        counter = ce.SequentialCounter(start=1, limit=100000)
        stride = (1).to_bytes(32, "big")

        def bench() -> None:
            ce.worker_sequential(counter, target, 0, stride, None)

        result = benchmark.pedantic(bench, rounds=5, iterations=50)
        target.close()
        if ce._db is not None:
            ce._db.close()
        return result  # type: ignore[no-any-return]

    def test_gpu_pipeline_mock(
        self,
        benchmark: Any,
        tmp_dir: Path,
        monkeypatch: Any,
    ) -> None:
        """基准: mock GPU 管道吞吐量。."""
        import sys

        # Mock pyopencl (与 test_benchmark.py 一致)
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
        dev.max_mem_alloc_size = 2 * 1024 * 1024 * 1024  # 2 GB

        plat = MagicMock()
        plat.name = "Mock Platform"
        plat.get_devices.return_value = [dev]
        cl.get_platforms.return_value = [plat]
        cl.Context = MagicMock(return_value=MagicMock())
        cl.CommandQueue = MagicMock(return_value=MagicMock())
        cl.Program = MagicMock()

        sys.modules["pyopencl"] = cl

        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=4096, quiet=True)

        def bench() -> None:
            pipe.submit_batch()

        result = benchmark.pedantic(bench, rounds=5, iterations=10)
        pipe.close()
        return result  # type: ignore[no-any-return]
