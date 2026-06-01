# 依赖安全审计

> 最后更新：2026-06-01
> 审计周期：**每月一次**（建议每月初执行）

---

## 关键依赖 CVE 状态

### 1. coincurve（核心加密依赖）

| 项目 | 值 |
|------|-----|
| 当前约束 | `>=3.0.0` |
| 最新版 | **21.0.0** (2025-03-08) |
| 捆绑 libsecp256k1 | **0.6.0** |
| Python 支持 | 3.9–3.13 |

**CVE 审查结果：无已知 CVE**

- coincurve 本身在 **PyPI、OSV.dev、NVD** 三大数据库中均 **无已报 CVE**。
- 捆绑的 **Bitcoin Core libsecp256k1 (C 库)** 是无签名验证更正的、经过 **Bitcoin Core 长期实战验证** 的密码学库，历史上无重大 CVE。
- **注意区分**：`CVE-2021-38195` / `CVE-2019-25003` / `CVE-2019-20399` 涉及 **Parity 的 Rust libsecp256k1**（不同项目），**不**影响 coincurve。

**建议**：
- [ ] 升级约束为 `>=21.0.0` 以锁定 libsecp256k1 0.6.0 并获得 Python 3.13 支持
- [ ] 每月检查 [coincurve Releases](https://github.com/ofek/coincurve/releases) 是否有更新

### 2. bech32（地址编码）

| 项目 | 值 |
|------|-----|
| 当前约束 | `>=1.2.0` |
| 最新版 | 2.x (纯 Python，无 C 扩展) |

**CVE 审查结果：无已知 CVE**

- bech32 是纯 Python 实现的地址编码库，攻击面极小。
- 无已报 CVE。

### 3. numpy（核心计算）

| 项目 | 值 |
|------|-----|
| 当前约束 | `>=1.24.0` |
| 最新版 | 2.x |

**CVE 审查结果：历史 CVE 均不涉及本项目使用场景**

- 历史 CVE（如反序列化相关）均要求加载 **不可信的 pickle/npz 文件**。
- 本项目仅使用 numpy 进行 **数组运算和内存映射**，不加载外部 pickle，风险极低。
- 建议保持 `>=1.24.0` 或升级至 2.x 系列。

### 4. Web 依赖（API Server）

| 包 | 最新版 | CVE 状态 |
|----|-------|----------|
| `fastapi` | 0.115.x+ | CVE-2025-46814 / CVE-2025-54365 等已知 CVE 均属于 `fastapi-guard` 第三方扩展库，**不**影响 FastAPI 核心。FastAPI 核心库无公开 CVE。 |
| `uvicorn` | 0.34.x+ | 无重大 CVE |
| `jinja2` | 3.1.x+ | CVE-2024-56326（SSTI 绕过）在 3.1.5+ 修复。当前约束 `>=3.1.0` 可能需要收紧。 |
| `websockets` | 13.x+ | 无重大 CVE |

### 5. 分布式依赖

| 包 | CVE 状态 |
|----|---------|
| `grpcio` | **CVE-2024-11407**（数据损坏，Zero Copy 场景）、**CVE-2024-7246**（HPACK 表投毒，HTTP/2 代理场景）。两者均为 Moderate 级别，需在非代理直连场景下才有风险。建议使用 `>=1.66.0`。 |
| `protobuf` | 无重大 CVE |

### 6. GPU 依赖

| 包 | CVE 状态 |
|----|---------|
| `pyopencl` | 无已知 CVE |

---

## 依赖版本收紧建议

当前版本约束过于宽松，建议收紧如下：

```toml
# pyproject.toml [project.dependencies]
dependencies = [
    "coincurve>=21.0.0,<22",    # 锁定 libsecp256k1 0.6.0
    "bech32>=1.2.0,<3",        # 安全
    "numpy>=1.24.0",           # 宽松即可
]

# Web 依赖
"fastapi>=0.110.0",
"uvicorn[standard]>=0.29.0",
"jinja2>=3.1.5",               # 修复 CVE-2024-56326
"websockets>=12.0",

# 分布式
"grpcio>=1.66.0",              # 修复 CVE-2024-11407 / CVE-2024-7246
"protobuf>=4.25.0",
```

---

## 定期审查流程

### 每月审查清单

1. **检查 coincurve 版本**
   ```bash
   pip index versions coincurve
   ```
   对比 [coincurve Releases](https://github.com/ofek/coincurve/releases) 和当前约束。

2. **检查 libsecp256k1 上游安全更新**
   - https://github.com/bitcoin-core/secp256k1/commits/master
   - 关注 CHANGELOG 中的 `security` 或 `fix` 标记

3. **检查全依赖 CVE**
   ```bash
   pip install pip-audit
   pip-audit
   ```
   或使用 GitHub Dependabot → 仓库 Settings → Security & analysis → Enable Dependabot alerts

4. **检查关键 CVE 数据库**
   - NVD: https://nvd.nist.gov/
   - OSV.dev: https://osv.dev/
   - PyPI Advisory Database: https://github.com/pypa/advisory-database

### 自动化建议

- **启用 GitHub Dependabot**：自动 PR 更新依赖
- **CI 中集成 `pip-audit`**：在 CI 工作流中添加步骤
  ```yaml
  - name: Dependency audit
    run: |
      pip install pip-audit
      pip-audit
  ```
- **使用 `pip-audit` 忽略不可修改的依赖**（如 coincurve 无 CVE，但避免误报）

---

## 紧急响应

如果发现影响本项目的 **Critical / High 级 CVE**：

1. 立即升级受影响的依赖
2. 更新本文件
3. 在 `CHANGELOG.md` 中记录安全更新
4. 重新发布版本

---

## 附录：libsecp256k1 与 Parity libsecp256k1 的区别

| 特性 | Bitcoin Core libsecp256k1 (C) | Parity libsecp256k1 (Rust) |
|------|-------------------------------|---------------------------|
| 使用方 | coincurve | 部分 Rust 项目 |
| CVE | 无重大 CVE | CVE-2021-38195 (9.8 Critical) |
| 审计 | Bitcoin Core 长期实战验证 | 同 |
| 本项目使用 | ✅ 依赖 | ❌ 无关 |
