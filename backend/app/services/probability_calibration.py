from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Game, GameResult, Prediction, PredictionSnapshot


MIN_CALIBRATION_SAMPLES = 30
MAX_CALIBRATION_SAMPLES = 1000
MIN_DISTRIBUTION_VALIDATION_SAMPLES = 200
CALIBRATION_METHOD = "LEAGUE_WALK_FORWARD_PLATT_V2_IRLS"
PLATT_L2_REGULARIZATION = 4.0
# Fallback only, for a league with too few finals to measure anything yet. It is deliberately
# HOLD: with no evidence the safe action is to leave probabilities untouched. Every league with
# data is gated by `walk_forward_win_validation` below, measured from that league's own results,
# because a hardcoded verdict silently goes stale - this table once held MLB at HOLD on a
# +.00023 Brier delta while the measured out-of-sample delta was -.0034, which kept every MLB
# forecast uncalibrated and materially overconfident.
CALIBRATION_VALIDATION = {
    "KBO": {"status": "HOLD", "sample_count": 0, "validation_scope": "NO_MEASUREMENT_YET"},
    "MLB": {"status": "HOLD", "sample_count": 0, "validation_scope": "NO_MEASUREMENT_YET"},
}
# Below this a walk-forward win-probability verdict is noise. The map has two parameters and
# needs a fitting window before it can be scored at all.
MIN_WIN_VALIDATION_SAMPLES = 150
MIN_SEGMENT_CALIBRATION_SAMPLES = 60
CALIBRATION_GUARDRAILS = {
    "brier": 0.0, "log_loss": 0.0, "run_mae": .02, "total_mae": .03,
    "margin_mae": .03, "handicap_brier": .002, "total_brier": .002,
}
TOTAL_VALIDATION_LINES = {"MLB": ("7.5", "8.5", "9.5"), "KBO": ("8.5", "9.5", "10.5")}


@dataclass(frozen=True)
class ProbabilityObservation:
    game_id: int
    season: int
    available_at: datetime
    probability: float
    outcome: float
    stage: str = "UNKNOWN"


@dataclass(frozen=True)
class DistributionCalibrationObservation:
    game_id: int
    raw: dict[str, Any]
    calibrated: dict[str, Any]
    home_score: int
    away_score: int


class LeagueProbabilityCalibrationHistory:
    """League-specific pregame probabilities paired only with already-final outcomes."""

    def __init__(self, observations: list[ProbabilityObservation],
                 validation: dict[str, Any] | None = None):
        self.observations = sorted(observations, key=lambda row: (row.available_at, row.game_id))
        self.validation = validation

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

        prediction_ids = [prediction.id for prediction, _, _ in by_game.values()]
        stage_by_prediction: dict[int, str] = {}
        if prediction_ids:
            snapshots = session.scalars(select(PredictionSnapshot).where(
                PredictionSnapshot.prediction_id.in_(prediction_ids),
            ).order_by(PredictionSnapshot.captured_at)).all()
            for snapshot in snapshots:
                stage_by_prediction[snapshot.prediction_id] = snapshot.stage
        observations = []
        distribution_observations = []
        for prediction, game, result in by_game.values():
            payload = prediction.payload or {}
            calibration = payload.get("probability_calibration") or {}
            raw_distribution = calibration.get("raw_distribution")
            calibrated_distribution = calibration.get("calibrated_distribution")
            # Disabled calibration produces an identical copy and is not evidence that a
            # candidate map is safe. Only genuinely reweighted, walk-forward games enter this
            # full-distribution promotion gate; MLB therefore remains HOLD until such a
            # candidate backtest exists instead of passing on identity-vs-identity metrics.
            if (calibration.get("enabled") and isinstance(raw_distribution, dict)
                    and isinstance(calibrated_distribution, dict)):
                distribution_observations.append(DistributionCalibrationObservation(
                    game_id=game.id, raw=raw_distribution, calibrated=calibrated_distribution,
                    home_score=int(result.home_score), away_score=int(result.away_score),
                ))
            if result.home_score == result.away_score:
                # The production KBO market is two-way with ties excluded.
                continue
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
                stage=stage_by_prediction.get(
                    prediction.id,
                    "HISTORICAL_REPLAY" if prediction.origin == "HISTORICAL_REPLAY" else "UNKNOWN",
                ),
            ))
        win_validation = walk_forward_win_validation(
            sorted(observations, key=lambda row: (row.available_at, row.game_id)), league,
        )
        validation = distribution_calibration_validation(
            distribution_observations, league, win_validation,
        )
        validation["segmented_challenger"] = walk_forward_segmented_validation(
            sorted(observations, key=lambda row: (row.available_at, row.game_id)), league,
        )
        return cls(observations, validation)

    def context_for(self, game: Game) -> dict[str, Any]:
        if game.start_at is None:
            return identity_calibration("GAME_TIME_UNCONFIRMED")
        rows = [
            row for row in self.observations
            if row.season == game.game_date.year
            and row.game_id != game.id
            and _naive(row.available_at) <= _naive(game.start_at)
        ][-MAX_CALIBRATION_SAMPLES:]
        validation = self.validation or CALIBRATION_VALIDATION.get(game.league, {"status": "HOLD"})
        if validation.get("status") != "PASS":
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


