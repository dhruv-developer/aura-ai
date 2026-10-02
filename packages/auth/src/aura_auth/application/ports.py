"""Ports for external services used by auth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None


class Mailer(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class OAuthUserInfo:
    subject: str
    email: str
    email_verified: bool
    name: str | None


class OAuthProvider(Protocol):
    name: str

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> OAuthUserInfo: ...
