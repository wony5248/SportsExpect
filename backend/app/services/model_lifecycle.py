from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.app.config import KST
from backend.app.models import (Game, GameResult, ModelArtifact, ModelLifecycleEvent, ModelRegistry,
                                ModelVersion, Prediction, PredictionSnapshot)


MIN_TRAINING_SAMPLES = 200
MIN_VALIDATION_SAMPLES = 40
MIN_NEW_SAMPLES = 25
MIN_ROLLBACK_SAMPLES = 50
VALIDATION_FRACTION = .20

TRAINABLE_FEATURES = [
    "base_home_expected", "base_away_expected", "league_average_runs",
    "season_win_rate_diff", "recent_10_win_rate_diff", "recent_run_diff",
    "recent_run_allowed_diff", "runs_per_game_diff", "runs_allowed_per_game_diff",
    "home_avg", "away_avg", "home_obp", "away_obp", "home_slg", "away_slg",
    "home_ops", "away_ops", "split_win_rate_diff", "starter_era_diff",
    "starter_whip_diff", "starter_war_diff", "starter_fip_diff", "starter_k_bb_diff",
    "starter_durability_diff", "quality_start_rate_diff", "bullpen_proxy_diff",
    "starter_rest_days_diff", "recent_pitch_burden_diff", "rest_days_diff",
    "doubleheader_diff", "head_to_head_diff", "head_to_head_run_diff",
    "starter_opponent_era_diff", "park_factor", "lineup_strength_diff", "lineup_bvp_diff",
    "bullpen_fatigue_edge", "schedule_fatigue_edge", "lineup_platoon_diff",
    "starter_recent_era_diff", "starter_recent_k_bb_diff", "fielding_edge", "baserunning_edge", "catcher_control_edge",
    "weather_run_multiplier",
    "home_starter_confirmed", "away_starter_confirmed", "home_lineup_confirmed",
    "away_lineup_confirmed", "recent_home_games", "recent_away_games",
]

POLICY = {
    "minimum_training_samples": MIN_TRAINING_SAMPLES,
    "minimum_validation_samples": MIN_VALIDATION_SAMPLES,
    "minimum_new_samples_for_retraining": MIN_NEW_SAMPLES,
    "minimum_live_samples_for_rollback": MIN_ROLLBACK_SAMPLES,
    "promotion": {
        "brier_improvement": .002,
        "or_run_mae_improvement": .10,
        "maximum_brier_regression": .005,
        "maximum_log_loss_regression": .01,
        "maximum_run_mae_regression": .05,
    },
    "rollback": {"brier_regression": .015, "log_loss_regression": .025, "run_mae_regression": .15},
}


def load_champion_runtime(session: Session, league: str) -> dict[str, Any] | None:
    registry = session.get(ModelRegistry, league)
    if not registry or registry.champion_model_version_id is None:
        return None
    artifact = session.scalar(select(ModelArtifact).options(joinedload(ModelArtifact.model_version)).where(
        ModelArtifact.model_version_id == registry.champion_model_version_id,
    ))
    return _runtime(artifact) if artifact else None


def predict_with_runtime(runtime: dict[str, Any], features: dict[str, Any],
                         base_home_runs: float, base_away_runs: float) -> tuple[float, float, float]:
    values = _feature_values(features, base_home_runs, base_away_runs, runtime["feature_names"])
    means = np.asarray(runtime["feature_means"], dtype=float)
    scales = np.asarray(runtime["feature_scales"], dtype=float)
    standardized = (values - means) / scales
    win_logit = float(runtime["win_intercept"] + standardized @ np.asarray(runtime["win_coefficients"], dtype=float))
    probability = 1 / (1 + math.exp(-max(-8.0, min(8.0, win_logit))))
    home_runs = float(runtime["home_run_intercept"] + standardized @ np.asarray(runtime["home_run_coefficients"], dtype=float))
    away_runs = float(runtime["away_run_intercept"] + standardized @ np.asarray(runtime["away_run_coefficients"], dtype=float))
    # The classifier must influence the same score distribution shown in the UI. Convert its
    # log-odds to a conservative run-margin signal and combine it with the two run regressors.
    home_runs, away_runs = _coherent_run_means(probability, home_runs, away_runs)
    return probability, home_runs, away_runs


