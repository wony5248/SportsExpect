"""Cache compact league prediction histories.

Revision ID: 20260902_01
Revises: 20260824_05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260902_01"
down_revision = "20260824_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "prediction_context_cache" not in inspector.get_table_names():
        op.create_table(
            "prediction_context_cache",
            sa.Column("league", sa.String(length=8), primary_key=True),
            sa.Column("fingerprint", sa.String(length=128), nullable=False),
            sa.Column("algorithm_version", sa.Integer(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_prediction_context_cache_fingerprint", "prediction_context_cache", ["fingerprint"],
        )


def downgrade() -> None:
    if "prediction_context_cache" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_prediction_context_cache_fingerprint", table_name="prediction_context_cache")
        op.drop_table("prediction_context_cache")
