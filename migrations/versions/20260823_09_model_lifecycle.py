"""Add automatic model lifecycle registry and trained artifacts.

Revision ID: 20260823_09
Revises: 20260823_08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_09"
down_revision = "20260823_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id"), nullable=False),
        sa.Column("league", sa.String(length=8), nullable=False),
        sa.Column("feature_names", sa.JSON(), nullable=False),
        sa.Column("feature_means", sa.JSON(), nullable=False),
        sa.Column("feature_scales", sa.JSON(), nullable=False),
        sa.Column("win_intercept", sa.Float(), nullable=False),
        sa.Column("win_coefficients", sa.JSON(), nullable=False),
        sa.Column("home_run_intercept", sa.Float(), nullable=False),
        sa.Column("home_run_coefficients", sa.JSON(), nullable=False),
        sa.Column("away_run_intercept", sa.Float(), nullable=False),
        sa.Column("away_run_coefficients", sa.JSON(), nullable=False),
        sa.Column("training_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_sample_size", sa.Integer(), nullable=False),
        sa.Column("validation_metrics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("model_version_id"),
    )
    op.create_index("ix_model_artifacts_model_version_id", "model_artifacts", ["model_version_id"])
    op.create_index("ix_model_artifacts_league", "model_artifacts", ["league"])
    op.create_index("ix_model_artifacts_training_cutoff", "model_artifacts", ["training_cutoff"])
    op.create_index("ix_model_artifacts_created_at", "model_artifacts", ["created_at"])
    op.create_table(
        "model_registry",
        sa.Column("league", sa.String(length=8), primary_key=True),
        sa.Column("champion_model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id"), nullable=True),
        sa.Column("previous_model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id"), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=False),
    )
    op.create_table(
        "model_lifecycle_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league", sa.String(length=8), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("candidate_model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id"), nullable=True),
        sa.Column("champion_model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id"), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_model_lifecycle_events_league", "model_lifecycle_events", ["league"])
    op.create_index("ix_model_lifecycle_events_event_type", "model_lifecycle_events", ["event_type"])
    op.create_index("ix_model_lifecycle_events_created_at", "model_lifecycle_events", ["created_at"])
    if op.get_bind().dialect.name == "postgresql":
        for table in ("model_artifacts", "model_registry", "model_lifecycle_events"):
            op.execute(f"alter table {table} enable row level security")
            op.execute(f"revoke all on table {table} from anon, authenticated")


def downgrade() -> None:
    op.drop_table("model_lifecycle_events")
    op.drop_table("model_registry")
    op.drop_table("model_artifacts")
