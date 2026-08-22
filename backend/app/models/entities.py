from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.base import Base


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("league", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league: Mapped[str] = mapped_column(String(8), index=True)
    code: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    league: Mapped[str] = mapped_column(String(8), index=True)
    game_date: Mapped[date] = mapped_column(Date, index=True)
    # Service grouping date is always KST. For MLB, venue_date preserves the
    # official US venue-local schedule date shown by MLB.
    venue_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    stadium: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id])
    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id])


class TeamStat(Base):
    __tablename__ = "team_stats"
    __table_args__ = (UniqueConstraint("team_id", "effective_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    games: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    draws: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.5)
    home_win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    runs_per_game: Mapped[float | None] = mapped_column(Float, nullable=True)
    runs_allowed_per_game: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    obp: Mapped[float | None] = mapped_column(Float, nullable=True)
    slg: Mapped[float | None] = mapped_column(Float, nullable=True)
    ops: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    walks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strikeouts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    era: Mapped[float | None] = mapped_column(Float, nullable=True)
    whip: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    team: Mapped[Team] = relationship()


class PitcherStat(Base):
    __tablename__ = "pitcher_stats"
    __table_args__ = (UniqueConstraint("game_id", "side"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    side: Mapped[str] = mapped_column(String(4))
    player_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confirmed: Mapped[bool] = mapped_column(default=False)
    era: Mapped[float | None] = mapped_column(Float, nullable=True)
    whip: Mapped[float | None] = mapped_column(Float, nullable=True)
    war: Mapped[float | None] = mapped_column(Float, nullable=True)
    games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_start_innings: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_starts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fip: Mapped[float | None] = mapped_column(Float, nullable=True)
    k_bb_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    rest_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recent_pitches: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handedness: Mapped[str | None] = mapped_column(String(4), nullable=True)
    opponent_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opponent_innings: Mapped[float | None] = mapped_column(Float, nullable=True)
    opponent_era: Mapped[float | None] = mapped_column(Float, nullable=True)
    opponent_whip: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LineupEntry(Base):
    __tablename__ = "lineups"
    __table_args__ = (UniqueConstraint("game_id", "side", "batting_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    side: Mapped[str] = mapped_column(String(4))
    batting_order: Mapped[int] = mapped_column(Integer)
    player_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    player_name: Mapped[str] = mapped_column(String(80))
    position: Mapped[str | None] = mapped_column(String(24), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_metric: Mapped[str | None] = mapped_column(String(12), nullable=True)
    opponent_pitcher_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    matchup_plate_appearances: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_at_bats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_hits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_doubles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_triples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_home_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_walks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_hit_by_pitch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_strikeouts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matchup_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    matchup_obp: Mapped[float | None] = mapped_column(Float, nullable=True)
    matchup_slg: Mapped[float | None] = mapped_column(Float, nullable=True)
    matchup_ops: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GameResult(Base):
    __tablename__ = "game_results"

    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), primary_key=True)
    away_score: Mapped[int] = mapped_column(Integer)
    home_score: Mapped[int] = mapped_column(Integer)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str] = mapped_column(Text)


class MarketConsensus(Base):
    """Latest normalized market snapshot; market prices are comparison data, never a model target."""

    __tablename__ = "market_consensus"
    __table_args__ = (UniqueConstraint("game_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    external_event_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bookmaker_count: Mapped[int] = mapped_column(Integer, default=0)
    total_line: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_implied_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_implied_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_url: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MarketSnapshot(Base):
    """Immutable normalized market observation for pregame baseline evaluation."""

    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshot_game_collected", "game_id", "collected_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    bookmaker_count: Mapped[int] = mapped_column(Integer, default=0)
    total_line: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_implied_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_implied_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_url: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # Keep the complete, auditable model description. V10 already exceeds the
    # former 120-character limit and PostgreSQL correctly rejected the insert.
    algorithm: Mapped[str] = mapped_column(Text)
    feature_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ModelArtifact(Base):
    """Immutable, database-backed parameters produced by an automatic training run."""

    __tablename__ = "model_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"), unique=True, index=True)
    league: Mapped[str] = mapped_column(String(8), index=True)
    feature_names: Mapped[list[str]] = mapped_column(JSON)
    feature_means: Mapped[list[float]] = mapped_column(JSON)
    feature_scales: Mapped[list[float]] = mapped_column(JSON)
    win_intercept: Mapped[float] = mapped_column(Float)
    win_coefficients: Mapped[list[float]] = mapped_column(JSON)
    home_run_intercept: Mapped[float] = mapped_column(Float)
    home_run_coefficients: Mapped[list[float]] = mapped_column(JSON)
    away_run_intercept: Mapped[float] = mapped_column(Float)
    away_run_coefficients: Mapped[list[float]] = mapped_column(JSON)
    training_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    training_sample_size: Mapped[int] = mapped_column(Integer)
    validation_metrics: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    model_version: Mapped[ModelVersion] = relationship()


class ModelRegistry(Base):
    """One atomic champion pointer per league; the previous pointer enables rollback."""

    __tablename__ = "model_registry"

    league: Mapped[str] = mapped_column(String(8), primary_key=True)
    champion_model_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_versions.id"), nullable=True,
    )
    previous_model_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_versions.id"), nullable=True,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModelLifecycleEvent(Base):
    """Auditable training, promotion, rejection, and rollback decision."""

    __tablename__ = "model_lifecycle_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    league: Mapped[str] = mapped_column(String(8), index=True)
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    candidate_model_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_versions.id"), nullable=True,
    )
    champion_model_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_versions.id"), nullable=True,
    )
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    home_win_probability: Mapped[float] = mapped_column(Float)
    away_win_probability: Mapped[float] = mapped_column(Float)
    home_expected_runs: Mapped[float] = mapped_column(Float)
    away_expected_runs: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    model_version: Mapped[ModelVersion] = relationship()


class PredictionHistory(Base):
    __tablename__ = "prediction_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


class PredictionSnapshot(Base):
    """Immutable pre-game input captured at a meaningful collection point."""

    __tablename__ = "prediction_snapshots"
    __table_args__ = (
        Index("ix_prediction_snapshot_game_captured", "game_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(24), index=True)
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    minutes_to_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)


class RuntimeSecret(Base):
    """Encrypted provider credential managed through the administrator UI."""

    __tablename__ = "runtime_secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class UserClaudeSetting(Base):
    """One encrypted Claude credential per authenticated Supabase user."""

    __tablename__ = "user_claude_settings"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class CrawlLog(Base):
    __tablename__ = "crawl_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    collector: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(16))
    source_url: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
