"""测试 collision_engine 模块 — 核心逻辑与提取的函数。.

策略：纯函数直接测试；需 mock 的用 monkeypatch；避免真实 GPU/线程密集路径。
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

# ── 确保项目根在 sys.path ────────────────────────────────
_engine_path = Path(__file__).resolve().parent.parent
if str(_engine_path) not in sys.path:
    sys.path.insert(0, str(_engine_path))

import collision_engine as ce

if TYPE_CHECKING:
    from collections.abc import Generator

# ═══════════════════════════════════════════════════════════
# 全局重置 fixture
# ═══════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_globals() -> Generator[None, None, None]:
    ce._logger = None
    ce._config = None
    ce._db = None
    ce._shutdown_requested = False
    ce._global_checked = 0
    ce._global_start_time = 0.0
    ce._global_last_checkpoint = 0.0
    ce._swappable_target = None
    ce._swappable_xonly = None
    ce._refresh_thread = None
    ce._refresh_last_time = 0.0
    ce._refresh_last_result = "N/A"
    ce._GPU_AVAILABLE = False  # 测试用默认
    return


@pytest.fixture
def mock_logger() -> mock.MagicMock:
    logger = mock.MagicMock()
    ce._logger = logger
    return logger


# ═══════════════════════════════════════════════════════════
# hash160
# ═══════════════════════════════════════════════════════════


class TestHash160:
    def test_length_and_type(self) -> None:
        result = ce.hash160(b"test data")
        assert len(result) == 20
        assert isinstance(result, bytes)

    def test_consistency(self) -> None:
        assert ce.hash160(b"hello") == ce.hash160(b"hello")

    def test_different_inputs_differ(self) -> None:
        assert ce.hash160(b"hello") != ce.hash160(b"world")


# ═══════════════════════════════════════════════════════════
# 地址编码
# ═══════════════════════════════════════════════════════════


class TestAddressEncoding:
    def test_wif_compressed_len(self) -> None:
        wif = ce.wif_encode(b"\x01" * 32, compressed=True)
        assert len(wif) == 52

    def test_wif_uncompressed_len(self) -> None:
        wif = ce.wif_encode(b"\x01" * 32, compressed=False)
        assert len(wif) == 51

    def test_p2pkh_starts_with_1(self) -> None:
        assert ce.p2pkh_address(bytes(20)).startswith("1")

    def test_p2wpkh_starts_with_bc1q(self) -> None:
        assert ce.p2wpkh_address(bytes(20)).startswith("bc1q")

    def test_p2wpkh_convertbits_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Convertbits 返回 None 时 p2wpkh_address 返回空字符串。."""
        monkeypatch.setattr(ce, "convertbits", lambda *a, **kw: None)
        assert ce.p2wpkh_address(bytes(20)) == ""

    def test_p2sh_starts_with_3(self) -> None:
        assert ce.p2sh_address(bytes(20)).startswith("3")

    def test_p2tr_starts_with_bc1p(self) -> None:
        assert ce.p2tr_address(bytes(32)).startswith("bc1p")

    def test_privkey_to_p2sh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class MockPub:
            @staticmethod
            def format(compressed: bool = True) -> bytes:
                return b"\x02" + b"\x01" * 32

        monkeypatch.setattr(
            "collision_engine.PrivateKey",
            lambda _: mock.MagicMock(public_key=MockPub()),
        )
        assert ce.privkey_to_p2sh(b"\x01" * 32).startswith("3")

    def test_p2tr_address_convertbits_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Convertbits 返回 None 时 p2tr_address 返回空字符串。."""
        monkeypatch.setattr(ce, "convertbits", lambda *a, **kw: None)
        assert ce.p2tr_address(bytes(32)) == ""

    def test_tweak_taproot_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tweak >= n 时 tweak_taproot 返回 None。."""
        monkeypatch.setattr(ce, "tagged_hash", lambda tag, data: b"\xff" * 32)
        mock_pub = mock.MagicMock()
        mock_pub.format.return_value = b"\x02" + b"\x01" * 32
        assert ce.tweak_taproot(mock_pub) is None


# ═══════════════════════════════════════════════════════════
# TaggedHash / Bech32m
# ═══════════════════════════════════════════════════════════


class TestCryptoHelpers:
    def test_tagged_hash_len(self) -> None:
        assert len(ce.tagged_hash("TapTweak", bytes(32))) == 32

    def test_tagged_hash_different_tags(self) -> None:
        a = ce.tagged_hash("A", b"x")
        b = ce.tagged_hash("B", b"x")
        assert a != b

    def test_bech32m_encode(self) -> None:
        assert ce.bech32m_encode("bc", [1, 2, 3]).startswith("bc1")


# ═══════════════════════════════════════════════════════════
# SequentialCounter
# ═══════════════════════════════════════════════════════════


class TestSequentialCounter:
    def test_default_start(self) -> None:
        assert ce.SequentialCounter().next() == 1

    def test_custom_start(self) -> None:
        assert ce.SequentialCounter(start=100).next() == 100

    def test_increment(self) -> None:
        c = ce.SequentialCounter(start=5)
        assert [c.next(), c.next(), c.next()] == [5, 6, 7]

    def test_limit(self) -> None:
        c = ce.SequentialCounter(start=1, limit=2)
        assert c.next() == 1
        assert c.next() == 2
        assert c.next() is None

    def test_checked(self) -> None:
        c = ce.SequentialCounter(start=1, limit=5)
        c.next()
        assert c.checked == 1

    def test_current(self) -> None:
        c = ce.SequentialCounter(start=42)
        c.next()
        assert c.current == 43

    def test_thread_safety(self) -> None:
        import threading

        c = ce.SequentialCounter(start=1, limit=500)
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            while True:
                v = c.next()
                if v is None:
                    break
                with lock:
                    results.append(v)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 500
        assert sorted(results) == list(range(1, 501))


# ═══════════════════════════════════════════════════════════
# CollisionResult
# ═══════════════════════════════════════════════════════════


