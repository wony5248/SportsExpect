from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from backend.app.models import Game, GameResult, MarketSnapshot, Prediction, PredictionSnapshot
from backend.app.services.team_residuals import (ResidualObservation, apply_residual_adjustment, available_before,
                                                 baseline_expected_runs, probability_from_run_means,
                                                 residual_context, RESIDUAL_ENABLED_LEAGUES)


EXACT_CHECKPOINT_STAGES = {"T_MINUS_24H", "T_MINUS_3H", "T_MINUS_60M", "T_MINUS_15M"}


def walk_forward_backtest(session: Session, league: str = "ALL", stage: str | None = None) -> dict[str, Any]:
    query = select(Prediction, Game, GameResult).join(Game, Game.id == Prediction.game_id).join(
        GameResult, GameResult.game_id == Game.id,
    ).where(Game.status == "FINAL").options(joinedload(Prediction.model_version))
    if league != "ALL":
        query = query.where(Game.league == league)
    raw = session.execute(query.order_by(Game.game_date, Game.start_at, Prediction.created_at)).all()
    completed_query = select(func.count(GameResult.game_id)).join(
        Game, Game.id == GameResult.game_id,
    ).where(Game.status == "FINAL")
    if league != "ALL":
        completed_query = completed_query.where(Game.league == league)
    completed_results = int(session.scalar(completed_query) or 0)
    snapshot_rows = session.scalars(select(PredictionSnapshot)).all()
    snapshots: dict[int, list[PredictionSnapshot]] = defaultdict(list)
    for snapshot_row in snapshot_rows:
        if snapshot_row.prediction_id is not None:
            snapshots[snapshot_row.prediction_id].append(snapshot_row)
    market_rows = session.scalars(select(MarketSnapshot).order_by(MarketSnapshot.collected_at)).all()
    markets: dict[int, list[MarketSnapshot]] = defaultdict(list)
    for market_row in market_rows:
        markets[market_row.game_id].append(market_row)
    candidates: dict[int, tuple[Prediction, Game, GameResult, PredictionSnapshot | None]] = {}
    replay_candidates: dict[int, tuple[Prediction, Game, GameResult, PredictionSnapshot | None]] = {}
    model_candidates: dict[tuple[int, str], tuple[Prediction, Game, GameResult, PredictionSnapshot | None]] = {}
    for prediction, game, result in raw:
        prediction_snapshots = sorted(snapshots.get(prediction.id, []), key=lambda item: item.captured_at)
        matching = [item for item in prediction_snapshots if stage is None or (
            item.stage == stage and (stage not in EXACT_CHECKPOINT_STAGES or item.trigger == "checkpoint_exact")
        )]
        snapshot = matching[-1] if matching else None
        if stage and snapshot is None:
            continue
        cutoff = prediction.data_cutoff or prediction.created_at
        if game.start_at and _naive(cutoff) > _naive(game.start_at):
            continue
        origin = prediction.origin or "LIVE_PREGAME"
        if origin == "LIVE_PREGAME" and _naive(prediction.created_at) > _naive(result.finalized_at):
            continue
        if origin == "HISTORICAL_REPLAY" and not bool((prediction.leakage_audit or {}).get("passed")):
            continue
        target = replay_candidates if origin == "HISTORICAL_REPLAY" else candidates
        current = target.get(game.id)
        if current is None or prediction.created_at > current[0].created_at:
            target[game.id] = (prediction, game, result, snapshot)
        if origin != "LIVE_PREGAME":
            continue
        model_key = (game.id, prediction.model_version.name)
        model_current = model_candidates.get(model_key)
        if model_current is None or prediction.created_at > model_current[0].created_at:
            model_candidates[model_key] = (prediction, game, result, snapshot)
    rows = sorted(candidates.values(), key=lambda row: (row[1].game_date, row[1].start_at or datetime.min, row[1].id))
    replay_rows = sorted(replay_candidates.values(), key=lambda row: (row[1].game_date, row[1].start_at or datetime.min, row[1].id))
    if not rows and not replay_rows:
        return {
            "sample_size": 0, "league": league, "stage": stage,
            "message": "종료 경기의 경기 전 예측 스냅샷이 쌓이면 walk-forward 평가가 시작됩니다.",
            "leakage_guard": "prediction.created_at <= game.start_at",
            "readiness": _readiness(0, completed_results),
        }

    history: list[tuple[float, float]] = []
    evaluated: list[dict[str, Any]] = []
    league_history: dict[str, list[float]] = defaultdict(list)
    for prediction, game, result, snapshot in rows:
        outcome = 1.0 if result.home_score > result.away_score else (0.5 if result.home_score == result.away_score else 0.0)
        prior_outcomes = league_history[game.league]
        empirical_home = (sum(prior_outcomes) + 1) / (len(prior_outcomes) + 2)
        calibrated = _platt_predict(prediction.home_win_probability, history) if len(history) >= 30 else prediction.home_win_probability
        evaluated.append({
            "game_id": game.id, "date": game.game_date.isoformat(), "league": game.league,
            "model": prediction.model_version.name, "stage": snapshot.stage if snapshot else "LEGACY",
            "probability": prediction.home_win_probability, "calibrated_probability": calibrated,
            "baseline_probability": empirical_home, "outcome": outcome,
            "home_run_error": prediction.home_expected_runs - result.home_score,
            "away_run_error": prediction.away_expected_runs - result.away_score,
            **_run_distribution_fields(prediction, result),
        })
        history.append((prediction.home_win_probability, outcome))
        prior_outcomes.append(outcome)

    replay_evaluated: list[dict[str, Any]] = []
    for prediction, game, result, snapshot in replay_rows:
        outcome = 1.0 if result.home_score > result.away_score else (0.5 if result.home_score == result.away_score else 0.0)
        replay_evaluated.append({
            "game_id": game.id, "date": game.game_date.isoformat(), "league": game.league,
            "model": prediction.model_version.name, "stage": snapshot.stage if snapshot else "HISTORICAL_REPLAY",
            "probability": prediction.home_win_probability, "outcome": outcome,
            "home_run_error": prediction.home_expected_runs - result.home_score,
            "away_run_error": prediction.away_expected_runs - result.away_score,
            **_run_distribution_fields(prediction, result),
        })

    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_league: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluated:
        by_month[row["date"][:7]].append(row)
        by_league[row["league"]].append(row)
    model_rows: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for prediction, game, result, snapshot in sorted(
        model_candidates.values(), key=lambda row: (row[1].game_date, row[1].start_at or datetime.min, row[1].id),
    ):
        outcome = 1.0 if result.home_score > result.away_score else (0.5 if result.home_score == result.away_score else 0.0)
        model_rows[prediction.model_version.name][game.id] = {
            "probability": prediction.home_win_probability, "outcome": outcome,
            "home_run_error": prediction.home_expected_runs - result.home_score,
            "away_run_error": prediction.away_expected_runs - result.away_score,
            **_run_distribution_fields(prediction, result),
        }
    paired_comparisons = []
    model_names = sorted(model_rows)
    for index, left_name in enumerate(model_names):
        for right_name in model_names[index + 1:]:
            common = sorted(set(model_rows[left_name]) & set(model_rows[right_name]))
            if not common:
                continue
            paired_comparisons.append({
                "models": [left_name, right_name], "common_games": len(common),
                "results": {
                    left_name: _metrics([model_rows[left_name][game_id] for game_id in common], "probability"),
                    right_name: _metrics([model_rows[right_name][game_id] for game_id in common], "probability"),
                },
                "paired_delta_left_minus_right": _paired_delta(
                    [model_rows[left_name][game_id] for game_id in common],
                    [model_rows[right_name][game_id] for game_id in common],
                ),
            })
    market_evaluated: list[dict[str, Any]] = []
    for row in evaluated:
        game_markets = [market for market in markets.get(row["game_id"], []) if
                        candidates[row["game_id"]][1].start_at is None or
                        _naive(market.collected_at) <= _naive(candidates[row["game_id"]][1].start_at)]
        if not game_markets:
            continue
        market = game_markets[-1]
        if market.home_implied_probability is None:
            continue
        market_evaluated.append({
            "probability": market.home_implied_probability, "outcome": row["outcome"],
            "total_error": (market.total_line - (
                candidates[row["game_id"]][2].home_score + candidates[row["game_id"]][2].away_score
            )) if market.total_line is not None else None,
        })
    market_metrics = _metrics(market_evaluated, "probability") if market_evaluated else {
        "sample_size": 0,
        "message": "ODDS_API_KEY와 경기 전 시장 스냅샷이 쌓이면 동일 경기 시장 기준 비교가 시작됩니다.",
    }
    market_total_errors = [abs(float(row["total_error"])) for row in market_evaluated if row["total_error"] is not None]
    if market_total_errors:
        market_metrics["total_line_mae"] = round(sum(market_total_errors) / len(market_total_errors), 4)
    return {
        "sample_size": len(evaluated), "league": league, "stage": stage,
        "leakage_guard": "LIVE: created_at <= start_at; REPLAY: audited data_cutoff <= start_at",
        "readiness": _readiness(len(evaluated), completed_results),
        "metrics": _metrics(evaluated, "probability") if evaluated else {"sample_size": 0, "message": "실전 경기 전 예측 표본이 없습니다."},
        "walk_forward_calibrated": _metrics(evaluated, "calibrated_probability") if evaluated else {"sample_size": 0},
        "expanding_home_rate_baseline": _metrics(evaluated, "baseline_probability") if evaluated else {"sample_size": 0},
        "historical_replay": {
            "sample_size": len(replay_evaluated),
            "metrics": _metrics(replay_evaluated, "probability") if replay_evaluated else {"sample_size": 0},
            "official_live_metric": False,
            "disclosure": "경기 전 데이터만 사용해 현재 코드로 다시 계산한 회고 성능입니다.",
        },
        "team_residual_walk_forward": {
            "official_live": _residual_walk_forward(rows),
            "historical_replay": _residual_walk_forward(replay_rows),
            "method": ("Each game is adjusted only from earlier finalized games. The comparison force-applies "
                       "the August 23 residual policy retrospectively to measure counterfactual lift."),
            "lower_is_better": ["runs_mae", "runs_rmse", "brier_score", "log_loss", "calibration_error"],
        },
        "market_consensus_baseline": market_metrics,
        "by_league": {key: _metrics(value, "probability") for key, value in by_league.items()},
        "model_leaderboard": sorted(
            ({"model": key, **_metrics(list(value.values()), "probability")} for key, value in model_rows.items()),
            key=lambda item: item["log_loss"],
        ),
        "paired_model_comparisons": paired_comparisons,
        "by_month": [{"month": key, **_metrics(value, "probability")} for key, value in sorted(by_month.items())],
    }


