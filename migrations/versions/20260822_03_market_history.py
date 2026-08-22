"""Add immutable market snapshots for pregame baseline evaluation.

Revision ID: 20260822_03
Revises: 20260822_02
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260822_03"
down_revision = "20260822_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "market_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("games.id"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("bookmaker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_line", sa.Float(), nullable=True),
        sa.Column("home_implied_probability", sa.Float(), nullable=True),
        sa.Column("away_implied_probability", sa.Float(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_market_snapshots_game_id", "market_snapshots", ["game_id"])
    op.create_index("ix_market_snapshots_collected_at", "market_snapshots", ["collected_at"])
    op.create_index("ix_market_snapshot_game_collected", "market_snapshots", ["game_id", "collected_at"])


def downgrade() -> None:
    op.drop_table("market_snapshots")
