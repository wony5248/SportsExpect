"""Widen model_lifecycle_events.event_type: WAITING_FOR_LIVE_VALIDATION (27 chars) overflowed
varchar(24) and crashed every lifecycle evaluation for a league without enough live samples.

Revision ID: 20260824_01
Revises: 20260823_13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_01"
down_revision = "20260823_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite has no real column-width enforcement and no native ALTER COLUMN TYPE; only
    # Postgres (Supabase) needs the actual widen.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("model_lifecycle_events", "event_type", type_=sa.String(length=40))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("model_lifecycle_events", "event_type", type_=sa.String(length=24))
