"""Normalize archived starter K-BB and FIP inputs.

Revision ID: 20260824_03
Revises: 20260824_02
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_03"
down_revision = "20260824_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("game_starters")}
    if "prior_hit_batters" not in columns:
        op.add_column("game_starters", sa.Column("prior_hit_batters", sa.Integer(), nullable=False,
                                                   server_default="0"))
    if "prior_batters_faced" not in columns:
        op.add_column("game_starters", sa.Column("prior_batters_faced", sa.Integer(), nullable=False,
                                                   server_default="0"))
    if "metric_schema_version" not in columns:
        op.add_column("game_starters", sa.Column("metric_schema_version", sa.Integer(), nullable=False,
                                                   server_default="1"))


def downgrade() -> None:
    op.drop_column("game_starters", "metric_schema_version")
    op.drop_column("game_starters", "prior_batters_faced")
    op.drop_column("game_starters", "prior_hit_batters")
