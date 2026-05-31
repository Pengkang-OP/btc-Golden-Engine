# Prometheus + Grafana 监控部署指南

## 概述

本指南为 Bitcoin Collision Engine 提供 Prometheus 指标采集和 Grafana 可视化配置。项目内置零依赖的 Prometheus 兼容指标端点 (`/metrics`)，无需额外客户端库。

## 可用指标

Web UI（FastAPI）自动在 `/metrics` 端点暴露以下指标：

| 指标名 | 类型 | 说明 |
|--------|------|------|
| `keys_per_second` | gauge | 当前扫描速率（KPS） |
| `total_keys_scanned` | gauge | 累计已检查私钥数 |
| `collision_count` | gauge | 碰撞命中总数 |
| `uptime_seconds` | gauge | 引擎运行时长（秒） |
| `python_info` | gauge | Python 运行时信息（带 version 标签） |

## 快速部署（Docker Compose）

在当前项目的 `docker-compose.yml` 中追加以下服务即可集成监控栈：

```yaml
version: "3.8"

services:
  # ... 现有 engine 和 web-ui 服务保持不变 ...

  prometheus:
    image: prom/prometheus:v2.55
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
      - "--storage.tsdb.retention.time=30d"
    restart: unless-stopped

  grafana:
    image: grafana/grafana:11.0
    ports:
      - "3000:3000"
    volumes:
      - grafana-data:/var/lib/grafana
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
      - ./grafana/datasources:/etc/grafana/provisioning/datasources:ro
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=admin
      - GF_INSTALL_PLUGINS=
    restart: unless-stopped

volumes:
  prometheus-data:
  grafana-data:
```

## Prometheus 配置

创建 `prometheus.yml`：

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

# 告警管理器（可选）
# alerting:
#   alertmanagers:
#     - static_configs:
#         - targets: ["alertmanager:9093"]