def walk_forward_win_validation(observations: list[ProbabilityObservation],
                                league: str) -> dict[str, Any]:
    """Score the calibration map the way it is actually used: fit on the past, predict forward.

    Every game is judged by a map fitted only on that league's games that were already final,
    so the verdict contains no information the live forecast would not have had. Anything less
    honest here is worse than no gate at all, because it would license a map onto every future
    forecast on the strength of results it had already seen.
    """
    if len(observations) < MIN_WIN_VALIDATION_SAMPLES:
        provisional = dict(CALIBRATION_VALIDATION.get(league, {"status": "HOLD"}))
        provisional.update({
            "validation_scope": "WIN_WALK_FORWARD",
            "status": "HOLD", "reason": "INSUFFICIENT_FINALS",
            "sample_count": len(observations),
            "minimum_samples": MIN_WIN_VALIDATION_SAMPLES,
        })
        return provisional
    raw_brier = raw_log = calibrated_brier = calibrated_log = 0.0
    scored = 0
    for index, row in enumerate(observations):
        prior = [
            candidate for candidate in observations[:index]
            if candidate.season == row.season and candidate.available_at <= row.available_at
        ][-MAX_CALIBRATION_SAMPLES:]
        if len(prior) < MIN_CALIBRATION_SAMPLES:
            continue
        slope, intercept = fit_platt([(item.probability, item.outcome) for item in prior])
        calibrated = _clip(_sigmoid(intercept + slope * _logit(row.probability)), .001, .999)
        raw = _clip(row.probability, .001, .999)
        raw_brier += (raw - row.outcome) ** 2
        calibrated_brier += (calibrated - row.outcome) ** 2
        raw_log += -(row.outcome * math.log(raw) + (1 - row.outcome) * math.log(1 - raw))
        calibrated_log += -(row.outcome * math.log(calibrated) + (1 - row.outcome) * math.log(1 - calibrated))
        scored += 1
    if scored < MIN_WIN_VALIDATION_SAMPLES:
        provisional = dict(CALIBRATION_VALIDATION.get(league, {"status": "HOLD"}))
        provisional.update({
            "validation_scope": "WIN_WALK_FORWARD",
            "status": "HOLD", "reason": "INSUFFICIENT_WALK_FORWARD_WINDOW",
            "sample_count": scored, "minimum_samples": MIN_WIN_VALIDATION_SAMPLES,
        })
        return provisional
    brier_delta = round(calibrated_brier / scored - raw_brier / scored, 8)
    log_loss_delta = round(calibrated_log / scored - raw_log / scored, 8)
    # A map has to beat leaving the probabilities alone on both scores, not just one.
    passed = brier_delta < 0 and log_loss_delta < 0
    return {
        "status": "PASS" if passed else "HOLD",
        "validation_scope": "WIN_WALK_FORWARD",
        "reason": None if passed else "NO_OUT_OF_SAMPLE_IMPROVEMENT",
        "sample_count": scored,
        "minimum_samples": MIN_WIN_VALIDATION_SAMPLES,
        "raw_brier": round(raw_brier / scored, 8),
        "calibrated_brier": round(calibrated_brier / scored, 8),
        "raw_log_loss": round(raw_log / scored, 8),
        "calibrated_log_loss": round(calibrated_log / scored, 8),
        "brier_delta": brier_delta,
        "log_loss_delta": log_loss_delta,
    }


