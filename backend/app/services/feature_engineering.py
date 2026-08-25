from __future__ import annotations

import math
from datetime import date
from typing import Any

from backend.app.services.trajectory import park_weather_home_run_multiplier


# Home-field run multipliers, fitted against real results rather than assumed. The simulation
# already gives the home club the batting-last advantage (it skips the ninth while ahead and
# walk-offs truncate the inning), which by itself produces a 52.6% home win rate. Applying the
# old 1.035/0.985 run edge on top double-counted home field and pushed the model to 55.2% when
# MLB actually runs 52.8%. These values reproduce both the observed home win rate and the
# observed home/away run ratio (MLB 1,938 games, KBO 555 games).
HOME_FIELD_MULTIPLIERS = {"MLB": (1.005, 0.995), "KBO": (0.985, 1.015)}

# League-average slash lines, measured from the collected team stats (MLB 30 clubs, KBO 10).
# Anchoring the batting factor here rather than to the two clubs in this game matters: with a
# shared two-team anchor, raising one club's OBP mechanically lowered the OTHER club's projected
# runs, because the opponent's line was then divided by a larger denominator. A club's hitting
# must never suppress its opponent's scoring.
LEAGUE_SLASH_ANCHORS = {
    "MLB": {"avg": .2436, "obp": .3182, "slg": .4004},
    "KBO": {"avg": .2696, "obp": .3484, "slg": .4053},
}

KBO_PARK_FACTORS = {
    "잠실": 0.96, "고척": 0.98, "대전": 1.02, "문학": 1.03, "창원": 1.01,
    "대구": 1.04, "사직": 1.01, "수원": 1.02, "광주": 1.01,
}
# Versioned multi-year run park factors covering every current MLB park (plus recent aliases),
# so no game silently falls back to a neutral 1.0.
MLB_PARK_FACTORS = {
    "Coors Field": 1.12, "Fenway Park": 1.04, "Yankee Stadium": 1.02,
    "Great American Ball Park": 1.05, "Globe Life Field": 1.01,
    "Oracle Park": 0.95, "Petco Park": 0.97, "T-Mobile Park": 0.94,
    "Wrigley Field": 1.00, "Dodger Stadium": 0.98, "Angel Stadium": 1.00,
    "Daikin Park": 1.00, "Minute Maid Park": 1.00,
    "Citi Field": 0.97, "Citizens Bank Park": 1.03, "Nationals Park": 1.01,
    "Truist Park": 1.01, "loanDepot park": 0.97, "LoanDepot Park": 0.97,
    "PNC Park": 0.98, "American Family Field": 1.02, "Busch Stadium": 0.98,
    "Rate Field": 1.03, "Guaranteed Rate Field": 1.03,
    "Progressive Field": 0.99, "Comerica Park": 0.98, "Kauffman Stadium": 1.01,
    "Target Field": 1.00, "Rogers Centre": 1.01, "Oriole Park at Camden Yards": 1.00,
    "Camden Yards": 1.00, "Tropicana Field": 0.96, "George M. Steinbrenner Field": 1.04,
    "Chase Field": 1.03, "Sutter Health Park": 1.04,
}


