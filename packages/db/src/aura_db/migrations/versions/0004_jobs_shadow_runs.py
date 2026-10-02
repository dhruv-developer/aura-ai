"""jobs queue and shadow runs

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_account_id"), "jobs", ["account_id"])
    op.create_index(
        "ix_jobs_ready",
        "jobs",
        ["priority", "run_after"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "shadow_runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dataset_key_id", sa.String(length=64), nullable=True),
        sa.Column("dataset_wrapped_dek", sa.LargeBinary(), nullable=True),
        sa.Column("dataset_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("dataset_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column(
            "spent_usd", sa.Numeric(precision=18, scale=8), server_default="0", nullable=False
        ),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_shadow_runs_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_runs")),
    )
    op.create_index(op.f("ix_shadow_runs_account_id"), "shadow_runs", ["account_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO aura_system")
    # Tenants may enqueue jobs for themselves inside their own transactions.
    op.execute("GRANT INSERT ON jobs TO aura_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shadow_runs TO aura_app, aura_system")
    op.execute("ALTER TABLE shadow_runs ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON shadow_runs TO aura_app
          USING (account_id = current_setting('aura.account_id', true))
          WITH CHECK (account_id = current_setting('aura.account_id', true))
    """)
    op.execute(
        "CREATE POLICY system_access ON shadow_runs TO aura_system USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_shadow_runs_account_id"), table_name="shadow_runs")
    op.drop_table("shadow_runs")
    op.drop_index(
        "ix_jobs_ready",
        table_name="jobs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index(op.f("ix_jobs_account_id"), table_name="jobs")
    op.drop_table("jobs")
