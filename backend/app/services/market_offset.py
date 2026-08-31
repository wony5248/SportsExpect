from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Game, GameResult, Prediction


MIN_OFFSET_FIT_SAMPLES = 100
MIN_OFFSET_VALIDATION_SAMPLES = 300
OFFSET_L2 = 8.0


@dataclass(frozen=True)
class MarketOffsetObservation:
    game_id: int
    season: int
    available_at: datetime
    market_probability: float
    independent_model_probability: float
    outcome: float


class MarketOffsetHistory:
    """Leakage-safe market-offset candidate; it never mutates the production probability."""

    def __init__(self, observations: list[MarketOffsetObservation]):
        self.observations = sorted(observations, key=lambda row: (row.available_at, row.game_id))
        self.validation = walk_forward_offset_validation(self.observations)

    @classmethod
    def from_session(cls, session: Session, league: str) -> "MarketOffsetHistory":
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
            if result.home_score == result.away_score:
                continue
            if prediction.origin == "HISTORICAL_REPLAY" and not bool(
                (prediction.leakage_audit or {}).get("passed")
            ):
                continue
            current = by_game.get(game.id)
            if current is None or _prefer(prediction, current[0]):
                by_game[game.id] = (prediction, game, result)
        observations: list[MarketOffsetObservation] = []
        for prediction, game, result in by_game.values():
            payload = prediction.payload or {}
            market = payload.get("headline_market") or {}
            calibration = payload.get("market_calibration") or {}
            market_probability = _number(market.get("home_implied_probability"))
            model_probability = _number(calibration.get("model_home_probability_before"))
            if market_probability is None or model_probability is None:
                continue
            observations.append(MarketOffsetObservation(
                game_id=game.id, season=game.game_date.year, available_at=result.finalized_at,
                market_probability=_clip(market_probability),
                independent_model_probability=_clip(model_probability),
                outcome=1.0 if result.home_score > result.away_score else 0.0,
            ))
        return cls(observations)

    def context_for(self, game: Game) -> dict[str, Any]:
        if game.start_at is None:
            return identity_offset("GAME_TIME_UNCONFIRMED")
        rows = [row for row in self.observations if row.season == game.game_date.year
                and row.game_id != game.id and _naive(row.available_at) <= _naive(game.start_at)]
        if len(rows) < MIN_OFFSET_FIT_SAMPLES:
            context = identity_offset("INSUFFICIENT_PRICED_FINALS", len(rows))
        else:
            intercept, disagreement_weight = fit_market_offset(rows[-1000:])
            context = {
                "enabled": True, "method": "MARKET_LOGIT_OFFSET_DISAGREEMENT_V1",
                "sample_count": len(rows), "intercept": round(intercept, 8),
                "disagreement_weight": round(disagreement_weight, 8),
                "future_results_used": 0,
            }
        context["validation"] = self.validation
        context["production_enabled"] = False
        return context


def market_offset_shadow_probability(model_probability: float, market_probability: float | None,
                                     context: dict[str, Any] | None) -> dict[str, Any]:
    if market_probability is None:
        return {"available": False, "reason": "NO_PREGAME_MONEYLINE", "production_enabled": False}
    market_probability = _clip(float(market_probability))
    model_probability = _clip(float(model_probability))
    if context and context.get("enabled"):
        intercept = float(context.get("intercept") or 0.0)
        weight = float(context.get("disagreement_weight") or 0.0)
        probability = _sigmoid(
            _logit(market_probability) + intercept
            + weight * (_logit(model_probability) - _logit(market_probability))
        )
    else:
        # A shadow row is still useful before fitting: the market baseline is the null candidate.
        probability = market_probability
    return {
        "available": True, "market_probability": round(market_probability, 6),
        "independent_model_probability": round(model_probability, 6),
        "shadow_probability": round(probability, 6),
        "model_market_edge": round(model_probability - market_probability, 6),
        "method": "MARKET_LOGIT_OFFSET_DISAGREEMENT_V1",
        "fit_enabled": bool(context and context.get("enabled")),
        "fit_sample_count": int((context or {}).get("sample_count") or 0),
        "validation": (context or {}).get("validation") or {},
        "production_enabled": False,
    }