def build_features(home: Any, away: Any, home_pitcher: Any | None, away_pitcher: Any | None,
                   stadium: str | None, league: str, lineups: list[Any] | None = None,
                   game_date: date | None = None, game_context: dict[str, Any] | None = None) -> dict[str, float | bool]:
    h_recent = (home.recent or {}).get("10", {})
    a_recent = (away.recent or {}).get("10", {})
    h_recent_5 = (home.recent or {}).get("5", {})
    a_recent_5 = (away.recent or {}).get("5", {})
    context = game_context or {}
    residual = context.get("team_residuals") or {}
    residual_home = residual.get("home") or {}
    residual_away = residual.get("away") or {}
    strength = context.get("team_strength") or {}
    market = context.get("market") or {}
    pregame = context.get("pregame") or {}
    bullpen = pregame.get("bullpen") or {}
    schedule = pregame.get("schedule") or {}
    home_bullpen_fatigue = _v((bullpen.get("home") or {}).get("fatigue_index"), 0.0)
    away_bullpen_fatigue = _v((bullpen.get("away") or {}).get("fatigue_index"), 0.0)
    home_schedule_fatigue = _v((schedule.get("home") or {}).get("fatigue_index"), 0.0)
    away_schedule_fatigue = _v((schedule.get("away") or {}).get("fatigue_index"), 0.0)
    (lineup_diff, home_lineup_index, away_lineup_index, home_lineup_confirmed, away_lineup_confirmed,
     home_bvp_adjustment, away_bvp_adjustment, home_bvp_coverage, away_bvp_coverage,
     home_bvp_pa, away_bvp_pa) = _lineup_feature(lineups or [])
    platoon_diff, home_platoon, away_platoon, home_platoon_coverage, away_platoon_coverage = _platoon_feature(lineups or [])
    statcast = _lineup_statcast_features(lineups or [])
    home_park, away_park, park_events = _dynamic_park_factors(pregame, lineups or [])
    # MLB only: the geometry table covers MLB parks, and KBO has no equivalent, so a KBO game
    # gets a neutral multiplier and keeps the season park factor exactly as before.
    park_weather = (park_weather_home_run_multiplier(stadium, pregame.get("weather"))
                    if league == "MLB" else {"available": False, "multiplier": 1.0})
    home_pitch_quality = _pitcher_statcast(home_pitcher)
    away_pitch_quality = _pitcher_statcast(away_pitcher)
    park_factors = MLB_PARK_FACTORS if league == "MLB" else KBO_PARK_FACTORS
    opponent_name = getattr(getattr(away, "team", None), "name", None)
    matchup = _matchup(home, opponent_name)
    home_matchup_era, home_matchup_weight = _opponent_pitcher_era(home_pitcher)
    away_matchup_era, away_matchup_weight = _opponent_pitcher_era(away_pitcher)
    return {
        "league_average_runs": float(context.get("league_average_runs") or (5.15 if league == "KBO" else 4.45)),
        "season_win_rate_diff": _v(home.win_rate, .5) - _v(away.win_rate, .5),
        "strength_elo_diff": _v(strength.get("elo_diff"), 0.0),
        "strength_srs_diff": _v(strength.get("srs_diff"), 0.0),
        "pythagorean_diff": _v(strength.get("pythagorean_diff"), 0.0),
        "schedule_strength_diff": _v(strength.get("schedule_strength_diff"), 0.0),
        "adjusted_offense_diff": _v(strength.get("adjusted_offense_diff"), 0.0),
        "adjusted_defense_edge": _v(strength.get("adjusted_defense_edge"), 0.0),
        "team_strength_available": bool(strength.get("available")),
        "recent_10_win_rate_diff": _v(h_recent.get("win_rate"), .5) - _v(a_recent.get("win_rate"), .5),
        "recent_run_diff": _v(h_recent.get("avg_runs"), _v(home.runs_per_game, 4.5)) - _v(a_recent.get("avg_runs"), _v(away.runs_per_game, 4.5)),
        "recent_run_allowed_diff": _v(a_recent.get("avg_runs_allowed"), _v(away.runs_allowed_per_game, 4.5)) - _v(h_recent.get("avg_runs_allowed"), _v(home.runs_allowed_per_game, 4.5)),
        "home_recent_runs": .60 * _v(h_recent_5.get("avg_runs"), _v(home.runs_per_game, 4.5)) + .40 * _v(h_recent.get("avg_runs"), _v(home.runs_per_game, 4.5)),
        "away_recent_runs": .60 * _v(a_recent_5.get("avg_runs"), _v(away.runs_per_game, 4.5)) + .40 * _v(a_recent.get("avg_runs"), _v(away.runs_per_game, 4.5)),
        "home_recent_allowed": .60 * _v(h_recent_5.get("avg_runs_allowed"), _v(home.runs_allowed_per_game, 4.5)) + .40 * _v(h_recent.get("avg_runs_allowed"), _v(home.runs_allowed_per_game, 4.5)),
        "away_recent_allowed": .60 * _v(a_recent_5.get("avg_runs_allowed"), _v(away.runs_allowed_per_game, 4.5)) + .40 * _v(a_recent.get("avg_runs_allowed"), _v(away.runs_allowed_per_game, 4.5)),
        "runs_per_game_diff": _v(home.runs_per_game, 4.5) - _v(away.runs_per_game, 4.5),
        "runs_allowed_per_game_diff": _v(away.runs_allowed_per_game, 4.5) - _v(home.runs_allowed_per_game, 4.5),
        "ops_diff": _v(home.ops, .72) - _v(away.ops, .72),
        "home_avg": _v(getattr(home, "avg", None), .260),
        "away_avg": _v(getattr(away, "avg", None), .260),
        "home_obp": _v(getattr(home, "obp", None), .330),
        "away_obp": _v(getattr(away, "obp", None), .330),
        "home_slg": _v(getattr(home, "slg", None), .410),
        "away_slg": _v(getattr(away, "slg", None), .410),
        "home_ops": _v(getattr(home, "ops", None), .740),
        "away_ops": _v(getattr(away, "ops", None), .740),
        "split_win_rate_diff": _v(home.home_win_rate, home.win_rate) - _v(away.away_win_rate, away.win_rate),
        "starter_era_diff": _v(getattr(away_pitcher, "era", None), 4.5) - _v(getattr(home_pitcher, "era", None), 4.5),
        "starter_whip_diff": _v(getattr(away_pitcher, "whip", None), 1.4) - _v(getattr(home_pitcher, "whip", None), 1.4),
        "starter_war_diff": _v(getattr(home_pitcher, "war", None), 0.0) - _v(getattr(away_pitcher, "war", None), 0.0),
        # FIP and K-BB% are independent skill signals. Falling back to ERA or zero would count
        # the season ERA edge a second time whenever one feed was incomplete.
        "starter_fip_diff": _paired_pitcher_difference(away_pitcher, home_pitcher, "fip"),
        "starter_k_bb_diff": _paired_pitcher_difference(home_pitcher, away_pitcher, "k_bb_rate"),
        "starter_durability_diff": _v(getattr(home_pitcher, "avg_start_innings", None), 5.0) - _v(getattr(away_pitcher, "avg_start_innings", None), 5.0),
        "quality_start_rate_diff": _quality_start_rate(home_pitcher) - _quality_start_rate(away_pitcher),
        "starter_rest_days_diff": _clip(_v(getattr(home_pitcher, "rest_days", None), 5.0), 2, 8) - _clip(_v(getattr(away_pitcher, "rest_days", None), 5.0), 2, 8),
        "bullpen_proxy_diff": _bullpen_proxy(home, home_pitcher) - _bullpen_proxy(away, away_pitcher),
        "bullpen_fatigue_edge": away_bullpen_fatigue - home_bullpen_fatigue,
        "home_bullpen_fatigue": home_bullpen_fatigue,
        "away_bullpen_fatigue": away_bullpen_fatigue,
        "bullpen_workload_available": bool((pregame.get("availability") or {}).get("bullpen")),
        "recent_pitch_burden_diff": _v(getattr(away_pitcher, "recent_pitches", None), 0.0) - _v(getattr(home_pitcher, "recent_pitches", None), 0.0),
        "rest_days_diff": _team_rest_days(home, game_date) - _team_rest_days(away, game_date),
        "schedule_fatigue_edge": away_schedule_fatigue - home_schedule_fatigue,
        "home_schedule_fatigue": home_schedule_fatigue,
        "away_schedule_fatigue": away_schedule_fatigue,
        "doubleheader_diff": float(context.get("away_games_today", 1) - context.get("home_games_today", 1)),
        "head_to_head_diff": matchup[0],
        "head_to_head_run_diff": matchup[1],
        "head_to_head_games": matchup[2],
        "market_available": bool(market.get("provider") or market.get("collected_at") or
                                 int(market.get("bookmaker_count") or 0) > 0),
        "market_total_line": _v(market.get("total_line"), 0.0),
        "market_home_probability": _v(market.get("home_implied_probability"), .5),
        "market_home_spread": _v(market.get("home_spread"), 0.0),
        "market_bookmaker_count": int(market.get("bookmaker_count") or 0),
        "home_starter_opponent_era": home_matchup_era,
        "away_starter_opponent_era": away_matchup_era,
        "home_starter_opponent_weight": home_matchup_weight,
        "away_starter_opponent_weight": away_matchup_weight,
        "starter_opponent_era_diff": (
            away_matchup_era - _v(getattr(away_pitcher, "era", None), 4.5)
        ) - (
            home_matchup_era - _v(getattr(home_pitcher, "era", None), 4.5)
        ),
        # Run-rate multipliers and workload for the inning-by-inning pitcher plan. The multiplier
        # is relative to the club's own staff, so it shapes when runs score, not how many.
        "home_starter_multiplier": _starter_multiplier(home_pitcher, home, league),
        "away_starter_multiplier": _starter_multiplier(away_pitcher, away, league),
        "home_starter_innings": _projected_starter_innings(home_pitcher),
        "away_starter_innings": _projected_starter_innings(away_pitcher),
        "static_park_factor": park_factors.get(stadium or "", 1.0),
        "weather_run_multiplier": _v((pregame.get("weather") or {}).get("run_multiplier"), 1.0),
        "weather_available": bool((pregame.get("weather") or {}).get("available")),
        "home_park_factor": home_park or park_factors.get(stadium or "", 1.0),
        "away_park_factor": away_park or park_factors.get(stadium or "", 1.0),
        "park_event_factors": park_events,
        "park_factor": ((home_park + away_park) / 2 if home_park and away_park else park_factors.get(stadium or "", 1.0)) * _v((pregame.get("weather") or {}).get("run_multiplier"), 1.0),
        "lineup_strength_diff": lineup_diff,
        "home_lineup_index": home_lineup_index,
        "away_lineup_index": away_lineup_index,
        "lineup_bvp_diff": home_bvp_adjustment - away_bvp_adjustment,
        "home_lineup_bvp_coverage": home_bvp_coverage,
        "away_lineup_bvp_coverage": away_bvp_coverage,
        "home_lineup_bvp_pa": home_bvp_pa,
        "away_lineup_bvp_pa": away_bvp_pa,
        "lineup_platoon_diff": platoon_diff,
        "home_lineup_platoon_index": home_platoon,
        "away_lineup_platoon_index": away_platoon,
        "home_lineup_platoon_coverage": home_platoon_coverage,
        "away_lineup_platoon_coverage": away_platoon_coverage,
        # Recent form means a deviation from that pitcher's own season level. An unavailable
        # recent log is neutral, rather than silently duplicating the season feature.
        "starter_recent_era_diff": (
            _recent_pitcher_deviation(away_pitcher, "era")
            - _recent_pitcher_deviation(home_pitcher, "era")
        ),
        "starter_recent_k_bb_diff": (
            _recent_pitcher_deviation(home_pitcher, "k_bb_rate")
            - _recent_pitcher_deviation(away_pitcher, "k_bb_rate")
        ),
        "starter_recent_form_available": bool((getattr(home_pitcher, "recent", None) or {}).get("available") and (getattr(away_pitcher, "recent", None) or {}).get("available")),
        "starter_xera_diff": _paired_nested_difference(away_pitch_quality, home_pitch_quality, "xera"),
        "starter_xwoba_diff": _paired_nested_difference(away_pitch_quality, home_pitch_quality, "xwoba"),
        "starter_velocity_trend_edge": _v(home_pitch_quality.get("velocity_change"), 0.0) - _v(away_pitch_quality.get("velocity_change"), 0.0),
        "starter_arsenal_stability_edge": _v(away_pitch_quality.get("usage_change"), 0.0) - _v(home_pitch_quality.get("usage_change"), 0.0),
        "home_lineup_hard_hit": statcast["home_hard_hit"],
        "away_lineup_hard_hit": statcast["away_hard_hit"],
        "hard_hit_available": bool(statcast["home_hard_hit_available"] and statcast["away_hard_hit_available"]),
        # Park and contact together, expressed as a change to the shape of an inning rather than
        # to its rate. The rate already carries the park through `park_factor`.
        "home_batted_ball_clumping": batted_ball_clumping(
            park_events.get("home"), statcast["home_hard_hit"], park_weather.get("multiplier", 1.0)),
        "away_batted_ball_clumping": batted_ball_clumping(
            park_events.get("away"), statcast["away_hard_hit"], park_weather.get("multiplier", 1.0)),
        "park_weather_home_run_multiplier": float(park_weather.get("multiplier") or 1.0),
        "park_weather_available": bool(park_weather.get("available")),
        "statcast_pitcher_available": bool(home_pitch_quality.get("available") and away_pitch_quality.get("available")),
        "lineup_xwoba_diff": statcast["home_xwoba"] - statcast["away_xwoba"],
        "lineup_pitch_type_edge": statcast["home_pitch_matchup"] - statcast["away_pitch_matchup"],
        "lineup_frv_edge": statcast["home_frv"] - statcast["away_frv"],
        "lineup_oaa_edge": statcast["home_oaa"] - statcast["away_oaa"],
        "catcher_framing_edge": statcast["home_framing"] - statcast["away_framing"],
        "battery_edge": statcast["home_battery"] - statcast["away_battery"],
        "statcast_lineup_coverage": statcast["coverage"],
        "fielding_edge": _fielding_index(home) - _fielding_index(away),
        "baserunning_edge": _baserunning_index(home) - _baserunning_index(away),
        "catcher_control_edge": _catcher_index(home) - _catcher_index(away),
        "baserunning_data_available": _advanced_pair_available(home, away, "stolen_bases"),
        "fielding_data_available": _advanced_pair_available(home, away, "fielding_percentage"),
        "catcher_data_available": _advanced_pair_available(home, away, "opponent_stolen_base_percentage"),
        "advanced_team_data_available": all((
            _advanced_pair_available(home, away, "stolen_bases"),
            _advanced_pair_available(home, away, "fielding_percentage"),
            _advanced_pair_available(home, away, "opponent_stolen_base_percentage"),
        )),
        "home_advantage": 1.0,
        "home_starter_confirmed": bool(home_pitcher and home_pitcher.confirmed and home_pitcher.era is not None),
        "away_starter_confirmed": bool(away_pitcher and away_pitcher.confirmed and away_pitcher.era is not None),
        "home_lineup_confirmed": home_lineup_confirmed,
        "away_lineup_confirmed": away_lineup_confirmed,
        "recent_home_games": int(h_recent.get("games", 0)),
        "recent_away_games": int(a_recent.get("games", 0)),
        "home_offense_residual_ewma": float(residual_home.get("offense") or 0.0),
        "away_offense_residual_ewma": float(residual_away.get("offense") or 0.0),
        "home_defense_residual_ewma": float(residual_home.get("defense") or 0.0),
        "away_defense_residual_ewma": float(residual_away.get("defense") or 0.0),
        "home_venue_offense_residual": float(residual_home.get("venue_offense") or 0.0),
        "away_venue_offense_residual": float(residual_away.get("venue_offense") or 0.0),
        "home_venue_defense_residual": float(residual_home.get("venue_defense") or 0.0),
        "away_venue_defense_residual": float(residual_away.get("venue_defense") or 0.0),
        "home_matchup_residual": float(residual_home.get("matchup") or 0.0),
        "away_matchup_residual": float(residual_away.get("matchup") or 0.0),
        "home_residual_run_adjustment": float(residual.get("home_run_adjustment") or 0.0),
        "away_residual_run_adjustment": float(residual.get("away_run_adjustment") or 0.0),
        "home_residual_variance_multiplier": float(residual.get("home_variance_multiplier") or 1.0),
        "away_residual_variance_multiplier": float(residual.get("away_variance_multiplier") or 1.0),
        "home_residual_games": int(residual_home.get("games") or 0),
        "away_residual_games": int(residual_away.get("games") or 0),
        "home_venue_residual_games": int(residual_home.get("venue_games") or 0),
        "away_venue_residual_games": int(residual_away.get("venue_games") or 0),
        "home_matchup_residual_games": int(residual_home.get("matchup_games") or 0),
        "away_matchup_residual_games": int(residual_away.get("matchup_games") or 0),
        "home_structure_residual_games": int(residual_home.get("structure_games") or 0),
        "away_structure_residual_games": int(residual_away.get("structure_games") or 0),
        "home_structure_residual": float(residual_home.get("structure") or 0.0),
        "away_structure_residual": float(residual_away.get("structure") or 0.0),
        "home_offense_residual_outlier_index": float(residual_home.get("offense_outlier_index") or 1.0),
        "away_offense_residual_outlier_index": float(residual_away.get("offense_outlier_index") or 1.0),
        "home_defense_residual_outlier_index": float(residual_home.get("defense_outlier_index") or 1.0),
        "away_defense_residual_outlier_index": float(residual_away.get("defense_outlier_index") or 1.0),
        "home_matchup_residual_consistency": float(residual_home.get("matchup_direction_consistency") or 0.0),
        "away_matchup_residual_consistency": float(residual_away.get("matchup_direction_consistency") or 0.0),
        "home_large_residual_flag": bool((residual.get("outlier_analysis") or {}).get("home_scoring", {}).get("large_residual_flag")),
        "away_large_residual_flag": bool((residual.get("outlier_analysis") or {}).get("away_scoring", {}).get("large_residual_flag")),
    }


