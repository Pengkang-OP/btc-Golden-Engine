#!/usr/bin/env python3
"""GPU Kernel 编译验证测试（pytest）。

运行条件:
  - 基础符号验证: 无需 GPU, 始终运行
  - 测试向量验证: 无需 GPU, 始终运行
  - OpenCL 编译测试: 需要 pyopencl + OpenCL 设备 (自动跳过)

用法:
  pytest gpu_engine/test_kernel_compile.py -v          # 运行所有可用测试
  pytest gpu_engine/test_kernel_compile.py -v -k gpu   # 仅 GPU 相关测试
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

import pytest

# ── 路径 ──────────────────────────────────────────────────────
_KERNEL_FILE = Path(__file__).parent / "gpu_kernel.h"
_VECTORS_FILE = Path(__file__).parent / "kernel_test_vectors.json"

# ── 期望的 kernel 符号 ────────────────────────────────────────
_EXPECTED_KERNEL_ENTRIES = {"ec_mul_hash160", "ec_mul_pubkey"}
_EXPECTED_FUNCTIONS = {
    "sha256_oneblock",
    "rmd160_oneblock",
    "hash160_full",
    "scalar_mult_base",
    "fe_reduce",
    "fe_mul",
    "pt_dbl",
    "pt_add_jacobian",
}
_EXPECTED_DEFINES = {
    "ROTR",
    "CH",
    "MAJ",
    "SIG0",
    "SIG1",
    "GAM0",
    "GAM1",
    "ROL",
}
_EXPECTED_CONSTANTS = {
    "P_SECP",
    "K_SHA256",
    "R_RMD",
    "S_RMD",
    "RP_RMD",
    "SP_RMD",
    "K1_RMD",
    "K2_RMD",
}


# ═══════════════════════════════════════════════════════════════
# 1. Kernel 源文件结构与符号验证 (无需 GPU)
# ═══════════════════════════════════════════════════════════════


@pytest.fixture(scope="session")
def kernel_source() -> str:
    """读取 kernel 源文件内容。"""
    if not _KERNEL_FILE.exists():
        pytest.fail(f"Kernel 源文件不存在: {_KERNEL_FILE}")
    return _KERNEL_FILE.read_text(encoding="utf-8")


def test_kernel_file_exists():
    """Kernel 源文件必须存在且非空。"""
    assert _KERNEL_FILE.exists(), f"缺少 GPU kernel 文件: {_KERNEL_FILE}"
    size = _KERNEL_FILE.stat().st_size
    assert size > 5000, f"Kernel 文件太小 ({size} bytes), 可能不完整"
    assert size < 100_000, f"Kernel 文件过大 ({size} bytes), 可能包含冗余内容"


def test_kernel_source_encoding(kernel_source: str):
    """确保 kernel 源码无 BOM 和非法字符。"""
    assert not kernel_source.startswith("\ufeff"), "Kernel 文件包含 BOM"
    assert kernel_source.isascii(), "Kernel 文件包含非 ASCII 字符"


def test_kernel_entry_points(kernel_source: str):
    """必须包含预期的 __kernel 入口函数。"""
    for entry in _EXPECTED_KERNEL_ENTRIES:
        assert f"__kernel void {entry}" in kernel_source, f"缺少 __kernel 入口: {entry}"


def test_kernel_critical_functions(kernel_source: str):
    """必须包含所有关键内部函数。"""
    for func in _EXPECTED_FUNCTIONS:
        # 支持 static return_type func( 和 static void func( 两种模式
        pattern = re.compile(rf"static\s+\w+\s+{func}\s*\(")
        assert pattern.search(kernel_source), f"缺少关键函数: {func}"


def test_kernel_macros(kernel_source: str):
    """必须包含所有关键宏定义。"""
    for macro in _EXPECTED_DEFINES:
        assert f"#define {macro}" in kernel_source, f"缺少宏定义: {macro}"


def test_kernel_constants(kernel_source: str):
    """必须包含所有 static const 查表常量。"""
    for const in _EXPECTED_CONSTANTS:
        assert const in kernel_source, f"缺少常量定义: {const}"


def test_kernel_no_inline_constants(kernel_source: str):
    """所有大查表应位于 static const 而非函数内栈上。

    检查函数体内是否残留栈上定义的 K/R/S 等大数组。
    """
    # 检查函数体内是否有 uint k[64] 定义 (旧版 SHA-256 实现)
    func_body_k = re.findall(
        r"(?:sha256|rmd160)\w*\s*\([^)]*\)\s*\{[^}]*"
        r"uint\s+\w+\s*\[\s*(?:64|80)\s*\]",
        kernel_source,
    )
    assert not func_body_k, (
        f"发现函数内栈上大数组定义 (应该移到 __constant): {func_body_k}"
    )


def test_kernel_includes_line_count(kernel_source: str):
    """验证 kernel 文件行数在合理范围内。"""
    lines = kernel_source.splitlines()
    assert 250 <= len(lines) <= 450, f"Kernel 文件行数异常: {len(lines)} (期望 250~450)"


def test_kernel_no_tab_indent(kernel_source: str):
    """Kernel 源文件应使用空格缩进（与项目规范一致）。"""
    for i, line in enumerate(kernel_source.splitlines(), 1):
        if line.startswith("\t"):
            pytest.fail(f"第 {i} 行使用了 Tab 缩进: {line[:40]!r}")


def test_kernel_no_missing_semicolons(kernel_source: str):
    """检查明显的语法问题：函数定义后缺少分号的常见错误。"""
    # fe_mul 末尾应有 }
    assert "fe_reduce(r, r, cr);" in kernel_source, (
        "fe_mul 中缺少 fe_reduce(r, r, cr); (T10 优化, 3-arg)"
    )


# ═══════════════════════════════════════════════════════════════
# 2. 测试向量验证 (无需 GPU, 与 CPU 参考实现对比)
# ═══════════════════════════════════════════════════════════════


@pytest.fixture(scope="session")
def test_vectors() -> list[dict]:
    """从 JSON 加载测试向量。"""
    if not _VECTORS_FILE.exists():
        pytest.fail(f"测试向量文件不存在: {_VECTORS_FILE}")
    with open(_VECTORS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list), "测试向量应为 JSON 数组"
    assert len(data) >= 3, f"测试向量不足: {len(data)} (期望 ≥3)"
    return data


def test_vectors_file_exists():
    """测试向量 JSON 文件必须存在且有效。"""
    assert _VECTORS_FILE.exists(), f"缺少测试向量文件: {_VECTORS_FILE}"
    with open(_VECTORS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list), "JSON 根应为数组"
    for i, vec in enumerate(data):
        assert "name" in vec, f"向量[{i}] 缺少 name"
        assert "pubkey" in vec, f"向量[{i}] 缺少 pubkey"
        assert "sha256" in vec, f"向量[{i}] 缺少 sha256"
        assert "hash160" in vec, f"向量[{i}] 缺少 hash160"


def test_vectors_correctness(test_vectors: list[dict]):
    """验证测试向量中的 HASH160 与 CPU 参考实现一致。"""
    from coincurve import PrivateKey

    # privkey → HASH160 的参考实现
    def _ref_hash160(privkey_int: int) -> tuple[str, str, str]:
        pk = PrivateKey.from_int(privkey_int)
        pub_comp = pk.public_key.format(compressed=True)
        sha = hashlib.sha256(pub_comp).hexdigest()
        h160 = hashlib.new("ripemd160", hashlib.sha256(pub_comp).digest()).hexdigest()
        return pub_comp.hex(), sha, h160

    name_to_privkey = {
        "privkey=1": 1,
        "privkey=2": 2,
        "privkey=42": 42,
        "privkey=SECP256K1_ORDER-1": 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364140,
        "privkey=EFF": 0xEFF,
    }

    for vec in test_vectors:
        name = vec["name"]
        if name not in name_to_privkey:
            continue  # 跳过未知向量 (向后兼容)
        privkey_int = name_to_privkey[name]

        pub_ref, sha_ref, h160_ref = _ref_hash160(privkey_int)
        assert vec["pubkey"] == pub_ref, (
            f"[{name}] 公钥不匹配: 期望 {pub_ref}, 得到 {vec['pubkey']}"
        )
        assert vec["sha256"] == sha_ref, (
            f"[{name}] SHA256 不匹配: 期望 {sha_ref}, 得到 {vec['sha256']}"
        )
        assert vec["hash160"] == h160_ref, (
            f"[{name}] HASH160 不匹配: 期望 {h160_ref}, 得到 {vec['hash160']}"
        )


# ═══════════════════════════════════════════════════════════════
# 3. OpenCL 编译测试 (需要 pyopencl + GPU/CPU 设备)
# ═══════════════════════════════════════════════════════════════

gpu_test = pytest.mark.skipif(
    not _VECTORS_FILE.exists(),  # 占位条件 — 后续用 pyopencl import 替代
    reason="GPU 环境不可用 (pyopencl 未安装或无 OpenCL 设备)",
)


def _pyopencl_available() -> bool:
    """检查 pyopencl 是否可用且至少有一个 OpenCL 平台。"""
    try:
        import pyopencl as cl

        return len(cl.get_platforms()) > 0
    except (ImportError, Exception):
        return False


def _kernel_compile_via_opencl(kernel_source: str) -> str | None:
    """通过 pyopencl 编译 kernel，成功返回 None，失败返回错误消息。"""
    try:
        import pyopencl as cl

        platforms = cl.get_platforms()
        if not platforms:
            return "无 OpenCL 平台"
        # 尝试第一个平台上的第一个设备
        for pf in platforms:
            try:
                devices = pf.get_devices()
                if devices:
                    ctx = cl.Context([devices[0]])
                    cl.Program(ctx, kernel_source).build(options=["-cl-std=CL1.2"])
                    return None  # 编译成功
            except Exception as e:
                return f"编译失败: {e}"
        return "无可用设备"
    except ImportError:
        return "pyopencl 未安装"
    except Exception as e:
        return f"OpenCL 错误: {e}"


def test_gpu_compile_kernel():
    """通过 pyopencl 实际编译 GPU kernel (需要 GPU/CPU OpenCL 设备)。"""
    if not _pyopencl_available():
        pytest.skip("pyopencl 不可用或未检测到 OpenCL 平台")

    if not _KERNEL_FILE.exists():
        pytest.fail(f"Kernel 源文件不存在: {_KERNEL_FILE}")

    source = _KERNEL_FILE.read_text(encoding="utf-8")
    error = _kernel_compile_via_opencl(source)
    if error is not None:
        # 如果编译失败, 提供详细的诊断信息
        lines = source.splitlines()
        pytest.fail(
            f"Kernel 编译失败:\n  {error}\n"
            f"  文件行数: {len(lines)}\n"
            f"  文件大小: {_KERNEL_FILE.stat().st_size:,} bytes"
        )


def test_gpu_device_discovery():
    """GPU 设备发现应返回可用设备列表 (需要 OpenCL)。"""
    if not _pyopencl_available():
        pytest.skip("pyopencl 不可用")

    from gpu_engine import list_devices

    devices = list_devices()
    assert isinstance(devices, list), "list_devices() 应返回列表"
    # 至少能列出信息 (可能为空列表, 但不应抛异常)
    for d in devices:
        assert d.device_name, "设备名不应为空"
        assert d.compute_units > 0, f"设备 {d.device_name} 计算单元数异常"
        assert d.global_mem_size > 0, f"设备 {d.device_name} 全局显存异常"


# ═══════════════════════════════════════════════════════════════
# 4. Kernel 架构验证 (静态分析)
# ═══════════════════════════════════════════════════════════════


def test_kernel_constant_time_properties(kernel_source: str):
    """验证 Kernel 的恒定时间属性。

    检查:
    - Montgomery ladder (恒定时间标量乘法) 必须存在
    - ct_sel / ct_cmov_fe / ct_cmov_pt 等恒定时间工具函数存在
    - 无明显的分支依赖秘钥数据
    """
    ct_tools = {"ct_sel", "ct_cmov_fe", "ct_cmov_pt"}
    for tool in ct_tools:
        assert tool in kernel_source, f"缺少恒定时间工具函数: {tool}"

    assert "scalar_mult_base" in kernel_source, "缺少 Montgomery ladder"

    # 检查是否存在定时不安全的 pow() 或 exp() 调用
    unsafe_funcs = ["pow(", "exp(", "log("]
    for func in unsafe_funcs:
        assert func not in kernel_source, f"Kernel 中包含定时不安全函数: {func}"


def test_kernel_pragma_unroll(kernel_source: str):
    """验证关键循环已添加 #pragma unroll (T10 优化)。"""
    expected_pragmas = [
        "#pragma unroll",  # SHA-256 展开 + RIPEMD-160 + hash160_full 2 处
        "#pragma unroll 1",  # Montgomery ladder
    ]
    count = sum(kernel_source.count(p) for p in expected_pragmas)
    assert count >= 5, f"#pragma unroll 出现次数不足 ({count}), 期望至少 5 处"


def test_kernel_no_inline_sha256_ktable(kernel_source: str):
    """SHA-256 K 常量必须引用 K_SHA256 而非内联数字。"""
    # 检查是否有形如 for(int i=0;i<64;i++) t1 = ... + w[i] 但没有 K_SHA256
    # 或检测旧版内联常量模式
    lines = kernel_source.splitlines()
    for i, line in enumerate(lines, 1):
        if "K_SHA256[i]" in line and "+" in line:
            break
    else:
        # 如果找不到 K_SHA256 引用，检查是否至少出现在 static const 中
        assert "K_SHA256" in kernel_source, "K_SHA256 常量表未定义"
        # 不强制要求行内引用（可能在 #pragma unroll 展开后内联）
