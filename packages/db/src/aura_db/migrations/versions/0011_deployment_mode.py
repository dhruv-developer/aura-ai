"""deployment mode (canary | experiment)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("mode", sa.String(length=16), server_default="canary", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("deployments", "mode")