def walk_forward_segmented_validation(observations: list[ProbabilityObservation],
                                      league: str) -> dict[str, Any]:
    """Shadow-test calibration by information horizon and favorite strength.

    The segmented map never touches production merely because one retrospective slice looks
    attractive.  Each row is scored with parameters fitted only on earlier finals in the same
    coarse stage/favorite regime, falling back to the league map until 60 comparable games exist.
    """
    raw_brier = raw_log = candidate_brier = candidate_log = 0.0
    scored = segmented = known_stage_predictions = 0
    segment_counts: dict[str, int] = {}
    for index, row in enumerate(observations):
        prior = [candidate for candidate in observations[:index]
                 if candidate.season == row.season and candidate.available_at <= row.available_at]
        global_prior = prior[-MAX_CALIBRATION_SAMPLES:]
        if len(global_prior) < MIN_CALIBRATION_SAMPLES:
            continue
        key = _calibration_segment(row)
        known_stage_predictions += int(not key.startswith("UNKNOWN:"))
        same_segment = [candidate for candidate in prior if _calibration_segment(candidate) == key]
        training = same_segment[-MAX_CALIBRATION_SAMPLES:] if len(same_segment) >= MIN_SEGMENT_CALIBRATION_SAMPLES else global_prior
        segmented += int(training is not global_prior)
        segment_counts[key] = segment_counts.get(key, 0) + 1
        slope, intercept = fit_platt([(item.probability, item.outcome) for item in training])
        candidate = _clip(_sigmoid(intercept + slope * _logit(row.probability)), .001, .999)
        raw = _clip(row.probability, .001, .999)
        raw_brier += (raw - row.outcome) ** 2
        candidate_brier += (candidate - row.outcome) ** 2
        raw_log += -(row.outcome * math.log(raw) + (1 - row.outcome) * math.log(1 - raw))
        candidate_log += -(row.outcome * math.log(candidate) + (1 - row.outcome) * math.log(1 - candidate))
        scored += 1
    if scored < MIN_WIN_VALIDATION_SAMPLES:
        return {
            "status": "COLLECTING", "league": league, "sample_count": scored,
            "minimum_samples": MIN_WIN_VALIDATION_SAMPLES,
            "minimum_segment_samples": MIN_SEGMENT_CALIBRATION_SAMPLES,
        }
    brier_delta = candidate_brier / scored - raw_brier / scored
    log_delta = candidate_log / scored - raw_log / scored
    passed = (brier_delta < 0 and log_delta < 0
              and segmented >= MIN_SEGMENT_CALIBRATION_SAMPLES
              and known_stage_predictions >= MIN_SEGMENT_CALIBRATION_SAMPLES)
    return {
        "status": "READY" if passed else "HOLD",
        "league": league, "sample_count": scored, "segmented_predictions": segmented,
        "known_stage_predictions": known_stage_predictions,
        "minimum_segment_samples": MIN_SEGMENT_CALIBRATION_SAMPLES,
        "raw_brier": round(raw_brier / scored, 8),
        "candidate_brier": round(candidate_brier / scored, 8),
        "raw_log_loss": round(raw_log / scored, 8),
        "candidate_log_loss": round(candidate_log / scored, 8),
        "brier_delta": round(brier_delta, 8), "log_loss_delta": round(log_delta, 8),
        "segments_scored": segment_counts,
        "production_enabled": False,
    }


