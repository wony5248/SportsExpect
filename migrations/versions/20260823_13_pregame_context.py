"""Store official pregame context and advanced matchup inputs.

Revision ID: 20260823_13
Revises: 20260823_12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_13"
down_revision = "20260823_12"
branch_labels = None
depends_on = None


def _add(table: str, name: str, column: sa.Column) -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if name not in existing:
        with op.batch_alter_table(table) as batch:
            batch.add_column(column)


def upgrade() -> None:
    _add("games", "pregame_context", sa.Column("pregame_context", sa.JSON(), nullable=False,
                                                  server_default=sa.text("'{}'")))
    _add("games", "context_collected_at", sa.Column("context_collected_at", sa.DateTime(timezone=True), nullable=True))
    _add("team_stats", "advanced", sa.Column("advanced", sa.JSON(), nullable=False,
                                                server_default=sa.text("'{}'")))
    _add("pitcher_stats", "recent", sa.Column("recent", sa.JSON(), nullable=False,
                                                 server_default=sa.text("'{}'")))
    _add("lineups", "batting_side", sa.Column("batting_side", sa.String(length=4), nullable=True))
    _add("lineups", "platoon_opponent_hand", sa.Column("platoon_opponent_hand", sa.String(length=4), nullable=True))
    _add("lineups", "platoon_plate_appearances", sa.Column("platoon_plate_appearances", sa.Integer(), nullable=True))
    _add("lineups", "platoon_ops", sa.Column("platoon_ops", sa.Float(), nullable=True))


def downgrade() -> None:
    for table, columns in (
        ("lineups", ("platoon_ops", "platoon_plate_appearances", "platoon_opponent_hand", "batting_side")),
        ("pitcher_stats", ("recent",)), ("team_stats", ("advanced",)),
        ("games", ("context_collected_at", "pregame_context")),
    ):
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.drop_column(column)
