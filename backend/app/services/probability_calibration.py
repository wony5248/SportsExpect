from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Game, GameResult, Prediction


MIN_CALIBRATION_SAMPLES = 30
MAX_CALIBRATION_SAMPLES = 1000
CALIBRATION_METHOD = "LEAGUE_WALK_FORWARD_PLATT_V2_IRLS"
PLATT_L2_REGULARIZATION = 4.0
# A calibrator is promoted per league only when chronological replay improves the proper scoring
# rules. KBO passed on 555 games; MLB's 1,963-game replay worsened both Brier and log loss, so its
# fitted map remains observable but cannot alter production probabilities yet.
CALIBRATION_ENABLED_LEAGUES = {"KBO"}
CALIBRATION_VALIDATION = {
    "KBO": {"status": "PASS", "sample_count": 555, "brier_delta": -.00014, "log_loss_delta": -.00029},
    "MLB": {"status": "HOLD", "sample_count": 1963, "brier_delta": .00023, "log_loss_delta": .00050},
}


@dataclass(frozen=True)
class ProbabilityObservation:
    game_id: int
    season: int
    available_at: datetime
    probability: float
    outcome: float


class LeagueProbabilityCalibrationHistory:
    """League-specific pregame probabilities paired only with already-final outcomes."""

    def __init__(self, observations: list[ProbabilityObservation]):
        self.observations = sorted(observations, key=lambda row: (row.available_at, row.game_id))

    @classmethod
    def from_session(cls, session: Session, league: str) -> LeagueProbabilityCalibrationHistory:
        rows = session.execute(
            select(Prediction, Game, GameResult)
            .join(Game, Game.id == Prediction.game_id)
            .join(GameResult, GameResult.game_id == Game.id)
            .where(Game.league == league, Game.start_at.is_not(None))
            .order_by(Game.start_at, Prediction.created_at)
        ).all()
        by_game: dict[int, tuple[Prediction, Game, GameResult]] = {}
        for prediction, game, result in rows:
            cutoff = prediction.data_cutoff or prediction.created_at
            if _naive(cutoff) > _naive(game.start_at):
                continue
            if prediction.origin == "HISTORICAL_REPLAY" and (
                not prediction.training_eligible
                or not bool((prediction.leakage_audit or {}).get("passed"))
            ):
                continue
            current = by_game.get(game.id)
            if current is None or _prefer(prediction, current[0]):
                by_game[game.id] = (prediction, game, result)

        observations = []
        for prediction, game, result in by_game.values():
            if result.home_score == result.away_score:
                # The production KBO market is two-way with ties excluded.
                continue
            payload = prediction.payload or {}
            raw_probability = payload.get("raw_simulation_home_probability")
            if raw_probability is None:
                raw_probability = payload.get("simulation_home_probability")
            if raw_probability is None:
                raw_probability = prediction.home_win_probability
            observations.append(ProbabilityObservation(
                game_id=game.id,
                season=game.game_date.year,
                available_at=result.finalized_at,
                probability=_clip(float(raw_probability), .001, .999),
                outcome=1.0 if result.home_score > result.away_score else 0.0,
            ))
        return cls(observations)

    def context_for(self, game: Game) -> dict[str, Any]:
        if game.start_at is None:
            return identity_calibration("GAME_TIME_UNCONFIRMED")
        rows = [
            row for row in self.observations
            if row.season == game.game_date.year
            and row.game_id != game.id
            and _naive(row.available_at) <= _naive(game.start_at)
        ][-MAX_CALIBRATION_SAMPLES:]
        validation = CALIBRATION_VALIDATION.get(game.league, {"status": "HOLD"})
        if game.league not in CALIBRATION_ENABLED_LEAGUES:
            context = identity_calibration("WALK_FORWARD_VALIDATION_HOLD", len(rows))
            context["validation"] = validation
            return context
        if len(rows) < MIN_CALIBRATION_SAMPLES:
            context = identity_calibration("INSUFFICIENT_PRIOR_FINALS", len(rows))
            context["validation"] = validation
            return context
        slope, intercept = fit_platt([(row.probability, row.outcome) for row in rows])
        return {
            "enabled": True,
            "method": CALIBRATION_METHOD,
            "sample_count": len(rows),
            "minimum_samples": MIN_CALIBRATION_SAMPLES,
            "slope": round(slope, 8),
            "intercept": round(intercept, 8),
            "data_cutoff": max(row.available_at for row in rows).isoformat(),
            "target_game_id": game.external_id,
            "future_results_used": 0,
            "validation": validation,
        }