def _calibration_segment(row: ProbabilityObservation) -> str:
    favorite = max(row.probability, 1 - row.probability)
    strength = "TOSSUP" if favorite < .55 else ("LEAN" if favorite < .65 else "STRONG")
    stage = row.stage
    if stage in {"T_MINUS_15M", "T_MINUS_40M", "T_MINUS_60M"}:
        horizon = "LATE"
    elif stage in {"T_MINUS_3H", "T_MINUS_24H"}:
        horizon = "EARLY"
    else:
        horizon = "UNKNOWN"
    return f"{horizon}:{strength}"


def distribution_calibration_validation(
    observations: list[DistributionCalibrationObservation], league: str,
    win_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate outcome reweighting on every distribution family it changes."""
    if len(observations) < MIN_DISTRIBUTION_VALIDATION_SAMPLES:
        # No pre/post distribution pairs yet, which is expected until a map has been live for a
        # while. Fall back to the win-probability verdict measured from this league's results.
        provisional = dict(win_validation or CALIBRATION_VALIDATION.get(league, {"status": "HOLD"}))
        provisional.update({
            "distribution_status": "COLLECTING",
            "distribution_sample_count": len(observations),
            "distribution_minimum_samples": MIN_DISTRIBUTION_VALIDATION_SAMPLES,
        })
        return provisional

    raw = _distribution_metrics(observations, league, "raw")
    calibrated = _distribution_metrics(observations, league, "calibrated")
    deltas = {key: round(calibrated[key] - raw[key], 8) for key in raw}
    failed = [key for key, tolerance in CALIBRATION_GUARDRAILS.items()
              if deltas[key] > tolerance]
    return {
        "status": "PASS" if not failed else "HOLD",
        "validation_scope": "FULL_SCORE_DISTRIBUTION",
        "sample_count": len(observations),
        "raw": raw, "calibrated": calibrated, "deltas": deltas,
        "guardrails": CALIBRATION_GUARDRAILS, "failed_metrics": failed,
    }


def _distribution_metrics(observations: list[DistributionCalibrationObservation], league: str,
                          side: str) -> dict[str, float]:
    brier: list[float] = []
    log_losses: list[float] = []
    run_errors: list[float] = []
    total_errors: list[float] = []
    margin_errors: list[float] = []
    handicap_briers: list[float] = []
    total_briers: list[float] = []
    for row in observations:
        snapshot = row.raw if side == "raw" else row.calibrated
        means = snapshot["mean_runs"]
        home_mean, away_mean = float(means["home"]), float(means["away"])
        actual_margin = row.home_score - row.away_score
        if actual_margin != 0:
            probability = _clip(float(snapshot["home_two_way_probability"]), .001, .999)
            outcome = 1.0 if actual_margin > 0 else 0.0
            brier.append((probability - outcome) ** 2)
            log_losses.append(-outcome * math.log(probability) - (1 - outcome) * math.log(1 - probability))
        run_errors.extend((abs(home_mean - row.home_score), abs(away_mean - row.away_score)))
        total_errors.append(abs(home_mean + away_mean - row.home_score - row.away_score))
        margin_errors.append(abs(home_mean - away_mean - actual_margin))
        handicap = snapshot["handicap"]
        handicap_briers.extend((
            (float(handicap["home_minus_1_5"]) - float(actual_margin >= 2)) ** 2,
            (float(handicap["away_minus_1_5"]) - float(actual_margin <= -2)) ** 2,
        ))
        actual_total = row.home_score + row.away_score
        for line in TOTAL_VALIDATION_LINES[league]:
            over = float(snapshot["totals"][line]["over"])
            total_briers.append((over - float(actual_total > float(line))) ** 2)

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 8) if values else 0.0

    return {
        "brier": mean(brier), "log_loss": mean(log_losses), "run_mae": mean(run_errors),
        "total_mae": mean(total_errors), "margin_mae": mean(margin_errors),
        "handicap_brier": mean(handicap_briers), "total_brier": mean(total_briers),
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
