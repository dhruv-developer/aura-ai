"""Envelope encryption for tenant secrets (BYOK provider keys, webhook secrets, ...).

- A fresh 256-bit data key (DEK) encrypts each secret with AES-256-GCM.
- The DEK is wrapped by a key-encryption key (KEK) from a ``KeyProvider``. Locally that
  is a master key from settings; in production it is a cloud KMS (V6 adds per-tenant
  customer-managed keys behind the same port).
- Associated data binds each ciphertext to its owner, e.g. ``account_id|provider``. A
  ciphertext copied to another tenant's row fails to decrypt.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aura_shared.errors import AuraError


class DecryptionFailed(AuraError):
    status, code, title = 500, "decryption_failed", "Secret could not be decrypted"


class KeyProvider(Protocol):
    key_id: str

    def wrap(self, dek: bytes, aad: bytes) -> bytes: ...
    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes: ...


class LocalKeyProvider:
    """KEK held in process memory (dev/test, or a single-tenant self-host)."""

    def __init__(self, master_key: bytes, *, key_id: str = "local-v1") -> None:
        if len(master_key) != 32:
            raise ValueError("master key must be 32 bytes")
        self._aead = AESGCM(master_key)
        self.key_id = key_id

    @classmethod
    def from_base64(cls, value: str, *, key_id: str = "local-v1") -> LocalKeyProvider:
        return cls(base64.b64decode(value), key_id=key_id)

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, dek, aad)

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        try:
            return self._aead.decrypt(wrapped[:12], wrapped[12:], aad)
        except InvalidTag as exc:
            raise DecryptionFailed("data key unwrap failed") from exc


@dataclass(frozen=True, slots=True)
class Sealed:
    key_id: str
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes

    def to_dict(self) -> dict[str, str]:
        b64 = base64.b64encode
        return {
            "key_id": self.key_id,
            "wrapped_dek": b64(self.wrapped_dek).decode(),
            "nonce": b64(self.nonce).decode(),
            "ciphertext": b64(self.ciphertext).decode(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> Sealed:
        b = base64.b64decode
        return cls(d["key_id"], b(d["wrapped_dek"]), b(d["nonce"]), b(d["ciphertext"]))


class Envelope:
    def __init__(self, providers: dict[str, KeyProvider], *, active: str) -> None:
        """``providers`` by key id. Only ``active`` encrypts; old ids still decrypt, so
        a KEK rotation re-wraps lazily."""
        if active not in providers:
            raise ValueError("active key id not among providers")
        self._providers = providers
        self._active = providers[active]

    def seal(self, plaintext: str, *, aad: str) -> Sealed:
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        ad = aad.encode()
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode(), ad)
        return Sealed(self._active.key_id, self._active.wrap(dek, ad), nonce, ciphertext)

    def open(self, sealed: Sealed, *, aad: str) -> str:
        provider = self._providers.get(sealed.key_id)
        if provider is None:
            raise DecryptionFailed(f"unknown key id {sealed.key_id!r}")
        ad = aad.encode()
        dek = provider.unwrap(sealed.wrapped_dek, ad)
        try:
            return AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, ad).decode()
        except InvalidTag as exc:
            raise DecryptionFailed("secret decryption failed") from exc


DEV_KEY_ID = "dev-insecure"


def envelope_from_settings(master_key_b64: str | None, *, allow_ephemeral: bool) -> Envelope:
    """Without a configured key, dev/test use a fixed, publicly known dev key, so that
    separate processes (api, runtime, workers) can decrypt each other's secrets locally.
    It is tagged ``dev-insecure``, so production never accepts data sealed with it.
    Every other environment must configure a real key (``allow_ephemeral=False``).
    """
    if master_key_b64:
        provider = LocalKeyProvider.from_base64(master_key_b64)
    elif allow_ephemeral:
        import hashlib

        dev_key = hashlib.sha256(b"aura-dev-only-master-key-never-use-in-prod").digest()
        provider = LocalKeyProvider(dev_key, key_id=DEV_KEY_ID)
    else:
        raise RuntimeError("AURA_MASTER_KEY is required outside dev/test")
    return Envelope({provider.key_id: provider}, active=provider.key_id)
