"""async execution-graph runs

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("template", sa.String(length=32), nullable=True),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "trace",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("key_id", sa.String(length=64), nullable=True),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column(
            "cost_usd", sa.Numeric(precision=18, scale=8), server_default="0", nullable=False
        ),
        sa.Column("error", sa.String(length=2000), nullable=True),
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
            name=op.f("fk_graph_runs_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_runs")),
    )
    op.create_index(op.f("ix_graph_runs_account_id"), "graph_runs", ["account_id"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON graph_runs TO aura_app, aura_system")
    op.execute("ALTER TABLE graph_runs ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON graph_runs TO aura_app
          USING (account_id = current_setting('aura.account_id', true))
          WITH CHECK (account_id = current_setting('aura.account_id', true))
    """)
    op.execute(
        "CREATE POLICY system_access ON graph_runs TO aura_system USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_graph_runs_account_id"), table_name="graph_runs")
    op.drop_table("graph_runs")