class TestCollisionResult:
    _BASE = {
        "privkey_hex": "01" * 32,
        "wif_compressed": "Kfc",
        "wif_uncompressed": "5Hp",
        "p2pkh_address_comp": "1a",
        "p2wpkh_address": "bc1qa",
        "p2pkh_address_uncomp": "1b",
        "h160_hex": "00" * 20,
        "address_type": "P2PKH",
        "found_via": "compressed",
    }

    def test_default_timestamp(self) -> None:
        r = ce.CollisionResult(**self._BASE)
        assert r.timestamp != ""

    def test_provided_timestamp(self) -> None:
        r = ce.CollisionResult(**self._BASE, timestamp="2000-01-01T00:00:00Z")
        assert r.timestamp == "2000-01-01T00:00:00Z"

    def test_optional_fields_default(self) -> None:
        r = ce.CollisionResult(**self._BASE)
        assert r.p2tr_address == ""
        assert r.xonly_hex == ""
        assert r.p2sh_address == ""


# ═══════════════════════════════════════════════════════════
# _build_arg_parser
# ═══════════════════════════════════════════════════════════


class TestBuildArgParser:
    def test_has_required_args(self) -> None:
        parser = ce._build_arg_parser()
        for flag in (
            "--mode",
            "--start",
            "--count",
            "--threads",
            "--gpu",
            "--p2tr",
            "--health",
            "--list-gpu",
        ):
            assert flag in parser._option_string_actions, f"缺失 {flag}"

    def test_mode_default(self) -> None:
        args = ce._build_arg_parser().parse_args([])
        assert args.mode == "sequential"

    def test_gpu_parse(self) -> None:
        args = ce._build_arg_parser().parse_args(
            ["--gpu", "--gpu-mode", "sequential", "--p2tr"],
        )
        assert args.gpu is True
        assert args.gpu_mode == "sequential"
        assert args.p2tr is True


# ═══════════════════════════════════════════════════════════
# _handle_signal
# ═══════════════════════════════════════════════════════════


class TestHandleSignal:
    def test_sets_shutdown_flag(self) -> None:
        ce._handle_signal(signal.SIGTERM)
        assert ce._shutdown_requested is True

    def test_sigint_raises(self) -> None:
        with pytest.raises(KeyboardInterrupt):
            ce._handle_signal(signal.SIGINT)

    def test_logs_warning(self, mock_logger: mock.MagicMock) -> None:
        ce._handle_signal(signal.SIGTERM)
        mock_logger.warning.assert_called_once()


# ═══════════════════════════════════════════════════════════
# Checkpoint I/O
# ═══════════════════════════════════════════════════════════


class TestCheckpoint:
    def test_no_file_returns_empty(self) -> None:
        assert ce.load_checkpoint() == {}

    def test_save_and_load(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ce, "CHECKPOINT_FILE", tmp_path / "ckpt.json")
        ce.save_checkpoint({"mode": "sequential", "next_key": 100})
        loaded = ce.load_checkpoint()
        assert loaded["mode"] == "sequential"
        assert loaded["next_key"] == 100

    def test_corrupted_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ce, "CHECKPOINT_FILE", tmp_path / "ckpt.json")
        ce.CHECKPOINT_FILE.write_text("bad-json")
        assert ce.load_checkpoint() == {}


# ═══════════════════════════════════════════════════════════
# save_result — 数据库写入失败 / JSON 文件损坏
# ═══════════════════════════════════════════════════════════


