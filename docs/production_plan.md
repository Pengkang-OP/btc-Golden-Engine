# 生产化计划

## 部署架构

```
┌──────────────┐     ┌──────────────┐
│  Web UI      │     │  Engine      │
│  (FastAPI)   │◄────│  worker(s)   │
│  :8080       │     │  (CPU/GPU)   │
└──────┬───────┘     └──────────────┘
       │
       ▼
┌──────────────┐
│  SQLite DB   │
│  (结果存储)   │
└──────────────┘
```

- **Engine**: 核心碰撞运算（CPU 多线程 / GPU OpenCL），运行 1 个或多个实例
- **Web UI**: FastAPI 服务（`api/`），提供 REST API + WebSocket + Prometheus `/metrics`
- **DB**: SQLite WAL 模式，单文件存储碰撞结果

## 推荐部署方式

### Docker Compose（推荐）

```bash
# 构建并启动
docker compose build
docker compose up -d

# 查看日志
docker compose logs -f

# 运行扫描会话
docker compose run --rm engine python collision_engine.py --gpu --gpu-mode sequential

# 停止
docker compose down
```

### 手动部署

```bash
# 安装依赖
pip install -e .
pip install -e ".[web]"

# 启动 Web UI（后台）
python -m api.server &

# 启动引擎
python collision_engine.py --gpu --gpu-mode sequential
```

## 可观测性

| 端点 / 工具 | 说明 |
|------------|------|
| `GET /api/health` | 引擎整体健康状态（DB / UTXO / GPU） |
| `GET /api/stats` | 实时扫描统计（KPS / 已检查 / 碰撞数） |
| `GET /api/status` | 引擎运行状态 |
| `GET /metrics` | Prometheus 格式指标 |
| WebSocket `/ws` | 实时推送扫描进度 |
| 日志 | `logs/collision.log`（支持 JSON 格式） |

## 配置热重载

通过 `--utxo-refresh-interval` 参数可控制引擎自动检查配置变更和 UTXO 集刷新的间隔（默认 3600 秒）。例如 `--utxo-refresh-interval 60` 即每 60 秒检查一次。

## 优雅关闭

- `SIGTERM`（`docker stop`）：保存 checkpoint，下次恢复
- `SIGINT`（Ctrl+C）：立即保存 checkpoint 停止

## UTXO 自动刷新

引擎支持在运行时自动重新解析 Bitcoin Core `dumptxoutset` 快照并热加载新 UTXO 目标集，无需重启。通过 `--utxo-refresh-interval` 参数启用。

## 后续规划

- [x] Prometheus + Grafana 仪表板部署指南 → `docs/monitoring_guide.md`
- [x] 多引擎分布式扫描（分片 + 协调器）— 参见 `distributed/` 模块
- [x] 告警通知（碰撞命中时邮件/Webhook）
- [x] 性能基准自动回归测试设计方案 → `docs/benchmark_regression.md`