def run_model_lifecycle(session: Session, league: str) -> dict[str, Any]:
    now = datetime.now(KST)
    registry = session.scalar(select(ModelRegistry).where(ModelRegistry.league == league).with_for_update())
    if registry is None:
        registry = ModelRegistry(league=league, policy=POLICY)
        session.add(registry)
        session.flush()
    registry.policy = POLICY
    registry.last_evaluated_at = now

    samples = _training_samples(session, league)
    live_samples = [row for row in samples if row["origin"] == "LIVE_PREGAME"]
    rollback = _maybe_rollback(session, registry, samples, now)
    if rollback:
        return {**lifecycle_status(session, league), "decision": rollback}
    if len(samples) < MIN_TRAINING_SAMPLES:
        reason = f"학습 가능 표본 {len(samples)}개: 최소 {MIN_TRAINING_SAMPLES}개까지 기존 운영 모델을 유지합니다."
        _event_once(session, league, "WAITING_FOR_DATA", len(samples), reason,
                    champion_id=registry.champion_model_version_id)
        return {**lifecycle_status(session, league), "decision": "WAITING_FOR_DATA", "reason": reason}
    if len(live_samples) < MIN_VALIDATION_SAMPLES:
        reason = (f"과거 재현을 포함한 학습 표본은 {len(samples)}개지만 독립 실전 검증 표본이 "
                  f"{len(live_samples)}개입니다. {MIN_VALIDATION_SAMPLES}개 전에는 자동 승격하지 않습니다.")
        _event_once(session, league, "WAITING_FOR_LIVE_VALIDATION", len(live_samples), reason,
                    champion_id=registry.champion_model_version_id)
        return {**lifecycle_status(session, league), "decision": "WAITING_FOR_LIVE_VALIDATION", "reason": reason}

    latest_artifact = session.scalar(select(ModelArtifact).where(
        ModelArtifact.league == league,
    ).order_by(ModelArtifact.training_sample_size.desc(), ModelArtifact.created_at.desc()).limit(1))
    if latest_artifact and len(samples) - latest_artifact.training_sample_size < MIN_NEW_SAMPLES:
        return {
            **lifecycle_status(session, league), "decision": "NO_NEW_DATA",
            "reason": f"마지막 학습 후 새 표본이 {len(samples) - latest_artifact.training_sample_size}개입니다.",
        }

    artifact, candidate_metrics, comparator_metrics = _train_candidate(session, league, samples, now)
    promoted, reason = _promotion_decision(candidate_metrics, comparator_metrics)
    if promoted:
        registry.previous_model_version_id = registry.champion_model_version_id
        registry.champion_model_version_id = artifact.model_version_id
        registry.promoted_at = now
        event_type = "PROMOTED"
    else:
        event_type = "REJECTED"
    session.add(ModelLifecycleEvent(
        league=league, event_type=event_type, candidate_model_version_id=artifact.model_version_id,
        champion_model_version_id=registry.champion_model_version_id,
        sample_size=len(samples), metrics={"candidate": candidate_metrics, "comparator": comparator_metrics},
        reason=reason, created_at=now,
    ))
    session.flush()
    return {**lifecycle_status(session, league), "decision": event_type, "reason": reason}


