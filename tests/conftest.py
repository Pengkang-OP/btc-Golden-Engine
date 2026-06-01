"""pytest 共享 fixtures。."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def tmp_dir() -> Generator[Path, None, None]:
    """临时目录 fixture，测试完成后自动清理（Windows 兼容）。."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        yield Path(d)


@pytest.fixture
def sample_result() -> dict[str, Any]:
    """可复用的碰撞结果字典。."""
    return {
        "privkey_hex": "a" * 64,
        "wif_compressed": "KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn",
        "wif_uncompressed": "5HpHagT65TZzG1PH3CSu63k8DbpvD8s5ip4nEB3kEsreAnchuDf",
        "p2pkh_address_comp": "1BgGZ9tcN4rm9KB1mD2Tk1YkX7nL6QKuJj",
        "p2wpkh_address": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        "p2pkh_address_uncomp": "1EHNa6cGCktDjnA8eJjgPGXN8Q6GG9TyEC",
        "h160_hex": "00" * 20,
        "address_type": "P2PKH",
        "found_via": "cpu_random",
        "timestamp": "2026-01-15T10:00:00Z",
        "p2tr_address": "",
        "xonly_hex": "",
    }


@pytest.fixture
def sample_config_json(tmp_dir: Path) -> Path:
    """创建临时配置文件并返回路径。."""
    cfg = {
        "results_db": str(tmp_dir / "test_results.db"),
        "checkpoint_file": str(tmp_dir / "test_checkpoint.json"),
        "log_file": str(tmp_dir / "logs" / "test.log"),
        "log_dir": str(tmp_dir / "logs"),
        "progress_interval": 1000,
        "checkpoint_interval": 50000,
        "log_level": "DEBUG",
        "log_max_bytes": 1048576,
        "log_backup_count": 2,
    }
    config_path = tmp_dir / "collision_engine.conf"
    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return config_path