def _residual_walk_forward(
    rows: list[tuple[Prediction, Game, GameResult, PredictionSnapshot | None]],
) -> dict[str, Any]:
    observations: list[ResidualObservation] = []
    baseline_rows: list[dict[str, Any]] = []
    adjusted_rows: list[dict[str, Any]] = []
    for prediction, game, result, _ in rows:
        if game.start_at is None or game.league not in RESIDUAL_ENABLED_LEAGUES:
            continue
        cutoff = _naive(game.start_at)
        eligible = [row for row in observations if available_before(row, cutoff)]
        baseline_home, baseline_away = baseline_expected_runs(prediction)
        context = residual_context(
            eligible, game.home_team_id, game.away_team_id, game.game_date,
            latest_game_id=eligible[-1].game_id if eligible else None, force_enabled=True,
        )
        adjusted_home, adjusted_away = apply_residual_adjustment(baseline_home, baseline_away, context)
        outcome = 1.0 if result.home_score > result.away_score else (
            .5 if result.home_score == result.away_score else 0.0)
        baseline_run_probability = probability_from_run_means(baseline_home, baseline_away)
        adjusted_run_probability = probability_from_run_means(adjusted_home, adjusted_away)
        probability_shift = _logit(adjusted_run_probability) - _logit(baseline_run_probability)
        adjusted_probability = _sigmoid(_logit(prediction.home_win_probability) + probability_shift)
        baseline_rows.append({
            "probability": prediction.home_win_probability, "outcome": outcome,
            "home_run_error": baseline_home - result.home_score,
            "away_run_error": baseline_away - result.away_score,
        })
        adjusted_rows.append({
            "probability": adjusted_probability, "outcome": outcome,
            "home_run_error": adjusted_home - result.home_score,
            "away_run_error": adjusted_away - result.away_score,
        })
        observations.append(ResidualObservation(
            game_id=game.id, started_at=cutoff, finalized_at=_naive(result.finalized_at),
            home_team_id=game.home_team_id, away_team_id=game.away_team_id,
            home_expected=baseline_home, away_expected=baseline_away,
            home_actual=result.home_score, away_actual=result.away_score,
        ))
    if not baseline_rows:
        return {"sample_size": 0, "baseline": {"sample_size": 0}, "adjusted": {"sample_size": 0}}
    baseline_metrics = _metrics(baseline_rows, "probability")
    adjusted_metrics = _metrics(adjusted_rows, "probability")
    compared = ("runs_mae", "runs_rmse", "brier_score", "log_loss", "calibration_error")
    return {
        "sample_size": len(baseline_rows),
        "baseline": baseline_metrics,
        "adjusted": adjusted_metrics,
        "delta_adjusted_minus_baseline": {
            key: round(float(adjusted_metrics[key]) - float(baseline_metrics[key]), 5)
            for key in compared if key in baseline_metrics and key in adjusted_metrics
        },
    }


