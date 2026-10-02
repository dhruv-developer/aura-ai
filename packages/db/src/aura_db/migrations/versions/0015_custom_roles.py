"""Enterprise RBAC: custom roles, membership source

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_roles",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("description", sa.String(length=300), server_default="", nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.String(length=64)), nullable=False),
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
            name=op.f("fk_custom_roles_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_custom_roles")),
        sa.UniqueConstraint("account_id", "name", name="uq_custom_role_name"),
    )
    op.create_index(op.f("ix_custom_roles_account_id"), "custom_roles", ["account_id"])
    op.add_column("memberships", sa.Column("custom_role_id", sa.String(length=40), nullable=True))
    op.add_column(
        "memberships",
        sa.Column("source", sa.String(length=16), server_default="manual", nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_memberships_custom_role_id_custom_roles"),
        "memberships",
        "custom_roles",
        ["custom_role_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_memberships_custom_role_id"), "memberships", ["custom_role_id"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON custom_roles TO aura_app, aura_system")
    op.execute("ALTER TABLE custom_roles ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON custom_roles TO aura_app
          USING (account_id = current_setting('aura.account_id', true))
          WITH CHECK (account_id = current_setting('aura.account_id', true))
    """)
    op.execute(
        "CREATE POLICY system_access ON custom_roles TO aura_system USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_memberships_custom_role_id"), table_name="memberships")
    op.drop_constraint(
        op.f("fk_memberships_custom_role_id_custom_roles"), "memberships", type_="foreignkey"
    )
    op.drop_column("memberships", "source")
    op.drop_column("memberships", "custom_role_id")
    op.drop_index(op.f("ix_custom_roles_account_id"), table_name="custom_roles")
    op.drop_table("custom_roles")