# 录制规则
rule_files:
  - /etc/prometheus/rules/*.yml

scrape_configs:
  # Prometheus 自身监控
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]

  # Web UI（FastAPI）/metrics 端点
  - job_name: "collision-engine"
    static_configs:
      - targets:
          - "web-ui:8080"
    metrics_path: "/metrics"
    # 如在同一主机，可替换为:
      # - "host.docker.internal:8080"

  # 节点监控（可选 — 采集宿主机 CPU/内存/磁盘）
  # - job_name: "node-exporter"
  #   static_configs:
  #     - targets: ["node-exporter:9100"]
```

## 录制规则

创建目录 `prometheus-rules/recording.yml`，预处理常用查询：

```yaml
groups:
  - name: collision_engine
    interval: 30s
    rules:
      # 5 分钟平均扫描速率
      - record: engine:keys_per_second:avg5m
        expr: |
          avg_over_time(keys_per_second[5m])

      # 碰撞命中率（每百万 keys）
      - record: engine:collision_rate:per_million
        expr: |
          (collision_count / total_keys_scanned) * 1e6

      # 引擎在线率
      - record: engine:uptime_ratio
        expr: |
          (uptime_seconds > 0)
```

## 告警规则

创建目录 `prometheus-rules/alerts.yml`：

```yaml
groups:
  - name: collision_engine_alerts
    interval: 1m
    rules:
      - alert: EngineOffline
        expr: uptime_seconds == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "引擎离线"
          description: "碰撞引擎已离线超过 5 分钟，请检查 web-ui 服务状态。"

      - alert: KeysPerSecondDropped
        expr: keys_per_second < 1000
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "扫描速率异常下降"
          description: "引擎 KPS 降至 {{ $value | humanize }}（阈值: 1000），可能存在性能瓶颈。"

      - alert: CollisionFound
        expr: collision_count > 0
        labels:
          severity: info
        annotations:
          summary: "碰撞命中！"
          description: "已发现 {{ $value }} 个碰撞结果，请查看 /api/results。"

      - alert: EngineRestart
        expr: changes(uptime_seconds[15m]) > 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "引擎发生重启"
          description: "uptime_seconds 在 15 分钟内发生变化，引擎可能已重启。"
```

## Grafana 自动配置

### 数据源自动配置

创建 `grafana/datasources/datasource.yml`：

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

### 仪表板自动配置

创建 `grafana/dashboards/dashboard.yml`：

```yaml
apiVersion: 1

providers:
  - name: "Collision Engine"
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    options:
      path: /etc/grafana/provisioning/dashboards
```

### Grafana 仪表板 JSON

创建 `grafana/dashboards/collision_engine.json`：

```json
{
  "__inputs": [],
  "__requires": [],
  "title": "Bitcoin Collision Engine",
  "uid": "collision-engine",
  "version": 1,
  "timezone": "browser",
  "editable": true,
  "refresh": "15s",
  "panels": [
    {
      "title": "扫描速率 (KPS)",
      "type": "stat",
      "gridPos": {"h": 6, "w": 6, "x": 0, "y": 0},
      "targets": [
        {
          "expr": "keys_per_second",
          "legendFormat": "当前 KPS"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "none",
          "color": {"mode": "thresholds"},
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"value": null, "color": "red"},
              {"value": 10000, "color": "yellow"},
              {"value": 50000, "color": "green"}
            ]
          }
        }
      }
    },
    {
      "title": "累计已扫描",
      "type": "stat",
      "gridPos": {"h": 6, "w": 6, "x": 6, "y": 0},
      "targets": [
        {
          "expr": "total_keys_scanned",
          "legendFormat": "总数"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "short"
        }
      }
    },
    {
      "title": "碰撞次数",
      "type": "stat",
      "gridPos": {"h": 6, "w": 6, "x": 12, "y": 0},
      "targets": [
        {
          "expr": "collision_count",
          "legendFormat": "碰撞"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "none",
          "color": {"mode": "thresholds"},
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"value": null, "color": "blue"},
              {"value": 1, "color": "purple"}
            ]
          }
        }
      }
    },
    {
      "title": "运行时长",
      "type": "stat",
      "gridPos": {"h": 6, "w": 6, "x": 18, "y": 0},
      "targets": [
        {
          "expr": "uptime_seconds",
          "legendFormat": "时长"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "dtdurations"
        }
      }
    },
    {
      "title": "扫描速率趋势 (5 分钟平均)",
      "type": "timeseries",
      "gridPos": {"h": 10, "w": 12, "x": 0, "y": 6},
      "targets": [
        {
          "expr": "engine:keys_per_second:avg5m",
          "legendFormat": "5m 平均 KPS"
        },
        {
          "expr": "keys_per_second",
          "legendFormat": "实时 KPS"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "none"
        }
      }
    },
    {
      "title": "碰撞命中率",
      "type": "timeseries",
      "gridPos": {"h": 10, "w": 12, "x": 12, "y": 6},
      "targets": [
        {
          "expr": "engine:collision_rate:per_million",
          "legendFormat": "每百万 keys 碰撞"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "none"
        }
      }
    },
    {
      "title": "运行状态",
      "type": "table",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 16},
      "targets": [
        {
          "expr": "python_info",
          "format": "table",
          "instant": true
        }
      ]
    }
  ]
}
```

## 手动启动

在 Docker Compose 之外，可单独启动 Prometheus：

```bash
# 启动 Prometheus
docker run -d \
  --name prometheus \
  -p 9090:9090 \
  -v $(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml:ro \
  prom/prometheus:v2.55 \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.retention.time=30d

# 启动 Grafana
docker run -d \
  --name grafana \
  -p 3000:3000 \
  -v $(pwd)/grafana:/etc/grafana/provisioning:ro \
  grafana/grafana:11.0
```

## 验证

```bash
# 检查 Prometheus 目标状态
curl http://localhost:9090/api/v1/targets | jq .

# 测试查询指标
curl 'http://localhost:9090/api/v1/query?query=keys_per_second'

# 直接访问 /metrics
curl http://localhost:8080/metrics
```

## 配置参考

- Prometheus 配置语法: <https://prometheus.io/docs/prometheus/latest/configuration/configuration/>
- Grafana 自动配置: <https://grafana.com/docs/grafana/latest/administration/provisioning/>
- 项目指标端点源码: `api/server.py` | `api/metrics.py`
