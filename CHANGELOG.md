# Changelog

## [2.1.0] — 2026-05-31

### Added
- **P3 #3: 分布式扫描 (Master-Worker gRPC 架构)**:
  - `distributed/protocol.proto`: MasterService gRPC 协议定义（注册/心跳/任务分配/碰撞上报/目标信息）
  - `distributed/master.py`: WorkerRegistry（线程安全）+ MasterService gRPC handler
  - `distributed/worker.py`: 注册/心跳/扫描循环 + CPU/GPU 双模式 + 断线重连
  - `distributed/models.py`: WorkerInfo/Assignment dataclass
  - `distributed/README.md`: 分布式部署和配置文档
  - `distributed/test_distributed.py`: 模块单元测试（365 passed）
  - `collision_engine.py`: 提取 `check_single_key/check_single_key_chain/save_result` 为可导入函数；新增 `--distributed/--master-addr/--worker-id` CLI 参数
  - `api/server.py`: `create_app(enable_grpc_master=True)` 可启动 gRPC server
  - `api/routes.py`: 新增 `/api/workers`、`/api/target/download/{filename}` 路由

### Changed
- `pyproject.toml`: 新增 `[distributed]` optional-deps（grpcio, protobuf）
- `requirements.txt`: 添加 `grpcio>=1.60.0`、`protobuf>=4.25.0`、`grpcio-tools`

## [2.0.1] — 2026-05-31

### Added
- **P3 #1: Collision Alert Notifications**: Notifier 集成到碰撞引擎 — `save_result()` 命中时自动触发邮件/Telegram/Webhook 通知
  - `core/config.py`: 新增 `telegram_bot_token`/`telegram_chat_id` 字段
  - `core/notifier.py`: 修复 `_is_configured()` 遗漏 Telegram 检查
  - `collision_engine.py`: `main()` 初始化 Notifier, `save_result()` 调用 `on_hit()`, `_cleanup()` 空安全关闭
- **P3 #2: 性能基准自动回归 (pytest-benchmark)**:
  - `tests/test_benchmark_regression.py`: 3 个 pytest-benchmark 回归测试（CPU single-key、点加法链、mock GPU）
  - `.github/workflows/benchmark.yml`: CI 性能基准工作流（自动对比基线 + PR 注释告警）
  - `tests/benchmark_baseline.json`: Git 版本控制的基线文件
  - `pyproject.toml`: 添加 `pytest-benchmark>=4.0` 依赖
- **ROADMAP #15**: 16 处缺失函数/方法 docstring 补全（collision_engine.py 6, collision_target.py 7, core/config.py 1, core/database.py 1, core/errors.py 1, core/logger.py 1）

### Changed
- Web UI: 地址类型筛选、搜索框、UTXO 刷新状态显示、双 Canvas 图表（速率趋势 + 累积碰撞）

### Fixed
- CI #25–#28 迭代修复：4 处 mypy `type: ignore[assignment]` 遗漏、`Path.stat` mock 补充、ruff 格式纠正、未使用导入清理
- 全部 7 个 CI job 通过（lint, 3 平台测试, security-scan, docker-build, gpu-smoke-test）

## [2.0.0] — 2026-05-31

### Added
- **Web API 层**（FastAPI）：`api/server.py` 提供 REST 端点（`/api/results`, `/api/stats`, `/api/status`, `/api/health`）+ WebSocket `/ws` + Prometheus `/metrics`
- **可观测性基础设施**：
  - `api/metrics.py`：零依赖 Prometheus MetricsRegistry（gauge/counter）
  - `core/logger.py`：JsonFormatter（`LOG_FORMAT=json` 环境变量）
  - `core/config.py`：`check_reload()` 配置热重载支持
- **API 自动化测试**：`tests/test_api.py`（18 个测试，FastAPI TestClient + monkeypatch mock）
- **GPU 管道深度测试**：`TestGPUPipelineHardware`（14 个测试，Intel Arc A770 实机验证）
- **GPU 设备测试**：`tests/test_gpu_device.py`（12 个测试，mock pyopencl）
- **TDR 处理器测试**：`tests/test_tdr_handler.py`（18 个测试，KernelTimer 校准/EMA/safe_sub_batch）
- **异常体系测试**：`tests/test_errors.py`（15 个测试，6 个异常类）
- **管理指标测试**：`tests/test_metrics.py`（11 个测试，gauge/counter 单例）
- **生产部署基础设施**：`__main__.py`（入口）、`docker-compose.yml`（engine + web-ui 编排）
- **优雅关闭**：`_handle_signal()` + `_shutdown_requested` 信号处理 + checkpoint 保存
- **健康检查**：`_health_check()` CLI 模式

