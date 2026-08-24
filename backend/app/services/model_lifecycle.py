from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
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
# Lifecycle checks use the same simulator and recipe as production at a reduced, deterministic
# draw count. Common seeds make challenger/comparator differences stable without multiplying a
# scheduled retrain into millions of unnecessary draws.
MODEL_VALIDATION_SIMULATIONS = 4000
CURRENT_MODEL_SCHEMA_VERSION = 9

TRAINABLE_FEATURES = [
    "base_home_expected", "base_away_expected", "league_average_runs",
    "season_win_rate_diff", "recent_10_win_rate_diff", "recent_run_diff",
    "recent_run_allowed_diff", "runs_per_game_diff", "runs_allowed_per_game_diff",
    "strength_elo_diff", "strength_srs_diff", "pythagorean_diff", "schedule_strength_diff",
    "adjusted_offense_diff", "adjusted_defense_edge",
    "home_avg", "away_avg", "home_obp", "away_obp", "home_slg", "away_slg",
    "home_ops", "away_ops", "split_win_rate_diff", "starter_era_diff",
    "starter_whip_diff", "starter_war_diff", "starter_fip_diff", "starter_k_bb_diff",
    "starter_durability_diff", "quality_start_rate_diff", "bullpen_proxy_diff",
    "starter_rest_days_diff", "recent_pitch_burden_diff", "rest_days_diff",
    "doubleheader_diff", "head_to_head_diff", "head_to_head_run_diff",
    "starter_opponent_era_diff", "park_factor", "lineup_strength_diff", "lineup_bvp_diff",
    "bullpen_fatigue_edge", "schedule_fatigue_edge", "lineup_platoon_diff",
    "starter_recent_era_diff", "starter_recent_k_bb_diff", "fielding_edge", "baserunning_edge", "catcher_control_edge",
    "starter_xera_diff", "starter_xwoba_diff", "starter_velocity_trend_edge",
    "starter_arsenal_stability_edge", "lineup_xwoba_diff", "lineup_pitch_type_edge",
    "lineup_frv_edge", "lineup_oaa_edge", "catcher_framing_edge", "battery_edge",
    "weather_run_multiplier",
    "home_starter_confirmed", "away_starter_confirmed", "home_lineup_confirmed",
    "away_lineup_confirmed", "recent_home_games", "recent_away_games",
]

