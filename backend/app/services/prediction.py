from __future__ import annotations

import hashlib
import json
from statistics import NormalDist
from typing import Any

from backend.app.config import settings
from backend.app.services.bullpen import staff_payload
from backend.app.services.feature_engineering import build_features, expected_runs, logistic_probability
from backend.app.services.model_lifecycle import predict_with_runtime
from backend.app.services.simulation import simulate_scores


MODEL_ALGORITHM = ("dynamic league environment + matchup-strength means + win-loss classifier blended into "
                   "simulation means + validated overdispersed correlated gamma-Poisson score distribution "
                   "+ inning-by-inning pitcher plan (starter workload, then leverage-tiered relief) "
                   "+ league-accurate extra innings "
                   "(MLB ghost-runner tiebreaker until decided, KBO ties stand after inning 11)")


def predict_game(game: Any, home: Any, away: Any, home_pitcher: Any | None, away_pitcher: Any | None,
                 lineups: list[Any] | None = None, game_context: dict[str, Any] | None = None,
                 model_runtime: dict[str, Any] | None = None,
                 bullpens: dict[str, dict[str, Any] | None] | None = None,
                 lineup_tables: dict[str, Any] | None = None) -> dict[str, Any]:
    model_name = (model_runtime or {}).get("model_name") or (
        "KBO_MATCHUP_V14" if game.league == "KBO" else "MLB_MATCHUP_V13"
    )
    features = build_features(home, away, home_pitcher, away_pitcher, game.stadium, game.league, lineups,
                              getattr(game, "game_date", None), game_context)
    base_logistic = logistic_probability(features)
    base_home_runs, base_away_runs, league_avg = expected_runs(
        home, away, home_pitcher, away_pitcher, float(features["park_factor"]),
        float(features["lineup_strength_diff"]), features,
    )
    logistic, home_runs, away_runs = base_logistic, base_home_runs, base_away_runs
    if model_runtime:
        logistic, home_runs, away_runs = predict_with_runtime(
            model_runtime, features, base_home_runs, base_away_runs,
        )
    # Runs alone underweight win-rate signals (record, recent form, starter dominance,
    # close-game execution) that the classifier reads. Tilt the simulation means toward a
    # probability blend so the displayed favorite reflects both, while every headline metric
    # still comes from the single simulated score population.
    home_runs, away_runs = blend_classifier_into_means(logistic, home_runs, away_runs)
    home_staff = staff_payload((bullpens or {}).get("home"), float(features["home_starter_multiplier"]),
                               float(features["home_starter_innings"]))
    away_staff = staff_payload((bullpens or {}).get("away"), float(features["away_starter_multiplier"]),
                               float(features["away_starter_innings"]))
    lineup_fingerprint = [
        (getattr(item, "side", None), getattr(item, "batting_order", None), getattr(item, "player_id", None),
         getattr(item, "player_name", None), getattr(item, "position", None), getattr(item, "value", None),
         getattr(item, "value_metric", None), getattr(item, "confirmed", None),
         getattr(item, "opponent_pitcher_id", None), getattr(item, "matchup_plate_appearances", None),
         getattr(item, "matchup_at_bats", None), getattr(item, "matchup_hits", None),
         getattr(item, "matchup_doubles", None), getattr(item, "matchup_triples", None),
         getattr(item, "matchup_home_runs", None), getattr(item, "matchup_walks", None),
         getattr(item, "matchup_hit_by_pitch", None), getattr(item, "matchup_strikeouts", None),
         getattr(item, "matchup_avg", None), getattr(item, "matchup_obp", None),
         getattr(item, "matchup_slg", None), getattr(item, "matchup_ops", None))
        for item in (lineups or [])
    ]
    input_data = {
        "game": game.external_id,
        "simulation_summary_schema": 9,
        "features": features,
        "home_expected": round(base_home_runs, 6),
        "away_expected": round(base_away_runs, 6),
        "model": model_name,
        "model_checksum": (model_runtime or {}).get("checksum"),
        "pitchers": [(getattr(away_pitcher, "player_id", None), getattr(away_pitcher, "name", None)),
                     (getattr(home_pitcher, "player_id", None), getattr(home_pitcher, "name", None))],
        # Including the staff plan means a bullpen profile update produces a new input hash,
        # so the next refresh regenerates the prediction instead of reusing a stale one.
        "staff": {"home": home_staff, "away": away_staff},
        # Split coverage belongs in the hash: adding a hitter's splits changes which engine runs.
        "split_coverage": (lineup_tables or {}).get("coverage"),
        "lineups": lineup_fingerprint,
    }
    input_hash = hashlib.sha256(json.dumps(input_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    seed = int(input_hash[:16], 16) % (2**32)
    combined_ops = float(features["home_ops"]) + float(features["away_ops"])
    combined_whip = float(getattr(home_pitcher, "whip", None) or 1.35) + float(getattr(away_pitcher, "whip", None) or 1.35)
    environment_variance = max(.04, min(.14, .075 + .035 * (combined_ops - 1.44) + .018 * (combined_whip - 2.70)))
    team_variance = .16 if game.league == "KBO" else .11
    if not (features["home_starter_confirmed"] and features["away_starter_confirmed"]):
        team_variance += .03
    if not (features["home_lineup_confirmed"] and features["away_lineup_confirmed"]):
        team_variance += .02
    team_variance = min(.24, team_variance)
    # The plate-appearance engine needs both lineups to have collected base-state splits.
    # Without them the inning-rate model runs instead, rather than the engine guessing hitters.
    home_table = (lineup_tables or {}).get("home")
    away_table = (lineup_tables or {}).get("away")
    simulation = simulate_scores(
        home_runs, away_runs, settings.simulations, seed, environment_variance, team_variance,
        league=game.league, home_staff=home_staff, away_staff=away_staff,
        home_lineup=home_table if away_table is not None else None,
        away_lineup=away_table if home_table is not None else None,
    )
    # Every headline score-distribution metric must describe the same population. The logistic
    # classifier remains an independent diagnostic, but win probability, expected score,
    # intervals, handicaps and totals now all come from this single simulation distribution.
    home_probability = simulation["home_two_way_probability"]
    away_probability = simulation["away_two_way_probability"]
    primary_score = select_primary_score(
        simulation["top_scores"], home_runs, away_runs, home_probability,
    )
    disagreement = abs(logistic - simulation["home_two_way_probability"])
    confidence, confidence_label, missing = _confidence(features, disagreement)
    reasons = _reasons(home, away, home_pitcher, away_pitcher, features)
    score_estimates = build_score_estimates(
        simulation["top_scores"], primary_score, home_runs, away_runs, settings.simulations,
    )
    # Hybrid headline: the distribution mean separates games from each other far better than
    # the mode (low-scoring baseball collapses every mode into the same handful of scores).
    # The exact-score mode and top-score candidates remain alongside it in score_estimates.
    display_score = dict(score_estimates["mean"])
    return {
        "input_hash": input_hash,
        "input_payload": input_data,
        "home_win_probability": round(home_probability, 4),
        # Derive the complement from the rounded value: two-way probabilities that are rounded
        # independently can each be correct yet fail to add up on screen.
        "away_win_probability": round(1 - round(home_probability, 4), 4),
        "home_expected_runs": round(home_runs, 2),
        "away_expected_runs": round(away_runs, 2),
        # The displayed total is derived from the same one-decimal expected scores shown in the UI.
        "expected_total": round(display_score["home"] + display_score["away"], 1),
        # Sum the two displayed team means so every rendition of the total matches them exactly.
        "statistical_expected_total": round(round(home_runs, 2) + round(away_runs, 2), 2),
        "confidence": confidence,
        "payload": {
            "summary_schema_version": 9,
            "coherence_valid": True,
            "probability_source": (
                "extra_innings_simulation_mlb_tiebreaker_no_ties" if game.league == "MLB"
                else "extra_innings_simulation_kbo_to_11_two_way_excluding_ties"
            ),
            "extra_innings": simulation["extra_innings"],
            "engine": simulation["engine"],
            "split_coverage": (lineup_tables or {}).get("coverage"),
            "bullpen_usage": simulation["bullpen_usage"],
            "staff_plan": {"home": home_staff, "away": away_staff},
            "model": {
                "name": model_name,
                "algorithm": (("automatically trained run regressors + " if model_runtime else "")
                              + MODEL_ALGORITHM
                              + (" + plate-appearance base-out engine driven by collected batter splits"
                                 if simulation["engine"] == "PLATE_APPEARANCE" else "")),
                "simulations": settings.simulations,
                "operating_mode": "AUTO_TRAINED_CHAMPION" if model_runtime else "VERSIONED_BASELINE",
            },
            "confidence_label": confidence_label,
            "confidence_missing": missing,
            "logistic_home_probability": round(logistic, 4),
            "classification_home_probability": round(logistic, 4),
            "simulation_home_probability": round(simulation["home_two_way_probability"], 4),
            "base_logistic_home_probability": round(base_logistic, 4),
            "base_home_expected_runs": round(base_home_runs, 2),
            "base_away_expected_runs": round(base_away_runs, 2),
            "league_average_runs": round(league_avg, 3),
            "statistical_expected_total": round(round(home_runs, 2) + round(away_runs, 2), 2),
            "display_expected_score": display_score,
            "score_estimates": score_estimates,
            "simulation_environment_variance": round(environment_variance, 4),
            "simulation_team_variance": round(team_variance, 4),
            "features": features,
            "handicap": {key: round(value, 4) for key, value in simulation["handicap"].items()},
            "totals": simulation["totals"],
            "tie_probability": round(simulation["tie_probability"], 4),
            "top_scores": simulation["top_scores"],
            "primary_score": primary_score,
            "simulation_modes": simulation["simulation_modes"],
            "total_quantiles": simulation["total_quantiles"],
            "team_quantiles": simulation["team_quantiles"],
            "team_dense_intervals": simulation["team_dense_intervals"],
            "total_dense_interval": simulation["total_dense_interval"],
            "game_shape": {key: round(value, 4) for key, value in simulation["game_shape"].items()},
            "reasons": reasons,
            "disclaimer": "통계 기반 추정치이며 경기 결과나 수익을 보장하지 않습니다.",
        },
    }


# Weight of the win/loss classifier when tilting the simulated run means. The runs model keeps
# the majority say; the classifier corrects cases where records and matchup edges disagree with
# raw scoring rates (e.g. a high-scoring, high-conceding team with a losing record).
CLASSIFIER_BLEND_WEIGHT = .35
# Total-runs variance inflation of the correlated gamma-Poisson process, used to convert
# win-probability tilts into run-margin tilts via a normal approximation.
MARGIN_VARIANCE_INFLATION = 1.6


def blend_classifier_into_means(logistic: float, home_runs: float, away_runs: float) -> tuple[float, float]:
    """Shift the run means so the simulated win probability blends runs and classifier signals."""
    normal = NormalDist()
    margin = home_runs - away_runs
    margin_sd = max(1.0, (MARGIN_VARIANCE_INFLATION * (home_runs + away_runs)) ** .5)
    runs_implied = normal.cdf(margin / margin_sd)
    target = min(.97, max(.03,
        (1 - CLASSIFIER_BLEND_WEIGHT) * runs_implied + CLASSIFIER_BLEND_WEIGHT * min(.97, max(.03, logistic))))
    shift = normal.inv_cdf(target) * margin_sd - margin
    # Split the tilt across both clubs so the expected total is preserved.
    return max(.6, home_runs + shift / 2), max(.6, away_runs - shift / 2)


def select_primary_score(top_scores: list[dict[str, Any]], home_expected: float, away_expected: float,
                         home_win_probability: float) -> dict[str, Any]:
    """Return the exact final score observed most often in the simulation."""
    if not top_scores:
        return {"home": round(home_expected), "away": round(away_expected), "probability": None}
    # simulate_scores orders this list by the unrounded occurrence count. Do not
    # replace the mode with a hand-picked score near the mean or favored winner.
    return top_scores[0]


def build_score_estimates(top_scores: list[dict[str, Any]], primary_score: dict[str, Any],
                          home_expected: float, away_expected: float, simulations: int) -> dict[str, Any]:
    """Three complementary point estimates of the same simulation distribution.

    - mean: distribution mean (the gamma shocks preserve expected means), minimizes per-team
      squared error and differs game to game — used as the displayed headline score.
    - top5_weighted: occurrence-weighted average of the five most frequent exact scores,
      an integer-anchored middle ground between mode and mean.
    - mode: the single most frequent exact score with its honest (low) probability.
    """
    estimates: dict[str, Any] = {
        "headline": "MEAN",
        "mean": {"away": round(away_expected, 1), "home": round(home_expected, 1)},
        "mode": {"away": primary_score["away"], "home": primary_score["home"],
                 "count": primary_score.get("count"), "probability": primary_score.get("probability")},
    }
    candidates = top_scores[:5]
    weight = sum(int(score.get("count") or 0) for score in candidates)
    estimates["top5_weighted"] = {
        "away": round(sum(score["away"] * int(score["count"]) for score in candidates) / weight, 1),
        "home": round(sum(score["home"] * int(score["count"]) for score in candidates) / weight, 1),
        "scores_used": len(candidates),
        "coverage_probability": round(weight / simulations, 4),
    } if weight else None
    return estimates


def _confidence(features: dict[str, Any], disagreement: float) -> tuple[int, str, list[str]]:
    score = 58
    missing = []
    if features["home_starter_confirmed"] and features["away_starter_confirmed"]:
        score += 16
    else:
        missing.append("선발투수 미확정 또는 기록 부족")
    if features["home_lineup_confirmed"] and features["away_lineup_confirmed"]:
        score += 8
        if min(int(features["home_lineup_bvp_coverage"]), int(features["away_lineup_bvp_coverage"])) >= 3:
            score += 3
    else:
        missing.append("실제 라인업 미확정")
    recent_games = min(int(features["recent_home_games"]), int(features["recent_away_games"]))
    score += min(10, recent_games)
    if recent_games < 10:
        missing.append("최근 10경기 표본 부족")
    score += max(0, round(8 - disagreement * 40))
    if disagreement > .12:
        missing.append("승패 모형과 득점 모형의 예측 차이")
    score = max(35, min(94, score))  # Injury and detailed bullpen fatigue remain unavailable.
    label = "HIGH" if score >= 80 else ("MEDIUM" if score >= 65 else "LOW")
    return score, label, missing


def _reasons(home: Any, away: Any, home_pitcher: Any | None, away_pitcher: Any | None, f: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if abs(f["starter_era_diff"]) >= .35:
        winner = home.team.name if f["starter_era_diff"] > 0 else away.team.name
        reasons.append(f"{winner} 선발이 시즌 ERA 비교에서 우세합니다.")
    if abs(f["recent_10_win_rate_diff"]) >= .15:
        winner = home.team.name if f["recent_10_win_rate_diff"] > 0 else away.team.name
        reasons.append(f"{winner}의 최근 10경기 승률이 더 높습니다.")
    if abs(f["ops_diff"]) >= .025:
        winner = home.team.name if f["ops_diff"] > 0 else away.team.name
        reasons.append(f"{winner}가 시즌 OPS에서 우세합니다.")
    if abs(f["runs_allowed_per_game_diff"]) >= .35:
        winner = home.team.name if f["runs_allowed_per_game_diff"] > 0 else away.team.name
        reasons.append(f"{winner}의 경기당 실점 억제력이 더 좋습니다.")
    if abs(f["bullpen_proxy_diff"]) >= .45:
        winner = home.team.name if f["bullpen_proxy_diff"] > 0 else away.team.name
        reasons.append(f"{winner}가 팀 ERA와 선발 소화 이닝 기반 불펜 부담 추정에서 우세합니다.")
    if abs(f["starter_fip_diff"]) >= .5:
        winner = home.team.name if f["starter_fip_diff"] > 0 else away.team.name
        reasons.append(f"{winner} 선발이 수비 영향 축소 지표(FIP 추정)에서 우세합니다.")
    if (f["home_starter_opponent_weight"] >= .12 or f["away_starter_opponent_weight"] >= .12) and abs(f["starter_opponent_era_diff"]) >= .45:
        winner = home.team.name if f["starter_opponent_era_diff"] > 0 else away.team.name
        reasons.append(f"{winner} 선발의 현재 상대팀 상대 기록이 표본 축소 후에도 우세합니다.")
    if f["head_to_head_games"] >= 3 and abs(f["head_to_head_run_diff"]) >= .35:
        winner = home.team.name if f["head_to_head_run_diff"] > 0 else away.team.name
        reasons.append(f"{winner}가 최근 팀 간 맞대결 득실점에서 우세합니다(소표본 보정).")
    if f["home_lineup_confirmed"] and f["away_lineup_confirmed"] and abs(f["lineup_strength_diff"]) >= .08:
        winner = home.team.name if f["lineup_strength_diff"] > 0 else away.team.name
        reasons.append(f"확정 라인업의 시즌 생산력은 {winner}가 우세합니다.")
    if min(int(f["home_lineup_bvp_coverage"]), int(f["away_lineup_bvp_coverage"])) >= 3 and abs(f["lineup_bvp_diff"]) >= .035:
        winner = home.team.name if f["lineup_bvp_diff"] > 0 else away.team.name
        reasons.append(f"{winner} 확정 타선의 상대 선발 맞대결 기록이 표본 축소 후 우세합니다.")
    if not reasons:
        reasons.append("주요 지표 차이가 작아 접전 가능성이 높게 평가됩니다.")
    reasons.append("홈 어드밴티지는 모든 경기에서 보수적으로 반영됩니다.")
    return reasons[:4]
