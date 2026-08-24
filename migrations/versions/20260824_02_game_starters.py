"""Store archived game starters with their strictly-prior season totals.

The replay drops any starter record collected after first pitch, which is correct but left every
starter feature constant across the historical archive: 35 of 51 trainable inputs had zero
variance, so the trainer could not learn from them at all.

Revision ID: 20260824_02
Revises: 20260824_01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_02"
down_revision = "20260824_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "game_starters" in set(sa.inspect(op.get_bind()).get_table_names()):
        _secure_backend_tables()
        return
    op.create_table(
        "game_starters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("games.id"), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("player_id", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=True),
        sa.Column("prior_games", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_starts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_innings", sa.Float(), nullable=False, server_default="0"),
        sa.Column("prior_earned_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_walks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_strikeouts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_home_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_quality_starts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("game_id", "side"),
    )
    op.create_index("ix_game_starters_game_id", "game_starters", ["game_id"])
    op.create_index("ix_game_starters_player_id", "game_starters", ["player_id"])
    _secure_backend_tables()


def _secure_backend_tables() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("alter table game_starters enable row level security")
        op.execute("revoke all on table game_starters from anon, authenticated")


def downgrade() -> None:
    op.drop_table("game_starters")
