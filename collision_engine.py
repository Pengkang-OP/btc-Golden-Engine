#!/usr/bin/env python3
"""碰撞对撞引擎 - 生成私钥 → 公钥 → HASH160 → 在 UTXO 集中查找匹配.

工作原理:
1. 加载 UTXO 集所有有余额地址的 HASH160(来自 collision_target + utxo_hash160.bin)
2. 并行生成私钥 → 推导公钥(压缩/非压缩)→ 计算 HASH160 = RIPEMD160(SHA256(pubkey))
3. 在 UTXO 集中查找匹配
4. 命中时保存私钥 (WIF),地址信息至 collision_results.json

每个私钥会生成 2 种 HASH160:
  - 压缩公钥 → HASH160_comp → 可匹配 P2WPKH 或 P2PKH
  - 非压缩公钥 → HASH160_uncomp → 可匹配 P2PKH (Legacy)

用法:
  # CPU 顺序扫描(默认,从私钥 1 开始)
  python collision_engine.py --threads 4

  # CPU 随机扫描
  python collision_engine.py --mode random --threads 8

  # CPU 从指定私钥开始扫描 100 万个
  python collision_engine.py --mode sequential --start 0x100000 --count 1000000

  # GPU 加速扫描(需 pyopencl)
  python collision_engine.py --gpu
  python collision_engine.py --gpu --gpu-devices 0 --gpu-batch-size 131072

  # GPU 顺序扫描(可恢复)
  python collision_engine.py --gpu --gpu-mode sequential --gpu-start 0x100000
"""

__version__ = "2.3.0"

import argparse
import hashlib
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ── 将项目根加入 sys.path ─────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.resolve()))
_local_pkg = Path(__file__).resolve().parent / ".local-packages"
if _local_pkg.is_dir():
    sys.path.insert(0, str(_local_pkg))

# ── 项目模块与第三方(依赖上述 sys.path) ─────────────────────
from bech32 import CHARSET, bech32_encode, convertbits
from coincurve import PrivateKey, PublicKey

from collision_target import (
    Hash160Set,
    SwappableTarget,
    TargetProtocol,
    XOnlySet,
)
from core import (
    DatabaseError,
    EngineConfig,
    Notifier,
    ResultDB,
    setup_logger,
)

# ── GPU 引擎(可选导入) ──────────────────────────────────────
_GPU_AVAILABLE = False
try:
    from gpu_engine import (
        DispatcherConfig,
        GPUBatchScheduler,
    )
    from gpu_engine import (
        list_devices as gpu_list_devices,
    )

    _GPU_AVAILABLE = True
except ImportError:
    pass

# ── 常量 ──────────────────────────────────────────────────────
SECP256K1_ORDER = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
    16,
)
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
RESULTS_FILE = Path(__file__).parent / "collision_results.json"
CHECKPOINT_FILE = Path(__file__).parent / "collision_checkpoint.json"
PROGRESS_INTERVAL = 5000  # 每 N 个 key 报告一次
CHECKPOINT_INTERVAL = 60_000  # 每 N 个 key 保存一次 checkpoint

# ── 全局日志与配置 ────────────────────────────────────────────
_logger: Any = None
_config: EngineConfig | None = None
_db: ResultDB | None = None
_notifier: Notifier | None = None
_shutdown_requested = False

# ── UTXO 自动刷新全局状态 ─────────────────────────────────────
_swappable_target: SwappableTarget | None = None  # 包裹 Hash160Set
_swappable_xonly: SwappableTarget | None = None  # 包裹 XOnlySet
_refresh_thread: threading.Thread | None = None
_refresh_last_time: float = 0.0  # 上次成功刷新时间戳
_refresh_last_result: str = "N/A"  # 上次刷新结果描述


def _handle_signal(signum: int, frame: object | None = None) -> None:
    """信号处理器:SIGTERM 设关闭标志,SIGINT 抛 KeyboardInterrupt 以触发清理.."""
    global _shutdown_requested
    _shutdown_requested = True
    if _logger:
        _logger.warning("收到信号 %d,开始优雅关闭...", signum)
    if signum == signal.SIGINT:
        raise KeyboardInterrupt


def _start_config_watcher(
    config: EngineConfig,
    interval: float = 5.0,
    logger: logging.Logger | None = None,
) -> None:
    """后台线程:定期检查配置文件是否变更并热重载.."""
    log = logger or logging.getLogger(__name__)

    def _watch() -> None:
        """轮询配置文件的 mtime 变更,检测到变化则自动重载.."""
        global _shutdown_requested
        while not _shutdown_requested:
            try:
                if config.check_reload():
                    log.info("配置文件已变更并自动重载")
            except Exception as exc:  # noqa: BLE001
                _logger.warning("配置热重载异常: %s", exc)
            time.sleep(interval)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()


# ── UTXO 自动刷新 ──────────────────────────────────────────────


def _find_bitcoin_cli(config: EngineConfig) -> str | None:
    """查找 bitcoin-cli 可执行文件路径..

    优先使用 config 中指定的路径,否则自动检测常见位置.
    """
    if config.bitcoin_cli_path:
        cli_path = Path(config.bitcoin_cli_path)
        if cli_path.is_file():
            return str(cli_path.resolve())
        _logger and _logger.warning(
            "配置的 bitcoin-cli 路径不存在: %s",
            config.bitcoin_cli_path,
        )

    # 自动检测
    candidates = [
        Path.cwd() / "daemon" / "bitcoin-cli.exe",
        Path.cwd() / "bitcoin-cli.exe",
        Path.cwd() / "bitcoin-cli",
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand.resolve())
    return None


def _find_bitcoin_datadir(config: EngineConfig) -> str | None:
    """查找 Bitcoin 数据目录.."""
    if config.bitcoin_datadir:
        datadir = Path(config.bitcoin_datadir)
        if datadir.is_dir():
            return str(datadir.resolve())
    # 使用 CWD 作为默认(项目目录就是 Bitcoin data dir)
    cwd = Path.cwd()
    if cwd.is_dir():
        return str(cwd.resolve())
    return None


def _run_bitcoin_cli_dumptxoutset(
    bitcoin_cli: str,
    datadir: str,
    snapshot_path: str,
    logger: logging.Logger,
) -> bool:
    """运行 bitcoin-cli dumptxoutset 生成快照..

    Returns:
        True 表示成功,False 表示失败.

    """
    try:
        logger.info("[UTXO][刷新] 运行 bitcoin-cli dumptxoutset ...")
        result = subprocess.run(  # noqa: S603  # 参数来自可信配置
            [
                bitcoin_cli,
                f"-datadir={datadir}",
                "dumptxoutset",
                snapshot_path,
            ],
            capture_output=True,
            text=True,
            timeout=7200,  # 2 小时超时
        )
        if result.returncode == 0:
            logger.info("[UTXO][刷新] dumptxoutset 成功")
            return True
        logger.error(
            "[UTXO][刷新] dumptxoutset 失败: %s",
            result.stderr.strip() or result.stdout.strip(),
        )
        return False
    except FileNotFoundError:
        logger.exception("[UTXO][刷新] bitcoin-cli 未找到: %s", bitcoin_cli)
        return False
    except subprocess.TimeoutExpired:
        logger.exception("[UTXO][刷新] dumptxoutset 超时 (2h)")
        return False
    except Exception as exc:
        logger.exception("[UTXO][刷新] dumptxoutset 异常")
        return False


