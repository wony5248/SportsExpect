from __future__ import annotations

import math
from datetime import date
from typing import Any


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
    (lineup_diff, home_lineup_index, away_lineup_index, home_lineup_confirmed, away_lineup_confirmed,
     home_bvp_adjustment, away_bvp_adjustment, home_bvp_coverage, away_bvp_coverage,
     home_bvp_pa, away_bvp_pa) = _lineup_feature(lineups or [])
    park_factors = MLB_PARK_FACTORS if league == "MLB" else KBO_PARK_FACTORS
    opponent_name = getattr(getattr(away, "team", None), "name", None)
    matchup = _matchup(home, opponent_name)
    home_matchup_era, home_matchup_weight = _opponent_pitcher_era(home_pitcher)
    away_matchup_era, away_matchup_weight = _opponent_pitcher_era(away_pitcher)
    return {
        "league_average_runs": float(context.get("league_average_runs") or (5.15 if league == "KBO" else 4.45)),
        "season_win_rate_diff": _v(home.win_rate, .5) - _v(away.win_rate, .5),
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
        "starter_fip_diff": _v(getattr(away_pitcher, "fip", None), _v(getattr(away_pitcher, "era", None), 4.5)) - _v(getattr(home_pitcher, "fip", None), _v(getattr(home_pitcher, "era", None), 4.5)),
        "starter_k_bb_diff": _v(getattr(home_pitcher, "k_bb_rate", None), 0.0) - _v(getattr(away_pitcher, "k_bb_rate", None), 0.0),
        "starter_durability_diff": _v(getattr(home_pitcher, "avg_start_innings", None), 5.0) - _v(getattr(away_pitcher, "avg_start_innings", None), 5.0),
        "quality_start_rate_diff": _quality_start_rate(home_pitcher) - _quality_start_rate(away_pitcher),
        "starter_rest_days_diff": _clip(_v(getattr(home_pitcher, "rest_days", None), 5.0), 2, 8) - _clip(_v(getattr(away_pitcher, "rest_days", None), 5.0), 2, 8),
        "bullpen_proxy_diff": _bullpen_proxy(home, home_pitcher) - _bullpen_proxy(away, away_pitcher),
        "recent_pitch_burden_diff": _v(getattr(away_pitcher, "recent_pitches", None), 0.0) - _v(getattr(home_pitcher, "recent_pitches", None), 0.0),
        "rest_days_diff": _team_rest_days(home, game_date) - _team_rest_days(away, game_date),
        "doubleheader_diff": float(context.get("away_games_today", 1) - context.get("home_games_today", 1)),
        "head_to_head_diff": matchup[0],
        "head_to_head_run_diff": matchup[1],
        "head_to_head_games": matchup[2],
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
        "home_starter_innings": _clip(_v(getattr(home_pitcher, "avg_start_innings", None), 5.3), 3.0, 7.5),
        "away_starter_innings": _clip(_v(getattr(away_pitcher, "avg_start_innings", None), 5.3), 3.0, 7.5),
        "park_factor": park_factors.get(stadium or "", 1.0),
        "lineup_strength_diff": lineup_diff,
        "home_lineup_index": home_lineup_index,
        "away_lineup_index": away_lineup_index,
        "lineup_bvp_diff": home_bvp_adjustment - away_bvp_adjustment,
        "home_lineup_bvp_coverage": home_bvp_coverage,
        "away_lineup_bvp_coverage": away_bvp_coverage,
        "home_lineup_bvp_pa": home_bvp_pa,
        "away_lineup_bvp_pa": away_bvp_pa,
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
    z += 0.004 * float(features["starter_rest_days_diff"])
    z += 0.0008 * float(features["recent_pitch_burden_diff"])
    z += 0.012 * float(features["rest_days_diff"])
    z += 0.018 * float(features["doubleheader_diff"])
    z += matchup_reliability * 0.10 * float(features["head_to_head_diff"])
    z += matchup_reliability * 0.025 * float(features["head_to_head_run_diff"])
    z += 0.045 * float(features["starter_opponent_era_diff"])
    z += lineup_reliability * 0.18 * float(features["lineup_strength_diff"])
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

    avg_anchor = max(.210, (_v(getattr(home, "avg", None), .260) + _v(getattr(away, "avg", None), .260)) / 2)
    obp_anchor = max(.280, (_v(getattr(home, "obp", None), .330) + _v(getattr(away, "obp", None), .330)) / 2)
    slg_anchor = max(.330, (_v(getattr(home, "slg", None), .410) + _v(getattr(away, "slg", None), .410)) / 2)
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
    home_expected = home_base * home_batting * away_pitching * park_factor * 1.035 * lineup_home * recent_home * bullpen_home * matchup_home
    away_expected = away_base * away_batting * home_pitching * park_factor * 0.985 * lineup_away * recent_away * bullpen_away * matchup_away
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


def _bullpen_proxy(team: Any, pitcher: Any | None) -> float:
    # Official team ERA plus expected bullpen innings: a transparent availability proxy, not a true bullpen roster model.
    team_era = _v(getattr(team, "era", None), 4.5)
    starter_innings = _v(getattr(pitcher, "avg_start_innings", None), 5.0)
    recent_burden = _v(getattr(pitcher, "recent_pitches", None), 0.0) / 100
    return -team_era - max(0.0, 5.5 - starter_innings) * .35 - recent_burden * .08


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
