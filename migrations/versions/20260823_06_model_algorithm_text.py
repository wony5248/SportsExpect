"""Allow complete model algorithm descriptions.

Revision ID: 20260823_06
Revises: 20260822_05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_06"
down_revision = "20260822_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "model_versions",
        "algorithm",
        existing_type=sa.String(length=120),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "model_versions",
        "algorithm",
        existing_type=sa.Text(),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
