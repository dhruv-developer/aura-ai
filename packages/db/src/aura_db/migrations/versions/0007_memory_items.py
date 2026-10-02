"""long-term memory and knowledge-base chunks

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_ref", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("embedding", postgresql.ARRAY(sa.REAL()), nullable=True),
        sa.Column("importance", sa.Float(), server_default="0.5", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.8", nullable=False),
        sa.Column("source", sa.String(length=120), server_default="api", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_memory_items_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    op.create_index(op.f("ix_memory_items_account_id"), "memory_items", ["account_id"])
    op.create_index("ix_memory_items_scope", "memory_items", ["account_id", "scope", "scope_ref"])
    op.create_index("ix_memory_items_tsv", "memory_items", ["tsv"], postgresql_using="gin")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memory_items TO aura_app, aura_system")
    op.execute("ALTER TABLE memory_items ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON memory_items TO aura_app
          USING (account_id = current_setting('aura.account_id', true))
          WITH CHECK (account_id = current_setting('aura.account_id', true))
    """)
    op.execute(
        "CREATE POLICY system_access ON memory_items TO aura_system USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_tsv", table_name="memory_items", postgresql_using="gin")
    op.drop_index("ix_memory_items_scope", table_name="memory_items")
    op.drop_index(op.f("ix_memory_items_account_id"), table_name="memory_items")
    op.drop_table("memory_items")
