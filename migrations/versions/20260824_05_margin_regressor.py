"""Add independently trained signed run-margin coefficients.

Revision ID: 20260824_05
Revises: 20260824_04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_05"
down_revision = "20260824_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("model_artifacts")}
    if "margin_intercept" not in columns:
        op.add_column("model_artifacts", sa.Column("margin_intercept", sa.Float(), nullable=True))
    if "margin_coefficients" not in columns:
        op.add_column("model_artifacts", sa.Column("margin_coefficients", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_artifacts", "margin_coefficients")
    op.drop_column("model_artifacts", "margin_intercept")