POLICY = {
    "minimum_training_samples": MIN_TRAINING_SAMPLES,
    "minimum_chronological_validation_samples": MIN_VALIDATION_SAMPLES,
    "minimum_new_samples_for_retraining": MIN_NEW_SAMPLES,
    "minimum_live_samples_for_rollback": MIN_ROLLBACK_SAMPLES,
    "promotion": {
        "brier_improvement": .002,
        "or_run_mae_improvement": .10,
        "or_margin_mae_improvement": .10,
        "maximum_brier_regression": .005,
        "maximum_log_loss_regression": .01,
        "maximum_run_mae_regression": .05,
        "maximum_margin_mae_regression": .10,
        "maximum_margin_sd_shrink_fraction": .10,
    },
    "rollback": {"brier_regression": .015, "log_loss_regression": .025,
                 "run_mae_regression": .15, "margin_mae_regression": .20},
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
    residual_stack = bool(runtime.get("residual_stack"))
    if residual_stack:
        from backend.app.services.feature_engineering import logistic_probability
        from backend.app.services.prediction import blend_classifier_into_means

        baseline_probability = logistic_probability(defaultdict(float, features))
        baseline_home, baseline_away = blend_classifier_into_means(
            baseline_probability, base_home_runs, base_away_runs,
        )
        baseline_logit = math.log(
            _clip(baseline_probability, .001, .999) /
            (1 - _clip(baseline_probability, .001, .999))
        )
    else:
        baseline_home, baseline_away, baseline_logit = 0.0, 0.0, 0.0
    win_logit = float(baseline_logit + runtime["win_intercept"] + standardized @ np.asarray(
        runtime["win_coefficients"], dtype=float,
    ))
    probability = 1 / (1 + math.exp(-max(-8.0, min(8.0, win_logit))))
    home_runs = float(baseline_home + runtime["home_run_intercept"] + standardized @ np.asarray(
        runtime["home_run_coefficients"], dtype=float,
    ))
    away_runs = float(baseline_away + runtime["away_run_intercept"] + standardized @ np.asarray(
        runtime["away_run_coefficients"], dtype=float,
    ))
    margin_runs = None
    if runtime.get("margin_intercept") is not None and runtime.get("margin_coefficients") is not None:
        margin_runs = float((baseline_home - baseline_away if residual_stack else 0.0)
                            + runtime["margin_intercept"] + standardized @ np.asarray(
            runtime["margin_coefficients"], dtype=float,
        ))
    if residual_stack:
        # baseline_home/away already contain the baseline classifier tilt. Apply only the
        # *change* in fitted log-odds here; sending the absolute probability through the same
        # conversion again would double-count the classifier even when every correction is 0.
        total = _clip(home_runs + away_runs, 1.2, 20.0)
        run_margin = home_runs - away_runs if margin_runs is None else margin_runs
        combined_margin = run_margin + .35 * 2.2 * (win_logit - baseline_logit)
        combined_margin = _clip(combined_margin, -(total - 1.2), total - 1.2)
        home_runs = _clip((total + combined_margin) / 2, .6, 10.0)
        away_runs = _clip((total - combined_margin) / 2, .6, 10.0)
    else:
        # Legacy raw artifacts still need their absolute classifier probability made coherent
        # with the independently fitted score means.
        home_runs, away_runs = _coherent_run_means(probability, home_runs, away_runs, margin_runs)
    return probability, home_runs, away_runs


def evaluate_candidate(session: Session, league: str) -> dict[str, Any]:
    """Train a challenger and report how it scores, without touching the live champion.

    The real lifecycle refuses to promote until enough independent live forecasts exist. That
    guard is right, but it also hides whether the replay archive is already good enough to beat
    the baseline, so this reports the same comparison read-only.
    """
    samples = _training_samples(session, league)
    if len(samples) < MIN_TRAINING_SAMPLES:
        return {"league": league, "trained": False, "samples": len(samples),
                "reason": f"학습 표본 {len(samples)}개로 최소 {MIN_TRAINING_SAMPLES}개에 미달합니다."}
    modeling_samples, modeling_cohort = _modeling_cohort(samples)
    validation, validation_source = _validation_partition(modeling_samples)
    validation_start = min(_naive(row["captured_at"]) for row in validation)
    train = [row for row in modeling_samples if _naive(row["captured_at"]) < validation_start]
    if len(train) < MIN_TRAINING_SAMPLES:
        return {"league": league, "trained": False, "samples": len(samples),
                "reason": f"검증 구간을 제외한 학습 표본이 {len(train)}개로 부족합니다."}
    try:
        artifact, candidate_metrics, comparator_metrics = _train_candidate(session, league, samples, datetime.now(KST))
        # Read the fitted coefficients out before the rollback: rolling back expires every
        # attribute on the pending artifact, which silently reported every weight as zero.
        starter_weights = _starter_feature_weights(artifact)
    finally:
        # A dry run must leave nothing behind, not even an unreferenced artifact row.
        session.rollback()
    promoted, reason = _promotion_decision(candidate_metrics, comparator_metrics)
    return {
        "league": league, "trained": True, "dry_run": True,
        "samples": len(samples), "modeling_samples": len(modeling_samples),
        "modeling_cohort": modeling_cohort,
        "train_samples": len(train), "validation_samples": len(validation),
        "validation_source": validation_source,
        "candidate": candidate_metrics, "comparator": comparator_metrics,
        "would_promote": promoted, "reason": reason,
        "policy": POLICY["promotion"],
        "starter_feature_weights": starter_weights,
        "constant_training_features": _constant_features(train),
    }


def _constant_features(train: list[dict[str, Any]]) -> dict[str, Any]:
    """Inputs with no variance across the training rows.

    A standardized fit cannot learn from a column that never moves: its scale is forced to 1 and
    every standardized value is 0, so the coefficient stays exactly 0. Reporting these separates
    "the data says this does not matter" from "this never reached the trainer".
    """
    matrix = np.vstack([_feature_values(row["features"], row["base_home_runs"], row["base_away_runs"])
                        for row in train])
    spread = matrix.std(axis=0)
    constant = [name for name, value in zip(TRAINABLE_FEATURES, spread, strict=True) if value < 1e-9]
    return {
        "count": len(constant),
        "of_total": len(TRAINABLE_FEATURES),
        "names": constant,
    }


def _starter_feature_weights(artifact: ModelArtifact) -> dict[str, Any]:
    """Standardized coefficients, for auditing how much the fit leans on each input.

    Standardizing first makes the magnitudes directly comparable: each is the effect of moving
    that input one standard deviation, so the starter's share can be read against everything else.
    """
    names = list(artifact.feature_names)
    win = [float(value) for value in artifact.win_coefficients]
    home = [float(value) for value in artifact.home_run_coefficients]
    away = [float(value) for value in artifact.away_run_coefficients]

    margin = [float(value) for value in (artifact.margin_coefficients or [])]

    def row(index: int) -> dict[str, float]:
        return {"win": round(win[index], 4), "home_runs": round(home[index], 4),
                "away_runs": round(away[index], 4),
                "margin": round(margin[index], 4) if index < len(margin) else 0.0}

    starter_names = [name for name in names
                     if name.startswith("starter_") or name == "quality_start_rate_diff"]
    starter_total = sum(abs(win[names.index(name)]) for name in starter_names)
    win_total = sum(abs(value) for value in win) or 1.0
    ranked = sorted(names, key=lambda name: -abs(win[names.index(name)]))[:8]
    return {
        "starter_inputs": {name: row(names.index(name)) for name in starter_names},
        "starter_share_of_win_weight": round(starter_total / win_total, 4),
        "largest_win_weights": {name: row(names.index(name)) for name in ranked},
    }


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
    replay_champion_audit = _maybe_rollback_compressed_replay_champion(
        session, registry, samples, now,
    )
    if replay_champion_audit:
        return {**lifecycle_status(session, league), "decision": replay_champion_audit,
                "reason": "과거 재현으로 승격된 모델이 새 마진 분산 하한을 통과하지 못해 기준 모델로 복귀했습니다."}
    rollback = _maybe_rollback(session, registry, samples, now)
    if rollback:
        return {**lifecycle_status(session, league), "decision": rollback}
    if len(samples) < MIN_TRAINING_SAMPLES:
        reason = f"학습 가능 표본 {len(samples)}개: 최소 {MIN_TRAINING_SAMPLES}개까지 기존 운영 모델을 유지합니다."
        _event_once(session, league, "WAITING_FOR_DATA", len(samples), reason,
                    champion_id=registry.champion_model_version_id)
        return {**lifecycle_status(session, league), "decision": "WAITING_FOR_DATA", "reason": reason}
    latest_artifact = session.scalar(select(ModelArtifact).where(
        ModelArtifact.league == league,
    ).order_by(ModelArtifact.training_sample_size.desc(), ModelArtifact.created_at.desc()).limit(1))
    latest_schema = int(((latest_artifact.model_version.feature_schema if latest_artifact else {}) or {}).get(
        "version", 0,
    ))
    if (latest_artifact and latest_schema >= CURRENT_MODEL_SCHEMA_VERSION and
            len(samples) - latest_artifact.training_sample_size < MIN_NEW_SAMPLES):
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
            "league": league,
            "simulation_recipe": payload.get("simulation_recipe"),
            "residual_context": (snapshot.input_payload or {}).get("team_residuals") or {},
            "market_context": (snapshot.input_payload or {}).get("headline_market") or {},
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
                     ) -> tuple[ModelArtifact, dict[str, Any], dict[str, Any]]:
    all_sample_count = len(samples)
    samples, modeling_cohort = _modeling_cohort(samples)
    validation, validation_source = _validation_partition(samples)
    validation_start = min(_naive(row["captured_at"]) for row in validation)
    # Strict walk-forward boundary: every fit row must precede the first holdout first-pitch
    # cutoff, whether the holdout came from live forecasts or audited historical replays.
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
    baseline = [_baseline_offsets(row) for row in train]
    baseline_logits = np.asarray([row[0] for row in baseline])
    baseline_home = np.asarray([row[1] for row in baseline])
    baseline_away = np.asarray([row[2] for row in baseline])
    win_intercept, win_coefficients = _fit_logistic_offset(standardized, outcomes, baseline_logits)
    home_intercept, home_coefficients = _fit_ridge(standardized, home_scores - baseline_home)
    away_intercept, away_coefficients = _fit_ridge(standardized, away_scores - baseline_away)
    margin_intercept, margin_coefficients = _fit_ridge(
        standardized, (home_scores - away_scores) - (baseline_home - baseline_away),
    )
    runtime = {
        "feature_names": TRAINABLE_FEATURES, "feature_means": means.tolist(), "feature_scales": scales.tolist(),
        "win_intercept": win_intercept, "win_coefficients": win_coefficients.tolist(),
        "home_run_intercept": home_intercept, "home_run_coefficients": home_coefficients.tolist(),
        "away_run_intercept": away_intercept, "away_run_coefficients": away_coefficients.tolist(),
        "margin_intercept": margin_intercept, "margin_coefficients": margin_coefficients.tolist(),
        "residual_stack": True,
    }
    candidate_metrics = _evaluate(runtime, validation)
    candidate_metrics["validation_source"] = validation_source
    candidate_metrics["modeling_cohort"] = modeling_cohort
    candidate_metrics["modeling_sample_size"] = len(samples)
    candidate_metrics["training_source_counts"] = {
        origin: sum(row["origin"] == origin for row in train)
        for origin in ("LIVE_PREGAME", "HISTORICAL_REPLAY")
    }
    registry = session.get(ModelRegistry, league)
    comparator = _artifact_runtime(session, registry.champion_model_version_id) if registry else None
    comparator_metrics = _evaluate(comparator, validation) if comparator else _evaluate(None, validation)
    comparator_metrics["validation_source"] = validation_source
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{league}_AUTO_{stamp}"
    checksum_payload = {**runtime, "training_cutoff": train[-1]["captured_at"].isoformat(), "sample_size": len(samples)}
    checksum = hashlib.sha256(json.dumps(checksum_payload, sort_keys=True).encode()).hexdigest()
    model = ModelVersion(
        name=name, algorithm=("standardized L2 logistic win classifier + ridge home/away run and signed-margin regressors; "
                             "leakage-audited chronological holdout"),
        feature_schema={"version": CURRENT_MODEL_SCHEMA_VERSION, "features": TRAINABLE_FEATURES},
        checksum=checksum, created_at=now,
    )
    session.add(model)
    session.flush()
    artifact = ModelArtifact(
        model_version_id=model.id, league=league, training_cutoff=train[-1]["captured_at"],
        training_sample_size=all_sample_count,
        validation_metrics={"candidate": candidate_metrics, "comparator": comparator_metrics},
        created_at=now, **{key: value for key, value in runtime.items() if key != "residual_stack"},
    )
    session.add(artifact)
    session.flush()
    return artifact, candidate_metrics, comparator_metrics


