"""Add per-team bullpen leverage profiles and their change history.

Revision ID: 20260823_10
Revises: 20260823_09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_10"
down_revision = "20260823_09"
branch_labels = None
depends_on = None

TABLES = ("team_bullpen", "team_bullpen_events")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    # Revision 01 creates the current metadata wholesale for a brand-new local SQLite
    # database, so these tables can already exist by the time Alembic reaches 10.
    if set(TABLES).issubset(existing):
        _secure_backend_tables()
        return
    partial = set(TABLES) & existing
    if partial:
        raise RuntimeError(f"Incomplete bullpen schema already exists: {sorted(partial)}")
    op.create_table(
        "team_bullpen",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("high_leverage_multiplier", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("middle_multiplier", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("chase_multiplier", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("mop_up_multiplier", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("high_leverage_arms", sa.JSON(), nullable=False),
        sa.Column("middle_arms", sa.JSON(), nullable=False),
        sa.Column("chase_arms", sa.JSON(), nullable=False),
        sa.Column("mop_up_arms", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="DERIVED"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("team_id"),
    )
    op.create_index("ix_team_bullpen_team_id", "team_bullpen", ["team_id"])
    op.create_table(
        "team_bullpen_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_team_bullpen_events_team_id", "team_bullpen_events", ["team_id"])
    op.create_index("ix_team_bullpen_events_created_at", "team_bullpen_events", ["created_at"])
    _secure_backend_tables()


def _secure_backend_tables() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"alter table {table} enable row level security")
            op.execute(f"revoke all on table {table} from anon, authenticated")


def downgrade() -> None:
    op.drop_table("team_bullpen_events")
    op.drop_table("team_bullpen")
