# ============================================================
# Bitcoin Collision Finder — 多阶段 Docker 构建
# ============================================================

# ── Stage 1: Builder ──────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# 系统构建依赖（OpenCL 支持）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    pkg-config \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单
COPY requirements.txt .
COPY pyproject.toml .

# 安装核心依赖 + Web 可选依赖
# （GPU 依赖 pyopencl 需要在宿主机有 OpenCL ICD 和运行时，容器内略去）
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir ".[web]"

# ── Stage 2: Runtime ─────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# 运行时系统依赖（OpenCL ICD 加载器）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    pkg-config \
    ocl-icd-libopencl1 \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制已安装的 pip 包
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制项目源码
COPY . .

# 默认入口：碰撞引擎帮助
ENTRYPOINT ["python", "-m", "collision_engine"]
CMD ["--help"]

# ── 使用示例（运行时覆盖）:
#   # 碰撞引擎模式
#   docker run --rm bitcoin-collision-finder --gpu --gpu-mode sequential --gpu-start 0x1
#
#   # Web UI 模式
#   docker run --rm --entrypoint uvicorn bitcoin-collision-finder \
#     api.server:app --host 0.0.0.0 --port 8080
