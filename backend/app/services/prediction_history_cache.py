from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.database import SessionLocal
from backend.app.models import (Game, GameResult, Prediction, PredictionContextCache,
                                PredictionSnapshot)
from backend.app.services.market_offset import MarketOffsetHistory, MarketOffsetObservation
from backend.app.services.operations import job_lock
from backend.app.services.probability_calibration import (
    LeagueProbabilityCalibrationHistory,
    ProbabilityObservation,
)
from backend.app.services.team_residuals import ResidualObservation, TeamResidualHistory


# Increment whenever the compact representation or any history-building policy changes. The
# source fingerprint handles data changes; this handles code changes between deployments.
HISTORY_CACHE_ALGORITHM_VERSION = 1


def load_prediction_histories(
    league: str,
) -> tuple[TeamResidualHistory, LeagueProbabilityCalibrationHistory, MarketOffsetHistory]:
    """Load one immutable league history snapshot shared by concurrent prediction workers.

    A blocking advisory lock prevents a manual refresh from making every game rebuild the same
    walk-forward calibration simultaneously. The cache transaction commits before the lock is
    released, so the next worker always sees the completed snapshot.
    """
    try:
        with job_lock(f"prediction-history-cache:{league}", blocking=True):
            with SessionLocal() as session:
                fingerprint = _source_fingerprint(session, league)
                cached = session.get(PredictionContextCache, league)
                if (cached is not None
                        and cached.algorithm_version == HISTORY_CACHE_ALGORITHM_VERSION
                        and cached.fingerprint == fingerprint):
                    return _deserialize(cached.payload)

                histories = _build(session, league)
                payload = _serialize(*histories)
                if cached is None:
                    cached = PredictionContextCache(
                        league=league,
                        fingerprint=fingerprint,
                        algorithm_version=HISTORY_CACHE_ALGORITHM_VERSION,
                        payload=payload,
                    )
                    session.add(cached)
                else:
                    cached.fingerprint = fingerprint
                    cached.algorithm_version = HISTORY_CACHE_ALGORITHM_VERSION
                    cached.payload = payload
                    cached.updated_at = datetime.now().astimezone()
                session.commit()
                return histories
    except SQLAlchemyError:
        # Deployments remain usable while the additive cache migration is rolling out. The
        # lightweight projection queries still avoid loading full simulation payloads.
        with SessionLocal() as session:
            return _build(session, league)


def _build(
    session: Any, league: str,
) -> tuple[TeamResidualHistory, LeagueProbabilityCalibrationHistory, MarketOffsetHistory]:
    return (
        TeamResidualHistory.from_session(session, league),
        LeagueProbabilityCalibrationHistory.from_session(session, league),
        MarketOffsetHistory.from_session(session, league),
    )


def _source_fingerprint(session: Any, league: str) -> str:
    """Hash every small field that can affect a history selection or outcome."""
    predictions = session.execute(
        select(
            Prediction.id, Prediction.game_id, Prediction.input_hash, Prediction.origin,
            Prediction.data_cutoff, Prediction.training_eligible, Prediction.leakage_audit,
            Prediction.created_at,
        )
        .join(Game, Game.id == Prediction.game_id)
        .join(GameResult, GameResult.game_id == Game.id)
        .where(Game.league == league)
        .order_by(Prediction.id)
    ).all()
    results = session.execute(
        select(
            Game.id, Game.status, Game.game_date, Game.start_at,
            Game.home_team_id, Game.away_team_id,
            GameResult.game_id.label("result_game_id"), GameResult.home_score,
            GameResult.away_score, GameResult.finalized_at,
        )
        .join(GameResult, GameResult.game_id == Game.id)
        .where(Game.league == league)
        .order_by(Game.id)
    ).all()
    snapshots = session.execute(
        select(PredictionSnapshot.id, PredictionSnapshot.prediction_id,
               PredictionSnapshot.stage, PredictionSnapshot.captured_at)
        .join(Prediction, Prediction.id == PredictionSnapshot.prediction_id)
        .join(Game, Game.id == Prediction.game_id)
        .join(GameResult, GameResult.game_id == Game.id)
        .where(Game.league == league)
        .order_by(PredictionSnapshot.id)
    ).all()
    encoded = json.dumps(
        {"predictions": predictions, "results": results, "snapshots": snapshots},
        default=_json_default, sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _serialize(residual: TeamResidualHistory,
               probability: LeagueProbabilityCalibrationHistory,
               market: MarketOffsetHistory) -> dict[str, Any]:
    payload = {
        "residual": [asdict(row) for row in residual.observations],
        "probability": {
            "observations": [asdict(row) for row in probability.observations],
            "validation": probability.validation,
        },
        "market": {
            "observations": [asdict(row) for row in market.observations],
            "validation": market.validation,
        },
    }
    # SQLAlchemy's JSON serializer does not accept datetime/date objects. Round-trip only this
    # compact payload through JSON so the database representation is portable across SQLite and
    # PostgreSQL; deserialization restores the timestamp fields used by cutoff comparisons.
    return json.loads(json.dumps(payload, default=_json_default))


def _deserialize(payload: dict[str, Any]) -> tuple[
    TeamResidualHistory, LeagueProbabilityCalibrationHistory, MarketOffsetHistory,
]:
    residual = TeamResidualHistory([
        ResidualObservation(**_restore_datetimes(row, ("started_at", "finalized_at")))
        for row in payload.get("residual", [])
    ])
    probability_payload = payload.get("probability") or {}
    probability = LeagueProbabilityCalibrationHistory([
        ProbabilityObservation(**_restore_datetimes(row, ("available_at",)))
        for row in probability_payload.get("observations", [])
    ], probability_payload.get("validation"))
    market_payload = payload.get("market") or {}
    market = MarketOffsetHistory([
        MarketOffsetObservation(**_restore_datetimes(row, ("available_at",)))
        for row in market_payload.get("observations", [])
    ], market_payload.get("validation"))
    return residual, probability, market


def _restore_datetimes(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    restored = dict(row)
    for field in fields:
        value = restored.get(field)
        if isinstance(value, str):
            restored[field] = datetime.fromisoformat(value)
    return restored


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime) or hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "_mapping"):
        return list(value)
    raise TypeError(f"Unsupported fingerprint value: {type(value).__name__}")
