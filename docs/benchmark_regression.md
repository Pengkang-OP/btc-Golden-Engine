# 性能基准自动回归测试设计

## 目标

在 CI 中自动运行性能基准测试，对比每次提交与历史基线（baseline）的吞吐量差异，在性能退化超过阈值时告警。

## 设计原则

1. **零依赖外部服务**：baseline 数据存储在 Git 管理的 JSON 文件内，无需外部数据库。
2. **CI 兼容**：测试在无 GPU 的 CI runner 上运行 mock 模式，CPU 吞吐量在同一规格 runner 上应保持稳定。
3. **可观测**：每次 CI 运行输出结构化性能报告，方便人工审查趋势。
4. **非阻塞**：性能退化不阻断 CI，仅生成告警注释到 PR 中。

## 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 基准框架 | `pytest-benchmark>=4.0` | 内置 min/max/mean/std/rounds，自动生成 JSON 历史 |
| 基线存储 | `tests/benchmark_baseline.json` | Git 版本控制，PR diff 可见 |
| CI 集成 | GitHub Actions + pytest-benchmark compare | 提取 delta 并检查阈值 |
| 通知 | PR comment (GitHub Actions) | 不阻塞 CI，提供可视化对比 |

## 实现方案

### 步骤 1：添加 pytest-benchmark 依赖

```toml
# pyproject.toml
[project.optional-dependencies]
dev = [
    "pytest-benchmark>=4.0",
    # ... 现有依赖
]
```

### 步骤 2：创建基准测试

利用现有 `test_benchmark.py` 中的测试，包装 `pytest.mark.benchmark` 装饰器：

```python
# tests/test_benchmark_regression.py
"""性能基准回归测试 — 使用 pytest-benchmark 自动对比基线。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_utxo(tmp_dir: Path, n: int = 10) -> dict:
    """创建模拟 UTXO 数据集。"""
    # 复用 test_benchmark.py 中的 _make_utxo 实现
    # 建议提取到 conftest.py 的 helper 中
    ...


@pytest.mark.benchmark(
    min_rounds=5,
    max_time=2.0,
    warmup=True,
    warmup_iterations=2,
    disable_gc=True,
    calibration_precision=100,
)
class TestBenchmarkRegression:
    """自动回归基准测试类。

    测得的吞吐量将自动与 tests/benchmark_baseline.json 中的基线对比。
    """

    def test_cpu_single_key(self, benchmark, tmp_dir, monkeypatch):
        """基准: CPU check_single_key 吞吐量。"""
        import collision_engine as ce
        import collision_target as ct

        mock_utxo = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct, "HASH_BIN", Path(mock_utxo["bin"]))
        monkeypatch.setattr(ct, "HASH_IDX", Path(mock_utxo["idx"]))
        monkeypatch.setattr(ct, "BLOOM_FILE", Path(mock_utxo["bloom"]))

        target = ct.Hash160Set()
        target.load(quiet=True)

        def bench():
            ce.check_single_key(42, target, None)

        # benchmark fixture 自动多次运行 bench()
        result = benchmark.pedantic(bench, rounds=10, iterations=100)
        target.close()
        return result

    def test_cpu_chain_sequential(self, benchmark, tmp_dir, monkeypatch):
        """基准: CPU worker_sequential 点加法链吞吐量。"""
        import collision_engine as ce
        import collision_target as ct

        mock_utxo = _make_utxo(tmp_dir)
        monkeypatch.setattr(ct, "HASH_BIN", Path(mock_utxo["bin"]))
        monkeypatch.setattr(ct, "HASH_IDX", Path(mock_utxo["idx"]))
        monkeypatch.setattr(ct, "BLOOM_FILE", Path(mock_utxo["bloom"]))

        target = ct.Hash160Set()
        target.load(quiet=True)
        counter = ce.SequentialCounter(start=1, limit=100000)
        stride = (1).to_bytes(32, "big")
        ce._init_core(str(tmp_dir / "bench.conf"))

        def bench():
            ce.worker_sequential(counter, target, 0, stride, None)

        result = benchmark.pedantic(bench, rounds=5, iterations=50)
        target.close()
        if ce._db is not None:
            ce._db.close()
        return result

    def test_gpu_pipeline_mock(self, benchmark, tmp_dir, monkeypatch):
        """基准: mock GPU 管道吞吐量。"""
        from gpu_engine.gpu_pipeline import GPUPipeline

        pipe = GPUPipeline(batch_size=4096, quiet=True)

        def bench():
            pipe.submit_batch()

        result = benchmark.pedantic(bench, rounds=5, iterations=10)
        pipe.close()
        return result
```

### 步骤 3：基线管理

