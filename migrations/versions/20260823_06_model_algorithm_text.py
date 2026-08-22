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
    algorithm = next(
        column for column in sa.inspect(op.get_bind()).get_columns("model_versions")
        if column["name"] == "algorithm"
    )
    if isinstance(algorithm["type"], sa.Text) and getattr(algorithm["type"], "length", None) is None:
        return
    with op.batch_alter_table("model_versions") as batch_op:
        batch_op.alter_column(
            "algorithm",
            existing_type=sa.String(length=120),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("model_versions") as batch_op:
        batch_op.alter_column(
            "algorithm",
            existing_type=sa.Text(),
            type_=sa.String(length=120),
            existing_nullable=False,
        )