def _modeling_cohort(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Select a chronologically coherent feature-availability cohort.

    Archived starter rows enter the database in a contiguous recent block. Mixing a training
    period with almost no starter inputs and a holdout with nearly complete starter inputs makes
    the fitted starter coefficients extrapolate far outside their training distribution. Once
    enough two-starter games exist, train and validate inside that complete block instead.
    """
    starter_complete = [row for row in samples if bool(row["features"].get("home_starter_confirmed"))
                        and bool(row["features"].get("away_starter_confirmed"))]
    minimum_complete = MIN_TRAINING_SAMPLES + MIN_VALIDATION_SAMPLES
    if len(starter_complete) >= minimum_complete:
        return starter_complete, "BOTH_STARTERS_CONFIRMED_CHRONOLOGICAL_COHORT"
    return samples, "ALL_LEAKAGE_AUDITED_SAMPLES"


def _validation_partition(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Prefer live holdout data, falling back to audited chronological replay data."""
    live = [row for row in samples if row["origin"] == "LIVE_PREGAME"]
    if len(live) >= MIN_VALIDATION_SAMPLES:
        split = max(MIN_VALIDATION_SAMPLES, round(len(live) * VALIDATION_FRACTION))
        validation = live[-split:]
        validation_source = "LIVE_PREGAME_CHRONOLOGICAL_HOLDOUT"
    else:
        # A leakage-audited historical replay has an explicit first-pitch cutoff. Once a season
        # has been backfilled, its latest chronological block is valid out-of-time validation
        # and should not sit unused merely because the service launched late in the season.
        split = max(MIN_VALIDATION_SAMPLES, round(len(samples) * VALIDATION_FRACTION))
        validation = samples[-split:]
        validation_source = "LEAKAGE_AUDITED_CHRONOLOGICAL_HOLDOUT"
    return validation, validation_source


def _promotion_decision(candidate: dict[str, Any], comparator: dict[str, Any]) -> tuple[bool, str]:
    p = POLICY["promotion"]
    improved = (candidate["brier"] <= comparator["brier"] - p["brier_improvement"] or
                candidate["run_mae"] <= comparator["run_mae"] - p["or_run_mae_improvement"] or
                candidate["margin_mae"] <= comparator["margin_mae"] - p["or_margin_mae_improvement"])
    guarded = (candidate["brier"] <= comparator["brier"] + p["maximum_brier_regression"] and
               candidate["log_loss"] <= comparator["log_loss"] + p["maximum_log_loss_regression"] and
               candidate["run_mae"] <= comparator["run_mae"] + p["maximum_run_mae_regression"] and
               candidate["margin_mae"] <= comparator["margin_mae"] + p["maximum_margin_mae_regression"])
    comparator_margin_sd = float(comparator.get("predicted_margin_sd") or 0.0)
    candidate_margin_sd = float(candidate.get("predicted_margin_sd") or 0.0)
    diversity_guarded = (
        comparator_margin_sd <= 0 or
        candidate_margin_sd >= comparator_margin_sd * (1 - p["maximum_margin_sd_shrink_fraction"])
    )
    if improved and guarded and diversity_guarded:
        return True, "동일한 날짜순 검증 구간에서 승률 또는 득점 오차가 개선되고 모든 성능 하한을 통과했습니다."
    if not diversity_guarded:
        return False, "후보 모델이 기준 모델보다 경기별 예상 마진 분산을 과도하게 줄여 접전 편향 방지 하한을 통과하지 못했습니다."
    return False, "후보 모델이 동일한 날짜순 검증 구간의 승격 기준과 성능 하한을 모두 통과하지 못했습니다."


def _maybe_rollback_compressed_replay_champion(session: Session, registry: ModelRegistry,
                                               samples: list[dict[str, Any]], now: datetime
                                               ) -> str | None:
    """Re-audit replay-promoted champions when a distribution-collapse guard is introduced."""
    if registry.champion_model_version_id is None:
        return None
    artifact = session.scalar(select(ModelArtifact).where(
        ModelArtifact.model_version_id == registry.champion_model_version_id,
    ))
    candidate_metadata = ((artifact.validation_metrics or {}).get("candidate") or {}) if artifact else {}
    source_counts = candidate_metadata.get("training_source_counts") or {}
    if int(source_counts.get("LIVE_PREGAME") or 0) > 0:
        return None
    cohort, _source = _modeling_cohort(samples)
    if len(cohort) < MIN_TRAINING_SAMPLES:
        return None
    validation, _validation_source = _validation_partition(cohort)
    champion = _artifact_runtime(session, registry.champion_model_version_id)
    baseline_metrics = _evaluate(None, validation)
    champion_metrics = _evaluate(champion, validation)
    promoted, reason = _promotion_decision(champion_metrics, baseline_metrics)
    if promoted:
        return None
    comparator_margin_sd = float(baseline_metrics.get("predicted_margin_sd") or 0.0)
    champion_margin_sd = float(champion_metrics.get("predicted_margin_sd") or 0.0)
    if comparator_margin_sd <= 0 or champion_margin_sd >= comparator_margin_sd * (
        1 - POLICY["promotion"]["maximum_margin_sd_shrink_fraction"]
    ):
        return None
    failed_id = registry.champion_model_version_id
    _restore_versioned_baseline(registry, failed_id)
    registry.promoted_at = now
    session.add(ModelLifecycleEvent(
        league=registry.league, event_type="ROLLED_BACK", candidate_model_version_id=failed_id,
        champion_model_version_id=registry.champion_model_version_id, sample_size=len(samples),
        metrics={"failed_champion": champion_metrics, "restored_baseline": baseline_metrics},
        reason=reason, created_at=now,
    ))
    session.flush()
    return "ROLLED_BACK_DISTRIBUTION_COLLAPSE"


def _restore_versioned_baseline(registry: ModelRegistry, failed_id: int) -> None:
    """Disable a collapsed replay champion instead of swapping between learned models.

    The audit compares the champion against the versioned baseline, so the safe rollback target
    is that baseline (represented by a null champion).  Restoring ``previous_model_version_id``
    could reactivate another replay-trained champion and make successive lifecycle runs oscillate
    forever between two models that both fail the same distribution guard.
    """
    registry.champion_model_version_id = None
    registry.previous_model_version_id = failed_id


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
        champion_metrics["run_mae"] > previous_metrics["run_mae"] + p["run_mae_regression"] or
        champion_metrics["margin_mae"] > previous_metrics["margin_mae"] + p["margin_mae_regression"]
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


def _evaluate(runtime: dict[str, Any] | None, samples: list[dict[str, Any]]) -> dict[str, Any]:
    predictions = []
    methods = []
    for row in samples:
        probability, home_runs, away_runs, method = _operating_prediction(runtime, row)
        predictions.append((probability, home_runs, away_runs, row))
        methods.append(method)
    brier = np.mean([(p - row["outcome"]) ** 2 for p, _, _, row in predictions])
    log_loss = np.mean([-row["outcome"] * math.log(_clip(p, .001, .999)) -
                        (1 - row["outcome"]) * math.log(1 - _clip(p, .001, .999))
                        for p, _, _, row in predictions])
    run_errors = [abs(h - row["home_score"]) for _, h, _, row in predictions]
    run_errors += [abs(a - row["away_score"]) for _, _, a, row in predictions]
    margin_errors = [abs((h - a) - (row["home_score"] - row["away_score"]))
                     for _, h, a, row in predictions]
    predicted_margins = np.asarray([h - a for _, h, a, _row in predictions], dtype=float)
    actual_margins = np.asarray([row["home_score"] - row["away_score"]
                                 for _, _h, _a, row in predictions], dtype=float)
    predicted_totals = np.asarray([h + a for _, h, a, _row in predictions], dtype=float)
    actual_totals = np.asarray([row["home_score"] + row["away_score"]
                                for _, _h, _a, row in predictions], dtype=float)
    return {"sample_size": len(samples), "brier": round(float(brier), 6),
            "log_loss": round(float(log_loss), 6), "run_mae": round(float(np.mean(run_errors)), 6),
            "margin_mae": round(float(np.mean(margin_errors)), 6),
            "predicted_margin_sd": round(float(predicted_margins.std()), 6),
            "actual_margin_sd": round(float(actual_margins.std()), 6),
            "predicted_total_sd": round(float(predicted_totals.std()), 6),
            "actual_total_sd": round(float(actual_totals.std()), 6),
            "evaluation_method": (
                "OPERATING_MONTE_CARLO_RECIPE" if all(
                    method == "OPERATING_MONTE_CARLO_RECIPE" for method in methods
                ) else "MIXED_OPERATING_RECIPE_AND_POISSON_FALLBACK"
            ),
            "simulation_draws_per_game": MODEL_VALIDATION_SIMULATIONS}


def _operating_prediction(runtime: dict[str, Any] | None,
                          row: dict[str, Any]) -> tuple[float, float, float, str]:
    """Evaluate through the exact production score engine, not an unrelated Poisson proxy."""
    from backend.app.services.feature_engineering import logistic_probability
    from backend.app.services.prediction import apply_market_consensus_anchor, blend_classifier_into_means
    from backend.app.services.simulation import simulate_scores
    from backend.app.services.team_residuals import apply_residual_adjustment

    if runtime:
        _probability, home_runs, away_runs = predict_with_runtime(
            runtime, row["features"], row["base_home_runs"], row["base_away_runs"],
        )
    else:
        # Older immutable live snapshots legitimately predate some baseline features. They
        # remain the best real pregame observations, so evaluate missing fields as neutral
        # instead of dropping the game or letting one legacy row abort the lifecycle run.
        probability = logistic_probability(defaultdict(float, row["features"]))
        home_runs, away_runs = blend_classifier_into_means(
            probability, row["base_home_runs"], row["base_away_runs"],
        )
    home_runs, away_runs = apply_residual_adjustment(
        home_runs, away_runs, row.get("residual_context") or {},
    )
    home_runs, away_runs, _market = apply_market_consensus_anchor(
        home_runs, away_runs, row.get("market_context") or {}, str(row.get("league") or "MLB"),
    )
    recipe = row.get("simulation_recipe")
    if not isinstance(recipe, dict):
        return (_two_way_poisson_probability(home_runs, away_runs), home_runs, away_runs,
                "POISSON_FALLBACK")
    result = simulate_scores(
        home_runs, away_runs, MODEL_VALIDATION_SIMULATIONS, int(recipe.get("seed", 0)),
        float(recipe.get("environment_variance", .08)), float(recipe.get("team_variance", .12)),
        league=str(recipe.get("league") or row.get("league") or "MLB"),
        home_staff=recipe.get("home_staff"), away_staff=recipe.get("away_staff"),
        home_lineup=(np.asarray(recipe["home_lineup"], dtype=float)
                     if recipe.get("home_lineup") is not None else None),
        away_lineup=(np.asarray(recipe["away_lineup"], dtype=float)
                     if recipe.get("away_lineup") is not None else None),
        home_team_variance=recipe.get("home_team_variance"),
        away_team_variance=recipe.get("away_team_variance"),
        headline_total_line=recipe.get("headline_total_line"),
        headline_home_spread=recipe.get("headline_home_spread"),
        probability_calibration=recipe.get("probability_calibration"),
        home_event_factors=recipe.get("home_event_factors"),
        away_event_factors=recipe.get("away_event_factors"),
    )
    return (float(result["home_two_way_probability"]), float(result["mean_runs"]["home"]),
            float(result["mean_runs"]["away"]), "OPERATING_MONTE_CARLO_RECIPE")


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


def _fit_logistic_offset(x: np.ndarray, y: np.ndarray,
                         offset: np.ndarray) -> tuple[float, np.ndarray]:
    """Fit conservative log-odds corrections on top of the versioned baseline classifier."""
    weights = np.zeros(x.shape[1], dtype=float)
    intercept = 0.0
    for step in range(800):
        logits = np.clip(offset + intercept + x @ weights, -20, 20)
        predicted = 1 / (1 + np.exp(-logits))
        error = predicted - y
        learning_rate = .05 / (1 + step / 250)
        intercept -= learning_rate * float(error.mean())
        # A stronger penalty than the raw challenger keeps a few hundred replay rows from
        # overwhelming the already validated baseline with correlated feature corrections.
        weights -= learning_rate * ((x.T @ error) / len(y) + .25 * weights)
    return float(intercept), weights


def _baseline_offsets(row: dict[str, Any]) -> tuple[float, float, float]:
    """Return the leakage-safe baseline logit and run means used as residual-model offsets."""
    from backend.app.services.feature_engineering import logistic_probability
    from backend.app.services.prediction import apply_market_consensus_anchor, blend_classifier_into_means
    from backend.app.services.team_residuals import apply_residual_adjustment

    probability = logistic_probability(defaultdict(float, row["features"]))
    home_runs, away_runs = blend_classifier_into_means(
        probability, row["base_home_runs"], row["base_away_runs"],
    )
    # Production applies the leakage-safe team residual layer after model inference. Fit the
    # learned correction against that same operating baseline; otherwise the challenger learns
    # residual effects already applied a second time during validation and production.
    home_runs, away_runs = apply_residual_adjustment(
        home_runs, away_runs, row.get("residual_context") or {},
    )
    home_runs, away_runs, _market = apply_market_consensus_anchor(
        home_runs, away_runs, row.get("market_context") or {}, str(row.get("league") or "MLB"),
    )
    logit = math.log(_clip(probability, .001, .999) / (1 - _clip(probability, .001, .999)))
    return logit, home_runs, away_runs


def _fit_ridge(x: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    centered = y - y.mean()
    regularization = np.eye(x.shape[1]) * 6.0
    coefficients = np.linalg.pinv(x.T @ x + regularization) @ x.T @ centered
    return float(y.mean()), coefficients


def _coherent_run_means(probability: float, home_runs: float, away_runs: float,
                        margin_runs: float | None = None) -> tuple[float, float]:
    home_runs, away_runs = _clip(home_runs, .6, 10.0), _clip(away_runs, .6, 10.0)
    total = _clip(home_runs + away_runs, 1.2, 20.0)
    logit = math.log(_clip(probability, .02, .98) / (1 - _clip(probability, .02, .98)))
    classifier_margin = 2.2 * logit
    run_margin = home_runs - away_runs if margin_runs is None else margin_runs
    combined_margin = .65 * run_margin + .35 * classifier_margin
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
    schema = artifact.model_version.feature_schema or {}
    return {
        "model_name": artifact.model_version.name,
        "checksum": artifact.model_version.checksum,
        "feature_names": artifact.feature_names, "feature_means": artifact.feature_means,
        "feature_scales": artifact.feature_scales, "win_intercept": artifact.win_intercept,
        "win_coefficients": artifact.win_coefficients, "home_run_intercept": artifact.home_run_intercept,
        "home_run_coefficients": artifact.home_run_coefficients, "away_run_intercept": artifact.away_run_intercept,
        "away_run_coefficients": artifact.away_run_coefficients,
        "margin_intercept": artifact.margin_intercept,
        "margin_coefficients": artifact.margin_coefficients,
        "residual_stack": int(schema.get("version") or 0) >= 8,
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
    return "KBO_MATCHUP_V16" if league == "KBO" else "MLB_MATCHUP_V15"


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