首次运行后生成基线文件：

```bash
# 生成基线（在稳定分支上运行一次）
python -m pytest tests/test_benchmark_regression.py \
  --benchmark-autosave \
  --benchmark-only

# 将生成的文件重命名为基准路径
mv .benchmarks/*/*/test_benchmark_regression.json tests/benchmark_baseline.json
```

基线文件格式（由 `pytest-benchmark` 自动生成）：

```json
{
  "test_cpu_single_key": {
    "min": 0.000123,
    "max": 0.000145,
    "mean": 0.000132,
    "stddev": 0.000008,
    "rounds": 10,
    "iterations": 100,
    "params": null
  },
  "test_cpu_chain_sequential": { ... },
  "test_gpu_pipeline_mock": { ... }
}
```

### 步骤 4：CI 集成（GitHub Actions）

创建 `.github/workflows/benchmark.yml`：

```yaml
name: Performance Benchmark

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  benchmark:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          pip install -e ".[dev]"
          pip install pytest-benchmark

      - name: Run benchmarks
        run: |
          python -m pytest tests/test_benchmark_regression.py \
            --benchmark-json output.json \
            --benchmark-only \
            -q

      - name: Compare with baseline
        id: compare
        run: |
          python -c "
          import json

          with open('tests/benchmark_baseline.json') as f:
              baseline = json.load(f)
          with open('output.json') as f:
              current = json.load(f)

          failed = []
          report_lines = ['## 性能基准回归报告', '', '| 测试 | 基线 (ops/s) | 当前 (ops/s) | 变化 | 状态 |',
                          '|------|-------------|-------------|------|------|']

          for test_name, b_data in baseline.items():
              c_data = current['benchmarks'].get(test_name, {})
              if not c_data:
                  continue

              b_ops = 1.0 / b_data['mean'] if b_data['mean'] > 0 else 0
              c_ops = 1.0 / c_data['stats']['mean'] if c_data['stats']['mean'] > 0 else 0
              change_pct = ((c_ops - b_ops) / b_ops) * 100 if b_ops > 0 else 0

              status = '✅' if change_pct > -5 else '❌'
              if change_pct < -5:
                  failed.append(test_name)

              report_lines.append(
                  f'| {test_name} | {b_ops:,.0f} | {c_ops:,.0f} | {change_pct:+.1f}% | {status} |'
              )

          report = '\n'.join(report_lines)
          print(report)

          # 输出到 summary
          with open(os.environ.get('GITHUB_STEP_SUMMARY', '/dev/null'), 'a') as f:
              f.write(report)

          if failed:
              print(f'::warning ::性能退化: {failed}')
          "
```

### 步骤 5：阈值与衰减策略

| 指标 | 阈值 | 处理方式 |
|------|------|----------|
| CPU single-key 吞吐量 | `变化 < -10%` | CI 告警 |
| CPU 点加法链吞吐量 | `变化 < -10%` | CI 告警 |
| Mock GPU 吞吐量 | `变化 < -15%` | CI 告警（mock 层波动较大） |
| 单次标准差 > 5% | 警告 | 表明测试稳定性不足，需排查环境差异 |

阈值采用 **-5% 警告、-10% 阻断** 两级。首次退化只告警不阻断，连续两次退化则阻断。

### 步骤 6：定期基线更新

在 main 分支合并后自动更新基线：

```bash
# GitHub Actions: on push to main
python -m pytest tests/test_benchmark_regression.py \
  --benchmark-autosave \
  --benchmark-only

# 将 latest 复制为新的基线
cp .benchmarks/*/*/test_benchmark_regression.json tests/benchmark_baseline.json
```

## 现有测试兼容性

`tests/test_benchmark.py` 中的现有测试保持不动，新增 `tests/test_benchmark_regression.py` 专门用于回归对比。两个文件的职责划分：

| 文件 | 用途 | 运行条件 |
|------|------|----------|
| `test_benchmark.py` | 手动观察基准 | `pytest -k benchmark`，无 pytest-benchmark 要求 |
| `test_benchmark_regression.py` | 自动回归检查 | `pytest --benchmark-only`，需要 pytest-benchmark |

## 预期效果

- **每次 PR**：CI 自动运行基准并注释性能变化报告
- **无回归**：保持基线 → ✅ 图标
- **性能退化**：标记退化测试并给出变化百分比
- **趋势可查**：基线的 Git 历史可回溯每次版本的性能快照

## 参考实现

- `pytest-benchmark` 官方文档: <https://pytest-benchmark.readthedocs.io/>
- 项目现有基准测试: `tests/test_benchmark.py`
- 吞吐量计算方式: `ops/s = 1 / mean_seconds`
