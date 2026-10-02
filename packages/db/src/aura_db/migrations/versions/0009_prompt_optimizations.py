"""prompt optimization runs

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_optimizations",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("template_id", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
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
            name=op.f("fk_prompt_optimizations_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_prompt_optimizations_template_id_prompt_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_optimizations")),
    )
    op.create_index(
        op.f("ix_prompt_optimizations_account_id"), "prompt_optimizations", ["account_id"]
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON prompt_optimizations TO aura_app, aura_system"
    )
    op.execute("ALTER TABLE prompt_optimizations ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON prompt_optimizations TO aura_app
          USING (account_id = current_setting('aura.account_id', true))
          WITH CHECK (account_id = current_setting('aura.account_id', true))
    """)
    op.execute(
        "CREATE POLICY system_access ON prompt_optimizations TO aura_system USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_prompt_optimizations_account_id"), table_name="prompt_optimizations")
    op.drop_table("prompt_optimizations")
