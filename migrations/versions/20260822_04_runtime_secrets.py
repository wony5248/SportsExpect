"""Add encrypted runtime provider secrets.

Revision ID: 20260822_04
Revises: 20260822_03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260822_04"
down_revision = "20260822_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "runtime_secrets" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "runtime_secrets",
        sa.Column("name", sa.String(length=64), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_secrets")
