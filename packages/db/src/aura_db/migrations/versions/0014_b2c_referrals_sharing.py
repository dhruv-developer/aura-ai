"""B2C: referral codes and shared optimization cards

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("referral_code", sa.String(length=16), nullable=True))
    op.create_unique_constraint(op.f("uq_users_referral_code"), "users", ["referral_code"])
    op.create_table(
        "shared_cards",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("execution_id", sa.String(length=40), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            name=op.f("fk_shared_cards_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shared_cards")),
        sa.UniqueConstraint("execution_id", name=op.f("uq_shared_cards_execution_id")),
    )
    op.create_index(op.f("ix_shared_cards_account_id"), "shared_cards", ["account_id"])
    # Tenant-owned like every other table; the public card view reads via the system role.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared_cards TO aura_app, aura_system")
    op.execute("ALTER TABLE shared_cards ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON shared_cards TO aura_app
          USING (account_id = current_setting('aura.account_id', true))
          WITH CHECK (account_id = current_setting('aura.account_id', true))
    """)
    op.execute(
        "CREATE POLICY system_access ON shared_cards TO aura_system USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_shared_cards_account_id"), table_name="shared_cards")
    op.drop_table("shared_cards")
    op.drop_constraint(op.f("uq_users_referral_code"), "users", type_="unique")
    op.drop_column("users", "referral_code")
