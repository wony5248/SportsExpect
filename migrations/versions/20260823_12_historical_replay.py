"""Separate live forecasts from historical replays and store post-game evaluations.

Revision ID: 20260823_12
Revises: 20260823_11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_12"
down_revision = "20260823_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    prediction_columns = {column["name"] for column in inspector.get_columns("predictions")}
    with op.batch_alter_table("predictions") as batch:
        if "origin" not in prediction_columns:
            batch.add_column(sa.Column("origin", sa.String(length=24), nullable=False,
                                       server_default="LIVE_PREGAME"))
        if "data_cutoff" not in prediction_columns:
            batch.add_column(sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=True))
        if "training_eligible" not in prediction_columns:
            batch.add_column(sa.Column("training_eligible", sa.Boolean(), nullable=False,
                                       server_default=sa.true()))
        if "leakage_audit" not in prediction_columns:
            batch.add_column(sa.Column("leakage_audit", sa.JSON(), nullable=False,
                                       server_default=sa.text("'{}'")))
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("predictions")}
    if "ix_predictions_origin" not in existing_indexes:
        op.create_index("ix_predictions_origin", "predictions", ["origin"])
    if "ix_predictions_data_cutoff" not in existing_indexes:
        op.create_index("ix_predictions_data_cutoff", "predictions", ["data_cutoff"])
    if "ix_predictions_training_eligible" not in existing_indexes:
        op.create_index("ix_predictions_training_eligible", "predictions", ["training_eligible"])

    result_columns = {column["name"] for column in sa.inspect(bind).get_columns("game_results")}
    if "innings" not in result_columns:
        with op.batch_alter_table("game_results") as batch:
            batch.add_column(sa.Column("innings", sa.JSON(), nullable=True))

    if "prediction_evaluations" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            "prediction_evaluations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("prediction_id", sa.Integer(), sa.ForeignKey("predictions.id"), nullable=False),
            sa.Column("game_id", sa.Integer(), sa.ForeignKey("games.id"), nullable=False),
            sa.Column("simulation_count", sa.Integer(), nullable=False),
            sa.Column("actual_score_count", sa.Integer(), nullable=False),
            sa.Column("actual_score_probability", sa.Float(), nullable=False),
            sa.Column("actual_outcome_count", sa.Integer(), nullable=False),
            sa.Column("actual_outcome_probability", sa.Float(), nullable=False),
            sa.Column("actual_total_count", sa.Integer(), nullable=False),
            sa.Column("actual_total_probability", sa.Float(), nullable=False),
            sa.Column("actual_margin_count", sa.Integer(), nullable=False),
            sa.Column("actual_margin_probability", sa.Float(), nullable=False),
            sa.Column("actual_inning_path_count", sa.Integer(), nullable=True),
            sa.Column("actual_inning_path_probability", sa.Float(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("prediction_id"),
        )
        op.create_index("ix_prediction_evaluations_prediction_id", "prediction_evaluations", ["prediction_id"])
        op.create_index("ix_prediction_evaluations_game_id", "prediction_evaluations", ["game_id"])
        op.create_index("ix_prediction_evaluations_evaluated_at", "prediction_evaluations", ["evaluated_at"])

    if bind.dialect.name == "postgresql":
        op.execute("alter table prediction_evaluations enable row level security")
        op.execute("revoke all on table prediction_evaluations from anon, authenticated")


def downgrade() -> None:
    op.drop_table("prediction_evaluations")
    with op.batch_alter_table("game_results") as batch:
        batch.drop_column("innings")
    with op.batch_alter_table("predictions") as batch:
        batch.drop_column("leakage_audit")
        batch.drop_column("training_eligible")
        batch.drop_column("data_cutoff")
        batch.drop_column("origin")
