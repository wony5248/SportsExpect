"""Add per-batter base-state splits for the plate-appearance engine.

Revision ID: 20260823_11
Revises: 20260823_10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_11"
down_revision = "20260823_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "batter_splits" in set(sa.inspect(op.get_bind()).get_table_names()):
        # Revision 01 creates the current metadata wholesale for a new local SQLite database.
        _secure_backend_tables()
        return
    op.create_table(
        "batter_splits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league", sa.String(length=8), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.String(length=24), nullable=False),
        sa.Column("player_name", sa.String(length=80), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("at_bats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("doubles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("triples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("home_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("walks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hit_by_pitch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("strikeouts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sacrifice_flies", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("grounded_into_double_play", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("league", "season", "player_id", "state"),
    )
    op.create_index("ix_batter_splits_league", "batter_splits", ["league"])
    op.create_index("ix_batter_splits_season", "batter_splits", ["season"])
    op.create_index("ix_batter_splits_player_id", "batter_splits", ["player_id"])
    _secure_backend_tables()


def _secure_backend_tables() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("alter table batter_splits enable row level security")
        op.execute("revoke all on table batter_splits from anon, authenticated")


def downgrade() -> None:
    op.drop_table("batter_splits")
