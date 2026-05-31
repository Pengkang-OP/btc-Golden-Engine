"""测试 core.config 模块。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.config import EngineConfig, load_config, save_config

if TYPE_CHECKING:
    from conftest import *  # noqa: F401, F403


class TestEngineConfig:
    """EngineConfig 基本功能测试。"""

    def test_default_construction(self):
        """测试默认构造 — 所有字段应有合理默认值。"""
        cfg = EngineConfig()
        assert cfg.secp256k1_order > 0
        assert cfg.results_db.endswith(".db")
        assert cfg.log_file is not None
        assert cfg.log_level == "INFO"
        assert cfg.progress_interval == 5000
        assert cfg.checkpoint_interval == 60_000

    def test_path_resolution(self):
        """测试相对路径解析为绝对路径。"""
        cfg = EngineConfig()
        # 默认构造时 _base_dir = cwd，相对路径应被解析为绝对路径
        assert Path(cfg.results_db).is_absolute()
        assert Path(cfg.checkpoint_file).is_absolute()
        assert Path(cfg.log_file).is_absolute()

    def test_custom_base_dir(self):
        """测试自定义 _base_dir 时路径正确解析。"""
        base = Path("/tmp/custom_base")
        cfg = EngineConfig(_base_dir=base)
        # 相对路径应基于 _base_dir
        assert Path(cfg.results_db).parent == base
        assert Path(cfg.checkpoint_file).parent == base
        assert Path(cfg.log_file).parent == base / "logs"

    def test_absolute_path_preserved(self):
        """测试绝对路径不被 _base_dir 影响（Windows 兼容）。"""
        import os

        abs_path = os.path.abspath("/absolute/path/db.sqlite")
        base = Path("/tmp/base")
        cfg = EngineConfig(
            results_db=abs_path,
            _base_dir=base,
        )
        assert Path(cfg.results_db) == Path(abs_path)

    def test_load_save_roundtrip(self, tmp_dir: Path):
        """测试配置保存/加载往返。"""
        cfg = EngineConfig(
            results_db=str(tmp_dir / "results.db"),
            checkpoint_file=str(tmp_dir / "cp.json"),
            log_file=str(tmp_dir / "logs" / "engine.log"),
            log_dir=str(tmp_dir / "logs"),
            log_level="DEBUG",
        )
        config_path = tmp_dir / "engine.conf"
        cfg.save(config_path)
        assert config_path.exists()

        loaded = EngineConfig.load(config_path)
        assert loaded.log_level == "DEBUG"
        assert loaded.results_db == str(tmp_dir / "results.db")
        assert loaded.log_dir == str(tmp_dir / "logs")

    def test_load_missing_file_raises(self, tmp_dir: Path):
        """测试加载不存在的配置文件抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            EngineConfig.load(tmp_dir / "nonexistent.conf")

    def test_load_invalid_json_raises(self, tmp_dir: Path):
        """测试损坏的 JSON 抛出 json.JSONDecodeError。"""
        bad_file = tmp_dir / "bad.conf"
        bad_file.write_text("{invalid json}", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            EngineConfig.load(bad_file)

    def test_save_excludes_private_fields(self, tmp_dir: Path):
        """测试 save() 不包含 _base_dir 等私有字段。"""
        cfg = EngineConfig()
        config_path = tmp_dir / "engine.conf"
        cfg.save(config_path)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "_base_dir" not in data

    def test_load_filters_private_fields(self, tmp_dir: Path):
        """测试 load() 忽略文件中的私有字段。"""
        raw = {"results_db": str(tmp_dir / "r.db"), "_base_dir": "/tmp"}
        bad_file = tmp_dir / "bad.conf"
        bad_file.write_text(json.dumps(raw), encoding="utf-8")
        # 不应该因为 _base_dir 报错
        cfg = EngineConfig.load(bad_file)
        assert cfg.results_db == str(tmp_dir / "r.db")

    def test_properties_are_path_objects(self):
        """测试 results_db_path / checkpoint_path / log_path 返回 Path。"""
        cfg = EngineConfig()
        assert isinstance(cfg.results_db_path, Path)
        assert isinstance(cfg.checkpoint_path, Path)
        assert isinstance(cfg.log_path, Path)

    def test_load_config_convenience(self, tmp_dir: Path):
        """测试 load_config() 便捷函数在文件存在时正常加载。"""
        config_path = tmp_dir / "collision_engine.conf"
        config_path.write_text(
            json.dumps({"log_level": "WARNING"}),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        assert cfg.log_level == "WARNING"

    def test_save_config_convenience(self, tmp_dir: Path):
        """测试 save_config() 便捷函数。"""
        cfg = EngineConfig(log_level="ERROR")
        config_path = tmp_dir / "saved.conf"
        save_config(cfg, config_path)
        assert config_path.exists()

    def test_secp256k1_order_default(self):
        """测试 secp256k1 阶的默认值与预期相符。"""
        cfg = EngineConfig()
        expected = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        assert cfg.secp256k1_order == expected
