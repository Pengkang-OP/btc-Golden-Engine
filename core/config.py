"""配置管理模块 - EngineConfig 配置类..

取代 collision_engine.py 顶部的硬编码常量 (第 65-74 行).
使用 Python dataclass + JSON 持久化.

用法:
    config = EngineConfig()
    config.results_file = Path("collision_results.db")
    config.save(Path("collision_engine.conf"))
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os


@dataclass
class EngineConfig:
    """碰撞引擎全局配置..

    所有配置项均有合理默认值,可直接使用 EngineConfig() 构造.
    通过 load() 从 JSON 文件加载,或 save() 持久化到 JSON 文件.
    """

    # ── 椭圆曲线 ──
    secp256k1_order: int = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

    # ── 文件路径 ──
    results_db: str = "collision_results.db"  # SQLite 数据库路径
    checkpoint_file: str = "collision_checkpoint.json"
    config_file: str = "collision_engine.conf"
    log_file: str = "logs/collision.log"
    log_dir: str = "logs"
    config_path: Path | None = None  # 配置文件路径 (用于 hot reload 监测)

    # ── 扫描参数 ──
    mode: str = "random"  # 扫描模式: "random" 或 "sequential"
    progress_interval: int = 5000  # 每 N 个 key 报告一次进度
    checkpoint_interval: int = 60_000  # 每 N 个 key 保存一次 checkpoint

    # ── 日志 ──
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB 每个日志文件
    log_backup_count: int = 5  # 保留 5 个备份

    # ── 通知 (Phase 2 使用) ──
    notify_on_hit: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── 统计 (Phase 2 使用) ──
    stats_window_seconds: int = 3600  # 滑动窗口大小 (1 小时)

    # ── UTXO 自动刷新 ──
    enable_utxo_auto_refresh: bool = False  # opt-in:默认关闭
    utxo_refresh_interval: int = 3600  # 刷新检查间隔 (秒),默认 1 小时
    utxo_snapshot_path: str = "utxo_snapshot.dat"  # 快照文件路径
    utxo_hash160_bin: str = "utxo_hash160.bin"  # Hash160 二进制输出
    utxo_hash160_idx: str = "utxo_hash160.idx"  # Hash160 索引文件
    bitcoin_cli_path: str = ""  # bitcoin-cli 路径,为空时自动检测
    bitcoin_datadir: str = ""  # Bitcoin 数据目录,为空时自动检测

    # ── 内部状态 ──
    _base_dir: Path = field(default_factory=Path.cwd, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _mtime: float = 0.0  # 文件修改时间戳 (check_reload 使用)

    def __post_init__(self) -> None:
        """初始化后处理:路径解析,URL 校验.."""
        self._resolve_paths()
        self._validate_urls()

    def __repr__(self) -> str:
        """脱敏输出,避免凭据泄露到日志.."""
        return (
            f"EngineConfig(mode={self.mode}, notify_on_hit={self.notify_on_hit}, "
            f"enable_utxo_auto_refresh={self.enable_utxo_auto_refresh})"
        )

    def _validate_urls(self) -> None:
        """验证 webhook_url 的 scheme 必须是 https://(防止 SSRF).."""
        url = self.webhook_url
        if url and not url.startswith("https://"):
            msg = f"webhook_url 必须使用 HTTPS: {url}"
            raise ValueError(msg)

    def _resolve_paths(self) -> None:
        """将所有相对路径字段转换为绝对路径.."""
        base = self._base_dir
        for field_name in ("results_db", "checkpoint_file", "config_file", "log_file"):
            val = getattr(self, field_name)
            if val and not Path(val).is_absolute():
                setattr(self, field_name, str(base / val))
        # log_dir
        if self.log_dir and not Path(self.log_dir).is_absolute():
            self.log_dir = str(base / self.log_dir)

        # UTXO 路径
        for field_name in (
            "utxo_snapshot_path",
            "utxo_hash160_bin",
            "utxo_hash160_idx",
        ):
            val = getattr(self, field_name)
            if val and not Path(val).is_absolute():
                setattr(self, field_name, str(base / val))

    def check_reload(self) -> bool:
        """检查配置文件是否已更改,若需要则重新加载..

        通过比较文件的 mtime (修改时间) 实现.
        仅更新允许热重载的字段 (不可变字段如 secp256k1_order 跳过).
        解析失败时保留旧配置.

        Returns:
            True 表示配置已重新加载, False 表示无变化.

        """
        if self.config_path is None or not self.config_path.exists():
            return False

        with self._lock:
            try:
                current_mtime = self.config_path.stat().st_mtime
                if current_mtime <= self._mtime:
                    return False
                self._mtime = current_mtime

                new_config = EngineConfig.load(self.config_path)
                # 选择性更新字段 (跳过不可变字段)  # noqa: ERA001
                for field_name in self.__dataclass_fields__:
                    if field_name in (
                        "secp256k1_order",
                        "config_path",
                        "_base_dir",
                        "_lock",
                    ):
                        continue
                    setattr(self, field_name, getattr(new_config, field_name))
                return True  # noqa: TRY300
            except Exception:  # noqa: BLE001
                logger = logging.getLogger("config.check_reload")
                logger.warning("配置热重载失败 (保留旧配置)")
                return False

    @classmethod
    def load(cls, path: os.PathLike[str] | None = None) -> EngineConfig:
        """从 JSON 配置文件加载配置..

        Args:
            path: 配置文件路径.None 则尝试在 CWD 中查找默认文件名.

        Returns:
            加载后的 EngineConfig 实例.

        Raises:
            FileNotFoundError: 配置文件不存在.
            json.JSONDecodeError: 配置文件格式错误.

        """
        path = Path.cwd() / "collision_engine.conf" if path is None else Path(path)

        if not path.exists():
            msg = f"配置文件不存在: {path}"
            raise FileNotFoundError(msg)

        raw = json.loads(path.read_text(encoding="utf-8"))
        # 过滤掉私有字段和 config_path (不在 JSON 中)
        filtered = {k: v for k, v in raw.items() if not k.startswith("_") and k != "config_path"}
        config = cls(**filtered)
        config.config_path = path  # 记录配置文件路径用于 hot reload
        config._mtime = path.stat().st_mtime
        return config

    def save(self, path: os.PathLike[str] | None = None) -> None:
        """将配置持久化到 JSON 文件..

        Args:
            path: 输出路径.None 则使用 config_file 字段值.

        """
        path = Path(self.config_file) if path is None else Path(path)

        path.parent.mkdir(parents=True, exist_ok=True)
        # 手动构建字典(避免 asdict() deepcopy 线程锁等不可 pickled 对象)
        data = {}
        for f in self.__dataclass_fields__:
            if f.startswith("_"):
                continue  # 跳过私有字段
            val = getattr(self, f)
            if isinstance(val, Path):
                val = str(val)
            data[f] = val
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    @property
    def results_db_path(self) -> Path:
        """SQLite 数据库路径 (Path 对象).."""
        return Path(self.results_db)

    @property
    def checkpoint_path(self) -> Path:
        """Checkpoint 文件路径 (Path 对象).."""
        return Path(self.checkpoint_file)

    @property
    def log_path(self) -> Path:
        """日志文件路径 (Path 对象).."""
        return Path(self.log_file)


