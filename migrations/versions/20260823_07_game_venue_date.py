"""Store the official venue-local game date alongside the KST service date.

Revision ID: 20260823_07
Revises: 20260823_06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_07"
down_revision = "20260823_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("games")}
    if "venue_date" not in columns:
        op.add_column("games", sa.Column("venue_date", sa.Date(), nullable=True))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("games")}
    if "ix_games_venue_date" not in indexes:
        op.create_index("ix_games_venue_date", "games", ["venue_date"], unique=False)
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""
            update games
            set venue_date = case when league = 'MLB' then date(game_date, '-1 day') else game_date end
            where venue_date is null
        """)
    else:
        op.execute("""
            update games
            set venue_date = case when league = 'MLB' then game_date - interval '1 day' else game_date end
            where venue_date is null
        """)


def downgrade() -> None:
    op.drop_index("ix_games_venue_date", table_name="games")
    op.drop_column("games", "venue_date")
