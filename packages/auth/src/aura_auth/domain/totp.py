"""RFC 6238 TOTP (SHA-1, 30 s, 6 digits: what authenticator apps implement). Pure."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

STEP_S = 30
DIGITS = 6


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    return base64.b32decode(padded.upper())


def code_at(secret_b32: str, step: int) -> str:
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def verify(secret_b32: str, code: str, *, now: float, window: int = 1) -> int | None:
    """Return the matched time step (for replay protection), or None. Accepts ±window
    steps of clock drift. Uses constant-time comparison."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int(now // STEP_S)
    for step in range(current - window, current + window + 1):
        if hmac.compare_digest(code_at(secret_b32, step), code):
            return step
    return None


def provisioning_uri(secret_b32: str, account: str, issuer: str = "AURA Admin") -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret_b32}"
        f"&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_S}"
    )
