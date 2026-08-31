from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import KST
from backend.app.models import Game, GameResult, Prediction, PredictionEvaluation
from backend.app.services.simulation import evaluate_simulation_recipe
from backend.app.services.residual_attribution import attribute_score_residual


def evaluate_game_predictions(session: Session, game: Game, result: GameResult) -> int:
    """Evaluate every auditable prediction for one final game, once."""
    predictions = session.scalars(select(Prediction).where(
        Prediction.game_id == game.id,
    ).order_by(Prediction.created_at)).all()
    existing = {row.prediction_id: row for row in session.scalars(select(PredictionEvaluation).where(
        PredictionEvaluation.game_id == game.id,
    )).all()}
    written = 0
    for prediction in predictions:
        stored = existing.get(prediction.id)
        stored_score = ((stored.details or {}).get("actual_score") if stored else None) or {}
        needs_score_update = bool(stored and (
            stored_score.get("away") != result.away_score or stored_score.get("home") != result.home_score
        ))
        needs_inning_update = bool(
            stored and stored.actual_inning_path_count is None and result.innings is not None and
            isinstance((prediction.payload or {}).get("simulation_recipe"), dict)
        )
        if (stored and not needs_score_update and not needs_inning_update) or not _eligible_for_result(prediction, game):
            continue
        evaluation = _evaluate(prediction, result)
        if evaluation is None:
            continue
        if stored:
            for field in (
                "simulation_count", "actual_score_count", "actual_score_probability",
                "actual_outcome_count", "actual_outcome_probability", "actual_total_count",
                "actual_total_probability", "actual_margin_count", "actual_margin_probability",
                "actual_inning_path_count", "actual_inning_path_probability",
            ):
                setattr(stored, field, evaluation.get(field))
            stored.details = evaluation
            stored.evaluated_at = datetime.now(KST)
            written += 1
            continue
        session.add(PredictionEvaluation(
            prediction_id=prediction.id, game_id=game.id,
            simulation_count=evaluation["simulation_count"],
            actual_score_count=evaluation["actual_score_count"],
            actual_score_probability=evaluation["actual_score_probability"],
            actual_outcome_count=evaluation["actual_outcome_count"],
            actual_outcome_probability=evaluation["actual_outcome_probability"],
            actual_total_count=evaluation["actual_total_count"],
            actual_total_probability=evaluation["actual_total_probability"],
            actual_margin_count=evaluation["actual_margin_count"],
            actual_margin_probability=evaluation["actual_margin_probability"],
            actual_inning_path_count=evaluation.get("actual_inning_path_count"),
            actual_inning_path_probability=evaluation.get("actual_inning_path_probability"),
            details=evaluation, evaluated_at=datetime.now(KST),
        ))
        written += 1
    if written:
        session.flush()
    return written


def evaluate_pending_predictions(session: Session, league: str | None = None,
                                 target_date: date | None = None) -> int:
    query = select(Game, GameResult).join(GameResult, GameResult.game_id == Game.id).where(Game.status == "FINAL")
    if league:
        query = query.where(Game.league == league)
    if target_date:
        query = query.where(Game.game_date == target_date)
    return sum(evaluate_game_predictions(session, game, result) for game, result in session.execute(query).all())


def _eligible_for_result(prediction: Prediction, game: Game) -> bool:
    cutoff = prediction.data_cutoff or prediction.created_at
    if game.start_at and _naive(cutoff) > _naive(game.start_at):
        return False
    if prediction.origin == "HISTORICAL_REPLAY":
        return bool((prediction.leakage_audit or {}).get("passed"))
    return prediction.origin == "LIVE_PREGAME"


def _evaluate(prediction: Prediction, result: GameResult) -> dict[str, Any] | None:
    payload = prediction.payload or {}
    observed = {
        "away_score": result.away_score, "home_score": result.home_score,
        "innings": result.innings,
    }
    recipe = payload.get("simulation_recipe")
    if isinstance(recipe, dict):
        evaluation = evaluate_simulation_recipe(recipe, observed)
        evaluation["residual_attribution"] = attribute_score_residual(prediction, result)
        return evaluation
    tables = payload.get("frequency_tables")
    if not isinstance(tables, dict):
        return None
    outcomes = tables.get("outcomes") or {}
    simulations = int((payload.get("model") or {}).get("simulations") or sum(outcomes.values()) or 0)
    if simulations <= 0:
        return None
    away_score, home_score = result.away_score, result.home_score
    outcome = "HOME_WIN" if home_score > away_score else ("AWAY_WIN" if away_score > home_score else "TIE")
    score_count = int((tables.get("scores") or {}).get(f"{away_score}:{home_score}", 0))
    total_count = int((tables.get("totals") or {}).get(str(away_score + home_score), 0))
    margin_count = int((tables.get("margins") or {}).get(str(home_score - away_score), 0))
    outcome_count = int(outcomes.get(outcome, 0))
    evaluation = {
        "simulation_count": simulations,
        "actual_score": {"away": away_score, "home": home_score},
        "actual_score_count": score_count, "actual_score_probability": round(score_count / simulations, 6),
        "actual_outcome": outcome,
        "actual_outcome_count": outcome_count, "actual_outcome_probability": round(outcome_count / simulations, 6),
        "actual_total": away_score + home_score,
        "actual_total_count": total_count, "actual_total_probability": round(total_count / simulations, 6),
        "actual_margin": home_score - away_score,
        "actual_margin_count": margin_count, "actual_margin_probability": round(margin_count / simulations, 6),
        "actual_inning_path_count": None, "actual_inning_path_probability": None,
        "inning_data_available": False,
    }
    evaluation["residual_attribution"] = attribute_score_residual(prediction, result)
    return evaluation


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
