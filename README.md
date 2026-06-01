# Bitcoin Private Key Collision Engine

比特币私钥碰撞对撞引擎 — 生成私钥 → 公钥 → HASH160 → 在 UTXO 集中查找匹配。

## 特性

- **CPU 多线程并行**：ThreadPoolExecutor 充分利用多核处理器
- **GPU 加速**（v1.1.0 新增）：OpenCL 并行 EC 乘法 + HASH160，Intel Arc A770 实测 ~1.65M keys/s（详见 [GPU 指南](docs/gpu_usage.md)）
- **两种扫描模式**：顺序扫描（可 checkpoint 恢复）和随机扫描
- **大目标集**：基于 mmap + 前缀索引 + 二分查找，支持 1.65 GB / 1.65 亿条 HASH160 的高效查询
- **碰撞结果保存**：自动保存碰撞结果到 JSON 文件（WIF、地址等详细信息）

## 快速开始

```bash
# 验证环境
python collision_engine.py --help

# CPU 模式：4 线程顺序扫描
python collision_engine.py --threads 4

# CPU 模式：8 线程随机扫描
python collision_engine.py --mode random --threads 8

# GPU 模式（需要 pyopencl）
python collision_engine.py --gpu

# 列出 OpenCL 设备
python collision_engine.py --list-gpu
```

## 安装

### 依赖

