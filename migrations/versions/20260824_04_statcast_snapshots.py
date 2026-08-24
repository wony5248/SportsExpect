"""Add advanced pregame player snapshots.

Revision ID: 20260824_04
Revises: 20260824_03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_04"
down_revision = "20260824_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("pitcher_stats", "lineups"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if "advanced" not in columns:
            op.add_column(table, sa.Column("advanced", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("lineups", "advanced")
    op.drop_column("pitcher_stats", "advanced")
