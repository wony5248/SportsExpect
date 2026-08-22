"""Store the selected Claude model with the encrypted provider key.

Revision ID: 20260822_05
Revises: 20260822_04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260822_05"
down_revision = "20260822_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("runtime_secrets")}
    if "model" not in columns:
        op.add_column("runtime_secrets", sa.Column("model", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("runtime_secrets", "model")