# ── 便捷函数 ──

_default_config: EngineConfig | None = None
_config_lock: threading.Lock = threading.Lock()


def load_config(path: os.PathLike[str] | None = None) -> EngineConfig:
    """加载配置,失败时回退到默认配置(线程安全).."""
    global _default_config  # noqa: PLW0603
    if path is not None:
        return EngineConfig.load(path)
    with _config_lock:
        if _default_config is None:
            try:
                _default_config = EngineConfig.load()
            except (FileNotFoundError, json.JSONDecodeError):
                _default_config = EngineConfig()
    return _default_config


def save_config(config: EngineConfig, path: os.PathLike[str] | None = None) -> None:
    """保存配置到 JSON 文件.."""
    config.save(path)


# ── 配置热重载 (Config Hot Reload) ───────────────────────────

_shutdown_requested: bool = False


def request_shutdown() -> None:
    """请求停止配置监视器线程 (全局标志)..

    同时同步 collision_engine 的关闭标志,避免双标志不同步导致线程无法关闭.
    """
    global _shutdown_requested  # noqa: PLW0603
    _shutdown_requested = True
    # 同步通知 collision_engine 的关闭标志(延迟导入避免循环依赖)
    import collision_engine as _ce  # noqa: PLC0415

    _ce._shutdown_requested = True  # noqa: SLF001


def start_config_watcher(
    config: EngineConfig,
    interval: float = 5.0,
    logger: logging.Logger | None = None,
) -> threading.Thread:
    """启动后台线程监视配置文件变更..

    每 *interval* 秒调用 config.check_reload() 检测文件变更.
    检测到变更时自动更新 EngineConfig 字段并记录日志.

    Args:
        config: EngineConfig 实例 (需有有效的 config_path).
        interval: 轮询间隔 (秒).
        logger: 可选的日志器实例 (用于记录重载事件).

    Returns:
        后台线程对象 (daemon=True).

    """
    if logger is None:
        logger = logging.getLogger("config.watcher")

    def _watcher_loop() -> None:
        """轮询检查配置文件变更的后台循环.."""
        while not _shutdown_requested:
            try:
                if config.check_reload():
                    logger.info("Configuration reloaded from file")
            except Exception:
                logger.exception("配置热重载轮询异常 (interval=%ss)", interval)
            time.sleep(interval)

    thread = threading.Thread(target=_watcher_loop, daemon=True, name="config-watcher")
    thread.start()
    return thread