def _readiness(evaluable: int, completed: int) -> dict[str, Any]:
    return {
        "evaluable_pregame_games": evaluable,
        "completed_results": completed,
        "completed_without_evaluable_pregame_prediction": max(0, completed - evaluable),
        "preliminary_minimum": 200,
        "recommended_minimum": 500,
        "status": "READY" if evaluable >= 500 else ("PRELIMINARY" if evaluable >= 200 else "COLLECTING"),
    }


def _paired_delta(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    eps = 1e-9
    per_game: dict[str, list[float]] = {"brier_score": [], "log_loss": [], "runs_mae": []}
    for left_row, right_row in zip(left, right, strict=True):
        outcome = float(left_row["outcome"])
        left_probability, right_probability = float(left_row["probability"]), float(right_row["probability"])
        per_game["brier_score"].append((left_probability - outcome) ** 2 - (right_probability - outcome) ** 2)
        left_log = -(outcome * math.log(max(eps, left_probability)) + (1 - outcome) * math.log(max(eps, 1 - left_probability)))
        right_log = -(outcome * math.log(max(eps, right_probability)) + (1 - outcome) * math.log(max(eps, 1 - right_probability)))
        per_game["log_loss"].append(left_log - right_log)
        left_runs = (abs(float(left_row["home_run_error"])) + abs(float(left_row["away_run_error"]))) / 2
        right_runs = (abs(float(right_row["home_run_error"])) + abs(float(right_row["away_run_error"]))) / 2
        per_game["runs_mae"].append(left_runs - right_runs)
    rng = random.Random(20260822)
    output: dict[str, Any] = {}
    for metric, values in per_game.items():
        mean = sum(values) / len(values)
        bootstrapped = sorted(
            sum(values[rng.randrange(len(values))] for _ in values) / len(values) for _ in range(1000)
        )
        output[metric] = {
            "delta": round(mean, 5),
            "ci95": [round(bootstrapped[24], 5), round(bootstrapped[974], 5)],
            "interpretation": "negative_favors_left",
        }
    return output


def _metrics(rows: list[dict[str, Any]], probability_key: str) -> dict[str, Any]:
    eps = 1e-9
    pairs = [(float(row[probability_key]), float(row["outcome"])) for row in rows]
    n = len(pairs)
    brier = sum((p - y) ** 2 for p, y in pairs) / n
    log_loss = -sum(y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1 - p)) for p, y in pairs) / n
    run_errors = [error for row in rows for error in (row.get("home_run_error"), row.get("away_run_error"))
                  if error is not None]
    bins = []
    calibration_error = 0.0
    for index in range(10):
        low, high = index / 10, (index + 1) / 10
        group = [(p, y) for p, y in pairs if low <= p < high or (index == 9 and p == 1)]
        if not group:
            continue
        predicted = sum(p for p, _ in group) / len(group)
        observed = sum(y for _, y in group) / len(group)
        calibration_error += len(group) / n * abs(predicted - observed)
        bins.append({"range": f"{low:.1f}-{high:.1f}", "predicted": round(predicted, 4),
                     "observed": round(observed, 4), "count": len(group)})
    output = {
        "sample_size": n,
        "accuracy": round(sum((p >= .5) == (y >= .5) for p, y in pairs) / n, 4),
        "brier_score": round(brier, 5), "log_loss": round(log_loss, 5),
        "calibration_error": round(calibration_error, 5),
        "calibration": bins,
    }
    if run_errors:
        output["runs_mae"] = round(sum(abs(value) for value in run_errors) / len(run_errors), 4)
        output["runs_rmse"] = round(math.sqrt(sum(value * value for value in run_errors) / len(run_errors)), 4)
    predicted_scores = [value for row in rows for value in (row.get("home_expected_runs"), row.get("away_expected_runs"))
                        if value is not None]
    actual_scores = [value for row in rows for value in (row.get("home_actual_runs"), row.get("away_actual_runs"))
                     if value is not None]
    if predicted_scores and actual_scores:
        output["predicted_team_score_sd"] = round(_population_sd(predicted_scores), 4)
        output["actual_team_score_sd"] = round(_population_sd(actual_scores), 4)
    interval_hits = [value for row in rows for value in (row.get("home_interval_hit"), row.get("away_interval_hit"))
                     if value is not None]
    interval_widths = [value for row in rows for value in (row.get("home_interval_width"), row.get("away_interval_width"))
                       if value is not None]
    if interval_hits:
        output["runs_p10_p90_coverage"] = round(sum(interval_hits) / len(interval_hits), 4)
        output["runs_p10_p90_average_width"] = round(sum(interval_widths) / len(interval_widths), 4)
    return output


