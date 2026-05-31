"""测试 collision_engine 模块 — 核心逻辑与提取的函数。

策略：纯函数直接测试；需 mock 的用 monkeypatch；避免真实 GPU/线程密集路径。
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path
from typing import Generator
from unittest import mock

import pytest

# ── 确保项目根在 sys.path ────────────────────────────────
_engine_path = Path(__file__).resolve().parent.parent
if str(_engine_path) not in sys.path:
    sys.path.insert(0, str(_engine_path))

import collision_engine as ce  # noqa: E402


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
    yield


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
    _BASE = dict(
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
            ["--gpu", "--gpu-mode", "sequential", "--p2tr"]
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ce, "CHECKPOINT_FILE", tmp_path / "ckpt.json")
        ce.save_checkpoint({"mode": "sequential", "next_key": 100})
        loaded = ce.load_checkpoint()
        assert loaded["mode"] == "sequential"
        assert loaded["next_key"] == 100

    def test_corrupted_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ce, "CHECKPOINT_FILE", tmp_path / "ckpt.json")
        ce.CHECKPOINT_FILE.write_text("bad-json")
        assert ce.load_checkpoint() == {}


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
    """完整的碰撞检查路径：压缩命中 / 非压缩命中 / 无命中 / 异常。"""

    def _setup_mocks(self, monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
        pub = mock.MagicMock()
        pub.format.side_effect = lambda compressed=True: (
            b"\x02" + b"\x01" * 32 if compressed else b"\x04" + b"\x01" * 64
        )
        monkeypatch.setattr(
            "collision_engine.PrivateKey", lambda _: mock.MagicMock(public_key=pub)
        )
        return pub

    def test_compressed_hit(
        self, monkeypatch: pytest.MonkeyPatch, mock_logger: mock.MagicMock
    ) -> None:
        self._setup_mocks(monkeypatch)
        target = mock.MagicMock()
        comp_h160 = ce.hash160(b"\x02" + b"\x01" * 32)
        target.__contains__.side_effect = lambda h: h == comp_h160
        result = ce.check_single_key(1, target)
        assert result is not None
        assert result.found_via == "compressed"

    def test_uncompressed_hit(
        self, monkeypatch: pytest.MonkeyPatch, mock_logger: mock.MagicMock
    ) -> None:
        self._setup_mocks(monkeypatch)
        target = mock.MagicMock()
        uncomp_h160 = ce.hash160(b"\x04" + b"\x01" * 64)
        target.__contains__.side_effect = lambda h: h == uncomp_h160
        result = ce.check_single_key(1, target)
        assert result is not None
        assert result.found_via == "uncompressed"

    def test_no_hit(
        self, monkeypatch: pytest.MonkeyPatch, mock_logger: mock.MagicMock
    ) -> None:
        self._setup_mocks(monkeypatch)
        target = mock.MagicMock()
        target.__contains__.return_value = False
        assert ce.check_single_key(1, target) is None

    def test_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, mock_logger: mock.MagicMock
    ) -> None:
        monkeypatch.setattr(
            "collision_engine.PrivateKey", mock.MagicMock(side_effect=ValueError("bad"))
        )
        assert ce.check_single_key(1, mock.MagicMock()) is None
        mock_logger.warning.assert_called_once()


# ═══════════════════════════════════════════════════════════
# check_single_key_chain — 链式加速路径
# ═══════════════════════════════════════════════════════════


class TestCheckSingleKeyChain:
    def test_compressed_hit(
        self, monkeypatch: pytest.MonkeyPatch, mock_logger: mock.MagicMock
    ) -> None:
        pub = mock.MagicMock()
        pub.format.side_effect = lambda compressed=True: (
            b"\x02" + b"\x01" * 32 if compressed else b"\x04" + b"\x01" * 64
        )
        monkeypatch.setattr(
            "collision_engine.PrivateKey", lambda _: mock.MagicMock(public_key=pub)
        )
        target = mock.MagicMock()
        comp_h160 = ce.hash160(b"\x02" + b"\x01" * 32)
        target.__contains__.side_effect = lambda h: h == comp_h160
        result, pubkey = ce.check_single_key_chain(1, target, b"\x00" * 32)
        assert result is not None
        assert result.found_via == "compressed"
        assert pubkey is not None

    def test_no_hit_returns_pubkey(
        self, monkeypatch: pytest.MonkeyPatch, mock_logger: mock.MagicMock
    ) -> None:
        pub = mock.MagicMock()
        pub.format.return_value = b"\x02" + b"\x01" * 32
        monkeypatch.setattr(
            "collision_engine.PrivateKey", lambda _: mock.MagicMock(public_key=pub)
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
        """mock _init_core 同时设置 _logger 和 _config（main() 在 _init_core 后立即用）。"""
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
        ce._health_check.assert_called_once()

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
        self, monkeypatch: pytest.MonkeyPatch, mock_logger: mock.MagicMock
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
