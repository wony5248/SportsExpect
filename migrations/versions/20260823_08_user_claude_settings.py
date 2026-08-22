"""Store one encrypted Claude setting per Supabase user.

Revision ID: 20260823_08
Revises: 20260823_07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_08"
down_revision = "20260823_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "user_claude_settings" not in tables:
        op.create_table(
            "user_claude_settings",
            sa.Column("user_id", sa.String(length=64), primary_key=True),
            sa.Column("ciphertext", sa.Text(), nullable=False),
            sa.Column("fingerprint", sa.String(length=16), nullable=False),
            sa.Column("model", sa.String(length=80), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if op.get_bind().dialect.name == "postgresql":
        # These ciphertext rows are backend-only. Users reach them exclusively through
        # authenticated API endpoints, never through Supabase's public PostgREST API.
        op.execute("alter table user_claude_settings enable row level security")
        op.execute("revoke all on table user_claude_settings from anon, authenticated")
        if "runtime_secrets" in tables:
            op.execute("alter table runtime_secrets enable row level security")
            op.execute("revoke all on table runtime_secrets from anon, authenticated")


def downgrade() -> None:
    op.drop_table("user_claude_settings")
