"""API key format and hashing. Pure: no I/O.

Format: ``aura_sk_<env>_<prefix>_<secret>``
  - ``env``: dev | stg | prod | personal (visible so leaked keys are easy to triage)
  - ``prefix``: 12 lowercase base32 chars, unique and stored in clear for lookup
  - ``secret``: 256 bits, URL-safe base64 (may itself contain ``_``)

Only a peppered HMAC-SHA256 of the full key is stored. The key is high-entropy, so a
fast keyed hash is appropriate. Argon2 would add per-request latency on the hot path
for no security gain.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from aura_shared.errors import Unauthenticated

KEY_ENVS = {"development": "dev", "staging": "stg", "production": "prod", None: "personal"}
_VALID_ENVS = frozenset(KEY_ENVS.values())


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    plaintext: str
    prefix: str
    secret_hash: str


@dataclass(frozen=True, slots=True)
class ParsedKey:
    env: str
    prefix: str
    plaintext: str


def _hash(plaintext: str, pepper: bytes) -> str:
    return hmac.new(pepper, plaintext.encode(), hashlib.sha256).hexdigest()


def generate(environment_kind: str | None, pepper: bytes) -> GeneratedKey:
    env = KEY_ENVS[environment_kind]
    prefix = base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")[:12]
    secret = secrets.token_urlsafe(32)
    plaintext = f"aura_sk_{env}_{prefix}_{secret}"
    return GeneratedKey(plaintext, prefix, _hash(plaintext, pepper))


def parse(plaintext: str) -> ParsedKey:
    parts = plaintext.strip().split("_", 4)
    if (
        len(parts) != 5
        or parts[0] != "aura"
        or parts[1] != "sk"
        or parts[2] not in _VALID_ENVS
        or len(parts[3]) != 12
        or len(parts[4]) < 32
    ):
        raise Unauthenticated("malformed API key")
    return ParsedKey(parts[2], parts[3], plaintext.strip())


def verify(parsed: ParsedKey, stored_hash: str, pepper: bytes) -> bool:
    return hmac.compare_digest(_hash(parsed.plaintext, pepper), stored_hash)
