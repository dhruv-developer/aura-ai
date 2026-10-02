"""Mailer adapters: SMTP (Mailpit locally, a provider relay in prod) and in-memory."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage as MimeMessage

from aura_auth.application.ports import EmailMessage


class SmtpMailer:
    def __init__(self, host: str, port: int, sender: str, *, timeout_s: float = 10.0) -> None:
        self._host, self._port, self._sender, self._timeout = host, port, sender, timeout_s

    def _send_sync(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"], mime["To"], mime["Subject"] = self._sender, message.to, message.subject
        mime.set_content(message.text)
        if message.html:
            mime.add_alternative(message.html, subtype="html")
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            smtp.send_message(mime)

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send_sync, message)


class InMemoryMailer:
    """For tests and local runs without SMTP."""

    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)


class LogMailer:
    """Dev only (``AURA_MAIL_BACKEND=log``): writes emails, including sign-in links, to
    the log instead of sending them. Never enabled outside dev/test."""

    def __init__(self) -> None:
        self._log = logging.getLogger("aura.mail")

    async def send(self, message: EmailMessage) -> None:
        self._log.warning(
            "DEV EMAIL to=%s subject=%r\n%s", message.to, message.subject, message.text
        )
