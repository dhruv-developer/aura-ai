"""memory full-text: english (stemmed) + simple tsvector

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace(expression: str) -> None:
    # PostgreSQL 14 cannot alter a generated column's expression in place.
    op.drop_index("ix_memory_items_tsv", table_name="memory_items", postgresql_using="gin")
    op.drop_column("memory_items", "tsv")
    op.add_column(
        "memory_items",
        sa.Column(
            "tsv", postgresql.TSVECTOR(), sa.Computed(expression, persisted=True), nullable=True
        ),
    )
    op.create_index("ix_memory_items_tsv", "memory_items", ["tsv"], postgresql_using="gin")


def upgrade() -> None:
    _replace("to_tsvector('english', content) || to_tsvector('simple', content)")


def downgrade() -> None:
    _replace("to_tsvector('simple', content)")