def logistic_probability(features: dict[str, float | bool]) -> float:
    """Transparent pre-training prior. Coefficients are versioned, not fitted claims."""
    recent_reliability = _clip(
        min(float(features["recent_home_games"]), float(features["recent_away_games"])) / 10, 0, 1,
    )
    starter_reliability = .50 + .25 * float(features["home_starter_confirmed"]) + .25 * float(features["away_starter_confirmed"])
    lineup_reliability = 1.0 if features["home_lineup_confirmed"] and features["away_lineup_confirmed"] else .35
    matchup_reliability = _clip(float(features["head_to_head_games"]) / 8, 0, 1)
    z = 0.14
    z += 0.55 * _logit(.5 + float(features["season_win_rate_diff"]) / 2)
    # Opponent-adjusted signals are deliberately conservative until the walk-forward trainer
    # has enough seasons to learn their coefficients. They reduce schedule-strength bias without
    # allowing several correlated standings measures to overwhelm starter and lineup evidence.
    strength_reliability = 1.0 if features.get("team_strength_available") else 0.0
    z += strength_reliability * .0010 * float(features.get("strength_elo_diff", 0))
    z += strength_reliability * .025 * float(features.get("strength_srs_diff", 0))
    z += strength_reliability * .30 * float(features.get("pythagorean_diff", 0))
    z += strength_reliability * .018 * float(features.get("adjusted_offense_diff", 0))
    z += strength_reliability * .018 * float(features.get("adjusted_defense_edge", 0))
    z += recent_reliability * 0.60 * float(features["recent_10_win_rate_diff"])
    z += recent_reliability * 0.045 * float(features["recent_run_diff"])
    z += recent_reliability * 0.040 * float(features["recent_run_allowed_diff"])
    z += 0.12 * float(features["runs_per_game_diff"])
    z += 0.10 * float(features["runs_allowed_per_game_diff"])
    z += 1.40 * float(features["ops_diff"])
    z += 0.35 * float(features["split_win_rate_diff"])
    z += starter_reliability * 0.075 * float(features["starter_era_diff"])
    z += starter_reliability * 0.22 * float(features["starter_whip_diff"])
    z += starter_reliability * 0.025 * float(features["starter_war_diff"])
    z += starter_reliability * 0.035 * float(features["starter_fip_diff"])
    z += starter_reliability * 0.50 * float(features["starter_k_bb_diff"])
    z += starter_reliability * 0.035 * float(features["starter_durability_diff"])
    z += starter_reliability * 0.10 * float(features["quality_start_rate_diff"])
    z += 0.018 * float(features["bullpen_proxy_diff"])
    z += 0.11 * float(features["bullpen_fatigue_edge"])
    z += 0.004 * float(features["starter_rest_days_diff"])
    z += 0.0008 * float(features["recent_pitch_burden_diff"])
    z += 0.012 * float(features["rest_days_diff"])
    z += 0.08 * float(features["schedule_fatigue_edge"])
    z += 0.018 * float(features["doubleheader_diff"])
    z += matchup_reliability * 0.10 * float(features["head_to_head_diff"])
    z += matchup_reliability * 0.025 * float(features["head_to_head_run_diff"])
    z += 0.045 * float(features["starter_opponent_era_diff"])
    z += lineup_reliability * 0.18 * float(features["lineup_strength_diff"])
    z += lineup_reliability * 0.12 * float(features["lineup_platoon_diff"])
    z += starter_reliability * 0.025 * float(features["starter_recent_era_diff"])
    z += starter_reliability * 0.14 * float(features["starter_recent_k_bb_diff"])
    if features.get("statcast_pitcher_available"):
        z += starter_reliability * .055 * float(features.get("starter_xera_diff", 0))
        z += starter_reliability * .90 * float(features.get("starter_xwoba_diff", 0))
        z += starter_reliability * .035 * float(features.get("starter_velocity_trend_edge", 0))
        z += starter_reliability * .10 * float(features.get("starter_arsenal_stability_edge", 0))
    z += lineup_reliability * .70 * float(features.get("lineup_xwoba_diff", 0))
    z += lineup_reliability * .20 * float(features.get("lineup_pitch_type_edge", 0))
    z += lineup_reliability * .020 * float(features.get("lineup_frv_edge", 0))
    z += lineup_reliability * .012 * float(features.get("lineup_oaa_edge", 0))
    z += lineup_reliability * .018 * float(features.get("catcher_framing_edge", 0))
    z += lineup_reliability * .012 * float(features.get("battery_edge", 0))
    z += 0.025 * float(features["fielding_edge"])
    z += 0.035 * float(features["baserunning_edge"])
    z += 0.020 * float(features["catcher_control_edge"])
    return 1 / (1 + math.exp(-max(-4.0, min(4.0, z))))


