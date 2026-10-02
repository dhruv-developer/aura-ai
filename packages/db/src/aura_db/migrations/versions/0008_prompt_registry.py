"""prompt registry

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("active_version_id", sa.String(length=40), nullable=True),
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
            name=op.f("fk_prompt_templates_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_templates")),
        sa.UniqueConstraint("account_id", "name", name="uq_prompt_name"),
    )
    op.create_index(op.f("ix_prompt_templates_account_id"), "prompt_templates", ["account_id"])
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("template_id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=40), server_default="manual", nullable=False),
        sa.Column(
            "evaluation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_prompt_versions_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_prompt_versions_template_id_prompt_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
        sa.UniqueConstraint("template_id", "version", name="uq_prompt_version"),
    )
    op.create_index(op.f("ix_prompt_versions_account_id"), "prompt_versions", ["account_id"])
    op.create_index(op.f("ix_prompt_versions_template_id"), "prompt_versions", ["template_id"])
    for t in ("prompt_templates", "prompt_versions"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO aura_app, aura_system")
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {t} TO aura_app
              USING (account_id = current_setting('aura.account_id', true))
              WITH CHECK (account_id = current_setting('aura.account_id', true))
        """)
        op.execute(
            f"CREATE POLICY system_access ON {t} TO aura_system USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_prompt_versions_template_id"), table_name="prompt_versions")
    op.drop_index(op.f("ix_prompt_versions_account_id"), table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index(op.f("ix_prompt_templates_account_id"), table_name="prompt_templates")
    op.drop_table("prompt_templates")
