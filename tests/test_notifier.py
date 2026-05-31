"""测试 core.notifier 模块 — Notifier 通知发送器。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest

from core.config import EngineConfig
from core.errors import NotifierError
from core.notifier import Notifier


@pytest.fixture
def config(tmp_dir: Path) -> EngineConfig:
    """基础配置（通知关闭）。"""
    return EngineConfig(_base_dir=tmp_dir)


@pytest.fixture
def config_with_smtp(tmp_dir: Path) -> EngineConfig:
    """启用 SMTP 的配置。"""
    return EngineConfig(
        _base_dir=tmp_dir,
        notify_on_hit=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secret",
        smtp_from="from@example.com",
        smtp_to="to@example.com",
    )


@pytest.fixture
def config_with_webhook(tmp_dir: Path) -> EngineConfig:
    """启用 Webhook 的配置。"""
    return EngineConfig(
        _base_dir=tmp_dir,
        notify_on_hit=True,
        webhook_url="https://hooks.example.com/collision",
    )


class TestNotifierConfig:
    """通知配置检测。"""

    def test_not_configured_when_notify_off(self, config):
        """notify_on_hit=False 时不应发送。"""
        n = Notifier(config)
        assert n._is_configured() is False
        n.shutdown(wait=False)

    def test_configured_with_smtp(self, config_with_smtp):
        """SMTP 配置时应认为已配置。"""
        n = Notifier(config_with_smtp)
        assert n._is_configured() is True
        n.shutdown(wait=False)

    def test_configured_with_webhook(self, config_with_webhook):
        """Webhook 配置时应认为已配置。"""
        n = Notifier(config_with_webhook)
        assert n._is_configured() is True
        n.shutdown(wait=False)

    def test_not_configured_email_missing_fields(self, tmp_dir):
        """SMTP 缺少必要字段时不应误判为已配置。"""
        cfg = EngineConfig(
            _base_dir=tmp_dir,
            notify_on_hit=True,
            smtp_host="smtp.example.com",  # 无 user 和 to
        )
        n = Notifier(cfg)
        assert n._is_configured() is False
        n.shutdown(wait=False)

    def test_on_hit_does_nothing_when_not_configured(self, config):
        """未配置时 on_hit 不应异常。"""
        n = Notifier(config)
        obj = _make_result_obj()
        # 不应抛出任何异常
        n.on_hit(obj)
        n.shutdown(wait=True)

    def test_shutdown_does_not_crash(self, config):
        """shutdown 不应异常。"""
        n = Notifier(config)
        n.shutdown(wait=False)
        n.shutdown(wait=False)  # 二次调用


class TestNotifierEmail:
    """邮件发送逻辑。"""

    def test_send_email_raises_on_bad_host(self, config_with_smtp):
        """无效的 SMTP 主机应抛出 NotifierError。"""
        n = Notifier(config_with_smtp)
        with pytest.raises(NotifierError):
            n.send_email("Test", "Body")
        n.shutdown(wait=False)

    def test_send_email_success(self, config_with_smtp):
        """成功的 SMTP 发送应返回 True。"""
        n = Notifier(config_with_smtp)

        mock_server = MagicMock()
        mock_server.__enter__.return_value = mock_server  # context manager
        mock_context = MagicMock()

        with (
            patch("smtplib.SMTP", return_value=mock_server) as mock_smtp,
            patch("ssl.create_default_context", return_value=mock_context),
        ):
            result = n.send_email("🔥 Hit!", "Details here")

        assert result is True
        mock_smtp.assert_called_once_with(
            config_with_smtp.smtp_host,
            config_with_smtp.smtp_port,
        )
        mock_server.starttls.assert_called_once_with(context=mock_context)
        mock_server.login.assert_called_once_with(
            config_with_smtp.smtp_user,
            config_with_smtp.smtp_password,
        )
        mock_server.send_message.assert_called_once()
        # 验证邮件内容
        sent_msg = mock_server.send_message.call_args[0][0]
        assert "🔥 Hit!" in sent_msg["Subject"]
        assert sent_msg["To"] == config_with_smtp.smtp_to

        n.shutdown(wait=False)


class TestNotifierWebhook:
    """Webhook 发送逻辑。"""

    def test_send_webhook_raises_on_bad_url(self, config_with_webhook):
        """无效 URL 应抛出 NotifierError。"""
        n = Notifier(config_with_webhook)
        with pytest.raises(NotifierError):
            n.send_webhook({"key": "value"})
        n.shutdown(wait=False)

    def test_send_webhook_no_url(self, config):
        """无 Webhook URL 应返回 False。"""
        n = Notifier(config)
        assert n.send_webhook({"key": "value"}) is False
        n.shutdown(wait=False)

    def test_send_webhook_success(self, config_with_webhook):
        """成功的 Webhook 返回 True。"""
        n = Notifier(config_with_webhook)

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__.return_value = mock_response

        with patch("core.notifier.urlopen", return_value=mock_response) as mock_urlopen:
            result = n.send_webhook({"key": "value"})

        assert result is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.method == "POST"
        assert req.headers["Content-type"] == "application/json"

        n.shutdown(wait=False)

    def test_send_webhook_http_error(self, config_with_webhook):
        """HTTP 4xx 应抛出 NotifierError。"""
        n = Notifier(config_with_webhook)

        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.__enter__.return_value = mock_response

        with patch("core.notifier.urlopen", return_value=mock_response):
            with pytest.raises(NotifierError, match="非正常状态码"):
                n.send_webhook({"data": "test"})

        n.shutdown(wait=False)


class TestNotifierTelegram:
    """Telegram 发送逻辑。"""

    def test_send_telegram_raises_on_bad_token(self, config):
        """无效 Bot Token 应抛出 NotifierError。"""
        n = Notifier(config)
        with pytest.raises(NotifierError):
            n.send_telegram("bad_token", "123", "Hello")
        n.shutdown(wait=False)

    def test_send_telegram_success(self, config):
        """成功的 Telegram 发送返回 True。"""
        n = Notifier(config)

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__.return_value = mock_response

        with patch("core.notifier.urlopen", return_value=mock_response) as mock_urlopen:
            result = n.send_telegram("token:abc", "chat_123", "<b>Hit!</b>")

        assert result is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.method == "POST"
        assert "token:abc" in req.full_url
        assert b"chat_123" in req.data

        n.shutdown(wait=False)


def _make_result_obj(**overrides: Any) -> Any:
    """创建模拟碰撞结果对象（支持属性访问）。"""
    defaults = {
        "address_type": "P2PKH",
        "privkey_hex": "a" * 64,
        "wif_compressed": "KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn",
        "wif_uncompressed": "5HpHagT65TZzG1PH3CSu63k8DbpvD8s5ip4nEB3kEsreAnchuDf",
        "p2pkh_address_comp": "1BgGZ9tcN4rm9KB1mD2Tk1YkX7nL6QKuJj",
        "p2wpkh_address": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        "p2pkh_address_uncomp": "1EHNa6cGCktDjnA8eJjgPGXN8Q6GG9TyEC",
        "h160_hex": "00" * 20,
        "found_via": "cpu_random",
        "timestamp": "2026-01-15T10:00:00Z",
        "p2tr_address": "",
        "xonly_hex": "",
    }
    defaults.update(overrides)
    obj = type("ResultObj", (), {})()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


class TestNotifierFormatting:
    """格式化辅助方法。"""

    def test_format_body_includes_key_fields(self):
        """_format_body 应包含关键字段。"""
        obj = _make_result_obj()
        body = Notifier._format_body(obj)
        assert obj.address_type in body
        assert obj.privkey_hex[:16] in body
        assert obj.wif_compressed[:16] in body

    def test_format_body_handles_empty_p2tr(self):
        """空的 p2tr_address 不应出现在正文中。"""
        obj = _make_result_obj(p2tr_address="")
        body = Notifier._format_body(obj)
        assert "P2TR" not in body

    def test_format_body_includes_p2tr_when_present(self):
        """非空 p2tr_address 应包含在正文中。"""
        obj = _make_result_obj(
            address_type="P2TR",
            p2tr_address="bc1p...",
            xonly_hex="ff" * 32,
        )
        body = Notifier._format_body(obj)
        assert "P2TR" in body
        assert "bc1p..." in body

    def test_format_body_includes_p2tr_when_present(self):
        """非空 p2tr_address 应包含在正文中。"""
        obj = _make_result_obj(
            address_type="P2TR",
            p2tr_address="bc1p...",
            xonly_hex="ff" * 32,
        )
        body = Notifier._format_body(obj)
        assert "P2TR" in body
        assert "bc1p..." in body

    def test_build_payload_structure(self):
        """_build_payload 返回字典结构。"""
        obj = _make_result_obj(address_type="P2PKH")
        payload = Notifier._build_payload(obj, "🔥 Test")
        assert isinstance(payload, dict)
        assert payload["subject"] == "🔥 Test"
        assert payload["address_type"] == "P2PKH"
        assert payload["privkey_hex"] == obj.privkey_hex