def expected_runs(home: Any, away: Any, home_pitcher: Any | None, away_pitcher: Any | None,
                  park_factor: float, lineup_strength_diff: float = 0.0,
                  advanced_features: dict[str, float | bool] | None = None) -> tuple[float, float, float]:
    values = [x for x in (home.runs_per_game, away.runs_per_game, home.runs_allowed_per_game, away.runs_allowed_per_game) if x is not None]
    league = getattr(getattr(home, "team", None), "league", None)
    league_baseline = 5.15 if league == "KBO" else 4.45
    observed_environment = sum(values) / len(values) if values else league_baseline
    supplied_environment = _v((advanced_features or {}).get("league_average_runs"), 0.0)
    # Prefer the current all-team scoring environment supplied by the refresh service. Two opponents
    # alone are a noisy substitute and are used only by isolated callers/tests.
    league_avg = supplied_environment if supplied_environment > 0 else .75 * league_baseline + .25 * observed_environment
    # Multiplicative strengths preserve matchup differences better than averaging two 4-6 run values.
    # Team rates are already sample-size shrunk once in _shrunk_rate; the exponents only trim the
    # residual opponent-quality double count, so genuine team differences survive to the mean.
    home_games = _v(getattr(home, "games", None), 0.0)
    away_games = _v(getattr(away, "games", None), 0.0)
    home_offense = _shrunk_rate(home.runs_per_game, league_avg, home_games) / league_avg
    away_offense = _shrunk_rate(away.runs_per_game, league_avg, away_games) / league_avg
    home_defense = _shrunk_rate(home.runs_allowed_per_game, league_avg, home_games) / league_avg
    away_defense = _shrunk_rate(away.runs_allowed_per_game, league_avg, away_games) / league_avg
    home_base = league_avg * home_offense ** .90 * away_defense ** .78
    away_base = league_avg * away_offense ** .90 * home_defense ** .78

    anchors = LEAGUE_SLASH_ANCHORS.get(league or "MLB", LEAGUE_SLASH_ANCHORS["MLB"])
    avg_anchor, obp_anchor, slg_anchor = anchors["avg"], anchors["obp"], anchors["slg"]
    home_batting = math.exp(_clip(
        .20 * (_v(getattr(home, "avg", None), avg_anchor) / avg_anchor - 1)
        + .40 * (_v(getattr(home, "obp", None), obp_anchor) / obp_anchor - 1)
        + .28 * (_v(getattr(home, "slg", None), slg_anchor) / slg_anchor - 1), -.14, .14))
    away_batting = math.exp(_clip(
        .20 * (_v(getattr(away, "avg", None), avg_anchor) / avg_anchor - 1)
        + .40 * (_v(getattr(away, "obp", None), obp_anchor) / obp_anchor - 1)
        + .28 * (_v(getattr(away, "slg", None), slg_anchor) / slg_anchor - 1), -.14, .14))

    home_starter = _effective_pitcher_era(home_pitcher, _v(home.era, league_avg))
    away_starter = _effective_pitcher_era(away_pitcher, _v(away.era, league_avg))
    home_share = _clip(_v(getattr(home_pitcher, "avg_start_innings", None), 5.0) / 9, .38, .72)
    away_share = _clip(_v(getattr(away_pitcher, "avg_start_innings", None), 5.0) / 9, .38, .72)
    home_pitching = (_clip(home_starter / max(_v(home.era, league_avg), 1.5), .50, 1.80)) ** (home_share * .72)
    away_pitching = (_clip(away_starter / max(_v(away.era, league_avg), 1.5), .50, 1.80)) ** (away_share * .72)
    home_team_whip = _v(getattr(home, "whip", None), 1.35)
    away_team_whip = _v(getattr(away, "whip", None), 1.35)
    home_season_whip = _v(getattr(home_pitcher, "whip", None), home_team_whip)
    away_season_whip = _v(getattr(away_pitcher, "whip", None), away_team_whip)
    home_split_weight = min(.50, _v(getattr(home_pitcher, "opponent_innings", None), 0.0) / (_v(getattr(home_pitcher, "opponent_innings", None), 0.0) + 45.0))
    away_split_weight = min(.50, _v(getattr(away_pitcher, "opponent_innings", None), 0.0) / (_v(getattr(away_pitcher, "opponent_innings", None), 0.0) + 45.0))
    home_whip = (1 - home_split_weight) * home_season_whip + home_split_weight * _v(getattr(home_pitcher, "opponent_whip", None), home_season_whip)
    away_whip = (1 - away_split_weight) * away_season_whip + away_split_weight * _v(getattr(away_pitcher, "opponent_whip", None), away_season_whip)
    home_pitching *= math.exp(_clip(.18 * (home_whip - home_team_whip), -.12, .12))
    away_pitching *= math.exp(_clip(.18 * (away_whip - away_team_whip), -.12, .12))
    lineup_home = math.exp(max(-.10, min(.10, .075 * lineup_strength_diff)))
    lineup_away = math.exp(max(-.10, min(.10, -.075 * lineup_strength_diff)))
    advanced = advanced_features or {}
    recent_home_signal = .55 * (_v(advanced.get("home_recent_runs"), league_avg) - _v(home.runs_per_game, league_avg)) + .45 * (_v(advanced.get("away_recent_allowed"), league_avg) - _v(away.runs_allowed_per_game, league_avg))
    recent_away_signal = .55 * (_v(advanced.get("away_recent_runs"), league_avg) - _v(away.runs_per_game, league_avg)) + .45 * (_v(advanced.get("home_recent_allowed"), league_avg) - _v(home.runs_allowed_per_game, league_avg))
    recent_home_weight = _clip(_v(advanced.get("recent_home_games"), 0.0) / 10, 0, 1)
    recent_away_weight = _clip(_v(advanced.get("recent_away_games"), 0.0) / 10, 0, 1)
    recent_home = math.exp(_clip(.08 * recent_home_signal * recent_home_weight, -.20, .20))
    recent_away = math.exp(_clip(.08 * recent_away_signal * recent_away_weight, -.20, .20))
    bullpen_home = math.exp(_clip(.015 * float(advanced.get("bullpen_proxy_diff", 0)), -.07, .07))
    bullpen_away = math.exp(_clip(-.015 * float(advanced.get("bullpen_proxy_diff", 0)), -.07, .07))
    matchup_home = math.exp(_clip(.03 * float(advanced.get("head_to_head_run_diff", 0)), -.10, .10))
    matchup_away = math.exp(_clip(-.03 * float(advanced.get("head_to_head_run_diff", 0)), -.10, .10))
    home_context = math.exp(_clip(
        .08 * float(advanced.get("away_bullpen_fatigue", 0))
        - .04 * float(advanced.get("home_schedule_fatigue", 0))
        + .03 * float(advanced.get("away_schedule_fatigue", 0))
        + .06 * float(advanced.get("home_lineup_platoon_index", 0))
        + .025 * float(advanced.get("baserunning_edge", 0))
        + .018 * float(advanced.get("fielding_edge", 0))
        + .012 * float(advanced.get("catcher_control_edge", 0))
        + .030 * float(advanced.get("adjusted_offense_diff", 0))
        + .022 * float(advanced.get("adjusted_defense_edge", 0)), -.16, .16))
    away_context = math.exp(_clip(
        .08 * float(advanced.get("home_bullpen_fatigue", 0))
        - .04 * float(advanced.get("away_schedule_fatigue", 0))
        + .03 * float(advanced.get("home_schedule_fatigue", 0))
        + .06 * float(advanced.get("away_lineup_platoon_index", 0))
        - .025 * float(advanced.get("baserunning_edge", 0))
        - .018 * float(advanced.get("fielding_edge", 0))
        - .012 * float(advanced.get("catcher_control_edge", 0))
        - .030 * float(advanced.get("adjusted_offense_diff", 0))
        - .022 * float(advanced.get("adjusted_defense_edge", 0)), -.16, .16))
    home_field, away_field = HOME_FIELD_MULTIPLIERS.get(league or "MLB", HOME_FIELD_MULTIPLIERS["MLB"])
    home_park = _v(advanced.get("home_park_factor"), park_factor) * _v(advanced.get("weather_run_multiplier"), 1.0)
    away_park = _v(advanced.get("away_park_factor"), park_factor) * _v(advanced.get("weather_run_multiplier"), 1.0)
    statcast_home = math.exp(_clip(.55 * float(advanced.get("lineup_xwoba_diff", 0))
                                  + .12 * float(advanced.get("lineup_pitch_type_edge", 0)), -.10, .10))
    statcast_away = math.exp(_clip(-.55 * float(advanced.get("lineup_xwoba_diff", 0))
                                  - .12 * float(advanced.get("lineup_pitch_type_edge", 0)), -.10, .10))
    defense_home = math.exp(_clip(.012 * float(advanced.get("lineup_frv_edge", 0))
                                 + .008 * float(advanced.get("catcher_framing_edge", 0))
                                 + .006 * float(advanced.get("battery_edge", 0)), -.07, .07))
    defense_away = math.exp(_clip(-.012 * float(advanced.get("lineup_frv_edge", 0))
                                 - .008 * float(advanced.get("catcher_framing_edge", 0))
                                 - .006 * float(advanced.get("battery_edge", 0)), -.07, .07))
    home_expected = home_base * home_batting * away_pitching * home_park * home_field * lineup_home * recent_home * bullpen_home * matchup_home * home_context * statcast_home * defense_home
    away_expected = away_base * away_batting * home_pitching * away_park * away_field * lineup_away * recent_away * bullpen_away * matchup_away * away_context * statcast_away * defense_away
    return _clip(home_expected, .6, 10.0), _clip(away_expected, .6, 10.0), league_avg