def lifecycle_status(session: Session, league: str) -> dict[str, Any]:
    registry = session.get(ModelRegistry, league)
    champion = session.get(ModelVersion, registry.champion_model_version_id) if registry and registry.champion_model_version_id else None
    previous = session.get(ModelVersion, registry.previous_model_version_id) if registry and registry.previous_model_version_id else None
    samples = _training_samples(session, league)
    sample_size = len(samples)
    source_counts = {origin: sum(row["origin"] == origin for row in samples)
                     for origin in ("LIVE_PREGAME", "HISTORICAL_REPLAY")}
    events = session.scalars(select(ModelLifecycleEvent).where(
        ModelLifecycleEvent.league == league,
    ).order_by(ModelLifecycleEvent.created_at.desc()).limit(10)).all()
    return {
        "league": league,
        "operating_model": champion.name if champion else _baseline_name(league),
        "operating_mode": "AUTO_TRAINED_CHAMPION" if champion else "VERSIONED_BASELINE",
        "previous_model": previous.name if previous else None,
        "promoted_at": registry.promoted_at.isoformat() if registry and registry.promoted_at else None,
        "last_evaluated_at": registry.last_evaluated_at.isoformat() if registry and registry.last_evaluated_at else None,
        "evaluable_samples": sample_size,
        "sample_sources": source_counts,
        "training_ready": sample_size >= MIN_TRAINING_SAMPLES,
        "samples_needed": max(0, MIN_TRAINING_SAMPLES - sample_size),
        "policy": registry.policy if registry else POLICY,
        "events": [{
            "type": event.event_type, "sample_size": event.sample_size, "reason": event.reason,
            "metrics": event.metrics, "created_at": event.created_at.isoformat(),
        } for event in events],
    }


def _training_samples(session: Session, league: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PredictionSnapshot, Prediction, Game, GameResult)
        .join(Prediction, Prediction.id == PredictionSnapshot.prediction_id)
        .join(Game, Game.id == PredictionSnapshot.game_id)
        .join(GameResult, GameResult.game_id == Game.id)
        .where(Game.league == league, Game.status == "FINAL")
        .order_by(Game.start_at, PredictionSnapshot.captured_at)
    ).all()
    by_game: dict[int, dict[str, Any]] = {}
    for snapshot, prediction, game, result in rows:
        origin = prediction.origin or "LIVE_PREGAME"
        cutoff = prediction.data_cutoff or prediction.created_at
        if game.start_at and _naive(cutoff) > _naive(game.start_at):
            continue
        if origin == "LIVE_PREGAME" and game.start_at and _naive(snapshot.captured_at) > _naive(game.start_at):
            continue
        if origin == "HISTORICAL_REPLAY" and (
            not prediction.training_eligible or not bool((prediction.leakage_audit or {}).get("passed"))
        ):
            continue
        features = snapshot.input_payload.get("features") if snapshot.input_payload else None
        if not isinstance(features, dict):
            continue
        payload = prediction.payload or {}
        base_home = float(snapshot.input_payload.get("home_expected", payload.get("base_home_expected_runs", prediction.home_expected_runs)))
        base_away = float(snapshot.input_payload.get("away_expected", payload.get("base_away_expected_runs", prediction.away_expected_runs)))
        baseline_probability = _two_way_poisson_probability(base_home, base_away)
        candidate = {
            "game_id": game.id, "captured_at": cutoff, "features": features, "origin": origin,
            "base_home_runs": base_home, "base_away_runs": base_away,
            "baseline_probability": baseline_probability,
            "home_score": float(result.home_score), "away_score": float(result.away_score),
            "outcome": 1.0 if result.home_score > result.away_score else (.5 if result.home_score == result.away_score else 0.0),
        }
        current = by_game.get(game.id)
        # A real pregame observation always supersedes a retrospective reconstruction.
        if current is None or (current["origin"] != "LIVE_PREGAME" and origin == "LIVE_PREGAME") or (
            current["origin"] == origin and _naive(candidate["captured_at"]) >= _naive(current["captured_at"])
        ):
            by_game[game.id] = candidate
    return sorted(by_game.values(), key=lambda row: (_naive(row["captured_at"]), row["game_id"]))


