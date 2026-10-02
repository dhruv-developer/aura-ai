"""execution policies, versions, deployments

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("execution_policies", "policy_versions", "deployments")


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
        "execution_policies",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_ref", sa.String(length=120), server_default="", nullable=False),
        sa.Column("active_version_id", sa.String(length=40), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_execution_policies_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_policies")),
        sa.UniqueConstraint("account_id", "scope", "scope_ref", name="uq_policy_scope"),
    )
    op.create_index(op.f("ix_execution_policies_account_id"), "execution_policies", ["account_id"])
    op.create_table(
        "policy_versions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("policy_id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.String(length=500), server_default="", nullable=False),
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
            name=op.f("fk_policy_versions_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["execution_policies.id"],
            name=op.f("fk_policy_versions_policy_id_execution_policies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_versions")),
        sa.UniqueConstraint("policy_id", "version", name="uq_policy_version"),
    )
    op.create_index(op.f("ix_policy_versions_account_id"), "policy_versions", ["account_id"])
    op.create_index(op.f("ix_policy_versions_policy_id"), "policy_versions", ["policy_id"])
    op.create_table(
        "deployments",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("policy_id", sa.String(length=40), nullable=False),
        sa.Column("from_version_id", sa.String(length=40), nullable=True),
        sa.Column("to_version_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("percent", sa.Integer(), nullable=False),
        sa.Column(
            "stage_started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("rollback_reason", sa.String(length=1000), nullable=True),
        sa.Column(
            "history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_deployments_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["execution_policies.id"],
            name=op.f("fk_deployments_policy_id_execution_policies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
    )
    op.create_index(op.f("ix_deployments_account_id"), "deployments", ["account_id"])
    op.create_index(
        "uq_deployments_inflight",
        "deployments",
        ["policy_id"],
        unique=True,
        postgresql_where=sa.text("status = 'canary'"),
    )
    for t in TABLES:
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
    op.drop_index(
        "uq_deployments_inflight",
        table_name="deployments",
        postgresql_where=sa.text("status = 'canary'"),
    )
    op.drop_index(op.f("ix_deployments_account_id"), table_name="deployments")
    op.drop_table("deployments")
    op.drop_index(op.f("ix_policy_versions_policy_id"), table_name="policy_versions")
    op.drop_index(op.f("ix_policy_versions_account_id"), table_name="policy_versions")
    op.drop_table("policy_versions")
    op.drop_index(op.f("ix_execution_policies_account_id"), table_name="execution_policies")
    op.drop_table("execution_policies")