- Python >= 3.12
- [coincurve](https://github.com/ofek/coincurve) (libsecp256k1 C 绑定)
- [bech32](https://github.com/pirapira/bech32)
- pyopencl >= 2024.1（仅 GPU 模式需要）

### 安装步骤

```bash
pip install coincurve bech32
# GPU 模式额外安装:
pip install pyopencl>=2024.1
```

### 数据准备

碰撞引擎需要 UTXO 集的 HASH160 数据文件，这些文件由 `extract_utxo_hash160.py` 从 Bitcoin Core 的 `dumptxoutset` 快照生成：

```bash
# 1. Bitcoin Core 中生成快照
bitcoin-cli dumptxoutset utxo_snapshot.dat

# 2. 提取 HASH160
python extract_utxo_hash160.py
```

生成的文件：
- `utxo_hash160.bin` — 排序后的 HASH160 列表（~1.65 GB）
- `utxo_hash160.idx` — 前缀索引

## 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--mode` | `sequential`, `random` | `sequential` | 扫描模式 |
| `--start` | hex | `0x1` | 起始私钥（顺序模式） |
| `--count` | int | 0 (无限) | 要检查的私钥数量 |
| `--threads` | int | CPU 核心数 | CPU 线程数 |
| `--gpu` | flag | — | 启用 GPU 加速模式 |
| `--gpu-mode` | `sequential`, `random` | `random` | GPU 扫描模式 |
| `--gpu-start` | hex | — | GPU 顺序模式起始私钥（支持 checkpoint 恢复） |
| `--gpu-devices` | str | 所有 GPU | GPU 设备索引（逗号分隔） |
| `--gpu-batch-size` | int | 65536 | 每个 GPU batch 的私钥数 |
| `--list-gpu` | flag | — | 列出 OpenCL 设备 |
| `--p2tr` | flag | — | 启用 P2TR (Taproot) 碰撞匹配 |
| `--utxo-refresh` | flag | — | 启用定时 UTXO 刷新 |
| `--utxo-refresh-interval` | int | 3600 | UTXO 刷新间隔（秒） |
| `--distributed` | flag | — | 分布式扫描模式（需要 gRPC） |
| `--master-addr` | str | `localhost:50051` | Master 地址 |
| `--worker-id` | str | auto | Worker 唯一标识 |
| `--health` | flag | — | 健康检查模式 |

## 输出

碰撞结果保存至 `collision_results.json`，每条记录包含：
- `privkey_hex`：私钥（十六进制）
- `wif_compressed` / `wif_uncompressed`：钱包导入格式
- `p2pkh_address_comp` / `p2wpkh_address`：对应的比特币地址
- `h160_hex`：匹配的 HASH160
- `address_type`：地址类型
- `timestamp`：命中时间

## 项目结构

```
g:/Bitcoin/
├── collision_engine.py          # 主入口（CPU + GPU + 分布式模式）
├── collision_target.py          # HASH160 / XOnly 目标集加载（mmap + Bloom filter）
├── extract_utxo_hash160.py      # UTXO 快照 P2PKH/P2WPKH 解析器
├── extract_utxo_xonly.py        # UTXO 快照 P2TR (Taproot) 解析器
├── __main__.py                  # python -m 入口
├── core/                        # 基础设施包
│   ├── config.py                # 配置管理 (EngineConfig + 热重载)
│   ├── logger.py                # 日志系统 (RotatingFileHandler + JSON 格式)
│   ├── errors.py                # 异常体系 (CollisionEngineError)
│   ├── database.py              # SQLite 结果持久化 (WAL 模式)
│   ├── notifier.py              # 碰撞通知（邮件/Telegram/Webhook）
│   └── metrics.py               # Prometheus 指标注册表
├── api/                         # Web API 层 (FastAPI)
│   ├── server.py                # FastAPI 应用 + lifespan 管理
│   ├── routes.py                # REST 端点 + WebSocket
│   ├── state.py                 # 应用状态单例
│   └── templates/index.html     # 仪表盘前端
├── gpu_engine/                  # GPU 加速模块
│   ├── gpu_kernel.h             # OpenCL C 内核（secp256k1 + SHA-256 + RIPEMD-160）
│   ├── gpu_pipeline.py          # pyopencl 管道
│   ├── gpu_dispatcher.py        # 多 GPU 调度器
│   └── gpu_device.py            # 设备发现工具
├── distributed/                 # 分布式扫描 (gRPC)
│   ├── master.py                # Master 协调器 + WorkerRegistry
│   ├── worker.py                # Worker 扫描器 (CPU/GPU)
│   ├── models.py                # WorkerInfo/Assignment dataclass
│   ├── protocol.proto           # gRPC 协议定义
│   └── protocol_pb2*.py         # 生成的 protobuf 代码
├── daemon/                      # Bitcoin Core 可执行文件
│   ├── bitcoind.exe             # Bitcoin Core daemon
│   ├── bitcoin-cli.exe          # RPC 客户端
│   ├── bitcoin-tx.exe           # 交易工具
│   └── bitcoin-wallet.exe       # 钱包工具
├── tests/                       # 测试套件（19 个测试文件, 348+ 测试）
├── docs/
│   ├── gpu_usage.md             # GPU 使用指南 + 基准数据
│   ├── gpu_optimization_plan.md # GPU 优化开发计划（10 项）
│   ├── production_plan.md       # 生产化部署计划
│   ├── monitoring_guide.md      # Prometheus + Grafana 监控
│   └── benchmark_regression.md  # 性能基准回归测试方案
├── utxo_hash160.bin / .idx / .bloom   # P2PKH/P2WPKH 目标数据
├── utxo_xonly.bin / .idx / .bloom     # P2TR (Taproot) 目标数据
├── collision_results.json       # 碰撞结果
├── collision_results.db         # SQLite 持久化
├── logs/collision.log           # 运行日志
├── CHANGELOG.md
└── COPYING.txt
```

## 性能（实测）

以下数据在裸 OpenCL kernel 条件下测得，3 次运行取均值（batch 因显存容量而异）：

| 配置 | 实测速率 | 备注 |
|------|----------|------|
| CPU 单线程 | ~8,000 keys/s | 基准 |
| CPU 8 线程 | ~40,000 keys/s | 8 核 AMD Ryzen 7 5700X |
| GTX 1660 Ti | ~871,000 keys/s | 单 batch 瞬时 / 24 CU / batch=65536 |
| Intel Arc A770 | ~1,651,000 keys/s | 512 CU @ 2400 MHz / 16 GB / batch=131072 |
| Arc A770 + GTX 1660 Ti 并发 | ~572,000 keys/s | PCIe 争用 — 建议只用 A770 |

## 生产部署

### Docker Compose（推荐）

```bash
# 构建并启动所有服务
docker compose build
docker compose up -d

# 查看日志
docker compose logs -f

# 运行扫描会话
docker compose run --rm engine python collision_engine.py --gpu --gpu-mode sequential

# 停止所有服务
docker compose down
```

### 独立运行 Web UI

```bash
# 安装全部依赖
pip install -e .
pip install -e ".[web]"

# 启动 Web UI（后台运行）
python -m api.server &

# 启动碰撞引擎
python collision_engine.py --gpu --gpu-mode sequential
```

### 健康检查

```bash
# 检查引擎各组件状态
python collision_engine.py --health
# 输出示例:
# {
#   "status": "ok",
#   "checks": {
#     "database": {"status": "ok", "result_count": 0},
#     "utxo_data": {"present": true, "size_gb": 1.65},
#     "gpu": {"available": true, "device_count": 1}
#   }
# }
```

### 优雅关闭

引擎支持 SIGTERM 和 SIGINT（Ctrl+C）优雅关闭：

- **SIGTERM**（`docker stop`）：保存 checkpoint 后退出，下次可恢复
- **SIGINT**（Ctrl+C）：立即保存 checkpoint 并停止

## 许可证

参见 [COPYING.txt](COPYING.txt)