def _do_utxo_refresh(logger: logging.Logger) -> bool:
    """执行一次完整的 UTXO 数据刷新..

    流程: dumptxoutset → 提取 Hash160 → 构建 Bloom Filter → 原子交换目标集

    Returns:
        True 表示刷新成功,False 表示失败.

    """
    global _refresh_last_time, _refresh_last_result
    if _config is None or _swappable_target is None:
        return False

    config = _config
    snapshot_path = config.utxo_snapshot_path
    hash160_bin = config.utxo_hash160_bin
    hash160_idx = config.utxo_hash160_idx

    # 1) 查找 bitcoin-cli
    bitcoin_cli = _find_bitcoin_cli(config)
    if bitcoin_cli is None:
        _refresh_last_result = "bitcoin-cli 未找到"
        logger.warning("[UTXO][刷新] 无法刷新: bitcoin-cli 未找到")
        return False

    datadir = _find_bitcoin_datadir(config)
    if datadir is None:
        _refresh_last_result = "数据目录未找到"
        logger.warning("[UTXO][刷新] 无法刷新: Bitcoin 数据目录未找到")
        return False

    # 2) dumptxoutset
    if not _run_bitcoin_cli_dumptxoutset(bitcoin_cli, datadir, snapshot_path, logger):
        _refresh_last_result = "dumptxoutset 失败"
        return False

    # 3) 检查快照文件
    if not Path(snapshot_path).is_file():
        _refresh_last_result = "快照文件未生成"
        logger.error(
            "[UTXO][刷新] dumptxoutset 完成后快照文件不存在: %s",
            snapshot_path,
        )
        return False

    # 4) 提取 Hash160
    logger.info("[UTXO][刷新] 从快照提取 Hash160 ...")

    def _quiet_print(*args: object, **kwargs: object) -> None:
        """静默函数,临时替换 builtins.print 以抑制子流程的 stdout 输出.."""

    try:
        # 抑制 extract_snapshot 的 stdout 输出
        # (通过临时替换 print 为静默函数)
        import builtins as _builtins

        builtins_print = _builtins.print
        _builtins.print = _quiet_print

        from extract_utxo_hash160 import extract_snapshot

        stats = extract_snapshot(
            snapshot_path=str(Path(snapshot_path).resolve()),
            hash_bin_path=str(Path(hash160_bin).resolve()),
            hash_idx_path=str(Path(hash160_idx).resolve()),
        )
        new_count = stats.get("TOTAL_HASH160", 0)
        logger.info(
            "[UTXO][刷新] 提取完成: %s 个 Hash160",
            f"{new_count:,}" if new_count else "N/A",
        )
    except Exception as exc:
        _refresh_last_result = f"提取失败: {exc}"
        logger.exception("[UTXO][刷新] 提取 Hash160 失败: %s", exc)
        return False
    finally:
        _builtins.print = builtins_print

    # 5) 创建新的 Hash160Set 并加载
    logger.info("[UTXO][刷新] 加载新目标集 ...")
    try:
        new_target = Hash160Set()
        new_target.load(
            bin_path=str(Path(hash160_bin).resolve()),
            idx_path=str(Path(hash160_idx).resolve()),
            quiet=True,
        )
    except Exception as exc:
        _refresh_last_result = f"新目标集加载失败: {exc}"
        logger.exception("[UTXO][刷新] 加载新目标集失败: %s", exc)
        return False

    # 6) P2TR 支持(如果当前有两份)
    new_xonly: XOnlySet | None = None
    if _swappable_xonly is not None and config.enable_utxo_auto_refresh:
        xonly_bin = str(Path.cwd() / "utxo_xonly.bin")
        xonly_idx = str(Path.cwd() / "utxo_xonly.idx")
        if Path(xonly_bin).is_file():
            try:
                new_xonly = XOnlySet()
                new_xonly.load(
                    bin_path=xonly_bin,
                    idx_path=xonly_idx,
                    quiet=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[UTXO][刷新] P2TR 目标集刷新失败(跳过): %s", exc)

    # 7) 原子交换
    _swappable_target.swap(new_set=new_target)
    if new_xonly is not None and _swappable_xonly is not None:
        _swappable_xonly.swap(new_set=new_xonly)

    # 8) 更新状态
    old_count = len(_swappable_target) if _swappable_target else 0
    _refresh_last_time = time.time()
    _refresh_last_result = f"成功 ({new_count:,} 个 Hash160, 之前 {old_count:,})"
    logger.info(
        "[UTXO][刷新] 目标集已更新: %s 个 Hash160 (之前: %s)",
        f"{new_count:,}" if new_count else "N/A",
        f"{old_count:,}",
    )
    return True


def _start_utxo_refresher(
    config: EngineConfig,
    swappable_target: SwappableTarget,
    swappable_xonly: SwappableTarget | None,
    logger: logging.Logger | None = None,
) -> threading.Thread | None:
    """启动 UTXO 自动刷新后台线程..

    每隔 ``config.utxo_refresh_interval`` 秒检查并执行一次刷新.
    需要 ``config.enable_utxo_auto_refresh == True`` 才启动.
    """
    global _refresh_thread
    if not config.enable_utxo_auto_refresh:
        if logger:
            logger.info("[UTXO] 自动刷新未启用")
        return None

    log = logger or logging.getLogger(__name__)
    interval = max(config.utxo_refresh_interval, 60)  # 最小 60 秒

    log.info(
        "[UTXO] 自动刷新已启用: interval=%ds, snapshot=%s",
        interval,
        config.utxo_snapshot_path,
    )

    def _refresher_loop() -> None:
        """后台循环,周期性调用 _do_utxo_refresh 执行 UTXO 刷新.."""
        global _shutdown_requested
        while not _shutdown_requested:
            try:
                _do_utxo_refresh(log)
            except Exception as exc:
                log.exception("[UTXO][刷新] 刷新循环异常: %s", exc)
            # 等待下一个周期(分段等待以响应关闭请求)
            for _ in range(int(interval / 2)):
                if _shutdown_requested:
                    return
                time.sleep(2)
            # 如果 interval 不能被 2 整除,补足剩余
            if not _shutdown_requested:
                time.sleep(interval % 2)

    t = threading.Thread(
        target=_refresher_loop,
        daemon=True,
        name="utxo-refresher",
    )
    t.start()
    _refresh_thread = t
    return t


def _init_core(cfg_path: str | None = None) -> None:
    """初始化全局日志和配置(惰性,在 main() 中首次调用).."""
    global _logger, _config, _db
    # 配置
    _config = EngineConfig.load(Path(cfg_path)) if cfg_path else EngineConfig()
    # 日志
    _logger = setup_logger(
        log_path=_config.log_path,
        level=_config.log_level,
        max_bytes=_config.log_max_bytes,
        backup_count=_config.log_backup_count,
    )
    _logger.info("核心基础设施初始化完成")
    _logger.debug(
        "配置: results_db=%s, log_file=%s",
        _config.results_db,
        _config.log_file,
    )
    # 数据库
    _db = ResultDB(_config.results_db_path)

    # ── 配置热重载后台线程 ──
    # 若 config_path 有效(从文件加载),启动监视器
    if _config.config_path is not None:
        _start_config_watcher(_config, interval=5.0, logger=_logger)
        _logger.info("配置热重载已启动: path=%s, interval=5s", _config.config_path)


# ── 哈希工具 ──────────────────────────────────────────────────
def hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data))."""
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


# ── 地址编码 ──────────────────────────────────────────────────
def base58check_encode(payload: bytes) -> str:
    """Base58Check 编码(payload 已包含前缀和可选压缩标记).
    内部自动计算并追加双 SHA256 checksum..
    """
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(payload + chk, "big")
    result = ""
    while n:
        n, r = divmod(n, 58)
        result = BASE58[r] + result
    for b in payload:
        if b == 0:
            result = "1" + result
        else:
            break
    return result


def wif_encode(privkey: bytes, compressed: bool = True) -> str:  # noqa: FBT001, FBT002
    """私钥 → WIF 格式(钱包导入格式)."""
    data = b"\x80" + privkey
    if compressed:
        data += b"\x01"
    return base58check_encode(data)


def p2pkh_address(h160: bytes) -> str:
    """P2PKH 地址: Base58Check(0x00 + HASH160)."""
    return base58check_encode(b"\x00" + h160)


def p2wpkh_address(h160: bytes) -> str:
    """P2WPKH 地址: bc1 + witver=0 + HASH160."""
    bits = convertbits(list(h160), 8, 5)
    if bits is None:
        return ""
    data = [0, *bits]
    return bech32_encode("bc", data)


def p2sh_address(h160: bytes) -> str:
    """P2SH-P2WPKH 嵌套 SegWit 地址: Base58Check(0x05 + Hash160(OP_0 <hash160>)).

    将压缩公钥的 HASH160 包装为 witness program:
        redeem_script = 0x00 0x14 <20-byte-hash160>
        地址 = Base58Check(0x05 + RIPEMD160(SHA256(redeem_script)))

    Args:
        h160: 压缩公钥的 20 字节 HASH160.

    Returns:
        P2SH 地址字符串 (以 3 开头).

    """
    redeem_script = b"\x00\x14" + h160
    script_hash = hash160(redeem_script)
    return base58check_encode(b"\x05" + script_hash)  # 0x05 = P2SH 主网版本字节


def privkey_to_p2sh(privkey: bytes, compressed: bool = True) -> str:  # noqa: FBT001, FBT002
    """从私钥直接生成 P2SH-P2WPKH 嵌套 SegWit 地址..

    P2SH 包装: OP_0 <20-byte-key-hash> → Hash160 → Base58Check(0x05)

    Args:
        privkey: 32 字节私钥.
        compressed: 是否使用压缩公钥 (P2SH-P2WPKH 总是用压缩公钥).

    Returns:
        P2SH 地址字符串.

    """
    priv = PrivateKey(privkey)
    pub = priv.public_key
    pub_compressed = pub.format(compressed=True)
    h160 = hash160(pub_compressed)
    return p2sh_address(h160)


# ── Bech32m (SegWit v1+, P2TR) ──────────────────────────────
_BECH32M_CONST = 0x2BC830A3


def _bech32m_create_checksum(hrp: str, data: list[int]) -> list[int]:
    """Bech32m 校验和(M = 0x2BC830A3 vs Bech32 的 M = 1).."""
    from bech32 import bech32_hrp_expand, bech32_polymod

    values = bech32_hrp_expand(hrp) + data
    polymod = bech32_polymod([*values, 0, 0, 0, 0, 0, 0]) ^ _BECH32M_CONST
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32m_encode(hrp: str, data: list[int]) -> str:
    """Bech32m encode (uses CHARSET from installed bech32 library)."""
    combined = data + _bech32m_create_checksum(hrp, data)
    return hrp + "1" + "".join([CHARSET[d] for d in combined])


def p2tr_address(xonly: bytes) -> str:
    """P2TR 地址: bc1p + bech32m(witver=1, xonly_pubkey)."""
    bits = convertbits(list(xonly), 8, 5)
    if bits is None:
        return ""
    return bech32m_encode("bc", [1, *bits])


# ── BIP 341 Taproot Tweak ────────────────────────────────────
def tagged_hash(tag: str, data: bytes) -> bytes:
    """BIP 340 TaggedHash: SHA256(SHA256(tag) || SHA256(tag) || data)."""
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + data).digest()


def tweak_taproot(pubkey: "PublicKey") -> bytes | None:
    """Compute x-only output key for P2TR: Q = P + t*G.

    Returns 32-byte x-only output pubkey, or None if tweak >= n (rare).
    """
    pub_compressed = pubkey.format(compressed=True)
    xonly_internal = pub_compressed[1:]  # 32-byte x-coordinate

    # t = int(TaggedHash("TapTweak", xonly(P))) mod n
    t_hash = tagged_hash("TapTweak", xonly_internal)
    t_int = int.from_bytes(t_hash, "big")

    if t_int >= SECP256K1_ORDER:
        return None  # negligible probability (< 2^-128)

    # Q = P + t*G
    t_bytes = t_int.to_bytes(32, "big")
    pubkey_copy = PublicKey(pubkey.format())  # 复制避免 side-effect 修改调用方对象
    pubkey_copy.add(t_bytes, update=True)

    return pubkey_copy.format(compressed=True)[1:]  # 32-byte x-only output key


# ── 碰撞结果 ──────────────────────────────────────────────────
results_lock = threading.Lock()


@dataclass
class CollisionResult:
    privkey_hex: str
    wif_compressed: str
    wif_uncompressed: str
    p2pkh_address_comp: str  # 压缩公钥的 P2PKH
    p2wpkh_address: str  # 压缩公钥的 P2WPKH
    p2pkh_address_uncomp: str  # 非压缩公钥的 P2PKH
    h160_hex: str
    address_type: str  # 'P2PKH', 'P2WPKH' 或 'P2TR (Taproot)'
    found_via: str  # 'compressed', 'uncompressed' 或 'tweaked'
    timestamp: str = ""
    p2tr_address: str = ""  # P2TR bech32m 地址(仅 P2TR 命中时)
    xonly_hex: str = ""  # P2TR x-only pubkey(仅 P2TR 命中时)
    p2sh_address: str = ""  # P2SH-P2WPKH 嵌套 SegWit 地址(仅压缩公钥路径命中时)

    def __post_init__(self) -> None:
        """Dataclass 初始化后处理:空 timestamp 自动填充当前 UTC 时间.."""
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def save_result(result: CollisionResult) -> None:
    """线程安全地保存碰撞结果到 JSON 文件(及 SQLite 数据库,如已初始化).."""
    # SQLite 写入(O(1),优先于 JSON)
    if _db is not None:
        try:
            _db.save_result(result)
        except DatabaseError as exc:
            _logger and _logger.error("数据库写入失败,回退到 JSON: %s", exc)  # noqa: TRY400

    # JSON 写入(向后兼容)
    with results_lock:
        results = []
        if RESULTS_FILE.exists():
            try:
                text = RESULTS_FILE.read_text(encoding="utf-8")
                if text.strip():
                    results = json.loads(text)
            except (json.JSONDecodeError, OSError):
                results = []
        results.append(asdict(result))
        RESULTS_FILE.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # 控制台突出显示
    _logger.info(
        "\n%s\n  [HIT] 碰撞命中! 时间: %s\n"
        "  私钥 (hex):     %s\n"
        "  WIF (压缩):     %s\n"
        "  WIF (非压缩):   %s\n"
        "  HASH160:        %s\n"
        "  地址类型:       %s\n"
        "  来源公钥:       %s\n"
        "  P2PKH (压缩):   %s\n"
        "  P2WPKH:         %s\n"
        "  P2SH (P2WPKH):  %s\n"
        "  P2PKH (非压缩): %s\n"
        "  P2TR (Taproot): %s\n"
        "  x-only pubkey:  %s\n"
        "%s\n",
        "=" * 70,
        result.timestamp,
        result.privkey_hex,
        result.wif_compressed,
        result.wif_uncompressed,
        result.h160_hex,
        result.address_type,
        result.found_via,
        result.p2pkh_address_comp,
        result.p2wpkh_address,
        result.p2sh_address,
        result.p2pkh_address_uncomp,
        result.p2tr_address,
        result.xonly_hex,
        "=" * 70,
    )

    # 异步通知(如已配置)
    if _notifier is not None:
        _notifier.on_hit(result)


# ── 检查点管理 ────────────────────────────────────────────────
checkpoint_lock = threading.Lock()


def load_checkpoint() -> dict[str, object]:
    """从磁盘加载 checkpoint."""
    if CHECKPOINT_FILE.exists():
        try:
            data: dict[str, object] = json.loads(CHECKPOINT_FILE.read_text())
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_checkpoint(state: dict[str, object]) -> None:
    """线程安全地保存 checkpoint."""
    with checkpoint_lock:
        state["_timestamp"] = time.time()
        CHECKPOINT_FILE.write_text(json.dumps(state, indent=2))


# ── 核心碰撞逻辑 ─────────────────────────────────────────────
class SequentialCounter:
    """线程安全的顺序计数器."""

    def __init__(self, start: int = 1, limit: int = 0) -> None:
        """初始化顺序计数器,设置起始值和上限.."""
        self._lock = threading.Lock()
        self._val = start
        self._limit = limit
        self._count = 0

    def next(self) -> int | None:
        """返回下一个私钥值(线程安全),超出上限返回 None.."""
        with self._lock:
            if self._limit > 0 and self._count >= self._limit:
                return None
            v = self._val
            self._val += 1
            self._count += 1
            return v

    @property
    def checked(self) -> int:
        """已分配且被检查的私钥总数.."""
        return self._count

    @property
    def current(self) -> int:
        """下一个将被分配的私钥值(用于 checkpoint).."""
        return self._val


def check_single_key(
    privkey_int: int,
    target: TargetProtocol,
    xonly_target: TargetProtocol | None = None,
) -> CollisionResult | None:
    """检查一个私钥:推导 2 种 HASH160 + P2TR Tweaked Key → 在 UTXO 集中查询."""
    try:
        privkey_bytes = privkey_int.to_bytes(32, "big")
        priv = PrivateKey(privkey_bytes)
        pub = priv.public_key

        # ── 压缩公钥路径 ──
        pub_compressed = pub.format(compressed=True)  # 33 bytes
        h160_comp = hash160(pub_compressed)

        if h160_comp in target:
            return CollisionResult(
                privkey_hex=privkey_bytes.hex(),
                wif_compressed=wif_encode(privkey_bytes, compressed=True),
                wif_uncompressed=wif_encode(privkey_bytes, compressed=False),
                p2pkh_address_comp=p2pkh_address(h160_comp),
                p2wpkh_address=p2wpkh_address(h160_comp),
                p2pkh_address_uncomp="",
                h160_hex=h160_comp.hex(),
                address_type="P2WPKH/P2PKH",
                found_via="compressed",
                p2sh_address=p2sh_address(h160_comp),
            )

        # ── 非压缩公钥路径 ──
        pub_uncompressed = pub.format(compressed=False)  # 65 bytes
        h160_uncomp = hash160(pub_uncompressed)

        if h160_uncomp in target:
            return CollisionResult(
                privkey_hex=privkey_bytes.hex(),
                wif_compressed=wif_encode(privkey_bytes, compressed=True),
                wif_uncompressed=wif_encode(privkey_bytes, compressed=False),
                p2pkh_address_comp="",
                p2wpkh_address="",
                p2pkh_address_uncomp=p2pkh_address(h160_uncomp),
                h160_hex=h160_uncomp.hex(),
                address_type="P2PKH (Legacy)",
                found_via="uncompressed",
                p2sh_address="",
            )

        # ── P2TR (Taproot) 路径 ──
        if xonly_target is not None:
            xonly_output = tweak_taproot(pub)
            if xonly_output is not None and xonly_output in xonly_target:
                return CollisionResult(
                    privkey_hex=privkey_bytes.hex(),
                    wif_compressed=wif_encode(privkey_bytes, compressed=True),
                    wif_uncompressed=wif_encode(privkey_bytes, compressed=False),
                    p2pkh_address_comp="",
                    p2wpkh_address="",
                    p2pkh_address_uncomp="",
                    h160_hex="",
                    address_type="P2TR (Taproot)",
                    found_via="tweaked",
                    p2tr_address=p2tr_address(xonly_output),
                    xonly_hex=xonly_output.hex(),
                    p2sh_address="",
                )

    except Exception as exc:  # noqa: BLE001
        _logger and _logger.warning("check_single_key 异常: %s", exc, exc_info=True)
    return None


def check_single_key_chain(
    privkey_int: int,
    target: TargetProtocol,
    stride_bytes: bytes,
    prev_pubkey_point: object | None = None,
    xonly_target: TargetProtocol | None = None,
) -> tuple[CollisionResult | None, object | None]:
    """顺序链式检查:利用点加法链加速公钥推导..

    顺序模式中,每线程处理 stride = n_threads 的等差数列.
    首 key 做完整 EC 乘法,后续 key 通过点加法
    pub(n+stride) = pub(n) + stride*G 快速推导(~200 倍加速).

    Args:
        privkey_int: 当前私钥值(仅首次需要全量 EC 乘法)
        target: HASH160 目标集
        stride_bytes: 线程步长的 32 字节大端编码
        prev_pubkey_point: 上一公钥的 PublicKey 对象(None 则全量计算)
        xonly_target: P2TR x-only pubkey 目标集(None 则跳过 P2TR 检查)

    Returns:
        (CollisionResult or None, 当前公钥 PublicKey or None)

    """
    try:
        if prev_pubkey_point is None:
            privkey_bytes = privkey_int.to_bytes(32, "big")
            priv = PrivateKey(privkey_bytes)
            pubkey = priv.public_key
        else:
            pubkey = prev_pubkey_point  # type: ignore[assignment]
            pubkey.add(stride_bytes, update=True)

        # ── 压缩公钥路径 ──
        pub_compressed = pubkey.format(compressed=True)
        h160_comp = hash160(pub_compressed)

        if h160_comp in target:
            privkey_bytes = privkey_int.to_bytes(32, "big")
            return (
                CollisionResult(
                    privkey_hex=privkey_bytes.hex(),
                    wif_compressed=wif_encode(privkey_bytes, compressed=True),
                    wif_uncompressed=wif_encode(privkey_bytes, compressed=False),
                    p2pkh_address_comp=p2pkh_address(h160_comp),
                    p2wpkh_address=p2wpkh_address(h160_comp),
                    p2pkh_address_uncomp="",
                    h160_hex=h160_comp.hex(),
                    address_type="P2WPKH/P2PKH",
                    found_via="compressed",
                    p2sh_address=p2sh_address(h160_comp),
                ),
                pubkey,
            )

        # ── 非压缩公钥路径 ──
        pub_uncompressed = pubkey.format(compressed=False)
        h160_uncomp = hash160(pub_uncompressed)

        if h160_uncomp in target:
            privkey_bytes = privkey_int.to_bytes(32, "big")
            return (
                CollisionResult(
                    privkey_hex=privkey_bytes.hex(),
                    wif_compressed=wif_encode(privkey_bytes, compressed=True),
                    wif_uncompressed=wif_encode(privkey_bytes, compressed=False),
                    p2pkh_address_comp="",
                    p2wpkh_address="",
                    p2pkh_address_uncomp=p2pkh_address(h160_uncomp),
                    h160_hex=h160_uncomp.hex(),
                    address_type="P2PKH (Legacy)",
                    found_via="uncompressed",
                    p2sh_address="",
                ),
                pubkey,
            )

        # ── P2TR (Taproot) 路径 ──
        if xonly_target is not None:
            # 创建 pubkey 副本(不改变链状态)
            pub_copy = PublicKey(pubkey.format())
            xonly_output = tweak_taproot(pub_copy)
            if xonly_output is not None and xonly_output in xonly_target:
                privkey_bytes = privkey_int.to_bytes(32, "big")
                return (
                    CollisionResult(
                        privkey_hex=privkey_bytes.hex(),
                        wif_compressed=wif_encode(privkey_bytes, compressed=True),
                        wif_uncompressed=wif_encode(privkey_bytes, compressed=False),
                        p2pkh_address_comp="",
                        p2wpkh_address="",
                        p2pkh_address_uncomp="",
                        h160_hex="",
                        address_type="P2TR (Taproot)",
                        found_via="tweaked",
                        p2tr_address=p2tr_address(xonly_output),
                        xonly_hex=xonly_output.hex(),
                        p2sh_address="",
                    ),
                    pubkey,
                )

        return (None, pubkey)

    except Exception as exc:  # noqa: BLE001
        _logger and _logger.warning(
            "check_single_key_chain 异常: %s",
            exc,
            exc_info=True,
        )
    return (None, None)


# ── 线程工作函数 ──────────────────────────────────────────────
_counter_lock = threading.Lock()
_global_checked = 0
_global_start_time = time.time()
_global_last_checkpoint = time.time()


def worker_sequential(
    counter: SequentialCounter,
    target: TargetProtocol,
    thread_id: int,
    stride_bytes: bytes | None = None,
    xonly_target: TargetProtocol | None = None,
) -> int:
    """顺序模式工作线程(点加法链加速)..

    Args:
        counter: 顺序计数器
        target: HASH160 目标集
        thread_id: 线程 ID(用于日志)
        stride_bytes: 线程步长的 32 字节大端编码.
        xonly_target: P2TR x-only pubkey 目标集(可选).

    """
    global _global_checked
    local_checked = 0
    last_reported = 0
    pubkey_point = None

    while True:
        key = counter.next()
        if key is None:
            break

        if _shutdown_requested:
            _logger.info("关闭请求,停止顺序工作线程 %d", thread_id)
            break

        if stride_bytes is not None:
            result, pubkey_point = check_single_key_chain(
                key,
                target,
                stride_bytes,
                pubkey_point,
                xonly_target,
            )
        else:
            result = check_single_key(key, target, xonly_target)

        if result:
            save_result(result)

        local_checked += 1

        # 每 1000 个 key 更新全局计数
        if local_checked % 1000 == 0:
            with _counter_lock:
                _global_checked += 1000
            # 报告进度
            if local_checked - last_reported >= PROGRESS_INTERVAL:
                _report_progress(counter.current, local_checked, thread_id)
                last_reported = local_checked
            # 检查点
            if local_checked % CHECKPOINT_INTERVAL == 0:
                with _counter_lock:
                    save_checkpoint(
                        {
                            "mode": "sequential",
                            "next_key": counter.current,
                            "checked": counter.checked,
                        },
                    )

    # 线程结束时刷新
    with _counter_lock:
        _global_checked += local_checked % 1000
    return local_checked


def worker_random(
    target: TargetProtocol,
    thread_id: int,
    check_limit: int = 0,
    xonly_target: TargetProtocol | None = None,
) -> int:
    """随机模式工作线程."""
    global _global_checked
    local_checked = 0
    last_reported = 0
    _thread_rng = random.Random()  # noqa: S311
    # 每线程独立 Random 实例,避免多线程竞争全局种子

    while True:
        privkey_int = _thread_rng.randint(1, SECP256K1_ORDER - 1)

        if _shutdown_requested:
            _logger.info("关闭请求,停止随机工作线程 %d", thread_id)
            break

        result = check_single_key(privkey_int, target, xonly_target)
        if result:
            save_result(result)

        local_checked += 1

        # 每 1000 个 key 更新一次全局计数并检查限制
        if local_checked % 1000 == 0:
            with _counter_lock:
                _global_checked += 1000
                if check_limit > 0 and _global_checked >= check_limit:
                    return local_checked
            # 报告进度
            if local_checked - last_reported >= PROGRESS_INTERVAL * 3:
                _report_progress(0, local_checked, thread_id)
                last_reported = local_checked

    return local_checked


# ── GPU 模式 ──────────────────────────────────────────────────
def _run_gpu_mode(
    target: TargetProtocol,
    args: argparse.Namespace,
    xonly_target: TargetProtocol | None = None,
) -> None:
    """GPU 加速的碰撞扫描入口.."""
    # 解析设备索引
    device_indices = None
    if args.gpu_devices:
        try:
            device_indices = [
                int(s.strip()) for s in args.gpu_devices.split(",") if s.strip()
            ]
        except ValueError:
            _logger.exception("[错误] --gpu-devices 格式无效: %s", args.gpu_devices)
            sys.exit(1)

    # 顺序模式 checkpoint 恢复
    seq_start = 1
    total_checked_pre = 0
    if args.gpu_mode == "sequential":
        seq_start = int(args.gpu_start, 16)
        cp = load_checkpoint()
        next_key_val: object = cp.get("next_key", 0)
        if (
            cp.get("mode") == "gpu_sequential"
            and isinstance(next_key_val, int)
            and next_key_val > seq_start
        ):
            seq_start = next_key_val
            checked_val: object = cp.get("checked", 0)
            total_checked_pre = checked_val if isinstance(checked_val, int) else 0
            _logger.info(
                "[GPU][恢复] 从 checkpoint 恢复: next_key=0x%064x (已检查 %s)",
                seq_start,
                f"{total_checked_pre:,}",
            )

        _logger.info("[GPU][开始] 顺序扫描 起始: 0x%064x", seq_start)
        _logger.info("        按 Ctrl+C 停止")
        tdr_tag = "TDR 安全" if args.gpu_tdr_safe else "TDR 未保护"
        _logger.info("        batch=%s | %s", f"{args.gpu_batch_size:,}", tdr_tag)

    # TDR 诊断(Windows 平台)
    if args.gpu_tdr_safe:
        from gpu_engine.tdr_handler import warn_tdr_settings

        warn_tdr_settings(quiet=False)

    # 碰撞检查回调
    def check_hit(h160: bytes) -> bool:
        """碰撞检测回调:检查 HASH160 是否在目标集中.."""
        return h160 in target

    # 命中保存回调: privkey_bytes(32B 小端, GPU kernel 编码) → 推导 HASH160 → 保存结果
    def on_hit(privkey_bytes: bytes) -> None:
        """碰撞命中回调:从 GPU 返回的 32 字节私钥推导地址并保存结果.."""
        privkey_int = int.from_bytes(privkey_bytes, "little")
        result = check_single_key(privkey_int, target, xonly_target)
        if result is not None:
            save_result(result)
        else:
            _logger.error(
                "[GPU] 碰撞命中但二次推导失败! privkey=0x%064x",
                privkey_int,
            )

    # P2-10: 当目标集有 Bloom 数据时启用 GPU 侧碰撞检测,
    # 将 check_collision 设为 None 以触发 _worker_loop 的 GPU 碰撞路径.
    # 假阳性由 on_hit 回调中的 check_single_key() 全量验证.
    gpu_bloom = target.bloom_data
    config = DispatcherConfig(
        batch_size=args.gpu_batch_size,
        device_indices=device_indices,
        total_keys=args.count,
        quiet=False,
        check_collision=(None if gpu_bloom is not None else check_hit),
        on_hit=on_hit,
        mode=args.gpu_mode,
        sequential_start=seq_start,
        tdr_safe=args.gpu_tdr_safe,
        max_kernel_time=args.gpu_max_kernel_time,
        bloom_data=gpu_bloom,
        bloom_m=target.bloom_m,
    )

    scheduler = GPUBatchScheduler(config)
    if not scheduler.initialize():
        sys.exit(1)

    try:
        workers = scheduler.run()
        hits = sum(w.hits for w in workers)
        if hits > 0:
            _logger.info("[GPU] 发现 %d 个碰撞!保存在 %s", hits, RESULTS_FILE)
    except KeyboardInterrupt:
        _logger.warning("\n[GPU] 扫描被用户中断")
        # GPU 顺序模式 checkpoint
        if args.gpu_mode == "sequential" and scheduler._pipelines:
            # 取第一个管道的当前起始值作为下一个检查点的 next_key
            # (多 GPU 时取最小的起始值,此方案保守但安全)
            next_k = min(p.sequential_start for p in scheduler._pipelines)
            checked = max(seq_start, total_checked_pre) + scheduler._total_checked
            save_checkpoint(
                {
                    "mode": "gpu_sequential",
                    "next_key": next_k,
                    "checked": checked,
                },
            )
            _logger.info("[GPU][检查点] 已保存 (next_key=0x%064x)", next_k)
    finally:
        scheduler.close()


def _report_progress(current_key: int, local_count: int, thread_id: int) -> None:
    """记录进度(周期性进度信息)+ 写入引擎状态文件.."""
    elapsed = time.time() - _global_start_time
    with _counter_lock:
        total = _global_checked
    rate = total / elapsed if elapsed > 0 else 0
    key_str = f"0x{current_key:064x}" if current_key else "random"
    _logger.info(
        "[进度] T%d | 已检查: %s | 速率: %s keys/s | 耗时: %.0fs | 当前: %s",
        thread_id,
        f"{total:,}",
        f"{rate:,.0f}",
        elapsed,
        key_str[:32],
    )
    # 写入状态文件供 API 读取(降频:每 30 秒写一次)
    _status_write_window = 5
    if int(elapsed) % 30 < _status_write_window:
        _write_engine_status(
            running=True,
            mode=_config.mode if _config else "unknown",
            keys_per_second=rate,
            total_keys=total,
            elapsed_seconds=elapsed,
        )


# ── 引擎状态文件写入(供 API 读取)─────────────────────────────
_STATUS_FILE_PATH = Path(__file__).resolve().parent / "collision_engine_status.json"


def _write_engine_status(
    running: bool,  # noqa: FBT001
    mode: str,
    keys_per_second: float = 0.0,
    total_keys: int = 0,
    elapsed_seconds: float = 0.0,
) -> None:
    """写入引擎运行状态 JSON 文件,供 API 读取..

    包含运行状态,扫描速率,UTXO 刷新状态等信息.
    """
    status: dict[str, Any] = {
        "running": running,
        "mode": mode,
        "keys_per_second": keys_per_second,
        "total_keys": total_keys,
        "elapsed_seconds": elapsed_seconds,
    }
    # ── UTXO 刷新状态 ──
    refresh_enabled = _config is not None and _config.enable_utxo_auto_refresh
    status["utxo_refresh"] = {
        "enabled": refresh_enabled,
        "last_refresh_time": (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(_refresh_last_time))
            if _refresh_last_time > 0
            else "never"
        ),
        "last_result": _refresh_last_result,
    }
    if refresh_enabled and _swappable_target is not None:
        status["utxo_refresh"]["current_count"] = len(_swappable_target)

    try:
        _STATUS_FILE_PATH.write_text(
            json.dumps(status, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass  # 写入失败不影响引擎运行


# ── 健康检查 ──────────────────────────────────────────────────
def _health_check() -> None:
    """执行健康检查并输出 JSON 状态到 stdout.."""
    status: dict[str, Any] = {"status": "ok", "checks": {}}

    # 数据库连接
    if _db is not None:
        try:
            count = _db.count_results()
            status["checks"]["database"] = {
                "status": "ok",
                "result_count": count,
            }
        except Exception as exc:  # noqa: BLE001
            status["checks"]["database"] = {
                "status": "error",
                "message": str(exc),
            }
    else:
        status["checks"]["database"] = {"status": "not_initialized"}

    # UTXO 数据文件
    utxo_path = Path(__file__).parent / "utxo_hash160.bin"
    idx_path = Path(__file__).parent / "utxo_hash160.idx"
    status["checks"]["utxo_data"] = {
        "present": utxo_path.exists(),
        "path": str(utxo_path),
        "index_present": idx_path.exists(),
    }
    if utxo_path.exists():
        status["checks"]["utxo_data"]["size_gb"] = round(
            utxo_path.stat().st_size / (1024**3),
            2,
        )

    # UTXO 自动刷新状态
    refresh_enabled = _config is not None and _config.enable_utxo_auto_refresh
    status["checks"]["utxo_refresh"] = {
        "enabled": refresh_enabled,
        "last_refresh_time": (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(_refresh_last_time))
            if _refresh_last_time > 0
            else "never"
        ),
        "last_result": _refresh_last_result,
    }
    if refresh_enabled and _swappable_target is not None:
        status["checks"]["utxo_refresh"]["current_count"] = len(_swappable_target)

    # GPU 可用性
    status["checks"]["gpu"] = {"available": _GPU_AVAILABLE}
    if _GPU_AVAILABLE:
        try:
            devices = gpu_list_devices()
            status["checks"]["gpu"]["device_count"] = len(devices)
            status["checks"]["gpu"]["devices"] = [str(d) for d in devices]
        except Exception as exc:  # noqa: BLE001
            status["checks"]["gpu"]["device_enum_error"] = str(exc)

    # 综合状态
    for chk in status["checks"].values():
        if isinstance(chk, dict) and chk.get("status") == "error":
            status["status"] = "degraded"
    if not status["checks"]["utxo_data"]["present"]:
        status["status"] = "degraded"

    print(json.dumps(status, indent=2, ensure_ascii=False))  # noqa: T201


# ── 主入口 ────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器.."""
    parser = argparse.ArgumentParser(
        description="比特币私钥碰撞对撞引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # CPU 模式
  python collision_engine.py --threads 4
  python collision_engine.py --mode random --threads 8
  python collision_engine.py --mode sequential --start 0x100000 --count 1000000

  # GPU 模式
  python collision_engine.py --gpu
  python collision_engine.py --gpu --gpu-mode sequential --gpu-start 0x100000
  python collision_engine.py --gpu --gpu-devices 0,1 --gpu-batch-size 131072

  # TDR 安全控制
  python collision_engine.py --gpu --gpu-no-tdr-safe  # 禁用 TDR 安全拆分
  python collision_engine.py --gpu --gpu-max-kernel-time 0.5  # 更短的 sub-batch

  # P2TR (Taproot) 模式(需先运行 extract_utxo_xonly.py)
  python collision_engine.py --p2tr --threads 4
  python collision_engine.py --p2tr --mode random --threads 8
  python collision_engine.py --p2tr --mode sequential --start 0x100000

  # 列出 OpenCL 设备
  python collision_engine.py --list-gpu
""",
    )
    parser.add_argument(
        "--mode",
        choices=["sequential", "random"],
        default="sequential",
        help="扫描模式 (default: sequential)",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="0x1",
        help="起始私钥 (hex, default: 1)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="要检查的私钥数量 (0 = 无限)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=os.cpu_count() or 4,
        help=f"CPU 线程数 (default: {os.cpu_count() or 4})",
    )
    # ── GPU 参数 ──
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="启用 GPU 加速模式 (代替 --mode/--threads)",
    )
    parser.add_argument(
        "--gpu-devices",
        type=str,
        default="",
        help="GPU 设备索引 (逗号分隔, 如 0,1; 默认使用所有可用 GPU)",
    )
    parser.add_argument(
        "--gpu-mode",
        choices=["random", "sequential"],
        default="random",
        help="GPU 扫描模式 (default: random)",
    )
    parser.add_argument(
        "--gpu-start",
        type=str,
        default="0x1",
        help="GPU 顺序扫描起始私钥 (hex, default: 1)",
    )
    parser.add_argument(
        "--gpu-batch-size",
        type=int,
        default=65536,
        help="每个 GPU batch 的私钥数 (default: 65536)",
    )
    parser.add_argument(
        "--gpu-tdr-safe",
        action="store_true",
        dest="gpu_tdr_safe",
        default=True,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--gpu-no-tdr-safe",
        action="store_false",
        dest="gpu_tdr_safe",
        help="禁用 TDR 安全 sub-batch 拆分 (默认启用)",
    )
    parser.add_argument(
        "--gpu-max-kernel-time",
        type=float,
        default=1.5,
        help="单个 sub-batch 内核最大执行时间(秒) (default: 1.5)",
    )
    parser.add_argument(
        "--p2tr",
        action="store_true",
        help="启用 P2TR (Taproot) 碰撞匹配(需先运行 extract_utxo_xonly.py)",
    )
    parser.add_argument(
        "--xonly-file",
        type=str,
        default="",
        help="P2TR x-only pubkey 二进制文件路径 (默认: utxo_xonly.bin)",
    )
    parser.add_argument(
        "--list-gpu",
        action="store_true",
        help="列出所有 OpenCL 设备并退出",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="执行健康检查并退出",
    )
    # ── UTXO 自动刷新参数 ──
    parser.add_argument(
        "--utxo-refresh",
        action="store_true",
        dest="utxo_refresh",
        default=None,
        help="启用 UTXO 自动刷新(覆盖配置)",
    )
    parser.add_argument(
        "--no-utxo-refresh",
        action="store_false",
        dest="utxo_refresh",
        help="禁用 UTXO 自动刷新",
    )
    parser.add_argument(
        "--utxo-refresh-interval",
        type=int,
        default=0,
        help="UTXO 刷新间隔(秒,覆盖配置)",
    )
    # ── 分布式扫描参数 ──
    parser.add_argument(
        "--distributed",
        action="store_true",
        help="启用分布式扫描模式(作为 Worker 节点运行)",
    )
    parser.add_argument(
        "--master-addr",
        type=str,
        default="localhost:50051",
        help="Master 地址 (host:port, default: localhost:50051)",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default="",
        help="Worker 唯一标识 (default: auto-generated)",
    )
    parser.add_argument(
        "--master-http-port",
        type=int,
        default=8080,
        help="Master HTTP 端口 (FastAPI, default: 8080)",
    )
    return parser


def _load_targets(
    args: argparse.Namespace,
) -> tuple[SwappableTarget, SwappableTarget | None]:
    """加载 UTXO HASH160 目标集(和可选的 P2TR x-only 目标集)..

    返回的 target / xonly_target 是 ``SwappableTarget`` 包装器,
    支持在运行时原子替换底层数据(UTXO 自动刷新).

    Returns:
        (swappable_target, swappable_xonly) 元组.

    """
    global _swappable_target, _swappable_xonly

    _logger.info("[...] 加载 UTXO HASH160 目标集...")
    target = Hash160Set()
    target.load(quiet=True)
    _logger.info("[OK] 已加载 %s 个 HASH160", f"{len(target):,}")

    swappable_target = SwappableTarget(initial_set=target)
    _swappable_target = swappable_target

    swappable_xonly: SwappableTarget | None = None
    if args.p2tr:
        xonly_path = args.xonly_file or None
        _logger.info("[...] 加载 P2TR x-only pubkey 目标集...")
        xonly_target_set = XOnlySet()
        xonly_target_set.load(bin_path=xonly_path, quiet=True)
        swappable_xonly = SwappableTarget(initial_set=xonly_target_set)
        _swappable_xonly = swappable_xonly
        _logger.info("[OK] 已加载 %s 个 x-only pubkey", f"{len(xonly_target_set):,}")

    return swappable_target, swappable_xonly


def _display_banner(
    target: TargetProtocol,
    args: argparse.Namespace,
    xonly_target: TargetProtocol | None,
) -> None:
    """打印引擎启动 banner.."""
    _logger.info("\n%s", "#" * 70)
    _logger.info("#   Bitcoin 私钥碰撞对撞引擎 v2.3.0")
    _logger.info(
        "#   目标集: %s 个有余额地址的 HASH160 | P2TR: %s",
        f"{len(target):,}",
        "Y" if args.p2tr else "N",
    )
    gpu_tag = " [GPU 可用]" if _GPU_AVAILABLE else ""
    _logger.info("#   模式: %s | 线程: %s%s", args.mode, args.threads, gpu_tag)
    if args.count:
        _logger.info("#   限量: %s 个私钥", f"{args.count:,}")
    else:
        _logger.info("#   无限扫描 (Ctrl+C 安全停止)")
    _logger.info("%s\n", "#" * 70)


def _run_cpu_mode(
    target: TargetProtocol,
    args: argparse.Namespace,
    xonly_target: TargetProtocol | None,
) -> None:
    """运行 CPU 扫描(顺序或随机模式).包含 checkpoint 恢复/保存逻辑.."""
    global _global_start_time, _global_checked
    _global_start_time = time.time()
    _global_checked = 0

    counter: SequentialCounter | None = None
    try:
        if args.mode == "sequential":
            start_val = int(args.start, 16)

            cp = load_checkpoint()
            next_key_val2: object = cp.get("next_key", 0)
            if (
                cp.get("mode") == "sequential"
                and isinstance(next_key_val2, int)
                and next_key_val2 > start_val
            ):
                start_val = next_key_val2
                _logger.info("[恢复] 从 checkpoint 恢复: next_key=0x%064x", start_val)
                chk_val: object = cp.get("checked", 0)
                _global_checked = chk_val if isinstance(chk_val, int) else 0

            _logger.info("[开始] 顺序扫描 起始: 0x%064x", start_val)
            _logger.info("        按 Ctrl+C 停止\n")

            counter = SequentialCounter(start_val, args.count)

            stride_bytes = args.threads.to_bytes(32, "big")

            with ThreadPoolExecutor(max_workers=args.threads) as executor:
                futures = [
                    executor.submit(
                        worker_sequential,
                        counter,
                        target,
                        i,
                        stride_bytes,
                        xonly_target,
                    )
                    for i in range(args.threads)
                ]
                sum(f.result() for f in as_completed(futures))

        else:  # random
            _logger.info("[开始] 随机扫描")
            _logger.info("        按 Ctrl+C 停止\n")

            with ThreadPoolExecutor(max_workers=args.threads) as executor:
                futures = [
                    executor.submit(
                        worker_random,
                        target,
                        i,
                        args.count,
                        xonly_target,
                    )
                    for i in range(args.threads)
                ]
                sum(f.result() for f in as_completed(futures))

    except KeyboardInterrupt:
        _logger.warning("\n\n[停止] 用户中断")
        if args.mode == "sequential" and counter is not None:
            save_checkpoint(
                {
                    "mode": "sequential",
                    "next_key": counter.current,
                    "checked": counter.checked,
                },
            )
            _logger.info("[检查点] 已保存")

    # 信号处理器的优雅关闭(若 KeyboardInterrupt 未触发或未保存 checkpoint)
    if _shutdown_requested and args.mode == "sequential" and counter is not None:
        save_checkpoint(
            {
                "mode": "sequential",
                "next_key": counter.current,
                "checked": counter.checked,
            },
        )
        _logger.info("优雅关闭 - checkpoint 已保存 (next_key=0x%064x)", counter.current)


def _print_final_report() -> None:
    """打印扫描结束的最终报告(耗时,速率,命中数).."""
    elapsed = time.time() - _global_start_time
    with _counter_lock:
        final_checked = _global_checked
    rate = final_checked / elapsed if elapsed > 0 else 0

    hit_count = 0
    if RESULTS_FILE.exists():
        try:
            hit_count = len(json.loads(RESULTS_FILE.read_text()))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("无法读取碰撞结果文件: %s", exc)

    _logger.info(
        "\n%s\n  扫描结束\n"
        "    已检查: %s 个私钥\n"
        "    耗时:   %.0fs\n"
        "    速率:   %s keys/s\n"
        "    命中:   %d\n"
        "%s\n",
        "=" * 70,
        f"{final_checked:,}",
        elapsed,
        f"{rate:,.0f}",
        hit_count,
        "=" * 70,
    )
    # 写入引擎已停止状态
    _write_engine_status(
        running=False,
        mode="stopped",
        keys_per_second=rate,
        total_keys=final_checked,
        elapsed_seconds=elapsed,
    )


def _cleanup(
    target: TargetProtocol | None,
    xonly_target: TargetProtocol | None,
) -> None:
    """清理资源:关闭目标集,通知器和数据库连接.."""
    if target is not None:
        target.close()
    if xonly_target is not None:
        xonly_target.close()

    if _notifier is not None:
        try:
            _notifier.shutdown(wait=True)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("通知器关闭异常: %s", exc)

    if _db is not None:
        try:
            _db.close()
            _logger.info("数据库连接已关闭")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("数据库关闭异常: %s", exc)


def main() -> None:
    """主入口:解析参数 → 初始化 → 分发 CPU 或 GPU 模式.."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    # ── 初始化核心基础设施 ──
    _init_core()
    _logger.info(
        "引擎启动: mode=%s, gpu=%s, p2tr=%s",
        args.mode,
        args.gpu,
        args.p2tr,
    )

    # ── 分布式扫描模式 ──
    if args.distributed:
        _logger.info(
            "启用分布式扫描模式 (master=%s, worker=%s)",
            args.master_addr,
            args.worker_id,
        )
        try:
            from distributed.worker import DistributedScanner

            worker_id = args.worker_id
            if not worker_id:
                import os as _os

                worker_id = f"worker-{_os.urandom(4).hex()}"

            scanner = DistributedScanner(
                master_addr=args.master_addr,
                worker_id=worker_id,
                master_http_port=args.master_http_port,
                cpu_cores=args.threads,
                gpu_enabled=args.gpu,
                gpu_devices=args.gpu_devices,
                gpu_batch_size=args.gpu_batch_size,
                count=args.count,
                p2tr=args.p2tr,
                xonly_file=args.xonly_file,
            )
            if scanner.connect():
                scanner.run()
            else:
                _logger.error("分布式 Worker 连接失败,退出")
                sys.exit(1)
            _cleanup(target=None, xonly_target=None)
            return
        except ImportError as exc:
            _logger.exception("分布式模块不可用,请安装 grpcio/protobuf")
            sys.exit(1)

    # ── 应用 UTXO 刷新 CLI 参数(覆盖配置) ──
    if args.utxo_refresh is not None:
        if _config is None:
            msg = "配置尚未初始化"
            raise RuntimeError(msg)
        _config.enable_utxo_auto_refresh = args.utxo_refresh
        _logger.info(
            "UTXO 自动刷新: %s (CLI 覆盖)",
            "启用" if args.utxo_refresh else "禁用",
        )
    if args.utxo_refresh_interval > 0:
        if _config is None:
            msg = "配置尚未初始化"
            raise RuntimeError(msg)
        _config.utxo_refresh_interval = args.utxo_refresh_interval
        _logger.info("UTXO 刷新间隔覆盖为: %ds", args.utxo_refresh_interval)

    # ── 存储扫描模式到配置 ──
    if _config is not None:
        _config.mode = args.mode

    # ── 初始化通知器 ──
    if _config is not None and _config.notify_on_hit:
        _notifier = Notifier(_config)
        _logger.info(
            "通知已启用 (SMTP=%s, Webhook=%s, Telegram=%s)",
            "✓" if _config.smtp_host else "✗",
            "✓" if _config.webhook_url else "✗",
            "✓" if _config.telegram_bot_token else "✗",
        )

    # ── 健康检查 ──
    if args.health:
        _health_check()
        return

    # ── 注册优雅关闭信号处理器 ──
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    _logger.info("已注册信号处理器: SIGTERM, SIGINT")

    # ── 列出 GPU 设备 ──
    if args.list_gpu:
        if _GPU_AVAILABLE:
            devices = gpu_list_devices()
            if devices:
                for _i, _dev in enumerate(devices):
                    pass
            else:
                pass
        else:
            pass
        return

    # ── 加载目标集 ──
    target, xonly_target = _load_targets(args)

    # ── 启动 UTXO 自动刷新 ──
    _start_utxo_refresher(
        _config,  # type: ignore[arg-type]
        target,
        xonly_target,
        logger=_logger,
    )

    # ── GPU 模式快速入口 ──
    if args.gpu:
        if not _GPU_AVAILABLE:
            _logger.error(
                "\n[错误] GPU 模式需要 pyopencl.请安装: pip install pyopencl>=2024.1",
            )
            sys.exit(1)

        _run_gpu_mode(target, args, xonly_target)
        _cleanup(target, xonly_target)
        return

    # ── Banner ──
    _display_banner(target, args, xonly_target)

    # ── 检查已有结果 ──
    if RESULTS_FILE.exists():
        old_hits = len(json.loads(RESULTS_FILE.read_text()))
        _logger.info("[信息] 已有碰撞结果文件: %d 条命中记录", old_hits)
    else:
        _logger.info("[信息] 碰撞结果将保存至: %s", RESULTS_FILE.name)

    # ── CPU 扫描 ──
    _run_cpu_mode(target, args, xonly_target)

    # ── 最终报告 ──
    _print_final_report()

    # ── 清理 ──
    _cleanup(target, xonly_target)


if __name__ == "__main__":
    main()