### Changed
- `collision_engine.py` 版本号升至 v2.0.0
- `main()` 重构：339 行单块 → 6 个具名函数（~50 行编排）
- 全项目 78 处 `print()` → `logging` 替换
- 12 处非必要 `# noqa` 移除，1 处 `# type:ignore` 替换为 `cast()`
- 13 处缺失类型注解补充
- `core/database.py`：`count_results()` 新增 `address_type` 参数，修复分页 total 不准确

### Fixed
- pyopencl 循环导入：`_remove_mock` 保存/恢复真实模块引用而非 pop+重导入
- GPU 硬件测试 4 个隐藏 bug（`device_name` 字段名、`pick_best_gpu` 返回类型、Hash160s 1D shape、全零 hash160s 断言）
- `_health_check()` 缩进错误（if 块内提前返回）
- 3 个 collision_engine 测试（缺 `_config` mock 导致 UTXO refresher 崩溃）
- `_reset_globals` fixture 缺失 5 个 UTXO 刷新全局变量重置
- 8 处 `target: Hash160Set` → `target: object` 适配 `SwappableTarget`
- `gpu_dispatcher._resolve_device_indices()` 裸 import 添加 try/except 防御
- 3 个 GPU 硬件测试 bug（`device_ids` → `device_indices`、缺 `initialize()`、错误 dict 访问）
- `api/server.py` 7 个 flake8 问题清理
- `BatchResult` 文档字符串 `(batch, 20)` → `(batch*20,)` 1D

### Removed
- 43 个根目录残留开发调试文件（`_debug*.py`, `debug_*.py`, `_compare*.py`, `parse_blocks.py` 等）
- 14 个调试/诊断/冗余文件（`diag_*.txt`, `test_*_out.txt`, `ruff_output2.txt`, `readme.txt`）

## [1.2.0] — 2026-05-30

### Added
- **P2TR (Taproot) 碰撞匹配** (`--p2tr` 参数)：新增约 5400 万 P2TR x-only pubkey 目标
  - `extract_utxo_xonly.py`：从 UTXO 快照提取 P2TR 输出的 32 字节 x-only pubkey
  - `XOnlySet` 类（`collision_target.py`）：mmap + Bloom Filter + 二分查找的 x-only 查询集
  - `bech32m` 编码：自实现 BECH32M 常数（M=0x2BC830A3），生成有效的 bc1p 地址
  - BIP 341 Taproot 调整：`tweak_taproot()` 函数计算 Q = P + t*G
  - 碰撞概率提升：P2PKH (39M) + P2WPKH (43M) + P2TR (54M) = 约 1.37 亿目标
- **`--p2tr` CLI 参数**：启用后额外加载 P2TR x-only 目标集，每次 key 检查额外做一次 tweak 查询
- **GPU 加速支持** (`--gpu` 参数)：利用 OpenCL 并行执行 EC 乘法和 HASH160 计算
  - `gpu_engine/` 目录包含完整的 GPU 基础设施
  - `gpu_kernel.h`：纯 OpenCL C 实现的 secp256k1 EC 乘法和 hash160 内核
  - `gpu_pipeline.py`：pyopencl 管道，封装设备内存管理、kernel 编译和执行
  - `gpu_dispatcher.py`：多 GPU 调度器，支持多个 GPU 并行执行
  - `gpu_device.py`：设备发现和信息查询工具
  - 支持 `--gpu-devices` 指定设备，`--gpu-batch-size` 调整 batch 大小
  - 预期性能：单 GPU ~500K keys/s，多卡 ~1.5M keys/s
- **`--list-gpu` 参数**：列出所有可用的 OpenCL 设备
- **CLI 帮助增强**：epilog 中包含 GPU 和 P2TR 模式使用示例

### Changed
- `collision_engine.py` 版本号升至 v1.2.0
- banner 显示 P2TR 启用状态，使用实际加载的目标集数量
- `CollisionResult` 新增 `p2tr_address` 和 `xonly_hex` 字段
- `check_single_key()` 和 `check_single_key_chain()` 新增 `xonly_target` 参数

## [1.0.0] — 2026-05-29

### Added
- 初步实现比特币私钥碰撞对撞引擎
- 支持顺序扫描和随机扫描两种模式
- 多线程并行（ThreadPoolExecutor）
- Checkpoint 恢复机制（Ctrl+C 安全停止）
- 碰撞结果保存为 JSON 格式
- 支持压缩/非压缩公钥的 P2PKH 和 P2WPKH 地址匹配
- 基于 mmap + 前缀索引 + 二分查找的 3.3 GB 大目标集查询