def _v(value: Any, fallback: float) -> float:
    return fallback if value is None else float(value)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _shrunk_rate(value: Any, league_average: float, games: float, prior_games: float = 12.0) -> float:
    sample = max(0.0, games)
    credibility = sample / (sample + prior_games)
    return credibility * _v(value, league_average) + (1 - credibility) * league_average


def _logit(p: float) -> float:
    p = _clip(p, .05, .95)
    return math.log(p / (1 - p))


def _lineup_feature(lineups: list[Any]) -> tuple[float, float, float, bool, bool, float, float, int, int, int, int]:
    by_side = {"home": [], "away": []}
    for item in lineups:
        if getattr(item, "side", None) in by_side and getattr(item, "value", None) is not None:
            by_side[item.side].append(item)
    confirmed = {
        side: len([item for item in lineups if getattr(item, "side", None) == side]) >= 9
        and all(bool(getattr(item, "confirmed", False)) for item in lineups if getattr(item, "side", None) == side)
        for side in ("home", "away")
    }
    home_bvp_adjustment, home_bvp_coverage, home_bvp_pa = _lineup_matchup_summary(by_side["home"])
    away_bvp_adjustment, away_bvp_coverage, away_bvp_pa = _lineup_matchup_summary(by_side["away"])
    if not by_side["home"] or not by_side["away"]:
        return (0.0, 0.0, 0.0, confirmed["home"], confirmed["away"],
                home_bvp_adjustment, away_bvp_adjustment, home_bvp_coverage, away_bvp_coverage,
                home_bvp_pa, away_bvp_pa)
    metric = getattr(by_side["home"][0], "value_metric", None)
    if metric == "WAR":
        # KBO's official lineup feed supplies WAR. Add a separately shrunk current-season BvP signal.
        home_index = sum(float(x.value) for x in by_side["home"]) / 10 + home_bvp_adjustment
        away_index = sum(float(x.value) for x in by_side["away"]) / 10 + away_bvp_adjustment
        diff = home_index - away_index
    elif metric == "OPS":
        home_avg = sum(_effective_lineup_ops(x) for x in by_side["home"]) / len(by_side["home"])
        away_avg = sum(_effective_lineup_ops(x) for x in by_side["away"]) / len(by_side["away"])
        home_index, away_index = (home_avg - .720) * 3, (away_avg - .720) * 3
        diff = (home_avg - away_avg) * 3
    else:
        diff = home_index = away_index = 0.0
    return (max(-1.5, min(1.5, diff)), _clip(home_index, -1.5, 1.5), _clip(away_index, -1.5, 1.5),
            confirmed["home"], confirmed["away"], home_bvp_adjustment, away_bvp_adjustment,
            home_bvp_coverage, away_bvp_coverage, home_bvp_pa, away_bvp_pa)