def identity_calibration(reason: str, sample_count: int = 0) -> dict[str, Any]:
    return {
        "enabled": False,
        "method": CALIBRATION_METHOD,
        "sample_count": sample_count,
        "minimum_samples": MIN_CALIBRATION_SAMPLES,
        "slope": 1.0,
        "intercept": 0.0,
        "reason": reason,
        "future_results_used": 0,
    }


def fit_platt(history: list[tuple[float, float]]) -> tuple[float, float]:
    """Fit a regularized two-parameter Platt map with Newton/IRLS updates.

    The former report implementation divided its learning rate by sqrt(n) and its gradient by
    n a second time. At production sample sizes it barely left the identity map, even when 64%
    forecasts won only half the time. This two-variable Hessian is cheap, converges fully, and
    the prior around slope=1/intercept=0 keeps small samples conservative.
    """
    if not history:
        return 1.0, 0.0
    slope, intercept = 1.0, 0.0
    regularization = PLATT_L2_REGULARIZATION
    for _ in range(40):
        gradient_slope = regularization * (slope - 1.0)
        gradient_intercept = regularization * intercept
        hessian_slope = regularization
        hessian_cross = 0.0
        hessian_intercept = regularization
        for probability, outcome in history:
            log_odds = _logit(probability)
            estimate = _sigmoid(slope * log_odds + intercept)
            gradient_slope += (estimate - outcome) * log_odds
            gradient_intercept += estimate - outcome
            weight = max(1e-6, estimate * (1 - estimate))
            hessian_slope += weight * log_odds * log_odds
            hessian_cross += weight * log_odds
            hessian_intercept += weight
        determinant = hessian_slope * hessian_intercept - hessian_cross * hessian_cross
        if determinant <= 1e-12:
            break
        slope_step = (
            hessian_intercept * gradient_slope - hessian_cross * gradient_intercept
        ) / determinant
        intercept_step = (
            hessian_slope * gradient_intercept - hessian_cross * gradient_slope
        ) / determinant
        slope = _clip(slope - slope_step, .25, 2.5)
        intercept = _clip(intercept - intercept_step, -1.5, 1.5)
        if max(abs(slope_step), abs(intercept_step)) < 1e-8:
            break
    return float(_clip(slope, .25, 2.5)), float(_clip(intercept, -1.5, 1.5))


def calibrated_probability(probability: float, context: dict[str, Any] | None) -> float:
    if not context or not context.get("enabled"):
        return _clip(float(probability), 0.0, 1.0)
    slope = float(context.get("slope", 1.0))
    intercept = float(context.get("intercept", 0.0))
    return _clip(_sigmoid(slope * _logit(probability) + intercept), .02, .98)


def _prefer(candidate: Prediction, current: Prediction) -> bool:
    candidate_live = candidate.origin == "LIVE_PREGAME"
    current_live = current.origin == "LIVE_PREGAME"
    if candidate_live != current_live:
        return candidate_live
    return _naive(candidate.data_cutoff or candidate.created_at) >= _naive(
        current.data_cutoff or current.created_at
    )


def _logit(value: float) -> float:
    value = _clip(float(value), .001, .999)
    return math.log(value / (1 - value))


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-20.0, min(20.0, value))))


def _clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)