def fit_market_offset(rows: list[MarketOffsetObservation]) -> tuple[float, float]:
    intercept, weight = 0.0, 0.0
    for _ in range(40):
        g0, g1 = OFFSET_L2 * intercept, OFFSET_L2 * weight
        h00, h11, h01 = OFFSET_L2, OFFSET_L2, 0.0
        for row in rows:
            disagreement = _logit(row.independent_model_probability) - _logit(row.market_probability)
            estimate = _sigmoid(_logit(row.market_probability) + intercept + weight * disagreement)
            error = estimate - row.outcome
            variance = max(1e-6, estimate * (1 - estimate))
            g0 += error; g1 += error * disagreement
            h00 += variance; h11 += variance * disagreement * disagreement
            h01 += variance * disagreement
        determinant = h00 * h11 - h01 * h01
        if determinant <= 1e-12:
            break
        step0 = (h11 * g0 - h01 * g1) / determinant
        step1 = (h00 * g1 - h01 * g0) / determinant
        intercept = max(-1.0, min(1.0, intercept - step0))
        weight = max(-1.0, min(1.5, weight - step1))
        if max(abs(step0), abs(step1)) < 1e-8:
            break
    return intercept, weight


def walk_forward_offset_validation(rows: list[MarketOffsetObservation]) -> dict[str, Any]:
    market_brier = market_log = shadow_brier = shadow_log = 0.0
    scored = 0
    for index, row in enumerate(rows):
        prior = [candidate for candidate in rows[:index]
                 if candidate.season == row.season and candidate.available_at <= row.available_at]
        if len(prior) < MIN_OFFSET_FIT_SAMPLES:
            continue
        intercept, weight = fit_market_offset(prior[-1000:])
        market = _clip(row.market_probability)
        shadow = _clip(_sigmoid(
            _logit(market) + intercept
            + weight * (_logit(row.independent_model_probability) - _logit(market))
        ))
        market_brier += (market - row.outcome) ** 2
        shadow_brier += (shadow - row.outcome) ** 2
        market_log += _log_loss(market, row.outcome)
        shadow_log += _log_loss(shadow, row.outcome)
        scored += 1
    if scored < MIN_OFFSET_VALIDATION_SAMPLES:
        return {"status": "COLLECTING", "sample_count": scored,
                "minimum_samples": MIN_OFFSET_VALIDATION_SAMPLES}
    brier_delta = shadow_brier / scored - market_brier / scored
    log_delta = shadow_log / scored - market_log / scored
    return {
        "status": "READY" if brier_delta < 0 and log_delta < 0 else "HOLD",
        "sample_count": scored, "market_brier": round(market_brier / scored, 8),
        "shadow_brier": round(shadow_brier / scored, 8),
        "market_log_loss": round(market_log / scored, 8),
        "shadow_log_loss": round(shadow_log / scored, 8),
        "brier_delta": round(brier_delta, 8), "log_loss_delta": round(log_delta, 8),
        "production_enabled": False,
    }


def identity_offset(reason: str, sample_count: int = 0) -> dict[str, Any]:
    return {"enabled": False, "method": "MARKET_LOGIT_OFFSET_DISAGREEMENT_V1",
            "reason": reason, "sample_count": sample_count, "future_results_used": 0}


def _prefer(candidate: Prediction, current: Prediction) -> bool:
    if candidate.origin != current.origin:
        return candidate.origin == "LIVE_PREGAME"
    return _naive(candidate.data_cutoff or candidate.created_at) > _naive(current.data_cutoff or current.created_at)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float) -> float:
    return max(.001, min(.999, float(value)))


def _logit(value: float) -> float:
    value = _clip(value)
    return math.log(value / (1 - value))


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-20.0, min(20.0, value))))


def _log_loss(probability: float, outcome: float) -> float:
    return -(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