def _lineup_matchup_summary(items: list[Any]) -> tuple[float, int, int]:
    signals: list[float] = []
    total_pa = 0
    for item in items:
        matchup_ops = getattr(item, "matchup_ops", None)
        plate_appearances = int(_v(getattr(item, "matchup_plate_appearances", None), 0.0))
        if matchup_ops is None or plate_appearances <= 0:
            continue
        # BvP is highly volatile. It can fine-tune a lineup, but cannot overpower season production.
        sample_weight = min(.45, plate_appearances / (plate_appearances + 75.0))
        signals.append((_clip(float(matchup_ops), .250, 1.400) - .720) * sample_weight * 3)
        total_pa += plate_appearances
    return (sum(signals) / max(len(items), 1), len(signals), total_pa)


def _effective_lineup_ops(item: Any) -> float:
    season_ops = _v(getattr(item, "value", None), .720)
    matchup_ops = getattr(item, "matchup_ops", None)
    plate_appearances = _v(getattr(item, "matchup_plate_appearances", None), 0.0)
    if matchup_ops is None or plate_appearances <= 0:
        return season_ops
    # Career BvP is noisy: 25 PA gives 25% weight, capped at 45% even for large samples.
    weight = min(.45, plate_appearances / (plate_appearances + 75.0))
    return (1 - weight) * season_ops + weight * _clip(float(matchup_ops), .250, 1.400)


def _platoon_feature(lineups: list[Any]) -> tuple[float, float, float, int, int]:
    values: dict[str, list[float]] = {"home": [], "away": []}
    for item in lineups:
        side = getattr(item, "side", None)
        ops = getattr(item, "platoon_ops", None)
        pa = int(_v(getattr(item, "platoon_plate_appearances", None), 0.0))
        if side not in values or ops is None or pa <= 0:
            continue
        season = _v(getattr(item, "value", None), .720)
        weight = min(.65, pa / (pa + 100.0))
        values[side].append((_clip(float(ops), .300, 1.300) - season) * weight * 3)
    home = sum(values["home"]) / max(9, len(values["home"]))
    away = sum(values["away"]) / max(9, len(values["away"]))
    return _clip(home - away, -.8, .8), _clip(home, -.5, .5), _clip(away, -.5, .5), len(values["home"]), len(values["away"])