def _train_candidate(session: Session, league: str, samples: list[dict[str, Any]], now: datetime
                     ) -> tuple[ModelArtifact, dict[str, float], dict[str, float]]:
    live = [row for row in samples if row["origin"] == "LIVE_PREGAME"]
    split = max(MIN_VALIDATION_SAMPLES, round(len(live) * VALIDATION_FRACTION))
    validation = live[-split:]
    validation_start = min(_naive(row["captured_at"]) for row in validation)
    # Strict walk-forward boundary: replay rows can enrich the fit only when their as-of cutoff
    # precedes the first live validation game.
    train = [row for row in samples if _naive(row["captured_at"]) < validation_start]
    x_train = np.vstack([_feature_values(row["features"], row["base_home_runs"], row["base_away_runs"])
                         for row in train])
    means = x_train.mean(axis=0)
    scales = x_train.std(axis=0)
    scales[scales < 1e-6] = 1.0
    standardized = (x_train - means) / scales
    outcomes = np.asarray([row["outcome"] for row in train])
    home_scores = np.asarray([row["home_score"] for row in train])
    away_scores = np.asarray([row["away_score"] for row in train])
    win_intercept, win_coefficients = _fit_logistic(standardized, outcomes)
    home_intercept, home_coefficients = _fit_ridge(standardized, home_scores)
    away_intercept, away_coefficients = _fit_ridge(standardized, away_scores)
    runtime = {
        "feature_names": TRAINABLE_FEATURES, "feature_means": means.tolist(), "feature_scales": scales.tolist(),
        "win_intercept": win_intercept, "win_coefficients": win_coefficients.tolist(),
        "home_run_intercept": home_intercept, "home_run_coefficients": home_coefficients.tolist(),
        "away_run_intercept": away_intercept, "away_run_coefficients": away_coefficients.tolist(),
    }
    candidate_metrics = _evaluate(runtime, validation)
    registry = session.get(ModelRegistry, league)
    comparator = _artifact_runtime(session, registry.champion_model_version_id) if registry else None
    comparator_metrics = _evaluate(comparator, validation) if comparator else _evaluate(None, validation)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{league}_AUTO_{stamp}"
    checksum_payload = {**runtime, "training_cutoff": train[-1]["captured_at"].isoformat(), "sample_size": len(samples)}
    checksum = hashlib.sha256(json.dumps(checksum_payload, sort_keys=True).encode()).hexdigest()
    model = ModelVersion(
        name=name, algorithm="standardized L2 logistic win classifier + ridge home/away run regressors; chronological holdout",
        feature_schema={"version": 5, "features": TRAINABLE_FEATURES}, checksum=checksum, created_at=now,
    )
    session.add(model)
    session.flush()
    artifact = ModelArtifact(
        model_version_id=model.id, league=league, training_cutoff=train[-1]["captured_at"],
        training_sample_size=len(samples), validation_metrics={"candidate": candidate_metrics, "comparator": comparator_metrics},
        created_at=now, **runtime,
    )
    session.add(artifact)
    session.flush()
    return artifact, candidate_metrics, comparator_metrics


def _promotion_decision(candidate: dict[str, float], comparator: dict[str, float]) -> tuple[bool, str]:
    p = POLICY["promotion"]
    improved = (candidate["brier"] <= comparator["brier"] - p["brier_improvement"] or
                candidate["run_mae"] <= comparator["run_mae"] - p["or_run_mae_improvement"])
    guarded = (candidate["brier"] <= comparator["brier"] + p["maximum_brier_regression"] and
               candidate["log_loss"] <= comparator["log_loss"] + p["maximum_log_loss_regression"] and
               candidate["run_mae"] <= comparator["run_mae"] + p["maximum_run_mae_regression"])
    if improved and guarded:
        return True, "동일한 날짜순 검증 구간에서 승률 또는 득점 오차가 개선되고 모든 성능 하한을 통과했습니다."
    return False, "후보 모델이 동일한 날짜순 검증 구간의 승격 기준과 성능 하한을 모두 통과하지 못했습니다."