class TestSaveResult:
    """save_result 的数据库/文件错误路径。."""

    def test_db_error_falls_back_to_json(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """数据库写入失败应回退到 JSON 并记录警告。."""
        from core.database import DatabaseError  # type: ignore[attr-defined]

        db = mock.MagicMock()
        db.save_result.side_effect = DatabaseError("db full")
        ce._db = db
        monkeypatch.setattr(ce, "RESULTS_FILE", mock.MagicMock())
        ce.RESULTS_FILE.exists.return_value = False  # type: ignore[attr-defined]  # 新文件

        result = ce.CollisionResult(
            privkey_hex="01" * 32,
            wif_compressed="Kfc",
            wif_uncompressed="5Hp",
            p2pkh_address_comp="1a",
            p2wpkh_address="bc1qa",
            p2pkh_address_uncomp="1b",
            h160_hex="00" * 20,
            address_type="P2PKH",
            found_via="compressed",
        )
        ce.save_result(result)
        mock_logger.error.assert_called_once()

    def test_corrupted_json_file(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """已损坏的 JSON 结果文件应被覆盖。."""
        tmp_json = mock.MagicMock()
        tmp_json.exists.return_value = True
        tmp_json.read_text.return_value = "not valid json{{"
        monkeypatch.setattr(ce, "RESULTS_FILE", tmp_json)

        result = ce.CollisionResult(
            privkey_hex="02" * 32,
            wif_compressed="Kfc",
            wif_uncompressed="5Hp",
            p2pkh_address_comp="1a",
            p2wpkh_address="bc1qa",
            p2pkh_address_uncomp="1b",
            h160_hex="11" * 20,
            address_type="P2SH",
            found_via="compressed",
        )
        ce.save_result(result)
        # write_text 应被调用（覆盖损坏文件）
        tmp_json.write_text.assert_called_once()


# ═══════════════════════════════════════════════════════════
# _display_banner
# ═══════════════════════════════════════════════════════════


class TestDisplayBanner:
    def test_logs_multiple_lines(self, mock_logger: mock.MagicMock) -> None:
        target = mock.MagicMock()
        target.__len__.return_value = 1000
        args = mock.MagicMock(mode="sequential", threads=4, count=0, p2tr=False)
        ce._display_banner(target, args, xonly_target=None)
        assert mock_logger.info.call_count >= 4

    def test_p2tr_shown(self, mock_logger: mock.MagicMock) -> None:
        target = mock.MagicMock()
        target.__len__.return_value = 1000
        args = mock.MagicMock(mode="random", threads=8, count=10000, p2tr=True)
        ce._display_banner(target, args, xonly_target=mock.MagicMock())
        log_str = str(mock_logger.info.call_args_list)
        assert "Y" in log_str


# ═══════════════════════════════════════════════════════════
# _print_final_report
# ═══════════════════════════════════════════════════════════


class TestPrintFinalReport:
    def test_logs_report(
        self,
        mock_logger: mock.MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ce._global_checked = 100000
        ce._global_start_time = time.time() - 10
        monkeypatch.setattr(ce, "RESULTS_FILE", tmp_path / "r.json")
        (tmp_path / "r.json").write_text(json.dumps([{"h": "abc"}]))
        ce._print_final_report()
        mock_logger.info.assert_called_once()

    def test_no_file_no_error(
        self,
        mock_logger: mock.MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ce._global_checked = 0
        ce._global_start_time = time.time() - 1
        monkeypatch.setattr(ce, "RESULTS_FILE", tmp_path / "nope.json")
        ce._print_final_report()  # 不应抛出异常


# ═══════════════════════════════════════════════════════════
# _cleanup
# ═══════════════════════════════════════════════════════════


class TestCleanup:
    def test_closes_target_and_xonly(self, mock_logger: mock.MagicMock) -> None:
        t = mock.MagicMock()
        x = mock.MagicMock()
        ce._cleanup(t, x)
        t.close.assert_called_once()
        x.close.assert_called_once()

    def test_none_xonly(self, mock_logger: mock.MagicMock) -> None:
        t = mock.MagicMock()
        ce._cleanup(t, None)
        t.close.assert_called_once()

    def test_closes_db(self, mock_logger: mock.MagicMock) -> None:
        db = mock.MagicMock()
        ce._db = db
        ce._cleanup(mock.MagicMock(), None)
        db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════
# check_single_key — 核心碰撞逻辑
# ═══════════════════════════════════════════════════════════


class TestCheckSingleKey:
    """完整的碰撞检查路径：压缩命中 / 非压缩命中 / 无命中 / 异常。."""

    def _setup_mocks(self, monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
        pub = mock.MagicMock()
        pub.format.side_effect = lambda compressed=True: (
            b"\x02" + b"\x01" * 32 if compressed else b"\x04" + b"\x01" * 64
        )
        monkeypatch.setattr(
            "collision_engine.PrivateKey",
            lambda _: mock.MagicMock(public_key=pub),
        )
        return pub

    def test_compressed_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        self._setup_mocks(monkeypatch)
        target = mock.MagicMock()
        comp_h160 = ce.hash160(b"\x02" + b"\x01" * 32)
        target.__contains__.side_effect = lambda h: h == comp_h160
        result = ce.check_single_key(1, target)
        assert result is not None
        assert result.found_via == "compressed"

    def test_uncompressed_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        self._setup_mocks(monkeypatch)
        target = mock.MagicMock()
        uncomp_h160 = ce.hash160(b"\x04" + b"\x01" * 64)
        target.__contains__.side_effect = lambda h: h == uncomp_h160
        result = ce.check_single_key(1, target)
        assert result is not None
        assert result.found_via == "uncompressed"

    def test_no_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        self._setup_mocks(monkeypatch)
        target = mock.MagicMock()
        target.__contains__.return_value = False
        assert ce.check_single_key(1, target) is None

    def test_exception_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        monkeypatch.setattr(
            "collision_engine.PrivateKey",
            mock.MagicMock(side_effect=ValueError("bad")),
        )
        assert ce.check_single_key(1, mock.MagicMock()) is None
        mock_logger.warning.assert_called_once()

    def test_p2tr_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        """P2TR (Taproot) 碰撞命中路径。."""
        pub = mock.MagicMock()
        pub.format.return_value = b"\x02" + b"\x01" * 32
        monkeypatch.setattr(
            "collision_engine.PrivateKey",
            lambda _: mock.MagicMock(public_key=pub),
        )
        monkeypatch.setattr(
            ce,
            "tweak_taproot",
            lambda pubkey: b"\x02" + b"\x02" * 31,  # 32B x-only
        )
        xonly_target = mock.MagicMock()
        xonly_target.__contains__.return_value = True
        target = mock.MagicMock()
        target.__contains__.return_value = False
        monkeypatch.setattr(ce, "p2tr_address", lambda xonly: "bc1p" + xonly.hex()[:10])
        result = ce.check_single_key(1, target, xonly_target)
        assert result is not None
        assert result.address_type == "P2TR (Taproot)"
        assert result.found_via == "tweaked"

    def test_p2tr_miss(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        """P2TR 目标未命中时返回 None。."""
        pub = mock.MagicMock()
        pub.format.return_value = b"\x02" + b"\x01" * 32
        monkeypatch.setattr(
            "collision_engine.PrivateKey",
            lambda _: mock.MagicMock(public_key=pub),
        )
        monkeypatch.setattr(
            ce,
            "tweak_taproot",
            lambda pubkey: None,  # 模拟 tweak 失败
        )
        xonly_target = mock.MagicMock()
        result = ce.check_single_key(
            1,
            mock.MagicMock(__contains__=lambda h: False),
            xonly_target,
        )
        assert result is None


# ═══════════════════════════════════════════════════════════
# check_single_key_chain — 链式加速路径
# ═══════════════════════════════════════════════════════════


class TestCheckSingleKeyChain:
    def test_compressed_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        pub = mock.MagicMock()
        pub.format.side_effect = lambda compressed=True: (
            b"\x02" + b"\x01" * 32 if compressed else b"\x04" + b"\x01" * 64
        )
        monkeypatch.setattr(
            "collision_engine.PrivateKey",
            lambda _: mock.MagicMock(public_key=pub),
        )
        target = mock.MagicMock()
        comp_h160 = ce.hash160(b"\x02" + b"\x01" * 32)
        target.__contains__.side_effect = lambda h: h == comp_h160
        result, pubkey = ce.check_single_key_chain(1, target, b"\x00" * 32)
        assert result is not None
        assert result.found_via == "compressed"
        assert pubkey is not None

    def test_no_hit_returns_pubkey(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        pub = mock.MagicMock()
        pub.format.return_value = b"\x02" + b"\x01" * 32
        monkeypatch.setattr(
            "collision_engine.PrivateKey",
            lambda _: mock.MagicMock(public_key=pub),
        )
        target = mock.MagicMock()
        target.__contains__.return_value = False
        result, pubkey = ce.check_single_key_chain(1, target, b"\x01" * 32)
        assert result is None
        assert pubkey is not None


# ═══════════════════════════════════════════════════════════
# main() — 关键路径（--health, --list-gpu, --gpu）
# ═══════════════════════════════════════════════════════════


class TestMain:
    def _mock_init_core(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock _init_core 同时设置 _logger 和 _config（main() 在 _init_core 后立即用）。."""
        ce._logger = mock.MagicMock()
        cfg = mock.MagicMock()
        cfg.enable_utxo_auto_refresh = False
        cfg.utxo_refresh_interval = 0
        ce._config = cfg
        monkeypatch.setattr(ce, "_init_core", mock.MagicMock())

    def test_health_early_return(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["prog", "--health"])
        monkeypatch.setattr(ce, "_health_check", mock.MagicMock())
        self._mock_init_core(monkeypatch)
        ce.main()
        ce._health_check.assert_called_once()  # type: ignore[attr-defined]

    def test_list_gpu_early_return(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["prog", "--list-gpu"])
        self._mock_init_core(monkeypatch)
        monkeypatch.setattr(ce, "_GPU_AVAILABLE", False)
        ce.main()

    def test_gpu_mode_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["prog", "--gpu"])
        monkeypatch.setattr(ce, "_GPU_AVAILABLE", False)
        self._mock_init_core(monkeypatch)
        target = mock.MagicMock()
        monkeypatch.setattr(ce, "_load_targets", lambda _: (target, None))
        with pytest.raises(SystemExit):
            ce.main()

    def test_cpu_mode_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["prog"])
        self._mock_init_core(monkeypatch)
        target = mock.MagicMock()
        monkeypatch.setattr(ce, "_load_targets", lambda _: (target, None))
        run = mock.MagicMock()
        monkeypatch.setattr(ce, "_run_cpu_mode", run)
        report = mock.MagicMock()
        monkeypatch.setattr(ce, "_print_final_report", report)
        cleanup = mock.MagicMock()
        monkeypatch.setattr(ce, "_cleanup", cleanup)
        ce.main()
        run.assert_called_once()
        report.assert_called_once()
        cleanup.assert_called_once()

    def test_gpu_mode_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_logger: mock.MagicMock,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["prog", "--gpu"])
        monkeypatch.setattr(ce, "_GPU_AVAILABLE", True)
        cfg = mock.MagicMock()
        cfg.enable_utxo_auto_refresh = False
        cfg.utxo_refresh_interval = 0
        ce._config = cfg
        monkeypatch.setattr(ce, "_init_core", mock.MagicMock())
        target = mock.MagicMock()
        monkeypatch.setattr(ce, "_load_targets", lambda _: (target, None))
        run_gpu = mock.MagicMock()
        monkeypatch.setattr(ce, "_run_gpu_mode", run_gpu)
        cleanup = mock.MagicMock()
        monkeypatch.setattr(ce, "_cleanup", cleanup)
        ce.main()
        run_gpu.assert_called_once()
        cleanup.assert_called_once()


# ═══════════════════════════════════════════════════════════
# _start_config_watcher — 配置热重载
# ═══════════════════════════════════════════════════════════


class TestStartConfigWatcher:
    """测试配置热重载后台线程的启动和行为。."""

    def test_starts_daemon_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """验证启动 daemon 线程并调用 start()."""
        config = mock.MagicMock()
        thread_mock = mock.MagicMock()

        def _thread_factory(**kw: object) -> mock.MagicMock:
            # 将 kwargs 传播到 thread_mock（如 daemon=True）
            for k, v in kw.items():
                setattr(thread_mock, k, v)
            return thread_mock

        monkeypatch.setattr(threading, "Thread", _thread_factory)
        ce._start_config_watcher(config, interval=5.0)
        thread_mock.start.assert_called_once()
        assert thread_mock.daemon is True

    def test_watch_loop_reloads_on_check_reload(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """验证 check_reload() 返回 True 时记录日志。."""
        config = mock.MagicMock()
        config.check_reload.return_value = True
        target_func: list[Any] = []

        monkeypatch.setattr(
            threading,
            "Thread",
            lambda target=None, **kw: target_func.append(target) or mock.MagicMock(),  # type: ignore[func-returns-value]
        )
        ce._start_config_watcher(config, interval=0.01, logger=mock_logger)
        fn = target_func[0]

        # 执行一次循环后退出
        ce._shutdown_requested = False
        monkeypatch.setattr(
            time,
            "sleep",
            lambda _: setattr(ce, "_shutdown_requested", True),
        )
        fn()
        mock_logger.info.assert_called_with("配置文件已变更并自动重载")

    def test_watch_loop_swallows_exceptions(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """验证 check_reload() 抛异常时被静默吞噬。."""
        config = mock.MagicMock()
        config.check_reload.side_effect = RuntimeError("unexpected")
        target_func: list[Any] = []

        monkeypatch.setattr(
            threading,
            "Thread",
            lambda target=None, **kw: target_func.append(target) or mock.MagicMock(),  # type: ignore[func-returns-value]
        )
        ce._start_config_watcher(config, interval=0.01, logger=mock_logger)
        fn = target_func[0]

        ce._shutdown_requested = False
        monkeypatch.setattr(
            time,
            "sleep",
            lambda _: setattr(ce, "_shutdown_requested", True),
        )
        fn()  # 不应抛出异常


# ═══════════════════════════════════════════════════════════
# _find_bitcoin_cli — bitcoin-cli 路径查找
# ═══════════════════════════════════════════════════════════


class TestFindBitcoinCli:
    """测试 bitcoin-cli 可执行文件查找逻辑。."""

    def test_uses_configured_path(self, tmp_path: Path) -> None:
        """配置路径存在时返回该路径。."""
        config = mock.MagicMock()
        cli_path = tmp_path / "custom-bitcoin-cli.exe"
        cli_path.write_text("")
        config.bitcoin_cli_path = str(cli_path)
        result = ce._find_bitcoin_cli(config)
        assert result == str(cli_path.resolve())

    def test_configured_path_not_found_logs_warning(
        self,
        mock_logger: mock.MagicMock,
        tmp_path: Path,
    ) -> None:
        """配置路径不存在时记录警告并回退到自动检测。."""
        config = mock.MagicMock()
        config.bitcoin_cli_path = str(tmp_path / "nonexistent.exe")
        ce._find_bitcoin_cli(config)
        mock_logger.warning.assert_called_once()

    def test_auto_detects_in_cwd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """CWD 中有 bitcoin-cli.exe 时自动发现。."""
        config = mock.MagicMock()
        config.bitcoin_cli_path = None
        cli_path = tmp_path / "bitcoin-cli.exe"
        cli_path.write_text("")
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
        result = ce._find_bitcoin_cli(config)
        assert result is not None

    def test_auto_detects_in_daemon_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """daemon/bitcoin-cli.exe 存在时自动发现。."""
        config = mock.MagicMock()
        config.bitcoin_cli_path = None
        daemon_dir = tmp_path / "daemon"
        daemon_dir.mkdir()
        cli_path = daemon_dir / "bitcoin-cli.exe"
        cli_path.write_text("")
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
        result = ce._find_bitcoin_cli(config)
        assert result is not None
        assert "daemon" in result

    def test_no_candidates_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """无任何候选文件时返回 None。."""
        config = mock.MagicMock()
        config.bitcoin_cli_path = None
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
        result = ce._find_bitcoin_cli(config)
        assert result is None


# ═══════════════════════════════════════════════════════════
# _find_bitcoin_datadir — Bitcoin 数据目录查找
# ═══════════════════════════════════════════════════════════


class TestFindBitcoinDatadir:
    """测试 Bitcoin 数据目录查找逻辑。."""

    def test_uses_configured_path(self, tmp_path: Path) -> None:
        """配置路径存在时返回该路径。."""
        config = mock.MagicMock()
        config.bitcoin_datadir = str(tmp_path)
        result = ce._find_bitcoin_datadir(config)
        assert result == str(tmp_path.resolve())

    def test_configured_path_not_found_falls_back_to_cwd(self, tmp_path: Path) -> None:
        """配置路径不存在时回退到 CWD。."""
        config = mock.MagicMock()
        config.bitcoin_datadir = str(tmp_path / "nonexistent")
        result = ce._find_bitcoin_datadir(config)
        assert result is not None  # CWD 总是存在

    def test_no_config_returns_cwd(self) -> None:
        """未配置时返回 CWD。."""
        config = mock.MagicMock()
        config.bitcoin_datadir = None
        result = ce._find_bitcoin_datadir(config)
        assert result is not None
        assert isinstance(result, str)

    def test_cwd_not_dir_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CWD 不是目录时返回 None。."""
        monkeypatch.setattr(Path, "cwd", lambda: Path("/nonexistent/file.txt"))
        monkeypatch.setattr(Path, "is_dir", lambda _: False)
        config = mock.MagicMock()
        config.bitcoin_datadir = None
        result = ce._find_bitcoin_datadir(config)
        assert result is None


# ═══════════════════════════════════════════════════════════
# _run_bitcoin_cli_dumptxoutset — bitcoin-cli 调用
# ═══════════════════════════════════════════════════════════


class TestRunBitcoinCliDumptxoutset:
    """测试运行 bitcoin-cli dumptxoutset 子进程。."""

    def test_success_returns_true(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: mock.MagicMock(returncode=0),
        )
        assert (
            ce._run_bitcoin_cli_dumptxoutset(
                "bitcoin-cli",
                "/datadir",
                "/tmp/snap.dat",
                mock_logger,
            )
            is True
        )

    def test_failure_returns_false(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: mock.MagicMock(returncode=1, stderr="error"),
        )
        assert (
            ce._run_bitcoin_cli_dumptxoutset(
                "bitcoin-cli",
                "/datadir",
                "/tmp/snap.dat",
                mock_logger,
            )
            is False
        )

    def test_file_not_found_returns_false(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            mock.MagicMock(side_effect=FileNotFoundError("not found")),
        )
        assert (
            ce._run_bitcoin_cli_dumptxoutset(
                "bitcoin-cli",
                "/datadir",
                "/tmp/snap.dat",
                mock_logger,
            )
            is False
        )

    def test_timeout_returns_false(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            mock.MagicMock(side_effect=subprocess.TimeoutExpired("cmd", 7200)),
        )
        assert (
            ce._run_bitcoin_cli_dumptxoutset(
                "bitcoin-cli",
                "/datadir",
                "/tmp/snap.dat",
                mock_logger,
            )
            is False
        )

    def test_generic_exception_returns_false(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            mock.MagicMock(side_effect=PermissionError("denied")),
        )
        assert (
            ce._run_bitcoin_cli_dumptxoutset(
                "bitcoin-cli",
                "/datadir",
                "/tmp/snap.dat",
                mock_logger,
            )
            is False
        )


# ═══════════════════════════════════════════════════════════
# _start_utxo_refresher — UTXO 自动刷新线程
# ═══════════════════════════════════════════════════════════


class TestStartUtxoRefresher:
    """测试 UTXO 自动刷新后台线程。."""

    def test_disabled_returns_none(self) -> None:
        """自动刷新未启用时返回 None。."""
        config = mock.MagicMock()
        config.enable_utxo_auto_refresh = False
        result = ce._start_utxo_refresher(config, mock.MagicMock(), mock.MagicMock())
        assert result is None

    def test_enabled_starts_daemon_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """启用后启动 daemon 线程。."""
        config = mock.MagicMock()
        config.enable_utxo_auto_refresh = True
        config.utxo_refresh_interval = 300
        config.utxo_snapshot_path = "/tmp/snap.dat"
        thread_mock = mock.MagicMock()

        def _thread_factory(**kw: object) -> mock.MagicMock:
            for k, v in kw.items():
                setattr(thread_mock, k, v)
            return thread_mock

        monkeypatch.setattr(threading, "Thread", _thread_factory)
        result = ce._start_utxo_refresher(config, mock.MagicMock(), mock.MagicMock())
        assert result is thread_mock
        thread_mock.start.assert_called_once()
        assert thread_mock.daemon is True

    def test_enabled_stores_thread_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """启动后设置 _refresh_thread 全局变量。."""
        config = mock.MagicMock()
        config.enable_utxo_auto_refresh = True
        config.utxo_refresh_interval = 120
        config.utxo_snapshot_path = "/tmp/snap.dat"
        monkeypatch.setattr(threading, "Thread", lambda **kw: mock.MagicMock())
        ce._start_utxo_refresher(config, mock.MagicMock(), mock.MagicMock())
        assert ce._refresh_thread is not None


# ═══════════════════════════════════════════════════════════
# _do_utxo_refresh — UTXO 刷新全流程
# ═══════════════════════════════════════════════════════════


class TestDoUtxoRefresh:
    """测试 UTXO 刷新各出错路径和完整流程。."""

    def _setup_base_mocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """设置 _config 和 _swappable_target 基本 mock。."""
        ce._config = mock.MagicMock()
        ce._config.utxo_snapshot_path = "/tmp/snap.dat"
        ce._config.utxo_hash160_bin = "/tmp/h160.bin"
        ce._config.utxo_hash160_idx = "/tmp/h160.idx"
        ce._swappable_target = mock.MagicMock()
        ce._swappable_xonly = None

    def test_no_config_returns_false(self, mock_logger: mock.MagicMock) -> None:
        ce._config = None
        ce._swappable_target = mock.MagicMock()
        assert ce._do_utxo_refresh(mock_logger) is False

    def test_no_swappable_target_returns_false(
        self,
        mock_logger: mock.MagicMock,
    ) -> None:
        ce._config = mock.MagicMock()
        ce._swappable_target = None
        assert ce._do_utxo_refresh(mock_logger) is False

    def test_bitcoin_cli_not_found(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._setup_base_mocks(monkeypatch)
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: None)
        assert ce._do_utxo_refresh(mock_logger) is False
        assert "bitcoin-cli 未找到" in ce._refresh_last_result

    def test_datadir_not_found(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._setup_base_mocks(monkeypatch)
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: "bitcoin-cli")
        monkeypatch.setattr(ce, "_find_bitcoin_datadir", lambda _: None)
        assert ce._do_utxo_refresh(mock_logger) is False
        assert "数据目录未找到" in ce._refresh_last_result

    def test_dumptxoutset_fails(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._setup_base_mocks(monkeypatch)
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: "bitcoin-cli")
        monkeypatch.setattr(ce, "_find_bitcoin_datadir", lambda _: "/datadir")
        monkeypatch.setattr(ce, "_run_bitcoin_cli_dumptxoutset", lambda *a, **kw: False)
        assert ce._do_utxo_refresh(mock_logger) is False
        assert "dumptxoutset 失败" in ce._refresh_last_result

    def test_snapshot_file_missing(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._setup_base_mocks(monkeypatch)
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: "bitcoin-cli")
        monkeypatch.setattr(ce, "_find_bitcoin_datadir", lambda _: "/datadir")
        monkeypatch.setattr(ce, "_run_bitcoin_cli_dumptxoutset", lambda *a, **kw: True)
        monkeypatch.setattr(Path, "is_file", lambda _: False)
        assert ce._do_utxo_refresh(mock_logger) is False
        assert "快照文件未生成" in ce._refresh_last_result

    def test_extraction_failure(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """extract_snapshot 异常时返回 False。."""
        self._setup_base_mocks(monkeypatch)
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: "bitcoin-cli")
        monkeypatch.setattr(ce, "_find_bitcoin_datadir", lambda _: "/datadir")
        monkeypatch.setattr(ce, "_run_bitcoin_cli_dumptxoutset", lambda *a, **kw: True)
        monkeypatch.setattr(Path, "is_file", lambda _: True)
        mock_extract = mock.MagicMock()
        mock_extract.extract_snapshot = mock.MagicMock(
            side_effect=ValueError("parse error"),
        )
        monkeypatch.setitem(sys.modules, "extract_utxo_hash160", mock_extract)
        assert ce._do_utxo_refresh(mock_logger) is False
        assert "提取失败" in ce._refresh_last_result

    def test_new_target_load_failure(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hash160Set.load 异常时返回 False。."""
        self._setup_base_mocks(monkeypatch)
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: "bitcoin-cli")
        monkeypatch.setattr(ce, "_find_bitcoin_datadir", lambda _: "/datadir")
        monkeypatch.setattr(ce, "_run_bitcoin_cli_dumptxoutset", lambda *a, **kw: True)
        monkeypatch.setattr(Path, "is_file", lambda _: True)
        mock_extract = mock.MagicMock()
        mock_extract.extract_snapshot = mock.MagicMock(
            return_value={"TOTAL_HASH160": 500},
        )
        monkeypatch.setitem(sys.modules, "extract_utxo_hash160", mock_extract)
        mock_hash160_cls = mock.MagicMock()
        mock_hash160_cls.return_value.load.side_effect = Exception("load failed")
        monkeypatch.setattr(ce, "Hash160Set", mock_hash160_cls)
        assert ce._do_utxo_refresh(mock_logger) is False
        assert "新目标集加载失败" in ce._refresh_last_result

    def test_p2tr_xonly_refresh_load_failure(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P2TR xonly load 失败时仍返回 True（XOnlySet() 实例化后 load 才抛异常）。."""
        self._setup_base_mocks(monkeypatch)
        ce._swappable_xonly = mock.MagicMock()
        ce._config.enable_utxo_auto_refresh = True
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: "bitcoin-cli")
        monkeypatch.setattr(ce, "_find_bitcoin_datadir", lambda _: "/datadir")
        monkeypatch.setattr(ce, "_run_bitcoin_cli_dumptxoutset", lambda *a, **kw: True)
        monkeypatch.setattr(Path, "is_file", lambda _: True)
        mock_extract = mock.MagicMock()
        mock_extract.extract_snapshot = mock.MagicMock(
            return_value={"TOTAL_HASH160": 500},
        )
        monkeypatch.setitem(sys.modules, "extract_utxo_hash160", mock_extract)
        monkeypatch.setattr(ce, "Hash160Set", mock.MagicMock())
        mock_xonly_cls = mock.MagicMock()
        mock_xonly_cls.return_value.load.side_effect = Exception("xonly fail")
        monkeypatch.setattr(ce, "XOnlySet", mock_xonly_cls)
        result = ce._do_utxo_refresh(mock_logger)
        assert result is True
        ce._swappable_target.swap.assert_called_once()
        # new_xonly 赋值在 load() 之前，所以 swap 仍被调用
        ce._swappable_xonly.swap.assert_called_once()
        assert "成功" in ce._refresh_last_result

    def test_full_success_path(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """完整的成功路径：提取、加载、双 swap 均成功。."""
        self._setup_base_mocks(monkeypatch)
        ce._swappable_xonly = mock.MagicMock()
        ce._config.enable_utxo_auto_refresh = True
        monkeypatch.setattr(ce, "_find_bitcoin_cli", lambda _: "bitcoin-cli")
        monkeypatch.setattr(ce, "_find_bitcoin_datadir", lambda _: "/datadir")
        monkeypatch.setattr(ce, "_run_bitcoin_cli_dumptxoutset", lambda *a, **kw: True)
        monkeypatch.setattr(Path, "is_file", lambda _: True)
        mock_extract = mock.MagicMock()
        mock_extract.extract_snapshot = mock.MagicMock(
            return_value={"TOTAL_HASH160": 500},
        )
        monkeypatch.setitem(sys.modules, "extract_utxo_hash160", mock_extract)
        monkeypatch.setattr(ce, "Hash160Set", mock.MagicMock())
        monkeypatch.setattr(ce, "XOnlySet", mock.MagicMock())
        result = ce._do_utxo_refresh(mock_logger)
        assert result is True
        ce._swappable_target.swap.assert_called_once()
        ce._swappable_xonly.swap.assert_called_once()
        assert ce._refresh_last_time > 0
        assert "成功" in ce._refresh_last_result


# ═══════════════════════════════════════════════════════════
# worker_sequential
# ═══════════════════════════════════════════════════════════


class TestWorkerSequential:
    """worker_sequential: shutdown / stride 等边界。."""

    def test_shutdown_with_stride(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """设置 _shutdown_requested 后停止, 带 stride_bytes。."""
        ce._shutdown_requested = True
        counter = mock.MagicMock()
        counter.next.return_value = 1
        target = mock.MagicMock()
        monkeypatch.setattr(
            ce,
            "check_single_key_chain",
            mock.MagicMock(return_value=(None, None)),
        )
        result = ce.worker_sequential(counter, target, 0, b"\x00" * 32)
        assert result >= 0

    def test_shutdown_without_stride(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shutdown 时无 stride_bytes, 走 check_single_key 路径。."""
        ce._shutdown_requested = True
        counter = mock.MagicMock()
        counter.next.return_value = 1
        target = mock.MagicMock()
        monkeypatch.setattr(ce, "check_single_key", mock.MagicMock(return_value=None))
        result = ce.worker_sequential(counter, target, 0)
        assert result >= 0

    def test_counter_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """counter.next() 返回 None 即退出。."""
        counter = mock.MagicMock()
        counter.next.return_value = None
        result = ce.worker_sequential(counter, mock.MagicMock(), 0)
        assert result == 0


# ═══════════════════════════════════════════════════════════
# worker_random
# ═══════════════════════════════════════════════════════════


class TestWorkerRandom:
    """worker_random: shutdown / count limit 边界。."""

    def test_shutdown_requested(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """收到 shutdown 请求后退出。."""
        ce._shutdown_requested = True
        monkeypatch.setattr(ce, "check_single_key", mock.MagicMock(return_value=None))
        result = ce.worker_random(mock.MagicMock(), 0)
        assert result >= 0

    def test_count_limit_reached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_limit 达到时退出。."""
        ce._global_checked = 1000
        ce._shutdown_requested = False
        monkeypatch.setattr(ce, "_counter_lock", mock.MagicMock())
        monkeypatch.setattr(ce, "check_single_key", mock.MagicMock(return_value=None))
        # 设置 _global_checked >= check_limit, 在 1000 次迭代后触发
        # 但我们可以用 mock counter_lock 来控制
        result = ce.worker_random(mock.MagicMock(), 0, check_limit=500)
        assert result >= 0


# ═══════════════════════════════════════════════════════════
# _run_gpu_mode — 错误路径
# ═══════════════════════════════════════════════════════════


class TestRunGpuMode:
    """_run_gpu_mode 的输入验证和错误路径。."""

    def test_bad_gpu_devices_exits(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--gpu-devices 格式无效时 sys.exit(1)。."""
        args = mock.MagicMock(
            gpu_devices="not-a-number",
            gpu_mode="random",
            gpu_start="0x1",
            gpu_tdr_safe=False,
            gpu_batch_size=65536,
            gpu_max_kernel_time=1.5,
            count=0,
        )
        ce._GPU_AVAILABLE = True
        with pytest.raises(SystemExit):
            ce._run_gpu_mode(mock.MagicMock(), args)

    def test_gpu_sequential_checkpoint_restore(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GPU 顺序模式从 checkpoint 恢复 next_key。."""
        args = mock.MagicMock(
            gpu_devices="",
            gpu_mode="sequential",
            gpu_start="0x100",
            gpu_tdr_safe=False,
            gpu_batch_size=65536,
            gpu_max_kernel_time=1.5,
            count=0,
        )
        ce._GPU_AVAILABLE = True
        monkeypatch.setattr(
            ce,
            "load_checkpoint",
            lambda: {"mode": "gpu_sequential", "next_key": 999, "checked": 5000},
        )
        monkeypatch.setattr(ce, "GPUBatchScheduler", mock.MagicMock())
        monkeypatch.setattr(ce, "DispatcherConfig", mock.MagicMock())
        scheduler_mock = mock.MagicMock()
        scheduler_mock.initialize.return_value = True
        monkeypatch.setattr(ce, "GPUBatchScheduler", lambda *a, **kw: scheduler_mock)
        ce._run_gpu_mode(mock.MagicMock(), args)
        scheduler_mock.run.assert_called_once()

    def test_gpu_keyboard_interrupt_checkpoint(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GPU 顺序模式收到 KeyboardInterrupt 应保存 checkpoint。."""
        args = mock.MagicMock(
            gpu_devices="",
            gpu_mode="sequential",
            gpu_start="0x100",
            gpu_tdr_safe=False,
            gpu_batch_size=65536,
            gpu_max_kernel_time=1.5,
            count=0,
        )
        ce._GPU_AVAILABLE = True
        save_ckpt = mock.MagicMock()
        monkeypatch.setattr(ce, "save_checkpoint", save_ckpt)
        scheduler_mock = mock.MagicMock()
        pipeline_mock = mock.MagicMock()
        pipeline_mock.sequential_start = 123456
        scheduler_mock._pipelines = [pipeline_mock, pipeline_mock]
        scheduler_mock._total_checked = 100
        scheduler_mock.run.side_effect = KeyboardInterrupt()
        monkeypatch.setattr(ce, "GPUBatchScheduler", lambda *a, **kw: scheduler_mock)
        monkeypatch.setattr(ce, "DispatcherConfig", mock.MagicMock())
        scheduler_mock.initialize.return_value = True

        ce._run_gpu_mode(mock.MagicMock(), args)
        save_ckpt.assert_called_once()
        # next_key 应为最小 sequential_start
        args = save_ckpt.call_args[0][0]
        assert args["mode"] == "gpu_sequential"
        assert args["next_key"] == 123456


# ═══════════════════════════════════════════════════════════
# _run_cpu_mode — 模式路径
# ═══════════════════════════════════════════════════════════


class TestRunCpuMode:
    """_run_cpu_mode 的顺序和随机模式路径。."""

    def test_sequential_basic(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """顺序模式基本路径（无 checkpoint 恢复）。."""
        args = mock.MagicMock(
            mode="sequential",
            start="0x1",
            count=0,
            threads=1,
        )
        monkeypatch.setattr(ce, "load_checkpoint", dict)
        monkeypatch.setattr(ce, "worker_sequential", mock.MagicMock(return_value=0))
        ce._run_cpu_mode(mock.MagicMock(), args, None)
        ce.worker_sequential.assert_called_once()  # type: ignore[attr-defined]

    def test_random_basic(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """随机模式基本路径。."""
        args = mock.MagicMock(
            mode="random",
            start="0x1",
            count=100,
            threads=1,
        )
        monkeypatch.setattr(ce, "worker_random", mock.MagicMock(return_value=50))
        ce._run_cpu_mode(mock.MagicMock(), args, None)
        ce.worker_random.assert_called_once()  # type: ignore[attr-defined]

    def test_sequential_with_checkpoint(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """顺序模式从 checkpoint 恢复起始值。."""
        args = mock.MagicMock(
            mode="sequential",
            start="0x1",
            count=0,
            threads=1,
        )
        monkeypatch.setattr(
            ce,
            "load_checkpoint",
            lambda: {"mode": "sequential", "next_key": 500, "checked": 400},
        )
        worker = mock.MagicMock(return_value=0)
        monkeypatch.setattr(ce, "worker_sequential", worker)
        ce._run_cpu_mode(mock.MagicMock(), args, None)
        worker.assert_called_once()


# ═══════════════════════════════════════════════════════════
# _health_check
# ═══════════════════════════════════════════════════════════


class TestHealthCheck:
    """_health_check 输出 JSON 状态。."""

    def test_basic_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """基本健康检查输出 JSON（模拟 UTXO 文件存在）。."""
        import io

        ce._db = mock.MagicMock()
        ce._db.count_results.return_value = 42
        ce._config = mock.MagicMock()
        ce._config.enable_utxo_auto_refresh = False
        monkeypatch.setattr(Path, "exists", lambda _: True)
        mock_stat = mock.MagicMock()
        mock_stat.st_size = 2 * 1024**3  # pretend 2 GB
        monkeypatch.setattr(Path, "stat", lambda _: mock_stat)
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        ce._health_check()
        data = json.loads(out.getvalue())
        assert data["status"] == "ok"
        assert data["checks"]["database"]["result_count"] == 42

    def test_database_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """数据库异常时标记 degraded。."""
        import io

        db = mock.MagicMock()
        db.count_results.side_effect = Exception("db down")
        ce._db = db
        ce._config = mock.MagicMock()
        ce._config.enable_utxo_auto_refresh = False
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        ce._health_check()
        data = json.loads(out.getvalue())
        assert data["checks"]["database"]["status"] == "error"

    def test_no_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """数据库未初始化。."""
        import io

        ce._db = None
        ce._config = mock.MagicMock()
        ce._config.enable_utxo_auto_refresh = False
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        ce._health_check()
        data = json.loads(out.getvalue())
        assert data["checks"]["database"]["status"] == "not_initialized"

    def test_gpu_device_enum_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GPU 设备枚举异常应记录到状态。."""
        import io

        ce._db = mock.MagicMock()
        ce._db.count_results.return_value = 0
        ce._config = mock.MagicMock()
        ce._config.enable_utxo_auto_refresh = False
        ce._GPU_AVAILABLE = True
        monkeypatch.setattr(
            ce,
            "gpu_list_devices",
            mock.MagicMock(side_effect=RuntimeError("opencl err")),
        )
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        ce._health_check()
        data = json.loads(out.getvalue())
        assert "device_enum_error" in data["checks"]["gpu"]
        assert "opencl err" in data["checks"]["gpu"]["device_enum_error"]

    def test_degraded_when_utxo_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """UTXO 数据缺失时状态为 degraded。."""
        import io
        from pathlib import Path

        monkeypatch.setattr(Path, "exists", lambda _: False)
        ce._db = mock.MagicMock()
        ce._db.count_results.return_value = 0
        ce._config = mock.MagicMock()
        ce._config.enable_utxo_auto_refresh = False
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        ce._health_check()
        data = json.loads(out.getvalue())
        assert data["status"] == "degraded"
        assert data["checks"]["utxo_data"]["present"] is False


# ═══════════════════════════════════════════════════════════
# _load_targets
# ═══════════════════════════════════════════════════════════


class TestLoadTargets:
    """_load_targets 的加载路径。."""

    def test_basic_load(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """基本加载路径（无 P2TR）。."""
        args = mock.MagicMock(p2tr=False, xonly_file="")
        mock_target = mock.MagicMock()
        monkeypatch.setattr(ce, "Hash160Set", lambda: mock_target)
        monkeypatch.setattr(ce, "SwappableTarget", lambda **kw: mock.MagicMock())
        _t, x = ce._load_targets(args)
        assert x is None

    def test_p2tr_load(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P2TR 加载路径。."""
        args = mock.MagicMock(p2tr=True, xonly_file="")
        mock_target = mock.MagicMock()
        mock_xonly = mock.MagicMock()
        monkeypatch.setattr(ce, "Hash160Set", lambda: mock_target)
        monkeypatch.setattr(ce, "XOnlySet", lambda: mock_xonly)
        monkeypatch.setattr(ce, "SwappableTarget", lambda **kw: mock.MagicMock())
        _t, x = ce._load_targets(args)
        assert x is not None


# ═══════════════════════════════════════════════════════════
# _report_progress
# ═══════════════════════════════════════════════════════════


class TestReportProgress:
    """_report_progress 进度日志。."""

    def test_reports_with_elapsed(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """在 time 间隔后输出进度。."""
        ce._global_checked = 5000
        ce._global_start_time = time.time() - 10
        ce._counter_lock = mock.MagicMock()
        ce._report_progress(0, 1000, 0)
        mock_logger.info.assert_called_once()
        log_str = str(mock_logger.info.call_args)
        assert "5,000" in log_str

    def test_reports_with_current_key(
        self,
        mock_logger: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """顺序模式显示当前 key。."""
        ce._global_checked = 3000
        ce._global_start_time = time.time() - 5
        ce._counter_lock = mock.MagicMock()
        ce._report_progress(100, 500, 1)
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args.args[1] == 1  # thread_id 为第一个格式参数