def _run_distribution_fields(prediction: Prediction, result: GameResult) -> dict[str, Any]:
    quantiles = (prediction.payload or {}).get("team_quantiles") or {}
    home_quantiles = quantiles.get("home") or {}
    away_quantiles = quantiles.get("away") or {}
    output: dict[str, Any] = {
        "home_expected_runs": prediction.home_expected_runs,
        "away_expected_runs": prediction.away_expected_runs,
        "home_actual_runs": result.home_score,
        "away_actual_runs": result.away_score,
    }
    for side, actual, values in (("home", result.home_score, home_quantiles), ("away", result.away_score, away_quantiles)):
        low, high = values.get("p10"), values.get("p90")
        if low is not None and high is not None:
            output[f"{side}_interval_hit"] = float(low <= actual <= high)
            output[f"{side}_interval_width"] = float(high - low)
    return output


def _population_sd(values: list[float]) -> float:
    average = sum(float(value) for value in values) / len(values)
    return math.sqrt(sum((float(value) - average) ** 2 for value in values) / len(values))


def _platt_predict(probability: float, history: list[tuple[float, float]]) -> float:
    a, b = 1.0, 0.0
    learning_rate = .04 / math.sqrt(len(history))
    for _ in range(160):
        grad_a = grad_b = 0.0
        for p, outcome in history:
            x = _logit(p)
            estimate = _sigmoid(a * x + b)
            grad_a += (estimate - outcome) * x
            grad_b += estimate - outcome
        a -= learning_rate * grad_a / len(history)
        b -= learning_rate * grad_b / len(history)
    return min(.98, max(.02, _sigmoid(a * _logit(probability) + b)))


def _logit(value: float) -> float:
    value = min(.999, max(.001, value))
    return math.log(value / (1 - value))


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-min(20, max(-20, value))))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
