"""Encrypted per-message traces (multi-agent trees) on conversation messages

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("trace_key_id", sa.String(length=64)),
    ("trace_wrapped_dek", sa.LargeBinary()),
    ("trace_nonce", sa.LargeBinary()),
    ("trace_ciphertext", sa.LargeBinary()),
)


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("conversation_messages", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("conversation_messages", name)