def _lineup_statcast_features(lineups: list[Any]) -> dict[str, float]:
    output: dict[str, float] = {"coverage": 0.0}
    coverage = []
    for side in ("home", "away"):
        xwoba: list[float] = []
        hard_hit: list[float] = []
        pitch_matchups: list[float] = []
        frv: list[float] = []
        oaa: list[float] = []
        framing: list[float] = []
        battery: list[float] = []
        side_rows = [item for item in lineups if getattr(item, "side", None) == side]
        for item in side_rows:
            advanced = getattr(item, "advanced", None) or {}
            expected = advanced.get("expected") or {}
            pa = _v(expected.get("pa"), 0.0)
            if expected.get("xwoba") is not None:
                weight = min(1.0, pa / (pa + 100.0))
                xwoba.append(.320 + (float(expected["xwoba"]) - .320) * weight)
            # How often this hitter actually squares one up. A park can only turn contact into
            # home runs that were struck hard enough to leave, so this is the half of the
            # interaction the park factor cannot supply.
            if expected.get("hard_hit_percent") is not None:
                weight = min(1.0, pa / (pa + 100.0))
                hard_hit.append(LEAGUE_HARD_HIT_RATE
                                + (float(expected["hard_hit_percent"]) / 100 - LEAGUE_HARD_HIT_RATE) * weight)
            matchup_rows = list((advanced.get("pitch_type_matchup") or {}).values())
            valid_matchups = [row for row in matchup_rows if row.get("xwoba") is not None]
            if valid_matchups:
                values = []
                for row in valid_matchups:
                    pitches = _v(row.get("pitches"), 0.0)
                    weight = min(.75, pitches / (pitches + 80.0))
                    values.append(.320 + (float(row["xwoba"]) - .320) * weight)
                pitch_matchups.append(sum(values) / len(values))
            fielding = advanced.get("fielding") or {}
            outs = _v(fielding.get("outs"), 0.0)
            if outs > 0 and fielding.get("fielding_runs") is not None:
                frv.append(_clip(float(fielding["fielding_runs"]) * 1000 / max(outs, 250), -20, 20))
            if outs > 0 and fielding.get("outs_above_average") is not None:
                oaa.append(_clip(float(fielding["outs_above_average"]) * 1000 / max(outs, 250), -20, 20))
            if str(getattr(item, "position", "")).upper() == "C":
                if outs > 0 and fielding.get("framing_runs") is not None:
                    framing.append(_clip(float(fielding["framing_runs"]) * 1000 / max(outs, 250), -20, 20))
                battery_row = advanced.get("battery") or {}
                pitches = _v(battery_row.get("pitches"), 0.0)
                value = battery_row.get("pitcher_run_value_per_100")
                if value is not None and pitches:
                    battery.append(float(value) * min(.70, pitches / (pitches + 300.0)))
        output[f"{side}_xwoba"] = sum(xwoba) / len(xwoba) if xwoba else .320
        output[f"{side}_hard_hit"] = sum(hard_hit) / len(hard_hit) if hard_hit else LEAGUE_HARD_HIT_RATE
        output[f"{side}_hard_hit_available"] = float(bool(hard_hit))
        output[f"{side}_pitch_matchup"] = sum(pitch_matchups) / len(pitch_matchups) if pitch_matchups else .320
        output[f"{side}_frv"] = sum(frv) / max(9, len(frv))
        output[f"{side}_oaa"] = sum(oaa) / max(9, len(oaa))
        output[f"{side}_framing"] = sum(framing) if framing else 0.0
        output[f"{side}_battery"] = sum(battery) if battery else 0.0
        coverage.append(len(xwoba) / 9)
    output["coverage"] = min(coverage) if coverage else 0.0
    return output


# Share of batted balls hit at 95 mph or more, league-wide. The shrink target for a hitter with
# few plate appearances and the neutral point of the interaction below.
LEAGUE_HARD_HIT_RATE = .385
# How far a park and a lineup together may move the shape of an inning, as a proportion of the
# league-fitted overdispersion. Weights are ordered by how directly each input creates a crooked
# inning: a home run scores everyone on base at once, an extra-base hit sets one up, and hard
# contact is what makes either possible.
BATTED_BALL_CLUMPING = {"home_run": .55, "extra_base": .20, "hard_hit": .25, "limit": .35}


def batted_ball_clumping(event_factors: dict[str, Any] | None, hard_hit_rate: float | None,
                         weather_home_run_multiplier: float = 1.0) -> float:
    """How much lumpier this club's scoring gets from its contact quality in this park.

    Returned as a proportional change to the inning overdispersion, never to the run rate: the
    park's effect on how many runs score is already carried by the park factor in the run means,
    and adding it again here would count the same ballpark twice. What is missing from a mean is
    that the same expected total arrives differently - a park that lets balls out converts quiet
    innings into three-run innings, and it can only do that for a lineup that hits the ball hard
    enough to reach the seats.
    """
    events = event_factors or {}
    # The season park factor says how this yard plays on an ordinary night. The trajectory model
    # says how far tonight's air is from ordinary. Multiplying them keeps the empirical baseline,
    # which knows things geometry cannot - a marine layer, a prevailing wind - while adding the
    # one thing a season average can never contain, which is tonight.
    home_run = _v(events.get("home_run"), 1.0) * _v(weather_home_run_multiplier, 1.0)
    extra_base = (_v(events.get("double"), 1.0) + _v(events.get("triple"), 1.0)) / 2
    contact = (_v(hard_hit_rate, LEAGUE_HARD_HIT_RATE) or LEAGUE_HARD_HIT_RATE) / LEAGUE_HARD_HIT_RATE
    raw = (BATTED_BALL_CLUMPING["home_run"] * (home_run - 1)
           + BATTED_BALL_CLUMPING["extra_base"] * (extra_base - 1)
           + BATTED_BALL_CLUMPING["hard_hit"] * (contact - 1))
    limit = BATTED_BALL_CLUMPING["limit"]
    return float(_clip(raw, -limit, limit))


def _dynamic_park_factors(pregame: dict[str, Any], lineups: list[Any]) -> tuple[float, float, dict[str, Any]]:
    park = pregame.get("park_factors") or {}
    by_hand = park.get("by_batter_hand") or {}
    if not park.get("available") or not by_hand.get("ALL"):
        return 0.0, 0.0, {}
    event_output: dict[str, Any] = {}
    composites = {}
    for side in ("home", "away"):
        rows = [item for item in lineups if getattr(item, "side", None) == side]
        factors = []
        for item in rows or [None]:
            hand = str(getattr(item, "batting_side", "") or "").upper() if item else ""
            if hand == "S" and by_hand.get("L") and by_hand.get("R"):
                value = {key: (_v(by_hand["L"].get(key), 1.0) + _v(by_hand["R"].get(key), 1.0)) / 2
                         for key in ("runs", "woba", "double", "triple", "home_run")}
            else:
                value = by_hand.get(hand) or by_hand["ALL"]
            factors.append(value)
        averaged = {key: sum(_v(value.get(key), 1.0) for value in factors) / len(factors)
                    for key in ("runs", "woba", "double", "triple", "home_run")}
        event_output[side] = averaged
        composites[side] = (.50 * averaged["runs"] + .20 * averaged["woba"] +
                            .18 * averaged["home_run"] + .09 * averaged["double"] + .03 * averaged["triple"])
    return composites["home"], composites["away"], event_output


def _pitcher_statcast(pitcher: Any | None) -> dict[str, Any]:
    advanced = getattr(pitcher, "advanced", None) or {}
    expected = advanced.get("expected") or {}
    trend = advanced.get("pitch_trend") or {}
    return {
        "available": expected.get("xera") is not None or expected.get("xwoba") is not None,
        "pa": expected.get("pa"), "xera": expected.get("xera"), "xwoba": expected.get("xwoba"),
        "velocity_change": trend.get("fastball_velocity_change") if trend.get("available") else None,
        "usage_change": trend.get("arsenal_usage_change") if trend.get("available") else None,
    }


def _paired_nested_difference(first: dict[str, Any], second: dict[str, Any], field: str) -> float:
    if first.get(field) is None or second.get(field) is None:
        return 0.0
    return float(first[field]) - float(second[field])


def _quality_start_rate(pitcher: Any | None) -> float:
    starts = _v(getattr(pitcher, "games", None), 0.0)
    quality = _v(getattr(pitcher, "quality_starts", None), 0.0)
    return quality / starts if starts else 0.0


