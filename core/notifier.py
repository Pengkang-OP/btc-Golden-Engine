"""通知发送模块 — 碰撞命中时发送邮件/Webhook/Telegram 通知。

使用 ThreadPoolExecutor 异步发送，不阻塞碰撞主循环。

用法:
    from core.notifier import Notifier

    notifier = Notifier(config)
    notifier.on_hit(collision_result)  # 异步发送通知
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import EngineConfig
from .errors import NotifierError

logger = logging.getLogger(__name__)


class Notifier:
    """碰撞命中通知发送器 — 邮件 + Webhook + Telegram。

    所有通知通过内部 ThreadPoolExecutor 异步发送。
    发送失败仅计入日志，不阻塞主流程。

    Attributes:
        config: 引擎配置（读取通知相关字段）。
        _executor: 线程池，用于异步发送。
    """

    def __init__(self, config: EngineConfig):
        """初始化通知器，配置 SMTP/Webhook 参数并创建线程池。"""
        self.config = config
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="notifier",
        )

    # ── 公共接口 ──────────────────────────────────────────

    def on_hit(self, result: Any) -> None:
        """碰撞命中时触发通知（异步发送）。

        Args:
            result: CollisionResult dataclass 实例。
        """
        if not self._is_configured():
            return

        try:
            self._executor.submit(self._send_all, result)
        except RuntimeError:
            logger.exception("通知提交失败 (executor 可能已关闭)")

    # ── 发送逻辑 ──────────────────────────────────────────

    def _send_all(self, result: Any) -> None:
        """发送所有已配置的通知通道。"""
        subject = f"🔥 碰撞命中! {getattr(result, 'address_type', '?')}"
        body = self._format_body(result)

        if self._should_email():
            try:
                self.send_email(subject, body)
            except NotifierError:
                pass  # 已由 send_email 记录日志

        if self._webhook_url():
            try:
                payload = self._build_payload(result, subject)
                self.send_webhook(payload)
            except NotifierError:
                pass

        # Telegram：使用配置中的 bot_token 和 chat_id
        bot_token = getattr(self.config, "telegram_bot_token", "")
        chat_id = getattr(self.config, "telegram_chat_id", "")
        if bot_token and chat_id:
            try:
                self.send_telegram(bot_token, chat_id, body)
            except NotifierError:
                pass

    def send_email(self, subject: str, body: str) -> bool:
        """通过 SMTP 发送邮件通知。

        Args:
            subject: 邮件主题。
            body: 邮件正文（纯文本）。

        Returns:
            发送成功返回 True。

        Raises:
            NotifierError: SMTP 连接或发送失败。
        """
        cfg = self.config
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = cfg.smtp_from
            msg["To"] = cfg.smtp_to

            context = ssl.create_default_context()
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
                server.starttls(context=context)
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg)

            logger.info("邮件通知发送成功: %s", subject[:50])
            return True

        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            logger.warning("邮件通知发送失败: %s", exc)
            raise NotifierError(f"邮件发送失败: {exc}", original=exc) from exc

    def send_webhook(self, payload: dict[str, Any]) -> bool:
        """通过 HTTP POST 发送 Webhook 通知。

        Args:
            payload: 发送的 JSON 字典。

        Returns:
            发送成功返回 True。

        Raises:
            NotifierError: HTTP 请求失败或状态码异常。
        """
        url = self._webhook_url()
        if not url:
            return False

        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                status = resp.status
                if status >= 400:
                    raise NotifierError(
                        f"Webhook 返回非正常状态码: {status}",
                    )

            logger.info("Webhook 通知发送成功 -> %s", url[:60])
            return True

        except (URLError, OSError, TypeError) as exc:
            logger.warning("Webhook 通知发送失败: %s", exc)
            raise NotifierError(f"Webhook 发送失败: {exc}", original=exc) from exc

    def send_telegram(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
    ) -> bool:
        """通过 Telegram Bot API 发送消息。

        Args:
            bot_token: Bot 令牌。
            chat_id: 目标聊天 ID。
            text: 消息文本。

        Returns:
            发送成功返回 True。

        Raises:
            NotifierError: API 请求失败。
        """
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    raise NotifierError(
                        f"Telegram API 返回错误: {resp.status}",
                    )

            logger.info("Telegram 通知发送成功")
            return True

        except (URLError, OSError, TypeError) as exc:
            logger.warning("Telegram 通知发送失败: %s", exc)
            raise NotifierError(
                f"Telegram 发送失败: {exc}",
                original=exc,
            ) from exc

    # ── 内部帮助方法 ──────────────────────────────────────

    def _is_configured(self) -> bool:
        """检查是否有至少一个通知通道已配置。"""
        if not self.config.notify_on_hit:
            return False
        return bool(self._should_email() or self._webhook_url())

    def _should_email(self) -> bool:
        """检查邮件通知是否已配置。"""
        cfg = self.config
        return bool(cfg.smtp_host and cfg.smtp_user and cfg.smtp_to)

    def _webhook_url(self) -> str:
        """返回 Webhook URL 或空字符串。"""
        return self.config.webhook_url or ""

    @staticmethod
    def _format_body(result: Any) -> str:
        """将碰撞结果格式化为可读文本。"""
        lines = [
            f"地址类型: {getattr(result, 'address_type', '?')}",
            f"私钥 (hex): {getattr(result, 'privkey_hex', '?')}",
            f"WIF (压缩): {getattr(result, 'wif_compressed', '?')}",
            f"P2PKH (压缩): {getattr(result, 'p2pkh_address_comp', '?')}",
            f"P2WPKH: {getattr(result, 'p2wpkh_address', '?')}",
            f"Hash160: {getattr(result, 'h160_hex', '?')}",
            f"扫描方式: {getattr(result, 'found_via', '?')}",
        ]
        p2tr = getattr(result, "p2tr_address", "")
        if p2tr:
            lines.append(f"P2TR: {p2tr}")
        p2sh = getattr(result, "p2sh_address", "")
        if p2sh:
            lines.append(f"P2SH: {p2sh}")
        return "\n".join(lines)

    @staticmethod
    def _build_payload(result: Any, subject: str) -> dict[str, Any]:
        """构造 Webhook JSON payload。"""
        return {
            "subject": subject,
            "address_type": getattr(result, "address_type", ""),
            "privkey_hex": getattr(result, "privkey_hex", ""),
            "wif_compressed": getattr(result, "wif_compressed", ""),
            "p2pkh_address_comp": getattr(result, "p2pkh_address_comp", ""),
            "p2wpkh_address": getattr(result, "p2wpkh_address", ""),
            "h160_hex": getattr(result, "h160_hex", ""),
            "found_via": getattr(result, "found_via", ""),
            "p2tr_address": getattr(result, "p2tr_address", ""),
            "p2sh_address": getattr(result, "p2sh_address", ""),
        }

    # ── 生命周期 ──────────────────────────────────────────

    def shutdown(self, wait: bool = True) -> None:
        """关闭线程池，等待待处理通知完成。

        Args:
            wait: 是否等待未完成的任务。
        """
        self._executor.shutdown(wait=wait)
        logger.debug("Notifier 已关闭")