def _maybe_rollback(session: Session, registry: ModelRegistry, samples: list[dict[str, Any]], now: datetime) -> str | None:
    if registry.champion_model_version_id is None or registry.promoted_at is None:
        return None
    live = [row for row in samples if row["origin"] == "LIVE_PREGAME" and
            _naive(row["captured_at"]) >= _naive(registry.promoted_at)]
    if len(live) < MIN_ROLLBACK_SAMPLES:
        return None
    champion = _artifact_runtime(session, registry.champion_model_version_id)
    previous = _artifact_runtime(session, registry.previous_model_version_id)
    if not champion:
        return None
    champion_metrics = _evaluate(champion, live)
    previous_metrics = _evaluate(previous, live) if previous else _evaluate(None, live)
    p = POLICY["rollback"]
    degraded = (
        champion_metrics["brier"] > previous_metrics["brier"] + p["brier_regression"] or
        champion_metrics["log_loss"] > previous_metrics["log_loss"] + p["log_loss_regression"] or
        champion_metrics["run_mae"] > previous_metrics["run_mae"] + p["run_mae_regression"]
    )
    if not degraded:
        return None
    failed_id = registry.champion_model_version_id
    registry.champion_model_version_id = registry.previous_model_version_id
    registry.previous_model_version_id = failed_id
    registry.promoted_at = now
    reason = f"승격 후 {len(live)}경기에서 이전 모델 대비 운영 성능 하락 기준을 초과하여 자동 롤백했습니다."
    session.add(ModelLifecycleEvent(
        league=registry.league, event_type="ROLLED_BACK", candidate_model_version_id=failed_id,
        champion_model_version_id=registry.champion_model_version_id, sample_size=len(live),
        metrics={"failed_champion": champion_metrics, "restored": previous_metrics}, reason=reason, created_at=now,
    ))
    session.flush()
    return "ROLLED_BACK"


def _evaluate(runtime: dict[str, Any] | None, samples: list[dict[str, Any]]) -> dict[str, float]:
    predictions = []
    for row in samples:
        if runtime:
            _classification_probability, home_runs, away_runs = predict_with_runtime(
                runtime, row["features"], row["base_home_runs"], row["base_away_runs"],
            )
            probability = _two_way_poisson_probability(home_runs, away_runs)
        else:
            probability, home_runs, away_runs = (
                row["baseline_probability"], row["base_home_runs"], row["base_away_runs"],
            )
        predictions.append((probability, home_runs, away_runs, row))
    brier = np.mean([(p - row["outcome"]) ** 2 for p, _, _, row in predictions])
    log_loss = np.mean([-row["outcome"] * math.log(_clip(p, .001, .999)) -
                        (1 - row["outcome"]) * math.log(1 - _clip(p, .001, .999))
                        for p, _, _, row in predictions])
    run_errors = [abs(h - row["home_score"]) for _, h, _, row in predictions]
    run_errors += [abs(a - row["away_score"]) for _, _, a, row in predictions]
    return {"sample_size": len(samples), "brier": round(float(brier), 6),
            "log_loss": round(float(log_loss), 6), "run_mae": round(float(np.mean(run_errors)), 6)}


