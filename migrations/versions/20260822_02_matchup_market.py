"""Add opponent matchup and optional market consensus data.

Revision ID: 20260822_02
Revises: 20260822_01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260822_02"
down_revision = "20260822_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    pitcher_columns = {column["name"] for column in inspector.get_columns("pitcher_stats")}
    pitcher_additions = {
        "opponent_games": sa.Integer(), "opponent_innings": sa.Float(),
        "opponent_era": sa.Float(), "opponent_whip": sa.Float(),
    }
    with op.batch_alter_table("pitcher_stats") as batch:
        for name, column_type in pitcher_additions.items():
            if name not in pitcher_columns:
                batch.add_column(sa.Column(name, column_type, nullable=True))
    lineup_columns = {column["name"] for column in inspector.get_columns("lineups")}
    lineup_additions = {
        "opponent_pitcher_id": sa.String(length=20), "matchup_plate_appearances": sa.Integer(),
        "matchup_at_bats": sa.Integer(), "matchup_hits": sa.Integer(), "matchup_doubles": sa.Integer(),
        "matchup_triples": sa.Integer(), "matchup_home_runs": sa.Integer(), "matchup_walks": sa.Integer(),
        "matchup_hit_by_pitch": sa.Integer(), "matchup_strikeouts": sa.Integer(),
        "matchup_avg": sa.Float(), "matchup_obp": sa.Float(), "matchup_slg": sa.Float(), "matchup_ops": sa.Float(),
    }
    with op.batch_alter_table("lineups") as batch:
        for name, column_type in lineup_additions.items():
            if name not in lineup_columns:
                batch.add_column(sa.Column(name, column_type, nullable=True))
    if "market_consensus" in inspector.get_table_names():
        return
    op.create_table(
        "market_consensus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("games.id"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("external_event_id", sa.String(length=80), nullable=True),
        sa.Column("bookmaker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_line", sa.Float(), nullable=True),
        sa.Column("home_spread", sa.Float(), nullable=True),
        sa.Column("home_implied_probability", sa.Float(), nullable=True),
        sa.Column("away_implied_probability", sa.Float(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("game_id", "provider"),
    )
    op.create_index("ix_market_consensus_game_id", "market_consensus", ["game_id"])
    op.create_index("ix_market_consensus_collected_at", "market_consensus", ["collected_at"])


def downgrade() -> None:
    op.drop_table("market_consensus")
    with op.batch_alter_table("lineups") as batch:
        for name in ("matchup_ops", "matchup_slg", "matchup_obp", "matchup_avg", "matchup_strikeouts",
                     "matchup_hit_by_pitch", "matchup_walks", "matchup_home_runs", "matchup_triples",
                     "matchup_doubles", "matchup_hits", "matchup_at_bats",
                     "matchup_plate_appearances", "opponent_pitcher_id"):
            batch.drop_column(name)
    with op.batch_alter_table("pitcher_stats") as batch:
        for name in ("opponent_whip", "opponent_era", "opponent_innings", "opponent_games"):
            batch.drop_column(name)
