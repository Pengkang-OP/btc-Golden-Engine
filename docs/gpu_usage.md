# GPU 加速使用指南

## 概述

`collision_engine.py` 从 v1.1.0 开始支持 GPU 加速模式，利用 OpenCL 并行计算大幅提升私钥碰撞搜索速度。

## 前提条件

### 硬件要求
- 支持 OpenCL 1.2+ 的 GPU（NVIDIA、AMD、Intel 均可）
- 最少 1 GB 显存（推荐 4 GB+）

### 软件要求
- Python 3.12+
- pyopencl >= 2024.1
- OpenCL 运行时（平台相关）

#### 安装 pyopencl
```bash
pip install pyopencl>=2024.1
```

#### OpenCL 运行时安装

| 平台 | 安装方法 |
|------|----------|
| **Windows (NVIDIA)** | 安装 NVIDIA 驱动即包含 OpenCL |
| **Windows (AMD)** | 安装 AMD Adrenalin 驱动 |
| **Windows (Intel)** | 安装 Intel GPU 驱动 |
| **Linux (NVIDIA)** | `apt install nvidia-opencl-icd` |
| **Linux (AMD)** | `apt install mesa-opencl-icd` |
| **macOS** | 系统内置 OpenCL 支持 |

## 快速开始

### 列出 OpenCL 设备
```bash
python collision_engine.py --list-gpu
```
输出示例（本机）：
```
发现 3 个 OpenCL 设备:

  [0] NVIDIA GeForce GTX 1660 Ti | NVIDIA CUDA | 24 CU @ 1785 MHz | 6.4 GB global
  [1] Intel(R) Arc(TM) A770 Graphics | Intel(R) OpenCL Graphics | 512 CU @ 2400 MHz | 16.7 GB global
  [2] AMD Ryzen 7 5700X 8-Core Processor | Intel(R) OpenCL | 16 CU @ 0 MHz | 34.3 GB global
```

### 使用 GPU 扫描
```bash
# 默认使用所有可用 GPU
python collision_engine.py --gpu

# 指定 GPU 设备（使用 --list-gpu 中的索引）
python collision_engine.py --gpu --gpu-devices 0

# 多 GPU
python collision_engine.py --gpu --gpu-devices 0,1

# 调整 batch 大小（显存大的 GPU 可用更大 batch）
python collision_engine.py --gpu --gpu-batch-size 131072

# 限制检查数量
python collision_engine.py --gpu --count 1000000
```

## 性能对比（实测）

以下数据在裸 OpenCL kernel（`_benchmark_gpu.py`）条件下测得，3 次运行取均值：

| 模式 | 实测速率 | 说明 |
|------|----------|------|
| CPU 1 线程 | ~8,000 keys/s | 单核 |
| CPU 8 线程 | ~40,000 keys/s | 8 核 AMD Ryzen 7 5700X |
| GTX 1660 Ti | **~871,000 keys/s** | 24 CU / batch=65536 |
| Intel Arc A770 | **~1,651,000 keys/s** | 512 CU @ 2400 MHz / 16 GB / batch=131072 |
| Arc A770 + GTX 1660 Ti | ~572,000 keys/s | 双卡并发 — PCIe 争用显著，建议只用 A770 |

> **瓶颈分析**（perf-optimization 原则）：Arc A770 拥有 512 CU vs GTX 1660 Ti 的 24 CU（21×），但吞吐量仅 ~1.9×，差距源于：
> - **Compute latency-bound**：每个工作项执行完整的 256-bit Montgomery ladder（256 轮点加倍 + 条件点加），是长串行关键路径，难以通过 wavefront 隐藏延迟
> - **本地内存/寄存器压力**：SHA-256 和 RIPEMD-160 需要大量暂存寄存器，限制了每 CU 的并发工作项数
> - **PCIe 争用**：双卡并发时带宽争用反而降低整体吞吐
>
> 优化方向：增大 batch 以提升 GPU 利用率；考虑合并 kernel 减少全局内存访问。

> **注**：多 GPU 并发在本项目实测中未获得线性加速。Intel Arc A770 单卡性能远超 GTX 1660 Ti，双卡并发时因 PCIe 带宽争用合计速率反而不如 A770 单卡。如果系统有多 GPU，建议通过 `--gpu-devices` 只选择最强的 GPU。

## 架构说明

### 数据流
```
Host: 生成 N 个随机私钥 (32B each)
       ↓
clEnqueueWriteBuffer → GPU 显存
       ↓
ec_mul_hash160 kernel (并行运行在每个工作项)
  ├─ 私钥(小端32B) → fe[8] 域元素
  ├─ Montgomery ladder: k * G → Jacobian 点
  ├─ Jacobian → 压缩公钥(33B)
  └─ HASH160 = RIPEMD160(SHA256(pubkey))
       ↓
clEnqueueReadBuffer → Host 内存
       ↓
Host 端二分查找碰撞
```

### 内核文件
`gpu_engine/gpu_kernel.h` 包含完整的 OpenCL C 实现：
- **secp256k1 域运算**：基于 8×32-bit 小端表示的大整数算术
- **Jacobian 点运算**：点加倍和混合坐标加法
- **常数时间标量乘法**：Montgomery ladder + cmov，避免时序侧信道
- **SHA-256 + RIPEMD-160**：完整的 HASH160 哈希链

## 故障排除

### "pyopencl 未安装"
```bash
pip install pyopencl>=2024.1
```

### "未找到任何 OpenCL 设备"
1. 确认安装了 GPU 驱动
2. 运行 `python collision_engine.py --list-gpu` 查看设备
3. Windows 用户安装 [GPU Caps Viewer](https://www.ozone3d.net/gpu_caps_viewer/) 检查 OpenCL 支持

### GPU 性能低于预期
- 增大 `--gpu-batch-size` 以提高 GPU 利用率（需足够显存）
- 确保没有其他 GPU 密集型程序在运行
- 对 Kepler 或更旧的 GPU，batch 建议 ≤ 16384

### 显存不足
- 减小 `--gpu-batch-size`（如 16384 或 8192）
- 每个 batch 占用约 `batch_size * (32 + 20)` 字节 + 内核局部内存
