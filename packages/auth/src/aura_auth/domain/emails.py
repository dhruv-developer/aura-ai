"""Email normalisation and validation (deliberately conservative; the magic link proves
ownership, so we only reject obviously malformed input)."""

from __future__ import annotations

import re

from aura_shared.errors import ValidationFailed

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > 320 or not _EMAIL.match(email):
        raise ValidationFailed("invalid email address")
    return email


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "org"
