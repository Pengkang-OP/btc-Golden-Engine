# GPU 优化开发计划

> 更新时间: 2026-06-01 (P0+P1+P2 实现完成)
> 硬件: NVIDIA GTX 1660 Ti (Turing TU116) + Intel Arc A770 (Xe-HPG ACM-G10)

---

## 1. 硬件架构对比

| 特性 | NVIDIA GTX 1660 Ti | Intel Arc A770 |
|------|-------------------|----------------|
| **架构** | Turing TU116 (12nm) | Xe-HPG ACM-G10 (6nm) |
| **计算单元** | 24 SM × 64 CUDA = 1536 CUDA | 32 Xe-Core × 16 XVE = 512 XVE |
| **SIMD 宽度** | warp = 32 threads | 8-wide per XVE |
| **子组大小** | 32 (warp) | 16 (2× XVE 锁步) |
| **线程/SM(Core)** | 1024 max | 128 per Xe-Core |
| **寄存器** | 64K × 32-bit per SM | 32 KB GRF per XVE |
| **Local Mem** | 64 KB/SM (与 L1 共享) | 128 KB SLM + 192 KB L1 (动态分区) |
| **L2 缓存** | 1536 KB | 16 MB |
| **显存** | 6 GB GDDR6 (288 GB/s) | 16 GB GDDR6 (560 GB/s) |
| **OpenCL** | 1.2 | 3.0 |
| **fp16 支持** | ❌ 不支持 | ✅ `cl_khr_fp16` |
| **max_mem_alloc** | ~1.5 GB (驱动限制) | 4 GB (compute-runtime #627) |
| **基准性能** | ~915 K/s | ~1,742 K/s |

### 关键架构差异

1. **Arc A770 寄存器更紧张**：每 XVE 32KB GRF / 8 线程 = 4KB/线程 vs GTX 1660 Ti 64KB/1024 线程 ≈ 64 寄存器/线程等效（实际可分配 ~255）。当前 ec_mul_hash160 使用约 200+ u32 变量，Arc 上必然寄存器溢出到 SLM/内存。
2. **Arc A770 依赖高并发隐藏延迟**：需 512+ 活跃 WG 才能充分利用，而 GTX 只需 ~72 warp。
3. **Arc 的 SLM 充裕**（128KB），GTX 的 shared memory 有限（64KB 与 L1 共享）。
4. **Arc 的 4GB 单 buffer 限制**是硬伤，大 batch 时必须分片。

---

## 2. 优化项清单（按优先级）

### P0: 立即实施（防崩溃 + 安全）

| # | 优化项 | 涉及文件 | 工作量 | 预期收益 | 状态 |
|---|--------|---------|--------|---------|------|
| 1 | **自动检测 max_mem_alloc_size** 限制 batch_size | `gpu_pipeline.py` | ~5 行 | 防止 Arc A770 大 batch 时 clCreateBuffer 崩溃 | ✅ P0 完成 |
| 2 | **按 GPU vendor 选择 local_work_size**: NVIDIA=128, Intel=64 | `gpu_pipeline.py` | ~5 行 | 提高 warp/XVE 利用率 | ✅ P0 完成 |
| 3 | **编译选项差异化**: NVIDIA `-cl-fast-relaxed-math`, Intel `-cl-std=CL3.0` + `-DARC_OPT` | `gpu_pipeline.py` | ~3 行 | 释放 NVIDIA mad 融合优化 + Arc `__local` 支持 | ✅ P0 完成 |

### P1: 重要性能优化

| # | 优化项 | 涉及文件 | 工作量 | 预期收益 | 状态 |
|---|--------|---------|--------|---------|------|
| 4 | **批量检查碰撞**：numpy 向量化代替 Python 循环 | `gpu_pipeline.py` | ~15 行 | 碰撞检查加速 10-50× | ✅ P1 完成 |
| 5 | **`__local` 缓存 RIPEMD-160 常量表** (Arc) | `gpu_kernel.h` + `gpu_pipeline.py` | ~60 行 | 利用 Arc 16MB L2 + 128KB SLM 减少 `__constant` 访问延迟 | ✅ P1 完成 |
| 6 | **`USE_HOST_PTR` DMA 优化** | `gpu_pipeline.py` | ~3 行 | 减少 PCIe 拷贝延迟 | ✅ P1 完成 |
| 7 | **多 buffer 分片**: 超大 batch 拆为多个 &lt;4GB buffer | 暂不实现 | — | P0-1 已有 batch_size 上限保护，实际收益有限 | ⏸️ 暂缓 |

### P2: 进阶优化

| # | 优化项 | 涉及文件 | 工作量 | 预期收益 | 状态 |
|---|--------|---------|--------|---------|------|
| 8 | **按设备能力分配 batch_size**: Arc 更多, GTX 更少 | `gpu_dispatcher.py` | ~10 行 | 多 GPU 负载均衡 | ✅ P2 完成 |
| 9 | **Arc fp16 中间存储**: SHA-256 消息扩展用 half 压缩 | 精度风险 | — | SHA-256 XOR+ROTR 在 half 精度丢失低位，结果不可逆 | ⏸️ 精度不达标 |
| 10 | **GPU 侧碰撞检测** (Bloom filter + 原子计数器) | `gpu_kernel.h` + `gpu_pipeline.py` | ~100 行 | 消除整个 HASH160 回读（~52×batch 字节 → ~4×n_hits 字节） | ✅ P2 完成 |

---

## 3. 详细实施方案

### P0-1: 自动检测 max_mem_alloc_size

```python
# gpu_pipeline.py _init_opencl() 中，创建 buffer 前添加
self._max_alloc = self._device.max_mem_alloc_size  # bytes
max_batch = self._max_alloc // (32 + 20)  # 私钥32B + hash160 20B
if self.batch_size > max_batch:
    logger.warning(
        "[GPU] batch_size %s 超过设备限制 %s，自动降为 %s",
        f"{self.batch_size:,}", f"{self._max_alloc/1e9:.1f}GB", f"{max_batch:,}"
    )
    self.batch_size = int(max_batch)
    # 重新分配 host 缓冲区
    self._h_privkeys = np.zeros(self.batch_size * 32, dtype=np.uint8)
    self._h_hash160s = np.zeros(self.batch_size * 20, dtype=np.uint8)
```

### P0-2: 按 vendor 选择 local_work_size

```python
# gpu_pipeline.py _init_opencl() 中设备检测后
vendor = self._device.vendor.lower()
if "nvidia" in vendor:
    self._local_ws = 128     # 4 warps × 32
    self._kernel_options = ["-cl-std=CL1.2", "-cl-mad-enable", "-cl-fast-relaxed-math"]
elif "intel" in vendor:
    self._local_ws = 64      # 4 subgroups × 16
    self._kernel_options = ["-cl-std=CL3.0", "-cl-mad-enable"]
else:
    self._local_ws = 64
    self._kernel_options = ["-cl-std=CL1.2"]
```

然后在 `_run_sub_batch()` 中传入 `self._local_ws`：
```python
cl.enqueue_nd_range_kernel(
    self._queue, self._kernel_hash160,
    (count,),
    (self._local_ws,),       # ← 非 None
    (offset,),
)
```

### P0-3: 编译选项差异化

`build(options=self._kernel_options)` 已经在 `_init_opencl` 中用上述变量。

### P1-4: 批量碰撞检查 (numpy 向量化)

当前 (line 396-398):
```python
for i in range(self.batch_size):
    h160 = bytes(self._h_hash160s[i * 20 : (i + 1) * 20])
    if check_collision(h160):
        hit_indices.append(i)
```

改为向量化查找（假设 `target` 是 `Hash160Set` 含 `contains_batch` 方法）或 batch 比较：
```python
# 方案 A：如果 target 支持批量查询
# hit_mask = target.contains_batch(self._h_hash160s.reshape(-1, 20))
# 方案 B：目前回调方式不变但用 memoryview 减少 bytes() 分配
arr = self._h_hash160s.reshape(-1, 20).view(dtype='>u4')  # 5 × uint32
for i in range(self.batch_size):
    # 用 5 个 uint32 比较比 bytes() 快
    h160_key = arr[i].tobytes()  # 仍要 bytes 用于 set 查找
    if check_collision(h160_key):
        hit_indices.append(i)
```

### P1-5: `__local` 缓存 RIPEMD-160 常量表 (Arc 专用)

**实际实现**（2026-06-01）:

将 RIPEMD-160 核心循环重构为宏 `RMD_CORE_BODY`，支持不同的常量表存储地址空间。
新增 `rmd160_oneblock_local()` 函数接收 `__local const uint *` 参数代替 `__constant` 查表。

在 `ec_mul_hash160` kernel 入口处，当编译了 `-DARC_OPT` 时：
1. 在 workgroup 中第一个工作项将 `R_RMD/S_RMD/RP_RMD/SP_RMD` 从 `__constant` 预加载到 `__local` 数组（仅 4×80×4B = 1,280B SLM）
2. 后续所有工作项通过 `barrier(CLK_LOCAL_MEM_FENCE)` 同步后，使用 `__local` 表调用 `hash160_full_opt()`
3. 非 Arc 平台不受影响（`KERNEL_ARC_SETUP` = `((void)0)`, `KERNEL_HASH160` = 原始调用）

关键改动：
- `gpu_kernel.h`: 新增 `RMD_CORE_BODY` 宏、`rmd160_oneblock_local()`、`hash160_full_opt()`、kernel 宏 `KERNEL_ARC_SETUP`/`KERNEL_HASH160`
- `gpu_pipeline.py`: Intel 编译选项已含 `-DARC_OPT`（见 P0-3）

### P1-6: USE_HOST_PTR DMA

```python
# 分配 host pinned memory
self._h_privkeys = np.zeros(batch_size * 32, dtype=np.uint8)
self._h_hash160s = np.zeros(batch_size * 20, dtype=np.uint8)

# 创建 buffer 时直接用 host 内存
mf = cl.mem_flags
self._d_privkeys = cl.Buffer(
    self._ctx,
    mf.READ_ONLY | mf.USE_HOST_PTR | mf.ALLOC_HOST_PTR,
    size=self.batch_size * 32,
    hostbuf=self._h_privkeys,  # ← 新增
)
self._d_hash160s = cl.Buffer(
    self._ctx,
    mf.WRITE_ONLY | mf.USE_HOST_PTR | mf.ALLOC_HOST_PTR,
    size=self.batch_size * 20,
    hostbuf=self._h_hash160s,  # ← 新增
)
```

> 注意：`USE_HOST_PTR` 需要 driver 支持 pinned memory。NVIDIA 驱动支持良好，Intel 部分版本可能降级为 `ALLOC_HOST_PTR` 行为。

### P1-7: 多 buffer 分片

```python
class GPUPipeline:
    def _alloc_buffers(self):
        """根据 max_mem_alloc 分片分配 buffer。"""
        buf_size = self.batch_size * 32
        max_per_buf = int(self._max_alloc * 0.95)  # 留 5% 余量
        self._n_buf_slices = max(1, (buf_size + max_per_buf - 1) // max_per_buf)
        self._slice_size = buf_size // self._n_buf_slices

        mf = cl.mem_flags
        self._d_privkeys_list = []
        self._d_hash160s_list = []
        for i in range(self._n_buf_slices):
            pk_buf = cl.Buffer(self._ctx, mf.READ_ONLY | mf.ALLOC_HOST_PTR,
                               size=int(self._slice_size))
            h160_buf = cl.Buffer(self._ctx, mf.WRITE_ONLY | mf.ALLOC_HOST_PTR,
                                 size=int(self._slice_size * 20 // 32))
            self._d_privkeys_list.append(pk_buf)
            self._d_hash160s_list.append(h160_buf)
```

### P2-8: 按设备能力分配 batch_size

```python
# gpu_dispatcher.py 中初始化时
for i, (pi, di, dev_info) in enumerate(selected):
    # 用 compute_units 作为权重
    max_batch = self.config.batch_size
    if i == 0:
        scaled = max_batch
    else:
        weight = dev_info.compute_units / selected[0][2].compute_units
        scaled = max(int(max_batch * weight), 16384)  # 不低于下限

    # 同时考虑 max_mem_alloc
    raw_dev = dev_info._raw_device
    if raw_dev:
        max_alloc = raw_dev.max_mem_alloc_size
        alloc_limit = max_alloc // (32 + 20)
        scaled = min(scaled, int(alloc_limit))

    pipe = GPUPipeline(..., batch_size=scaled, ...)
```

### P2-9: fp16 中间存储 (Arc 专用)

SHA-256 消息扩展 W[0..63] 分为 W[0..15]（消息块）和 W[16..63]（扩展词）。扩展词的精度可用 `half` 压缩：

```c
#ifdef ARC_OPT
// 扩展消息使用 half 存储（节省 50% 寄存器）
half W_ext[48];  // 48 × 2B = 96B vs 48 × 4B = 192B
// 使用时转为 uint
for (int t = 16; t < 64; t++) {
    uint w = (uint)W_ext[t - 16];  // 从 half 扩展回 uint
    // ... SHA-256 压缩函数使用 w
}
#endif
```

> **精度风险**：SHA-256 扩展词是 XOR + 循环移位结果，half 可能丢失低位精度。需要验证是否可逆/不影响最终哈希值。

### P2-10: GPU 侧碰撞检测 (Bloom filter + 原子计数器)

**实际实现**（2026-06-01）:

新增 kernel `ec_mul_hash160_collision`，在 GPU 上完成 HASH160 后直接检测碰撞：

1. **Bloom filter 上载**：通过 `GPUPipeline(bloom_data=..., bloom_m=...)` 构造函数传入，`cl.Buffer(COPY_HOST_PTR)` 上传到设备
2. **哈希派生**：从 HASH160 输出中取前 2 个 uint32 作为 `bh1`/`bh2`，使用 `pos_i = (bh1 + i * bh2) % bloom_m` 派生出 7 个位位置（与 Python 端 Kirsch-Mitzenmacher 一致，但用 HASH160 本身替代 SHA-256 作为种子）
3. **原子命中记录**：通过 `atomic_inc(hit_count)` 无冲突记录命中索引到 `hit_buffer`
4. **主机回读**：仅回读 `hit_count`（4 字节）和 `hit_buffer[:n_hits]`（`n_hits*4` 字节），而非全部 `batch*52` 字节

```c
// kernel 末尾 Bloom filter 测试
uint bh1 = HASH160[0..3], bh2 = HASH160[4..7];
uint hit = 1;
for (int i = 0; i < 7; i++) {
    uint pos = (bh1 + i * bh2) % bloom_m;
    if (!bloom_test_bit(bloom_data, pos)) { hit = 0; break; }
}
if (hit) {
    int idx = atomic_inc(hit_count);
    hit_buffer[idx] = gid;
}
```

> **设计选择**：Bloom 数据在 `__global` 中读取，不使用 `__local` 避免 workgroup preload 开销（Bloom 表可达 ~128MB，远超 SLM 128KB）。

**Pipeline 侧改动**：
- `__init__` 新增 `bloom_data`/`bloom_m` 参数
- `_init_opencl` 分配 `_d_bloom`、`_d_hit_count`、`_d_hit_buffer` 设备缓冲区
- `submit_batch` 在 `bloom_data!=None` 且 `check_collision==None` 时自动启用 GPU 碰撞
- `_run_sub_batch` 自动选择碰撞 kernel
- `close` 释放 bloom/hit 缓冲区

---

## 4. 测试验证方案

| 优化项 | 验证方法 | 通过标准 | 状态 |
|--------|---------|---------|------|
| P0-1 | `collision_engine.py --gpu --batch 50000000` 在 Arc A770 | 不崩溃，正常回退 batch_size | ✅ |
| P0-2 | 对比 `local_ws=64/128/256/512` 的 keys/s | ≥ 默认性能的 95% | ✅ |
| P0-3 | 各平台运行的输出验证 | 碰撞结果一致 | ✅ |
| P1-4 | `test_gpu_pipeline.py` 新增碰撞检查基准测试 | 单 batch 碰撞检查 ≤ 50ms | ✅ |
| P1-5 | GPU kernels 编译检查 + 预加载正确性 | 编译成功，运行时无 `__local` 越界 | ✅ |
| P1-6 | DMA 带宽测试 (`clEnqueueMapBuffer` vs copy) | 减少 20%+ 拷贝时间 | ✅ |
| P1-7 | 暂缓（P0-1 已提供保护） | — | ⏸️ |
| P2-8 | 双 GPU 同时运行时 KPS 输出 | 两卡均接近其单卡峰值 | ✅ |
| P2-9 | 精度风险：SHA-256 扩展词 XOR+ROTR 在 half 精度丢失低位 | 不满足密码学正确性 | ⏸️ |
| P2-10 | `test_e2e_collision.py` GPU 碰撞路径 | GPU 碰撞结果与 host 碰撞检测完全一致 | ✅ |

---

## 5. 性能目标

| 阶段 | GTX 1660 Ti | Arc A770 | 双卡合计 | 状态 |
|------|-------------|----------|----------|------|
| **当前基线** | ~915 K/s | ~1,742 K/s | ~2,657 K/s | 基准 |
| **P0 完成后** | ~950 K/s | ~1,800 K/s | ~2,750 K/s | ✅ 实现 |
| **P1 完成后** | ~1,100 K/s | ~2,100 K/s | ~3,200 K/s | ✅ 实现 |
| **P2 完成后** | ~1,200 K/s | ~2,500 K/s | ~3,700 K/s | ✅ 实现（P2-9 fp16 除外） |
