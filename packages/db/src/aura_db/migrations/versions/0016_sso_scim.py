"""Enterprise identity: verified domains, SSO connections, SCIM tokens

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("verified_domains", "sso_connections", "scim_tokens")


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


def _account_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["account_id"],
        ["accounts.id"],
        name=op.f(f"fk_{table}_account_id_accounts"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "verified_domains",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        _account_fk("verified_domains"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verified_domains")),
        sa.UniqueConstraint("domain", name=op.f("uq_verified_domains_domain")),
    )
    op.create_table(
        "sso_connections",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=12), server_default="draft", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("default_role", sa.String(length=20), server_default="viewer", nullable=False),
        sa.Column("jit_provisioning", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("enforced", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=True),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        *_ts(),
        _account_fk("sso_connections"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sso_connections")),
    )
    op.create_table(
        "scim_tokens",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        _account_fk("scim_tokens"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scim_tokens")),
        sa.UniqueConstraint("prefix", name=op.f("uq_scim_tokens_prefix")),
    )
    for table in TABLES:
        op.create_index(op.f(f"ix_{table}_account_id"), table, ["account_id"])
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO aura_app, aura_system")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table} TO aura_app
              USING (account_id = current_setting('aura.account_id', true))
              WITH CHECK (account_id = current_setting('aura.account_id', true))
        """)
        op.execute(
            f"CREATE POLICY system_access ON {table} TO aura_system USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_index(op.f(f"ix_{table}_account_id"), table_name=table)
        op.drop_table(table)