def _fit_logistic(x: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    weights = np.zeros(x.shape[1], dtype=float)
    mean = _clip(float(y.mean()), .05, .95)
    intercept = math.log(mean / (1 - mean))
    for step in range(800):
        logits = np.clip(intercept + x @ weights, -20, 20)
        predicted = 1 / (1 + np.exp(-logits))
        error = predicted - y
        learning_rate = .08 / (1 + step / 250)
        intercept -= learning_rate * float(error.mean())
        weights -= learning_rate * ((x.T @ error) / len(y) + .08 * weights)
    return float(intercept), weights


def _fit_ridge(x: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    centered = y - y.mean()
    regularization = np.eye(x.shape[1]) * 6.0
    coefficients = np.linalg.pinv(x.T @ x + regularization) @ x.T @ centered
    return float(y.mean()), coefficients


def _coherent_run_means(probability: float, home_runs: float, away_runs: float) -> tuple[float, float]:
    home_runs, away_runs = _clip(home_runs, .6, 10.0), _clip(away_runs, .6, 10.0)
    total = _clip(home_runs + away_runs, 1.2, 20.0)
    logit = math.log(_clip(probability, .02, .98) / (1 - _clip(probability, .02, .98)))
    classifier_margin = 2.2 * logit
    combined_margin = .65 * (home_runs - away_runs) + .35 * classifier_margin
    combined_margin = _clip(combined_margin, -(total - 1.2), total - 1.2)
    return _clip((total + combined_margin) / 2, .6, 10.0), _clip((total - combined_margin) / 2, .6, 10.0)


def _two_way_poisson_probability(home_mean: float, away_mean: float) -> float:
    """Deterministic score proxy used for candidate comparison; production still uses Monte Carlo."""
    maximum = 30
    home_pmf = [math.exp(-home_mean) * home_mean ** score / math.factorial(score) for score in range(maximum)]
    away_pmf = [math.exp(-away_mean) * away_mean ** score / math.factorial(score) for score in range(maximum)]
    home_win = sum(home_pmf[h] * away_pmf[a] for h in range(maximum) for a in range(h))
    away_win = sum(home_pmf[h] * away_pmf[a] for a in range(maximum) for h in range(a))
    return home_win / max(home_win + away_win, 1e-12)


def _feature_values(features: dict[str, Any], base_home_runs: float, base_away_runs: float,
                    names: list[str] | None = None) -> np.ndarray:
    source = {**features, "base_home_expected": base_home_runs, "base_away_expected": base_away_runs}
    selected = TRAINABLE_FEATURES if names is None else names
    return np.asarray([float(source.get(name, 0.0) or 0.0) for name in selected], dtype=float)


def _artifact_runtime(session: Session, model_version_id: int | None) -> dict[str, Any] | None:
    if model_version_id is None:
        return None
    artifact = session.scalar(select(ModelArtifact).options(joinedload(ModelArtifact.model_version)).where(
        ModelArtifact.model_version_id == model_version_id,
    ))
    return _runtime(artifact) if artifact else None


def _runtime(artifact: ModelArtifact) -> dict[str, Any]:
    return {
        "model_name": artifact.model_version.name,
        "checksum": artifact.model_version.checksum,
        "feature_names": artifact.feature_names, "feature_means": artifact.feature_means,
        "feature_scales": artifact.feature_scales, "win_intercept": artifact.win_intercept,
        "win_coefficients": artifact.win_coefficients, "home_run_intercept": artifact.home_run_intercept,
        "home_run_coefficients": artifact.home_run_coefficients, "away_run_intercept": artifact.away_run_intercept,
        "away_run_coefficients": artifact.away_run_coefficients,
    }


def _event_once(session: Session, league: str, event_type: str, sample_size: int, reason: str,
                champion_id: int | None) -> None:
    latest = session.scalar(select(ModelLifecycleEvent).where(
        ModelLifecycleEvent.league == league, ModelLifecycleEvent.event_type == event_type,
    ).order_by(ModelLifecycleEvent.created_at.desc()).limit(1))
    if latest and latest.sample_size == sample_size:
        return
    session.add(ModelLifecycleEvent(
        league=league, event_type=event_type, champion_model_version_id=champion_id,
        sample_size=sample_size, metrics={}, reason=reason, created_at=datetime.now(KST),
    ))


def _baseline_name(league: str) -> str:
    return "KBO_MATCHUP_V11" if league == "KBO" else "MLB_MATCHUP_V10"


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
