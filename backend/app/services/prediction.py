from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.app.config import settings
from backend.app.services.claude_advisor import blend_with_claude, claude_prediction_advice
from backend.app.services.feature_engineering import build_features, expected_runs, logistic_probability
from backend.app.services.simulation import simulate_scores


MODEL_ALGORITHM = "dynamic league environment + matchup-strength means + bounded optional Claude advisor + overdispersed correlated gamma-Poisson Monte Carlo"


def predict_game(game: Any, home: Any, away: Any, home_pitcher: Any | None, away_pitcher: Any | None,
                 lineups: list[Any] | None = None, game_context: dict[str, Any] | None = None) -> dict[str, Any]:
    model_name = "KBO_MATCHUP_V9" if game.league == "KBO" else "MLB_MATCHUP_V8"
    features = build_features(home, away, home_pitcher, away_pitcher, game.stadium, game.league, lineups,
                              getattr(game, "game_date", None), game_context)
    base_logistic = logistic_probability(features)
    base_home_runs, base_away_runs, league_avg = expected_runs(
        home, away, home_pitcher, away_pitcher, float(features["park_factor"]),
        float(features["lineup_strength_diff"]), features,
    )
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
        "features": features,
        "home_expected": round(base_home_runs, 6),
        "away_expected": round(base_away_runs, 6),
        "model": model_name,
        "pitchers": [(getattr(away_pitcher, "player_id", None), getattr(away_pitcher, "name", None)),
                     (getattr(home_pitcher, "player_id", None), getattr(home_pitcher, "name", None))],
        "lineups": lineup_fingerprint,
        "ai_config": {
            "enabled": bool(settings.claude_prediction_enabled and settings.claude_api_key),
            "model": settings.claude_model,
            "blend_weight": settings.claude_blend_weight,
        },
    }
    base_input_hash = hashlib.sha256(json.dumps(input_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    advice_context = {
        "game": {
            "league": game.league,
            "date": str(getattr(game, "game_date", "")),
            "stadium": getattr(game, "stadium", None),
            "away_team": away.team.name,
            "home_team": home.team.name,
        },
        "baseline": {
            "home_win_probability": round(base_logistic, 6),
            "home_expected_runs": round(base_home_runs, 6),
            "away_expected_runs": round(base_away_runs, 6),
            "league_average_runs_per_team": round(league_avg, 6),
        },
        "features": features,
    }
    advice, ai_metadata = claude_prediction_advice(base_input_hash, advice_context)
    logistic, home_runs, away_runs, ai_weight = blend_with_claude(
        base_logistic, base_home_runs, base_away_runs, advice,
    )
    ai_metadata["blend_weight"] = round(ai_weight, 4)
    if advice is not None:
        ai_metadata["forecast"] = {
            "home_win_probability": round(float(advice["home_win_probability"]), 4),
            "home_expected_runs": round(float(advice["home_expected_runs"]), 2),
            "away_expected_runs": round(float(advice["away_expected_runs"]), 2),
            "confidence": round(float(advice["confidence"]), 1),
        }
        ai_metadata["reasons"] = [str(reason)[:180] for reason in advice.get("reasons", [])[:3]]
        ai_metadata["caution"] = str(advice.get("caution", ""))[:240]
    input_data["ai_result"] = {
        "status": "applied" if ai_metadata["used"] else ai_metadata["status"],
        "model": ai_metadata.get("model"),
        "blend_weight": ai_metadata["blend_weight"],
        "forecast": ai_metadata.get("forecast"),
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
    simulation = simulate_scores(
        home_runs, away_runs, settings.simulations, seed, environment_variance, team_variance,
    )
    home_probability = .42 * logistic + .58 * simulation["home_two_way_probability"]
    away_probability = 1 - home_probability
    primary_score = select_primary_score(
        simulation["top_scores"], home_runs, away_runs, home_probability,
    )
    disagreement = abs(logistic - simulation["home_two_way_probability"])
    if advice is not None:
        advice_probability = max(0.0, min(1.0, float(advice["home_win_probability"])))
        disagreement = max(disagreement, abs(base_logistic - advice_probability))
    confidence, confidence_label, missing = _confidence(features, disagreement)
    reasons = _reasons(home, away, home_pitcher, away_pitcher, features)
    display_score = {"away": round(away_runs, 1), "home": round(home_runs, 1)}
    return {
        "input_hash": input_hash,
        "input_payload": input_data,
        "home_win_probability": round(home_probability, 4),
        "away_win_probability": round(away_probability, 4),
        "home_expected_runs": round(home_runs, 2),
        "away_expected_runs": round(away_runs, 2),
        # The displayed total is derived from the same one-decimal expected scores shown in the UI.
        "expected_total": round(display_score["home"] + display_score["away"], 1),
        # Preserve the distribution mean for model evaluation and market-line comparisons.
        "statistical_expected_total": round(home_runs + away_runs, 2),
        "confidence": confidence,
        "payload": {
            "model": {"name": model_name, "algorithm": MODEL_ALGORITHM, "simulations": settings.simulations},
            "confidence_label": confidence_label,
            "confidence_missing": missing,
            "logistic_home_probability": round(logistic, 4),
            "base_logistic_home_probability": round(base_logistic, 4),
            "base_home_expected_runs": round(base_home_runs, 2),
            "base_away_expected_runs": round(base_away_runs, 2),
            "league_average_runs": round(league_avg, 3),
            "statistical_expected_total": round(home_runs + away_runs, 2),
            "display_expected_score": display_score,
            "ai_assist": ai_metadata,
            "simulation_environment_variance": round(environment_variance, 4),
            "simulation_team_variance": round(team_variance, 4),
            "features": features,
            "handicap": {"home_minus_1_5": round(simulation["home_minus_1_5"], 4), "away_plus_1_5": round(simulation["away_plus_1_5"], 4)},
            "totals": simulation["totals"],
            "tie_probability": round(simulation["tie_probability"], 4),
            "top_scores": simulation["top_scores"],
            "primary_score": primary_score,
            "total_quantiles": simulation["total_quantiles"],
            "team_quantiles": simulation["team_quantiles"],
            "game_shape": {key: round(value, 4) for key, value in simulation["game_shape"].items()},
            "reasons": reasons,
            "disclaimer": "통계 기반 추정치이며 경기 결과나 수익을 보장하지 않습니다.",
        },
    }


def select_primary_score(top_scores: list[dict[str, Any]], home_expected: float, away_expected: float,
                         home_win_probability: float) -> dict[str, Any]:
    """Choose a representative score coherent with the winner, component means, and rounded total."""
    if not top_scores:
        return {"home": round(home_expected), "away": round(away_expected), "probability": None}
    expects_home_win = home_win_probability >= .5
    winner_consistent = [
        score for score in top_scores
        if score["home"] != score["away"] and ((score["home"] > score["away"]) == expects_home_win)
    ]
    candidates = winner_consistent or top_scores
    target_total = round(home_expected + away_expected)
    return min(candidates, key=lambda score: (
        abs((score["home"] + score["away"]) - target_total),
        abs(score["home"] - home_expected) + abs(score["away"] - away_expected),
        -float(score.get("probability") or 0.0),
    ))


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