def _opponent_pitcher_era(pitcher: Any | None) -> tuple[float, float]:
    season_era = _v(getattr(pitcher, "era", None), 4.5)
    innings = _v(getattr(pitcher, "opponent_innings", None), 0.0)
    opponent_era = getattr(pitcher, "opponent_era", None)
    if opponent_era is None or innings <= 0:
        return season_era, 0.0
    # At 45 innings the opponent split receives half weight; never let it dominate the season body of work.
    weight = min(.50, innings / (innings + 45.0))
    return (1 - weight) * season_era + weight * float(opponent_era), weight


def _effective_pitcher_era(pitcher: Any | None, team_era: float) -> float:
    if pitcher is None:
        return team_era
    era, _ = _opponent_pitcher_era(pitcher)
    fip = _v(getattr(pitcher, "fip", None), era)
    skill_estimate = .58 * era + .42 * fip
    statcast = _pitcher_statcast(pitcher)
    if statcast.get("xera") is not None:
        expected_weight = min(.30, _v(statcast.get("pa"), 0.0) / (_v(statcast.get("pa"), 0.0) + 500.0))
        skill_estimate = (1 - expected_weight) * skill_estimate + expected_weight * _clip(float(statcast["xera"]), 1.0, 9.0)
    recent = getattr(pitcher, "recent", None) or {}
    recent_era = recent.get("era") if recent.get("available") else None
    recent_starts = int(recent.get("starts") or 0)
    if recent_era is not None and recent_starts:
        recent_weight = min(.18, recent_starts / (recent_starts + 15.0))
        skill_estimate = (1 - recent_weight) * skill_estimate + recent_weight * _clip(float(recent_era), 1.0, 9.0)
    games = _v(getattr(pitcher, "games", None), 0.0)
    sample_credibility = games / (games + 8.0)
    confirmation_factor = 1.0 if bool(getattr(pitcher, "confirmed", False)) else .35
    credibility = confirmation_factor * (.35 + .65 * sample_credibility)
    return (1 - credibility) * team_era + credibility * skill_estimate


def _starter_multiplier(pitcher: Any | None, team: Any, league: str) -> float:
    """How this starter suppresses runs relative to their own staff (below 1.0 is better)."""
    league_era = 4.60 if league == "KBO" else 4.10
    team_era = max(_v(getattr(team, "era", None), league_era), 1.5)
    return _clip(_effective_pitcher_era(pitcher, team_era) / team_era, .55, 1.70)


def _projected_starter_innings(pitcher: Any | None) -> float:
    innings = _clip(_v(getattr(pitcher, "avg_start_innings", None), 5.3), 3.0, 7.5)
    recent = getattr(pitcher, "recent", None) or {}
    limit = recent.get("derived_pitch_limit") if recent.get("available") else None
    if limit is None:
        return innings
    # A workload-derived ceiling only nudges duration; it never masquerades as a manager quote.
    return _clip(innings * _clip(float(limit) / 95.0, .85, 1.10), 3.0, 7.5)


def _bullpen_proxy(team: Any, pitcher: Any | None) -> float:
    # Official team ERA plus expected bullpen innings: a transparent availability proxy, not a true bullpen roster model.
    team_era = _v(getattr(team, "era", None), 4.5)
    starter_innings = _v(getattr(pitcher, "avg_start_innings", None), 5.0)
    recent_burden = _v(getattr(pitcher, "recent_pitches", None), 0.0) / 100
    return -team_era - max(0.0, 5.5 - starter_innings) * .35 - recent_burden * .08


def _paired_pitcher_difference(first: Any | None, second: Any | None, field: str) -> float:
    first_value = getattr(first, field, None)
    second_value = getattr(second, field, None)
    if first_value is None or second_value is None:
        return 0.0
    return float(first_value) - float(second_value)


def _recent_pitcher_deviation(pitcher: Any | None, field: str) -> float:
    recent = getattr(pitcher, "recent", None) or {}
    recent_value = recent.get(field) if recent.get("available") else None
    season_value = getattr(pitcher, field, None)
    if recent_value is None or season_value is None:
        return 0.0
    return float(recent_value) - float(season_value)


def _fielding_index(stat: Any) -> float:
    advanced = getattr(stat, "advanced", None) or {}
    games = max(1, int(_v(getattr(stat, "games", None), 1)))
    percentage = _v(advanced.get("fielding_percentage"), .982)
    errors_per_game = _v(advanced.get("errors"), .55 * games) / games
    return _clip((percentage - .982) / .01 - .35 * (errors_per_game - .55), -1.5, 1.5)


def _baserunning_index(stat: Any) -> float:
    advanced = getattr(stat, "advanced", None) or {}
    games = max(1, int(_v(getattr(stat, "games", None), 1)))
    value = (.20 * _v(advanced.get("stolen_bases"), 0.0) -
             .40 * _v(advanced.get("caught_stealing"), 0.0)) / games
    return _clip(value, -.25, .25)


def _catcher_index(stat: Any) -> float:
    advanced = getattr(stat, "advanced", None) or {}
    allowed = advanced.get("opponent_stolen_base_percentage")
    games = max(1, int(_v(getattr(stat, "games", None), 1)))
    passed = _v(advanced.get("passed_balls"), 0.0) / games
    if allowed is None:
        return 0.0
    return _clip((.72 - float(allowed)) * 2 - passed * .5, -.5, .5)


def _advanced_pair_available(home: Any, away: Any, field: str) -> bool:
    return all((getattr(stat, "advanced", None) or {}).get(field) is not None for stat in (home, away))


def _team_rest_days(team: Any, target: date | None) -> float:
    if not target:
        return 1.0
    details = (getattr(team, "recent", None) or {}).get("10", {}).get("games_detail", [])
    if not details:
        return 1.0
    try:
        last = max(date.fromisoformat(row["date"]) for row in details if row.get("date"))
    except (ValueError, TypeError):
        return 1.0
    return float(_clip((target - last).days, 0, 5))


def _matchup(team: Any, opponent: str | None) -> tuple[float, float, int]:
    if not opponent:
        return 0.0, 0.0, 0
    recent = getattr(team, "recent", None) or {}
    stored = recent.get("matchups", {}).get(opponent)
    if stored:
        n = int(stored.get("games", 0))
        shrink = n / (n + 12)
        win_diff = (_v(stored.get("win_rate"), .5) - .5) * shrink
        run_diff = _v(stored.get("avg_run_diff"), 0.0) * shrink
        return win_diff, _clip(run_diff, -2.5, 2.5), n
    details = recent.get("20", {}).get("games_detail", [])
    sample = [row for row in details if row.get("opponent") == opponent]
    if not sample:
        return 0.0, 0.0, 0
    points = sum(1.0 if row.get("result") == "W" else (.5 if row.get("result") == "D" else 0.0) for row in sample)
    shrink = len(sample) / (len(sample) + 12)
    return (points / len(sample) - .5) * shrink, _clip(sum(row["runs"] - row["allowed"] for row in sample) / len(sample) * shrink, -2.5, 2.5), len(sample)


def _head_to_head(team: Any, opponent: str | None) -> float:
    """Compatibility helper retained for callers and historical tests."""
    return _matchup(team, opponent)[0]
