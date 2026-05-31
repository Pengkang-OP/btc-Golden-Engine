# 分布式扫描

基于 Master-Worker 架构的比特币私钥碰撞分布式扫描系统。

## 架构

```
Master (gRPC :50051 + FastAPI :8080)
  ├── Worker 1  (CPU/GPU scan)
  ├── Worker 2  (CPU/GPU scan)
  └── Worker N  (CPU/GPU scan)
```

- **Master**: 同一进程中运行 FastAPI Dashboard (:8080) + gRPC server (:50051)
- **Worker**: 独立进程，向 Master 注册后获取 key 范围，在本地执行扫描
- **通信**: gRPC（注册/心跳/任务分配/结果上报）+ HTTP（目标集下载）

## 安装

```bash
# 安装分布式扫描依赖
pip install grpcio protobuf

# 或使用 optional-deps
pip install -e ".[distributed]"
```

## 用法

### 1. 启动 Master

```bash
# 在具有目标集数据的机器上启动 Master
python -m distributed.master --port 50051
```

Master 自动同时启动：
- gRPC 服务（端口 50051，用于与 Worker 通信）
- FastAPI 服务（端口 8080，提供 Dashboard 和目标集文件下载）

### 2. 启动 Worker

```bash
# 在远程机器上启动 Worker 并连接到 Master
python -m distributed.worker --master-addr 192.168.1.100:50051 --worker-id node-1

# 指定 CPU 核心数和 GPU 设置
python -m distributed.worker \
  --master-addr 192.168.1.100:50051 \
  --worker-id node-2 \
  --cpu-cores 8 \
  --gpu-batch-size 131072

# 仅使用 CPU
python -m distributed.worker --master-addr master:50051 --no-gpu
```

### 3. 查看状态

在浏览器中打开 `http://master-ip:8080`，Dashboard 会显示所有 worker 的状态、扫描进度和碰撞结果。

## 目标集部署

### 方式 A：本地预部署（推荐用于重复运行）

将目标集文件（`utxo_hash160.bin`、`utxo_hash160.idx`、`utxo_hash160.bloom`）直接复制到 Worker 机器的工作目录：

```bash
scp utxo_hash160.bin worker:/path/to/Bitcoin/
scp utxo_hash160.idx worker:/path/to/Bitcoin/
scp utxo_hash160.bloom worker:/path/to/Bitcoin/
```

### 方式 B：HTTP 下载（适用于首次部署）

Worker 首次启动时若检测到本地无目标集文件，会自动向 Master 的 HTTP 端点下载。文件通过 zstd 压缩传输以节省带宽。

## 任务分配策略

采用 sequential 模式的 stride 分区：

1. Master 维护全局 `cursor`，每次为 Worker 分配约 `10^12` 个 key 的连续范围
2. Worker 内部：CPU 模式时再拆分为 `n_threads` 等份（点加法链加速）
3. Worker 内部：GPU 模式时由 `GPUBatchScheduler` 管理
4. 每个 range 只分配一次，无重叠

## 心跳与故障恢复

- Worker 每 5 秒向 Master 发送心跳（含已检查 key 数、当前状态）
- Master 检测到 Worker 心跳超时（30 秒）后，将其未完成 range 回收并重新分配
- Worker 保存本地 checkpoint 作为双保险

## 碰撞结果处理

- Worker 发现碰撞时，优先通过 gRPC `ReportHit` 上报
- 同时调用 `save_result()` 保存到 SQLite 数据库作为备份
- Master 端存储到共享 SQLite 数据库
- ResultDB 的唯一约束自动处理重复上报

## CLI 参数

### Master

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | 50051 | gRPC 端口 |
| `--assignment-size` | 10^12 | 每次分配的 range 大小 |
| `--max-workers` | 10 | gRPC 线程池大小 |

### Worker

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--master-addr` | (必填) | Master 地址 (host:port) |
| `--worker-id` | auto | Worker 唯一标识 |
| `--cpu-cores` | auto | CPU 核心数 |
| `--no-gpu` | — | 禁用 GPU 扫描 |
| `--gpu-batch-size` | 65536 | GPU batch 大小 |
| `--gpu-devices` | 全部 | GPU 设备索引 (逗号分隔) |
| `--count` | 0 | 扫描上限 (0=无限) |
| `--p2tr` | — | 启用 P2TR 匹配 |

## 安全建议

- 推荐在同一局域网内使用（gRPC insecure 模式）
- 生产环境应配置 mTLS 或 VPN 保护 gRPC 通信
- Worker 的 `--worker-id` 应使用唯一且不易冲突的标识
