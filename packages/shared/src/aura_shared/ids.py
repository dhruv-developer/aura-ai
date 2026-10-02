"""Prefixed, time-sortable identifiers (``org_01J...``).

Uses a ULID-style layout: 48-bit millisecond timestamp + 80 random bits, Crockford base32.
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PREFIXES = frozenset(
    {
        # identity & tenancy
        "usr",
        "oid",
        "acct",
        "org",
        "mbr",
        "role",
        "sso",
        "dom",
        "scim",
        "ws",
        "prj",
        "env",
        "key",
        "pcr",
        "xr",
        "lead",
        "job",
        "shd",
        "dep",
        "prm",
        "prmv",
        "popt",
        "grun",
        "shr",
        "llm",
        "conv",
        "msg",
        # commercial
        "sub",
        "ovr",
        "txn",
        "led",
        "inv",
        "pev",
        # engine
        "pol",
        "polv",
        "wl",
        "exe",
        "trc",
        "run",
        "mem",
        # ops
        "evt",
        "aud",
    }
)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def new_id(prefix: str, *, now_ms: int | None = None) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")
    ts = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    rand = secrets.randbits(80)
    return f"{prefix}_{_encode(ts, 10)}{_encode(rand, 16)}"


def id_prefix(value: str) -> str:
    prefix, sep, _ = value.partition("_")
    if not sep or prefix not in PREFIXES:
        raise ValueError(f"not an AURA id: {value!r}")
    return prefix
