from __future__ import annotations

from collections import Counter
import math
from typing import Any

import numpy as np

from backend.app.services.probability_calibration import calibrated_probability


def validate_simulation_summary(summary: dict[str, Any]) -> None:
    """Reject a simulation summary when probabilities or intervals are internally inconsistent."""
    tolerance = 1e-9

    def probability(name: str, value: Any) -> float:
        number = float(value)
        if not np.isfinite(number) or not 0 <= number <= 1:
            raise ValueError(f"invalid probability {name}: {value}")
        return number

    home_win = probability("home_win_probability", summary["home_win_probability"])
    away_win = probability("away_win_probability", summary["away_win_probability"])
    tie = probability("tie_probability", summary["tie_probability"])
    home_two_way = probability("home_two_way_probability", summary["home_two_way_probability"])
    away_two_way = probability("away_two_way_probability", summary["away_two_way_probability"])
    if abs(home_win + away_win + tie - 1) > tolerance:
        raise ValueError("9-inning win/loss/tie probabilities must sum to 1")
    if abs(home_two_way + away_two_way - 1) > tolerance:
        raise ValueError("two-way win probabilities must sum to 1")

    handicap = summary["handicap"]
    for name, value in handicap.items():
        probability(f"handicap.{name}", value)
    if abs(handicap["home_minus_1_5"] + handicap["away_plus_1_5"] - 1) > tolerance:
        raise ValueError("home -1.5 and away +1.5 probabilities must be complements")
    if abs(handicap["away_minus_1_5"] + handicap["home_plus_1_5"] - 1) > tolerance:
        raise ValueError("away -1.5 and home +1.5 probabilities must be complements")

    for line, values in summary["totals"].items():
        over = probability(f"totals.{line}.over", values["over"])
        under = probability(f"totals.{line}.under", values["under"])
        push = probability(f"totals.{line}.push", values["push"])
        if abs(over + under + push - 1) > tolerance:
            raise ValueError(f"total probabilities at {line} must sum to 1")

    market_handicap = summary.get("market_handicap")
    if market_handicap:
        minus = probability("market_handicap.minus_probability", market_handicap["minus_probability"])
        plus = probability("market_handicap.plus_probability", market_handicap["plus_probability"])
        push = probability("market_handicap.push_probability", market_handicap["push_probability"])
        if abs(minus + plus + push - 1) > tolerance:
            raise ValueError("market run-line probabilities must sum to 1")

    conditional = summary.get("winner_conditional_market")
    if conditional:
        scenario = probability("winner_conditional_market.scenario_probability",
                               conditional["scenario_probability"])
        expected_winner = "HOME" if home_two_way >= away_two_way else "AWAY"
        if conditional["winner"] != expected_winner:
            raise ValueError("the conditional branch must be the club the two-way probability favors")
        conditional_handicap = conditional["handicap"]
        minus = probability("winner_conditional_market.handicap.model_minus_probability",
                            conditional_handicap["model_minus_probability"])
        plus = probability("winner_conditional_market.handicap.model_plus_probability",
                           conditional_handicap["model_plus_probability"])
        if abs(minus + plus - 1) > .0002:
            raise ValueError("run-line two-way probabilities must sum to 1")
        market_minus = conditional_handicap["market_minus_probability"]
        if market_minus is not None:
            priced = probability("winner_conditional_market.handicap.market_minus_probability",
                                 market_minus)
            expected_edge = round(minus - priced, 4)
            if abs(float(conditional_handicap["edge"]) - expected_edge) > .0002:
                raise ValueError("run-line edge must be the model probability less the market price")
        cover = probability("winner_conditional_market.handicap.winner_cover_probability",
                            conditional_handicap["winner_cover_probability"])
        short = probability("winner_conditional_market.handicap.winner_short_probability",
                            conditional_handicap["winner_short_probability"])
        push = probability("winner_conditional_market.handicap.winner_push_probability",
                           conditional_handicap["winner_push_probability"])
        if abs(cover + short + push - 1) > .0002:
            raise ValueError("winner-conditional run-line probabilities must sum to 1")
        total = conditional["headline_total"]
        over = probability("winner_conditional_market.headline_total.over_probability",
                           total["over_probability"])
        under = probability("winner_conditional_market.headline_total.under_probability",
                            total["under_probability"])
        total_push = probability("winner_conditional_market.headline_total.push_probability",
                                 total["push_probability"])
        if abs(over + under + total_push - 1) > .0002:
            raise ValueError("conditional total probabilities must sum to 1")
        model_over = probability("winner_conditional_market.headline_total.model_over_probability",
                                 total["model_over_probability"])
        model_under = probability("winner_conditional_market.headline_total.model_under_probability",
                                  total["model_under_probability"])
        if abs(model_over + model_under - 1) > .0002:
            raise ValueError("total two-way probabilities must sum to 1")
        market_over = total["market_over_probability"]
        if market_over is not None:
            priced_over = probability("winner_conditional_market.headline_total.market_over_probability",
                                      market_over)
            if abs(float(total["edge"]) - round(model_over - priced_over, 4)) > .0002:
                raise ValueError("total edge must be the model probability less the market price")
        # A conditional percentage describes only its own branch, so the chance of the pick
        # landing at all can never exceed the chance the branch happens.
        joint_cover = probability(
            "winner_conditional_market.handicap.joint_winner_cover_probability",
            conditional_handicap["joint_winner_cover_probability"])
        if joint_cover > scenario + 1e-6:
            raise ValueError("winner-conditional cover cannot exceed its scenario probability")
        if probability("winner_conditional_market.headline_total.joint_pick_probability",
                       total["joint_pick_probability"]) > scenario + 1e-6:
            raise ValueError("headline_total joint probability cannot exceed its scenario probability")

    quantile_groups = [summary["total_quantiles"], *summary["team_quantiles"].values()]
    if any(group["p10"] > group["p50"] or group["p50"] > group["p90"] for group in quantile_groups):
        raise ValueError("simulation quantiles must be ordered p10 <= p50 <= p90")

    dense_groups = [summary["total_dense_interval"], *summary["team_dense_intervals"].values()]
    for group in dense_groups:
        if group["low"] > group["high"] or not 0 < probability("dense_interval.mass", group["mass"]) <= 1:
            raise ValueError("dense intervals must be ordered with a valid probability mass")


# Relief tiers, ordered worst-to-best at suppressing runs. A manager does not empty the bullpen
# at random: the closer/setup group (필승조) protects a close late game, the chase group (추격조)
# holds the line while the club is behind but still within reach, and the mop-up group (등봉조)
# eats innings once the game is decided. These league-wide defaults apply until a per-team
# profile replaces them; see services/bullpen.py.
BULLPEN_TIERS = ("high_leverage", "middle", "chase", "mop_up")
DEFAULT_BULLPEN = {"high_leverage": .82, "middle": 1.00, "chase": 1.12, "mop_up": 1.28}
# Typical share of relief innings by tier. Only a starting point for the mean normalizer, which
# is replaced with the usage the leverage rules actually produce for this matchup.
BULLPEN_USAGE_MIX = {"high_leverage": .34, "middle": .40, "chase": .16, "mop_up": .10}
# Starters do not all last their season average; this spread turns the average into a workload.
STARTER_EXIT_SPREAD = 1.2
LEAGUE_AVERAGE_STARTER_INNINGS = 5.3
# Leverage rules, read from the pitching club's own point of view.
# From the sixth inning on, a tie, a lead of up to three, or a one-run deficit is the classic
# save/hold situation the best arms are held back for.
LATE_INNING_INDEX = 5
HIGH_LEVERAGE_LEAD = (-1, 3)
# Behind by two to five late: still one swing from level, so the chase group works.
CHASE_LEAD = (-5, -2)
# Six runs either way and the game is over as a contest; the mop-up group finishes it.
BLOWOUT_MARGIN = 6

# KBO regular-season games end after inning 11 with no tiebreaker; ties stand.
KBO_MAX_EXTRA_INNINGS = 2
# MLB extras start every half-inning with an automatic runner on second base. Empirically that
# lifts the half-inning run expectancy from roughly .5 to about 1.1 runs.
MLB_GHOST_RUNNER_BONUS = .55
# Extra innings are pitched by late-game bullpens; weight mirrors the ninth-inning share below.
EXTRA_INNING_WEIGHT = .115
# Practical cap only: with ghost runners the chance of 30 straight tied extra innings is ~1e-14.
MLB_MAX_EXTRA_INNINGS = 30
# Headline forecast band: the shortest run interval holding this much probability mass.
# Far tighter than the central 80% band on these right-skewed scoring distributions.
DENSE_INTERVAL_MASS = .60
# Cover every plausible baseball total posted by a book. Integer values are retained as ints so
# JSON keys match JavaScript's String(8) rather than becoming "8.0".
TOTAL_MARKET_LINES = tuple(value // 2 if value % 2 == 0 else value / 2 for value in range(8, 41))
# Ratio of variance to mean for the runs scored in one half-inning. Real half-innings are
# nothing like Poisson: across 34,278 MLB half-innings the ratio is 2.14, because runs arrive in
# rallies rather than one at a time. 73% of half-innings are scoreless and 5.7% score three or
# more, where a Poisson draw at the same mean gives 61% and 1.4%. Modelling them as Poisson made
# runs dribble out evenly, which pushed the two clubs' totals together and produced far too many
# one-run games and far too few blowouts. Fitted per league against the real half-inning tables.
# Fitted jointly with the team and matchup terms below against the real per-game distributions,
# because all three draw on the same variance budget: raising one without lowering the others
# simply inflates every score. Overdispersion this side of the fitted value would match the real
# half-inning table more closely still, but only by giving game totals more spread than real
# games have - real innings are negatively dependent within a game (a club that puts up five
# gets shut down after the pitching change) and this engine draws them independently.
INNING_VARIANCE_RATIO = {"MLB": 1.6, "KBO": 1.6}
# Log-scale spread of the unobserved day-of matchup tilt, fitted per league. This is the term
# that carries the strength gap the model cannot identify: real margins are wider than anything
# our per-game means explain, but our predicted margins correlate only .08 with actual ones, so
# widening the means would be false precision. An unattributed opposing tilt widens the margin
# distribution without claiming to know which club it favours in any given game.
MATCHUP_VARIANCE = {"MLB": .18, "KBO": .22}


def _draw_runs(rng: np.random.Generator, rate: np.ndarray | float, ratio: float) -> np.ndarray:
    """Runs in one half-inning: overdispersed around `rate` by `ratio`, mean preserved."""
    if ratio <= 1.0:
        return rng.poisson(rate)
    mean = np.maximum(np.asarray(rate, dtype=float), 1e-9)
    # Negative binomial with variance `ratio` x mean. Shape falls as the mean falls, which is
    # what keeps a quiet inning quiet instead of merely rescaling a Poisson.
    shape = mean / (ratio - 1.0)
    return rng.negative_binomial(shape, shape / (shape + mean))


# A representative score is already conditional on the predicted winner winning. Requiring only
# 50% cover probability inside that branch overstates weak favorites, so promotion beyond the
# active market run line needs a materially stronger conditional majority. This value is
# walk-forward tuned.
HEADLINE_CONDITIONAL_COVER_THRESHOLD = .72


def highest_density_interval(values: np.ndarray, mass: float = DENSE_INTERVAL_MASS) -> dict[str, Any]:
    """Shortest contiguous integer interval containing at least `mass` of the simulations."""
    counts = np.bincount(values)
    target = mass * values.size
    best_low, best_high, best_count = 0, counts.size - 1, int(values.size)
    for low in range(counts.size):
        cumulative = 0
        for high in range(low, counts.size):
            cumulative += int(counts[high])
            if cumulative >= target:
                if (high - low, -cumulative) < (best_high - best_low, -best_count):
                    best_low, best_high, best_count = low, high, cumulative
                break
    return {"low": best_low, "high": best_high, "mass": round(best_count / values.size, 4)}


def _systematic_branch_sample(indices: np.ndarray, target_size: int) -> np.ndarray:
    """Resize one outcome branch without favoring its most common scores.

    The source simulation is already exchangeable, so evenly spaced deterministic positions
    preserve its within-branch score and inning-path shape while making stored replays exactly
    reproducible.
    """
    if target_size <= 0:
        return np.empty(0, dtype=np.int64)
    if indices.size == 0:
        raise ValueError("cannot assign calibrated mass to an empty outcome branch")
    positions = np.floor((np.arange(target_size, dtype=float) + .5) * indices.size / target_size).astype(np.int64)
    return indices[np.minimum(positions, indices.size - 1)]


def _reweight_win_branches(home_innings: np.ndarray, away_innings: np.ndarray,
                           home: np.ndarray, away: np.ndarray,
                           calibration: dict[str, Any] | None
                           ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Match the simulated winner mix to a leakage-safe calibrated two-way probability.

    KBO tie mass is retained unchanged. Only the decided-game home/away mixture is resized, and
    every downstream metric is subsequently calculated from this same integer population.
    """
    home_indices = np.flatnonzero(home > away)
    away_indices = np.flatnonzero(away > home)
    tie_indices = np.flatnonzero(home == away)
    decided = home_indices.size + away_indices.size
    raw_home_two_way = home_indices.size / decided if decided else .5
    target_home_two_way = calibrated_probability(raw_home_two_way, calibration)
    if not decided or not home_indices.size or not away_indices.size:
        target_home_count = home_indices.size
        target_home_two_way = raw_home_two_way
    else:
        target_home_count = int(round(target_home_two_way * decided))
    target_away_count = decided - target_home_count
    selected = np.concatenate([
        _systematic_branch_sample(home_indices, target_home_count),
        _systematic_branch_sample(away_indices, target_away_count),
        tie_indices,
    ])
    # Sorting restores chronological draw order where possible. Repeated indices remain exact
    # copies of the same simulated game, including its full inning trajectory.
    selected.sort(kind="stable")
    metadata = {
        **(calibration or {}),
        "enabled": bool((calibration or {}).get("enabled")),
        "raw_home_two_way_probability": round(raw_home_two_way, 8),
        "raw_away_two_way_probability": round(1 - raw_home_two_way, 8),
        "target_home_two_way_probability": round(target_home_two_way, 8),
        "target_away_two_way_probability": round(1 - target_home_two_way, 8),
        "raw_branch_counts": {
            "home_win": int(home_indices.size), "away_win": int(away_indices.size),
            "tie": int(tie_indices.size),
        },
        "reweighted_branch_counts": {
            "home_win": target_home_count, "away_win": target_away_count,
            "tie": int(tie_indices.size),
        },
        "population_size": int(selected.size),
        "population_method": "DETERMINISTIC_STRATIFIED_OUTCOME_RESAMPLING",
    }
    return (home_innings[selected], away_innings[selected], home[selected], away[selected], metadata)


def _compact_distribution(home: np.ndarray, away: np.ndarray) -> dict[str, Any]:
    """Small, durable audit surface for scoring calibration before and after reweighting."""
    total = home + away
    margin = home - away
    decided = int(np.count_nonzero(margin))
    home_two_way = float(np.count_nonzero(margin > 0) / decided) if decided else .5
    return {
        "home_two_way_probability": round(home_two_way, 8),
        "mean_runs": {"home": round(float(home.mean()), 6), "away": round(float(away.mean()), 6)},
        "handicap": {
            "home_minus_1_5": round(float(np.mean(margin >= 2)), 8),
            "away_minus_1_5": round(float(np.mean(margin <= -2)), 8),
            "home_plus_1_5": round(float(np.mean(margin >= -1)), 8),
            "away_plus_1_5": round(float(np.mean(margin <= 1)), 8),
        },
        "totals": {
            str(line): {
                "over": round(float(np.mean(total > line)), 8),
                "under": round(float(np.mean(total < line)), 8),
                "push": round(float(np.mean(total == line)), 8),
            }
            for line in TOTAL_MARKET_LINES
        },
    }


def simulate_scores(home_expected: float, away_expected: float, simulations: int, seed: int,
                    environment_variance: float = .08, team_variance: float = .12,
                    league: str = "MLB", home_staff: dict[str, Any] | None = None,
                    away_staff: dict[str, Any] | None = None, home_lineup: np.ndarray | None = None,
                    away_lineup: np.ndarray | None = None,
                    observed_result: dict[str, Any] | None = None,
                    home_team_variance: float | None = None,
                    away_team_variance: float | None = None,
                    headline_total_line: float | None = None,
                    headline_home_spread: float | None = None,
                    headline_spread_probability: float | None = None,
                    headline_total_over_probability: float | None = None,
                    probability_calibration: dict[str, Any] | None = None,
                    home_event_factors: dict[str, float] | None = None,
                    away_event_factors: dict[str, float] | None = None,
                    inning_variance_ratio: float | None = None,
                    home_inning_variance_ratio: float | None = None,
                    away_inning_variance_ratio: float | None = None,
                    matchup_variance: float | None = None) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    if home_lineup is not None and away_lineup is not None:
        # Both lineups have collected splits, so play the game out plate appearance by plate
        # appearance instead of drawing inning run totals.
        return _summarize(*_plate_appearance_game(
            rng, home_expected, away_expected, simulations, league, home_staff, away_staff,
            home_lineup, away_lineup, home_team_variance, away_team_variance,
            home_event_factors, away_event_factors),
            observed_result=observed_result, headline_total_line=headline_total_line,
            headline_home_spread=headline_home_spread,
            headline_spread_probability=headline_spread_probability,
            headline_total_over_probability=headline_total_over_probability,
            probability_calibration=probability_calibration)
    # A shared gamma run environment would make both clubs score together. Real games do not
    # behave that way: across 1,953 finished MLB games the two clubs' scores correlate -0.02,
    # and across 555 KBO games -0.03, i.e. independent. The conditions this term was meant to
    # represent - park, weather, umpire - are already in the run means through the park factor
    # and the weather multiplier, so drawing them again here double-counted them and forced a
    # +0.16 correlation into every matchup. That inflated total variance and, because the shared
    # factor cancels in the difference, squeezed run margins toward one run.
    variance = min(.18, max(0.0, environment_variance))
    shared_environment = (rng.gamma(1 / variance, variance, simulations) if variance > 0
                          else np.ones(simulations))
    inning_ratio = (float(inning_variance_ratio) if inning_variance_ratio is not None
                    else INNING_VARIANCE_RATIO.get(league, 1.0))
    # A park's effect on run totals is already in the run means through the park factor. What it
    # also does, and what a mean cannot express, is change the shape: a park where balls leave
    # the yard turns quiet innings into three-run innings, so the same expected total arrives in
    # lumps. Each club carries its own value because the park is shared but the contact is not.
    home_ratio = max(1.0, float(home_inning_variance_ratio)) if home_inning_variance_ratio is not None else inning_ratio
    away_ratio = max(1.0, float(away_inning_variance_ratio)) if away_inning_variance_ratio is not None else inning_ratio
    # Whatever separates two clubs on the day that the model could not see - a starter sharper
    # than his ERA, a lineup that is hot - lifts one side and suppresses the other, because runs
    # are a contest between an offense and the opposing pitching. That is one unobserved shock
    # pointing in opposite directions, so it widens run margins without touching run totals,
    # which independent per-club shocks cannot do. Mean-preserving in both directions.
    matchup_sigma = max(0.0, float(matchup_variance if matchup_variance is not None
                                   else MATCHUP_VARIANCE.get(league, 0.0)))
    if matchup_sigma > 0:
        tilt = rng.normal(0.0, matchup_sigma, simulations)
        home_tilt = np.exp(tilt - matchup_sigma ** 2 / 2)
        away_tilt = np.exp(-tilt - matchup_sigma ** 2 / 2)
    else:
        home_tilt = away_tilt = np.ones(simulations)
    # Baseball scoring is more dispersed than a Poisson process even after shared weather/park effects.
    # Independent gamma shocks represent sequencing, defense and bullpen execution specific to each club.
    home_independent_variance = min(.32, max(.01,
        team_variance if home_team_variance is None else home_team_variance))
    away_independent_variance = min(.32, max(.01,
        team_variance if away_team_variance is None else away_team_variance))
    home_environment = rng.gamma(1 / home_independent_variance, home_independent_variance, simulations)
    away_environment = rng.gamma(1 / away_independent_variance, away_independent_variance, simulations)
    # Split each expected total across nine innings. Slightly higher late-inning weights reflect
    # starter fatigue and bullpen exposure while preserving each team's full-game expectation.
    away_weights = np.array([.105, .105, .105, .108, .110, .112, .115, .118, .122])
    home_weights = np.array([.108, .106, .105, .108, .110, .112, .114, .117, .120])
    away_weights /= away_weights.sum()
    home_weights /= home_weights.sum()
    # Who is on the mound depends on the score, so innings are drawn one at a time. The away
    # staff suppresses home scoring and vice versa.
    home_rate = home_expected * shared_environment * home_environment * home_tilt
    away_rate = away_expected * shared_environment * away_environment * away_tilt
    away_staff_profile = _staff_profile(away_staff, rng, simulations)
    home_staff_profile = _staff_profile(home_staff, rng, simulations)

    def play_nine(calibrating: bool, home_scale: float = 1.0, away_scale: float = 1.0
                  ) -> tuple[np.ndarray, np.ndarray, Counter[str], Counter[str]]:
        home_line = np.zeros((simulations, 9), dtype=np.int64)
        away_line = np.zeros((simulations, 9), dtype=np.int64)
        home_total = np.zeros(simulations, dtype=np.int64)
        away_total = np.zeros(simulations, dtype=np.int64)
        away_tiers: Counter[str] = Counter()
        home_tiers: Counter[str] = Counter()
        for inning in range(9):
            # Top half: the away club bats against the home staff. Each staff reads its own
            # scoreboard, so leading by two calls for different arms than trailing by two.
            home_multiplier, home_used = _inning_multiplier(
                home_staff_profile, inning, home_total - away_total, calibrating)
            home_tiers.update(home_used)
            away_runs = _draw_runs(rng, away_rate * away_scale * away_weights[inning] * home_multiplier,
                                   away_ratio)
            away_line[:, inning] = away_runs
            away_total += away_runs
            # Bottom half, with the top half already on the board.
            away_multiplier, away_used = _inning_multiplier(
                away_staff_profile, inning, away_total - home_total, calibrating)
            away_tiers.update(away_used)
            home_runs = _draw_runs(rng, home_rate * home_scale * home_weights[inning] * away_multiplier,
                                   home_ratio)
            if inning == 8:
                # The home club does not bat in the ninth while ahead, and a walk-off ends the
                # inning the moment the winning run scores. Both cap home scoring in a way a
                # plain nine-inning draw cannot reproduce.
                deficit = away_total - home_total
                home_runs = np.where(deficit >= 0, np.minimum(home_runs, deficit + 1), 0)
            home_line[:, inning] = home_runs
            home_total += home_runs
        return home_line, away_line, home_tiers, away_tiers

    # A neutral pass first. It measures two things the rules make impossible to know upfront:
    # how often each relief tier is actually called on, and how much the ninth-inning rules
    # trim home scoring. Both feed corrections so the pitcher plan changes the shape of the
    # game while each club still lands on its expected run total.
    calibration_home_line, calibration_away_line, calibration_home, calibration_away = play_nine(calibrating=True)
    _apply_realized_normalizer(home_staff_profile, calibration_home)
    _apply_realized_normalizer(away_staff_profile, calibration_away)
    # League-accurate extra innings. MLB plays the automatic-runner tiebreaker until every game
    # is decided; KBO plays innings 10-11 without a tiebreaker and lets remaining ties stand.
    # Extra innings are the definition of high leverage: both managers are down to their best
    # available arms, so the high-leverage multiplier applies for the rest of the game.
    home_extra_rate = home_rate * EXTRA_INNING_WEIGHT * _normalized_tier(away_staff_profile, "high_leverage")
    away_extra_rate = away_rate * EXTRA_INNING_WEIGHT * _normalized_tier(home_staff_profile, "high_leverage")
    ghost_bonus = MLB_GHOST_RUNNER_BONUS if league == "MLB" else 0.0
    max_extra_innings = MLB_MAX_EXTRA_INNINGS if league == "MLB" else KBO_MAX_EXTRA_INNINGS
    # A club's season run rate already counts the runs it scored in extra innings, so the nine
    # innings must not be asked to reproduce that whole rate and then have the tiebreaker add
    # more on top. Play the tiebreaker once on the neutral pass to measure what it contributes,
    # and hand the nine innings only the rest. Without this every total ran about a third of a
    # run high, which biased every over/under read toward the over.
    calibration_extra_home, calibration_extra_away, _, _ = _play_extras(
        rng, calibration_home_line.sum(axis=1), calibration_away_line.sum(axis=1),
        home_extra_rate, away_extra_rate, ghost_bonus, max_extra_innings, league,
        home_ratio, away_ratio, simulations, collect_columns=False,
    )
    home_scale = _rate_correction(home_expected - float(calibration_extra_home.mean()),
                                  calibration_home_line)
    away_scale = _rate_correction(away_expected - float(calibration_extra_away.mean()),
                                  calibration_away_line)
    home_innings, away_innings, home_tier_counts, away_tier_counts = play_nine(False, home_scale, away_scale)
    home = home_innings.sum(axis=1)
    away = away_innings.sum(axis=1)
    extra_home_added, extra_away_added, extra_home_columns, extra_away_columns = _play_extras(
        rng, home, away, home_extra_rate, away_extra_rate, ghost_bonus, max_extra_innings,
        league, home_ratio, away_ratio, simulations,
    )
    if extra_away_columns:
        away_innings = np.concatenate([away_innings, np.stack(extra_away_columns, axis=1)], axis=1)
        home_innings = np.concatenate([home_innings, np.stack(extra_home_columns, axis=1)], axis=1)
    usage = {"home": _bullpen_usage(home_staff_profile, home_tier_counts),
             "away": _bullpen_usage(away_staff_profile, away_tier_counts)}
    return _summarize(home_innings, away_innings, home, away, simulations, league, usage, "INNING_RATE",
                      observed_result=observed_result, headline_total_line=headline_total_line,
                      headline_home_spread=headline_home_spread,
                      headline_spread_probability=headline_spread_probability,
                      headline_total_over_probability=headline_total_over_probability,
                      probability_calibration=probability_calibration)


def evaluate_simulation_recipe(recipe: dict[str, Any], observed_result: dict[str, Any]) -> dict[str, Any]:
    """Deterministically reproduce one stored simulation and compare its full population."""
    result = simulate_scores(
        float(recipe["home_expected"]), float(recipe["away_expected"]), int(recipe["simulations"]),
        int(recipe["seed"]), float(recipe.get("environment_variance", .08)),
        float(recipe.get("team_variance", .12)), league=str(recipe.get("league", "MLB")),
        home_staff=recipe.get("home_staff"), away_staff=recipe.get("away_staff"),
        home_lineup=_recipe_array(recipe.get("home_lineup")),
        away_lineup=_recipe_array(recipe.get("away_lineup")), observed_result=observed_result,
        home_team_variance=recipe.get("home_team_variance"),
        away_team_variance=recipe.get("away_team_variance"),
        headline_total_line=recipe.get("headline_total_line"),
        headline_home_spread=recipe.get("headline_home_spread"),
        headline_spread_probability=recipe.get("headline_spread_probability"),
        headline_total_over_probability=recipe.get("headline_total_over_probability"),
        home_inning_variance_ratio=recipe.get("home_inning_variance_ratio"),
        away_inning_variance_ratio=recipe.get("away_inning_variance_ratio"),
        probability_calibration=recipe.get("probability_calibration"),
    )
    return result["observed_evaluation"]


def _recipe_array(value: Any) -> np.ndarray | None:
    return np.asarray(value, dtype=float) if value is not None else None


def _plate_appearance_game(rng: np.random.Generator, home_expected: float, away_expected: float,
                           simulations: int, league: str, home_staff: dict[str, Any] | None,
                           away_staff: dict[str, Any] | None, home_lineup: np.ndarray,
                           away_lineup: np.ndarray, home_team_variance: float | None,
                           away_team_variance: float | None,
                           home_event_factors: dict[str, float] | None = None,
                           away_event_factors: dict[str, float] | None = None) -> tuple[Any, ...]:
    from backend.app.services.plate_engine import simulate_game

    max_extra = MLB_MAX_EXTRA_INNINGS if league == "MLB" else KBO_MAX_EXTRA_INNINGS
    played = simulate_game(
        rng, simulations,
        {"tables": home_lineup, "staff": home_staff or {}, "expected_runs": home_expected,
         "event_factors": home_event_factors or {}},
        {"tables": away_lineup, "staff": away_staff or {}, "expected_runs": away_expected,
         "event_factors": away_event_factors or {}},
        league, {"max_extra": max_extra}, home_team_variance, away_team_variance)
    home, away = played["home"], played["away"]
    if league == "MLB":
        # Beyond the practical cap, decide the handful still tied by relative scoring strength.
        tied = np.flatnonzero(home == away)
        if tied.size:
            home_walkoff = rng.random(tied.size) < home_expected / max(home_expected + away_expected, 1e-9)
            home[tied] += home_walkoff
            away[tied] += ~home_walkoff
    usage = {side: _plate_usage(played["tier_counts"][side], (home_staff if side == "home" else away_staff) or {})
             for side in ("home", "away")}
    return (played["home_innings"], played["away_innings"], home, away, simulations, league, usage,
            "PLATE_APPEARANCE")


def _plate_usage(counts: dict[str, int], staff: dict[str, Any]) -> dict[str, Any]:
    innings = max(1, sum(counts.values()))
    bullpen = {**DEFAULT_BULLPEN, **(staff.get("bullpen") or {})}
    return {
        "starter_innings": round(float(staff.get("starter_innings") or LEAGUE_AVERAGE_STARTER_INNINGS), 1),
        "starter_share": round(counts.get("starter", 0) / innings, 4),
        **{f"{tier}_share": round(counts.get(tier, 0) / innings, 4) for tier in BULLPEN_TIERS},
        "multipliers": {tier: round(bullpen[tier], 3) for tier in BULLPEN_TIERS},
    }


def _run_histogram(runs: np.ndarray, simulations: int, cap: int = 15) -> list[float]:
    """Probability of scoring 0..cap runs, with the final bucket holding everything above."""
    counts = np.bincount(np.minimum(runs, cap), minlength=cap + 1)
    return [round(float(value) / simulations, 5) for value in counts[:cap + 1]]


def _outcome_scores(score_counts: Counter[tuple[int, int]], outcome_counts: Counter[str],
                    league: str) -> dict[str, Any]:
    """Most likely exact scores inside each outcome, rather than one mode over the whole game.

    The single most frequent score is a poor headline: with roughly forty plausible scores no
    candidate holds more than about 4% of the runs, so the top two are inside sampling noise and
    the winner flips with the seed. Worse, the joint mode follows whichever club has the tighter
    distribution, which can name a home-winning score in a game the away club is favoured to win.
    Conditioning on the outcome removes both problems: each branch is coherent with its own
    result and the branches differ from matchup to matchup.
    """
    branches = {
        "HOME_WIN": lambda h, a: h > a,
        "AWAY_WIN": lambda h, a: a > h,
        "TIE": lambda h, a: h == a,
    }
    scores: dict[str, Any] = {}
    for outcome, matches in branches.items():
        # A tied final score is not a possible MLB result, so the branch is absent rather than
        # empty - the card must never be able to render one from a stored payload.
        if outcome == "TIE" and league == "MLB":
            continue
        decided = outcome_counts.get(outcome, 0)
        if not decided:
            continue
        ranked = sorted(((count, home_runs, away_runs)
                         for (home_runs, away_runs), count in score_counts.items()
                         if matches(home_runs, away_runs)), reverse=True)[:3]
        scores[outcome] = [
            {"home": home_runs, "away": away_runs, "count": count,
             # Probability of this exact score given the outcome actually happens, which is what
             # "if the home club wins, it most likely ends 4-2" means.
             "probability_given_outcome": round(count / decided, 4)}
            for count, home_runs, away_runs in ranked
        ]
    return scores


def _counter_quantile(counts: Counter[int], probability: float) -> int:
    """Exact integer quantile over a frequency table without expanding it back into rows."""
    target = probability * sum(counts.values())
    cumulative = 0
    for value, count in sorted(counts.items()):
        cumulative += count
        if cumulative >= target:
            return int(value)
    return int(max(counts))


def _total_probabilities(total_counts: Counter[int], simulations: int, line: float) -> dict[str, float]:
    over = sum(count for total, count in total_counts.items() if total > line) / simulations
    under = sum(count for total, count in total_counts.items() if total < line) / simulations
    push = sum(count for total, count in total_counts.items() if total == line) / simulations
    return {"over": over, "under": under, "push": push}


def _fair_total_line(total_counts: Counter[int], simulations: int) -> float:
    """Return the supported half-run line closest to an even two-way total market."""
    lines = tuple(float(line) for line in TOTAL_MARKET_LINES if not float(line).is_integer())
    return min(lines, key=lambda line: abs(
        _total_probabilities(total_counts, simulations, line)["over"] -
        _total_probabilities(total_counts, simulations, line)["under"]
    ))


# Widest run line worth quoting: beyond this the two sides stop being a real two-way market.
FAIR_SPREAD_LINES = tuple(value + .5 for value in range(0, 7))


def _fair_home_spread(margin_counts: Counter[int], simulations: int) -> float:
    """The home run line our own distribution would post, in the book's sign convention.

    Negative means we would lay the runs on the home club. Recorded on every forecast, market or
    not, so the number the model arrived at on its own can later be compared against the number
    the market actually posted for the same game.
    """
    def imbalance(spread: float) -> float:
        home_cover = sum(count for margin, count in margin_counts.items()
                         if margin + spread > 0) / simulations
        away_cover = sum(count for margin, count in margin_counts.items()
                         if margin + spread < 0) / simulations
        return abs(home_cover - away_cover)

    candidates = [-line for line in FAIR_SPREAD_LINES] + list(FAIR_SPREAD_LINES)
    return min(candidates, key=lambda spread: (imbalance(spread), abs(spread)))


def _market_handicap(home_margins: np.ndarray, simulations: int,
                     home_spread: float | None) -> dict[str, Any] | None:
    """Price the book's actual run line from the unchanged simulation population."""
    if home_spread is None or not math.isfinite(float(home_spread)) or float(home_spread) == 0:
        return None
    spread = float(home_spread)
    adjusted_home_margin = home_margins + spread
    home_cover = float(np.mean(adjusted_home_margin > 0))
    away_cover = float(np.mean(adjusted_home_margin < 0))
    push = float(np.mean(adjusted_home_margin == 0))
    minus_home = spread < 0
    run_line = abs(spread)
    return {
        "home_spread": spread,
        "run_line": run_line,
        "minimum_margin": math.floor(run_line) + 1,
        "minus_side": "HOME" if minus_home else "AWAY",
        "minus_probability": home_cover if minus_home else away_cover,
        "plus_probability": away_cover if minus_home else home_cover,
        "push_probability": push,
    }


# Minimum simulations that must land inside the winning branch before it is priced. Below this
# the conditional percentages are sampling noise, so the card falls back to the full-population
# markets rather than showing a confident number built on a handful of runs.
MINIMUM_CONDITIONAL_BRANCH = 500


def _winner_conditional_market(score_counts: Counter[tuple[int, int]], simulations: int,
                               home_two_way: float, away_two_way: float,
                               headline_total_line: float | None,
                               headline_home_spread: float | None,
                               headline_spread_probability: float | None = None,
                               headline_total_over_probability: float | None = None) -> dict[str, Any] | None:
    """Price the run line and the total inside only the simulations the forecast winner wins.

    Stage one names a winner from the whole population. Reading the derived markets from that
    same whole population then answers a question nobody asked: an unconditional run line is
    dominated by the games the favourite loses, so it reports the plus side almost every time,
    and an unconditional total inherits the low-scoring losing branch, so it reports under
    almost every time. Both collapse to the same answer for every matchup on the slate.

    Conditioning on the winner actually winning is the second stage. Filtering the population
    that already exists is exact rejection sampling from that conditional distribution, so a
    second Monte Carlo run would add cost without adding information.

    Conditional percentages are not the chance of the bet landing: that is the joint
    probability, conditional x winner. Both are returned so the card can show the scenario
    number it is describing without ever implying the bet is more likely than it is.
    """
    home_favored = home_two_way >= away_two_way
    favorite_side = "HOME" if home_favored else "AWAY"
    branch: Counter[tuple[int, int]] = Counter({
        (home_runs, away_runs): count
        for (home_runs, away_runs), count in score_counts.items()
        if (home_runs > away_runs if home_favored else away_runs > home_runs)
    })
    branch_size = sum(branch.values())
    if branch_size < MINIMUM_CONDITIONAL_BRANCH:
        return None
    scenario_probability = branch_size / simulations
    total_counts: Counter[int] = Counter()
    # The favourite's own margin is positive by construction inside its winning branch.
    favorite_margin_counts: Counter[int] = Counter()
    home_counts: Counter[int] = Counter()
    away_counts: Counter[int] = Counter()
    for (home_runs, away_runs), count in branch.items():
        total_counts[home_runs + away_runs] += count
        favorite_margin_counts[
            home_runs - away_runs if home_favored else away_runs - home_runs
        ] += count
        home_counts[home_runs] += count
        away_counts[away_runs] += count

    def branch_share(counts: Counter[int], matches: Any) -> float:
        return sum(count for value, count in counts.items() if matches(value)) / branch_size

    def branch_mean(counts: Counter[int]) -> float:
        return sum(value * count for value, count in counts.items()) / branch_size

    # The line itself is the market's number and is never replaced by one of ours. A run line is
    # a single two-sided quote - home -1.5 is away +1.5 - so its magnitude prices both clubs and
    # stays the reference whichever club the book made favourite. The sign records which club the
    # book laid the runs on, and that is the side the market's own price refers to.
    market_spread = (float(headline_home_spread)
                     if headline_home_spread is not None and math.isfinite(float(headline_home_spread))
                     and float(headline_home_spread) != 0 else None)
    market_agrees = market_spread is not None and (market_spread < 0) == home_favored
    run_line = abs(market_spread) if market_spread is not None else 1.5
    run_line_source = "MARKET" if market_spread is not None else "MODEL_FALLBACK"
    minus_side = ("HOME" if market_spread < 0 else "AWAY") if market_spread is not None else favorite_side

    # How the forecast winner gets there, inside its own branch. This is the narrative the card
    # tells, not the decision: given a club wins a baseball game it clears a 1.5 line about 58%
    # to 78% of the time in every matchup, so a 50% bar on this number would name the same side
    # on every card in the slate. It is evidence, not a comparison.
    cover = branch_share(favorite_margin_counts, lambda margin: margin > run_line)
    short = branch_share(favorite_margin_counts, lambda margin: margin < run_line)
    branch_push = branch_share(favorite_margin_counts, lambda margin: margin == run_line)

    # The decision is made on the market's own event, over the whole population, because that is
    # what the book priced: does the club laying the runs clear the line, counting the games it
    # loses. Both sides are renormalised over the decidable outcomes so they meet the two-way
    # price the de-vig produced.
    def signed(margin: int) -> int:
        return margin if minus_side == "HOME" else -margin

    population_margins: Counter[int] = Counter()
    for (home_runs, away_runs), count in score_counts.items():
        population_margins[home_runs - away_runs] += count
    model_minus_raw = sum(count for margin, count in population_margins.items()
                          if signed(margin) > run_line) / simulations
    model_plus_raw = sum(count for margin, count in population_margins.items()
                         if signed(margin) < run_line) / simulations
    decidable = model_minus_raw + model_plus_raw
    model_minus = model_minus_raw / decidable if decidable else .5
    market_minus = (float(headline_spread_probability)
                    if headline_spread_probability is not None
                    and math.isfinite(float(headline_spread_probability))
                    and 0 < float(headline_spread_probability) < 1 else None)
    if market_minus is not None and minus_side == "AWAY":
        # The collected price is always the home club's chance of covering `home_spread`.
        market_minus = 1 - market_minus
    edge = model_minus - market_minus if market_minus is not None else None
    if edge is not None:
        pick_minus = edge > 0
        pick_basis = "EDGE_VS_MARKET"
    else:
        # No collected run-line price, so there is nothing to disagree with and no pick worth
        # calling one. The card reports the branch narrative instead and says the comparison is
        # unavailable; the recommendation race excludes it. The unconditional majority is not a
        # substitute here - on a 1.5 line it names the plus side in nearly every game, which is
        # what used to force every headline score to a one-run win.
        pick_minus = cover >= short
        pick_basis = "NO_MARKET_PRICE"
    handicap = {
        "run_line": run_line,
        "run_line_source": run_line_source,
        "market_home_spread": market_spread,
        # True when the book lays the runs on the same club the model made favourite.
        "market_agrees_with_model": market_agrees,
        "minus_side": minus_side,
        "plus_side": "AWAY" if minus_side == "HOME" else "HOME",
        "minimum_margin": math.floor(run_line) + 1,
        "model_minus_probability": round(model_minus, 4),
        "model_plus_probability": round(1 - model_minus, 4),
        "market_minus_probability": round(market_minus, 4) if market_minus is not None else None,
        "market_plus_probability": round(1 - market_minus, 4) if market_minus is not None else None,
        # Positive means we give the club laying the runs a better chance than the book does.
        "edge": round(edge, 4) if edge is not None else None,
        "pick": "MINUS" if pick_minus else "PLUS",
        # Priced against the market, the pick is quoted on the market's own unconditional event.
        # With no price the card is showing the branch narrative, so the number matches that.
        "pick_probability": round(
            (model_minus if pick_minus else 1 - model_minus) if edge is not None
            else (cover if pick_minus else short), 4),
        "pick_edge": round(edge if pick_minus else -edge, 4) if edge is not None else None,
        "pick_basis": pick_basis,
        # Only a market-priced comparison is a recommendation; everything else is information.
        "comparable": bool(edge is not None),
        # Branch narrative, kept beside the decision: how the forecast winner clears the line.
        "winner_side": favorite_side,
        "winner_cover_probability": round(cover, 4),
        "winner_short_probability": round(short, 4),
        "winner_push_probability": round(branch_push, 4),
        "joint_winner_cover_probability": round(cover * scenario_probability, 4),
    }

    market_line = (float(headline_total_line)
                   if headline_total_line is not None and math.isfinite(float(headline_total_line))
                   else None)
    line = market_line if market_line is not None else _fair_total_line(total_counts, branch_size)
    line_source = "MARKET" if market_line is not None else "MODEL_FAIR"
    # Branch narrative for the total, on the same footing as the run line above.
    probabilities = _total_probabilities(total_counts, branch_size, line)

    # The decision, again on the market's own unconditional event. A book sets the total so the
    # two sides are close to even and then states the rest in the price, so the number worth
    # answering is not "which side is over half" but "where do we disagree with what was quoted".
    population_totals: Counter[int] = Counter()
    for (home_runs, away_runs), count in score_counts.items():
        population_totals[home_runs + away_runs] += count
    population = _total_probabilities(population_totals, simulations, line)
    decidable_total = population["over"] + population["under"]
    model_over = population["over"] / decidable_total if decidable_total else .5
    market_over = (float(headline_total_over_probability)
                   if headline_total_over_probability is not None
                   and math.isfinite(float(headline_total_over_probability))
                   and 0 < float(headline_total_over_probability) < 1 else None)
    # A collected price belongs to the line it was quoted at, so it is only usable while the
    # line being priced here is that same market line.
    if market_over is not None and line_source != "MARKET":
        market_over = None
    total_edge = model_over - market_over if market_over is not None else None
    if total_edge is not None:
        pick_over = total_edge > 0
        total_pick_basis = "EDGE_VS_MARKET"
    else:
        # No collected price, so nothing to disagree with. Report the branch read instead and
        # let the card say the comparison is unavailable.
        pick_over = probabilities["over"] >= probabilities["under"]
        total_pick_basis = "NO_MARKET_PRICE"
    headline_total = {
        "line": line,
        "line_source": line_source,
        # Conditional on the forecast winner winning: how the total looks inside that branch.
        "over_probability": round(probabilities["over"], 4),
        "under_probability": round(probabilities["under"], 4),
        "push_probability": round(probabilities["push"], 4),
        # The market's own event, over the whole population, two-way.
        "model_over_probability": round(model_over, 4),
        "model_under_probability": round(1 - model_over, 4),
        "market_over_probability": round(market_over, 4) if market_over is not None else None,
        "market_under_probability": round(1 - market_over, 4) if market_over is not None else None,
        "edge": round(total_edge, 4) if total_edge is not None else None,
        "pick": "OVER" if pick_over else "UNDER",
        "pick_probability": round(
            (model_over if pick_over else 1 - model_over) if total_edge is not None
            else max(probabilities["over"], probabilities["under"]), 4),
        "pick_edge": round(total_edge if pick_over else -total_edge, 4) if total_edge is not None else None,
        "pick_basis": total_pick_basis,
        "comparable": bool(total_edge is not None),
        "joint_over_probability": round(probabilities["over"] * scenario_probability, 4),
        "joint_under_probability": round(probabilities["under"] * scenario_probability, 4),
        "joint_pick_probability": round(
            (probabilities["over"] if pick_over else probabilities["under"]) * scenario_probability, 4,
        ),
    }

    return {
        "winner": favorite_side,
        "winner_probability": round(home_two_way if home_favored else away_two_way, 4),
        # Share of all simulations in this branch. Below the two-way probability whenever the
        # league allows the game to end level, because ties belong to neither branch.
        "scenario_probability": round(scenario_probability, 4),
        "sample_size": branch_size,
        "population_size": simulations,
        "conditioning": "WINNER_WINS_OUTRIGHT",
        "mean_runs": {"home": round(branch_mean(home_counts), 3),
                      "away": round(branch_mean(away_counts), 3)},
        "median_runs": {"home": _counter_quantile(home_counts, .50),
                        "away": _counter_quantile(away_counts, .50)},
        "median_total": _counter_quantile(total_counts, .50),
        "median_margin": _counter_quantile(favorite_margin_counts, .50),
        "handicap": handicap,
        "headline_total": headline_total,
        "totals": {
            str(candidate): _total_probabilities(total_counts, branch_size, candidate)
            for candidate in TOTAL_MARKET_LINES
        },
        # The headline integer score is chosen inside this same branch under these decisions. It
        # follows the pick, so the score and the handicap on the card can never disagree - but
        # only while the pick constrains our winner's margin at all. When the book laid the runs
        # on the other club, its plus side is satisfied by any win, leaving the margin free.
        "favorite_run_line": run_line,
        "minimum_favorite_margin": math.floor(run_line) + 1,
        "favorite_cover_probability": round(cover, 4),
        "projects_favorite_cover": bool(pick_minus if minus_side == favorite_side else cover >= .5),
        # True when the displayed pick is what set the margin above. False only when the book laid
        # the runs on the other club, where its plus side is satisfied by any win and leaves our
        # winner's margin free; the branch's own cover majority sets it instead.
        "headline_follows_pick": bool(minus_side == favorite_side),
        "margin_probabilities": {
            str(margin): round(count / branch_size, 4)
            for margin, count in sorted(favorite_margin_counts.items())
        },
        "top_scores": [
            {"home": home_runs, "away": away_runs, "count": count,
             "probability_given_winner": round(count / branch_size, 4)}
            for (home_runs, away_runs), count in branch.most_common(5)
        ],
    }

def _coherent_scenario_score_projection(
    score_counts: Counter[tuple[int, int]], total_counts: Counter[int], margin_counts: Counter[int],
    simulations: int, league: str, home_mean: float, away_mean: float,
    home_two_way: float, away_two_way: float, handicap: dict[str, float],
    headline_total_line: float | None = None,
    headline_home_spread: float | None = None,
    conditional: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Select a representative score from the forecast winner's winning branch.

    The unconditional joint mode is biased toward low, one-run scores.  A headline score already
    commits to one winner, so its relevant population is the simulations in which the forecast
    winner actually wins, and component medians of that branch are the Bayes action for absolute
    error inside it. The selected total direction may narrow that winning branch, but a run-line
    never does: handicap output is not part of the product and must not reshape the score shown
    to the reader. A market line never changes a simulation; it can only select a representative
    from outcomes that were already generated.
    """
    home_favored = home_two_way >= away_two_way
    favorite_win_probability = home_two_way if home_favored else away_two_way
    market_spread = (
        float(headline_home_spread)
        if headline_home_spread is not None and math.isfinite(float(headline_home_spread))
        and float(headline_home_spread) != 0
        else None
    )
    market_minus_home = market_spread is not None and market_spread < 0
    market_matches_model_favorite = market_spread is not None and market_minus_home == home_favored
    favorite_run_line = float(conditional["favorite_run_line"]) if conditional else (
        abs(market_spread) if market_matches_model_favorite else 1.5
    )
    minimum_favorite_margin = math.floor(favorite_run_line) + 1
    favorite_cover_probability = sum(
        count for margin, count in margin_counts.items()
        if (margin if home_favored else -margin) > favorite_run_line
    ) / simulations
    opponent_plus_probability = sum(
        count for margin, count in margin_counts.items()
        if (margin if home_favored else -margin) < favorite_run_line
    ) / simulations
    favorite_cover_given_win = float(conditional["favorite_cover_probability"]) if conditional else min(
        1.0, favorite_cover_probability / max(favorite_win_probability, 1e-9)
    )
    unconditional_cover_majority = favorite_cover_probability > opponent_plus_probability
    # Second stage: inside the branch the favourite wins, a plain majority is the decision. The
    # older full-population rule needed a .72 conditional bar because it was competing with an
    # unconditional majority that the losing branch had already decided; that bar exists only on
    # the fallback path now.
    project_cover = bool(conditional["projects_favorite_cover"]) if conditional else (
        unconditional_cover_majority
        or favorite_cover_given_win >= HEADLINE_CONDITIONAL_COVER_THRESHOLD
    )
    run_line_conditioning = (
        "WINNER_CONDITIONAL_COVER_MAJORITY" if conditional else
        "UNCONDITIONAL_COVER_MAJORITY" if unconditional_cover_majority else
        "WINNER_CONDITIONAL_COVER_SIGNAL" if project_cover else
        "RUN_LINE_CONSERVATIVE"
    )
    line = float(conditional["headline_total"]["line"]) if conditional else (
        float(headline_total_line) if headline_total_line is not None and math.isfinite(
            float(headline_total_line)
        ) else _fair_total_line(total_counts, simulations)
    )
    total_probabilities = _total_probabilities(total_counts, simulations, line)
    if conditional:
        scenario_total = conditional["headline_total"]
        total_pick = scenario_total["pick"]
        scenario_over = float(scenario_total["over_probability"])
        scenario_under = float(scenario_total["under_probability"])
        # Named for what actually decided the direction, so a stored score can be audited
        # against the rule that produced it.
        total_conditioning = ("MARKET_EDGE" if scenario_total["pick_basis"] == "EDGE_VS_MARKET"
                              else "WINNER_CONDITIONAL")
    else:
        decidable_total_probability = total_probabilities["over"] + total_probabilities["under"]
        over_given_decision = total_probabilities["over"] / max(decidable_total_probability, 1e-9)
        total_pick = "OVER" if over_given_decision >= .50 else "UNDER"
        scenario_over, scenario_under = total_probabilities["over"], total_probabilities["under"]
        total_conditioning = "FULL_POPULATION"

    def favorite_margin(home_runs: int, away_runs: int) -> int:
        return home_runs - away_runs if home_favored else away_runs - home_runs

    winner_rows = [
        (home_runs, away_runs, count)
        for (home_runs, away_runs), count in score_counts.items()
        if favorite_margin(home_runs, away_runs) >= 1
        and not (league == "MLB" and home_runs == away_runs)
    ]
    # The score scenario is conditional only on the predicted winner winning. Neither handicap
    # nor total market directions may filter it: markets are priced against this distribution,
    # not permitted to reshape a score selected from it.
    eligible = winner_rows
    if not eligible:
        eligible = [(home_runs, away_runs, count) for (home_runs, away_runs), count in score_counts.items()
                    if not (league == "MLB" and home_runs == away_runs)]

    branch_count = sum(row[2] for row in eligible)
    if conditional:
        # Aim at the branch the candidates are drawn from. Full-population medians describe a
        # population that includes every game the forecast winner loses, so for a mild favourite
        # they point at a level or one-run score the branch cannot even contain, and the selector
        # then settles on whichever admissible score sits nearest that unreachable target.
        target_home = int(conditional["median_runs"]["home"])
        target_away = int(conditional["median_runs"]["away"])
        target_total = int(conditional["median_total"])
        target_favorite_margin = int(conditional["median_margin"])
        mean_home = float(conditional["mean_runs"]["home"])
        mean_away = float(conditional["mean_runs"]["away"])
        target_population = "WINNER_BRANCH"
    else:
        home_branch: Counter[int] = Counter()
        away_branch: Counter[int] = Counter()
        for (home_runs, away_runs), count in score_counts.items():
            home_branch[home_runs] += count
            away_branch[away_runs] += count
        favorite_margin_branch = Counter({
            (margin if home_favored else -margin): count for margin, count in margin_counts.items()
        })
        target_home = _counter_quantile(home_branch, .50)
        target_away = _counter_quantile(away_branch, .50)
        target_total = _counter_quantile(total_counts, .50)
        target_favorite_margin = _counter_quantile(favorite_margin_branch, .50)
        mean_home, mean_away = home_mean, away_mean
        target_population = "FULL_POPULATION"
    maximum_count = max(row[2] for row in eligible) or 1

    def fit(row: tuple[int, int, int]) -> tuple[float, int, float]:
        home_runs, away_runs, count = row
        total = home_runs + away_runs
        margin = favorite_margin(home_runs, away_runs)
        frequency_fit = count / maximum_count
        team_fit = 1 / (1 + (abs(home_runs - target_home) + abs(away_runs - target_away)) / 2)
        mean_fit = 1 / (1 + (
            abs(home_runs - mean_home) + abs(away_runs - mean_away)
        ) / 2)
        total_fit = 1 / (1 + abs(total - target_total))
        margin_fit = 1 / (1 + abs(margin - target_favorite_margin))
        selection_score = (
            .10 * frequency_fit + .30 * team_fit + .20 * mean_fit +
            .20 * total_fit + .20 * margin_fit
        )
        return selection_score, count, -abs(total - target_total)

    ranked = sorted(eligible, key=fit, reverse=True)
    candidates: list[dict[str, Any]] = []
    # Keep several credible alternatives from the full support. A soft diversity rule avoids
    # returning the same total/margin shape three times while never admitting a weak tail score.
    used_shapes: set[tuple[int, int]] = set()
    for row in ranked:
        home_runs, away_runs, count = row
        shape = (min(3, abs(home_runs - away_runs)), (home_runs + away_runs) // 2)
        if candidates and shape in used_shapes and len(candidates) < 6:
            continue
        candidates.append({
            "rank": len(candidates) + 1,
            "home": home_runs,
            "away": away_runs,
            "count": count,
            "probability": round(count / simulations, 4),
            "selection_method": "COHERENT_BAYES_MEDIAN_V3",
            "selection_score": round(fit(row)[0], 6),
        })
        used_shapes.add(shape)
        if len(candidates) >= 8:
            break
    primary = dict(candidates[0])
    primary.update({
        "target_home_median": target_home,
        "target_away_median": target_away,
        "target_total_median": target_total,
        "target_favorite_margin_median": target_favorite_margin,
        "target_population": target_population,
        "favorite_cover_probability": round(favorite_cover_probability, 4),
        "favorite_cover_probability_given_win": round(favorite_cover_given_win, 4),
        "favorite_run_line": favorite_run_line,
        "minimum_favorite_margin": minimum_favorite_margin,
        "run_line_source": "MARKET" if market_matches_model_favorite else "MODEL_FALLBACK",
        "projects_favorite_cover": project_cover,
        "run_line_conditioning": run_line_conditioning,
        "headline_total_line": line,
        "headline_total_pick": total_pick,
        "total_conditioning": total_conditioning,
        # Full-population reference numbers, kept so the conditional read can be audited against
        # the distribution it was drawn from.
        "headline_over_probability": round(total_probabilities["over"], 4),
        "headline_under_probability": round(total_probabilities["under"], 4),
        "headline_push_probability": round(total_probabilities["push"], 4),
        # The same two numbers inside the winning branch, which is what the pick was made on.
        "scenario_over_probability": round(scenario_over, 4),
        "scenario_under_probability": round(scenario_under, 4),
        "scenario_probability": round(branch_count / simulations, 4),
        "scenario_conditioning": "FAVORITE_WIN+HEADLINE_TOTAL",
        "population_coverage": 1.0,
    })
    return primary, candidates


def _summarize(home_innings: np.ndarray, away_innings: np.ndarray, home: np.ndarray, away: np.ndarray,
               simulations: int, league: str, bullpen_usage: dict[str, Any], engine: str,
               observed_result: dict[str, Any] | None = None,
               headline_total_line: float | None = None,
               headline_home_spread: float | None = None,
               headline_spread_probability: float | None = None,
               headline_total_over_probability: float | None = None,
               probability_calibration: dict[str, Any] | None = None) -> dict[str, Any]:
    if len(home) != simulations or len(away) != simulations:
        raise ValueError("simulation population size does not match its recipe")
    raw_distribution = _compact_distribution(home, away)
    home_innings, away_innings, home, away, calibration_summary = _reweight_win_branches(
        home_innings, away_innings, home, away, probability_calibration,
    )
    calibration_summary["raw_distribution"] = raw_distribution
    calibration_summary["calibrated_distribution"] = _compact_distribution(home, away)
    total = home + away
    home_win_probability = float(np.mean(home > away))
    away_win_probability = float(np.mean(away > home))
    tie_probability = float(np.mean(home == away))
    decided_probability = home_win_probability + away_win_probability
    # KBO ties that survive inning 11 are excluded from the two-way market instead of being
    # arbitrarily split. For MLB every game is decided, so two-way equals the raw probabilities.
    home_two_way = home_win_probability / decided_probability if decided_probability else .5
    away_two_way = away_win_probability / decided_probability if decided_probability else .5

    def trajectory_of(index: int) -> tuple[tuple[int, int], ...]:
        # A half-inning that was never played is stored as -1 and simply ends the scorebook line.
        pairs = []
        for away_runs, home_runs in zip(away_innings[index].tolist(), home_innings[index].tolist(), strict=True):
            if away_runs < 0 and home_runs < 0:
                break
            pairs.append((max(away_runs, 0), max(home_runs, 0)))
        return tuple(pairs)

    score_counts = Counter(zip(home.tolist(), away.tolist(), strict=False))
    def with_trajectory(payload: dict[str, Any]) -> dict[str, Any]:
        h, a, count = int(payload["home"]), int(payload["away"]), int(payload["count"])
        indices = np.flatnonzero((home == h) & (away == a))
        trajectory_counts = Counter(trajectory_of(index) for index in indices)
        trajectory, trajectory_count = trajectory_counts.most_common(1)[0]
        away_cumulative = home_cumulative = 0
        inning_line = []
        for inning, (away_runs, home_runs) in enumerate(trajectory, 1):
            away_cumulative += away_runs
            home_cumulative += home_runs
            inning_line.append({
                "inning": inning, "away": away_runs, "home": home_runs,
                "away_cumulative": away_cumulative, "home_cumulative": home_cumulative,
            })
        return {
            **payload,
            "inning_line": inning_line,
            "trajectory_count": trajectory_count,
            "trajectory_probability_given_score": round(trajectory_count / count, 4),
        }

    top_scores = [
        with_trajectory({
            "rank": rank, "home": h, "away": a, "count": count,
            "probability": round(count / simulations, 4),
        })
        for rank, ((h, a), count) in enumerate(score_counts.most_common(16), 1)
    ]
    full_distribution_score = dict(top_scores[0])
    outcome_counts = Counter({
        "HOME_WIN": int(np.count_nonzero(home > away)),
        "AWAY_WIN": int(np.count_nonzero(away > home)),
        "TIE": int(np.count_nonzero(home == away)),
    })
    outcome_scores = _outcome_scores(score_counts, outcome_counts, league)
    total_counts = Counter(total.tolist())
    margin_counts = Counter((home - away).tolist())
    simulation_modes = {
        "home_runs": _mode_payload(Counter(home.tolist()), simulations),
        "away_runs": _mode_payload(Counter(away.tolist()), simulations),
        "total_runs": _mode_payload(total_counts, simulations),
        "run_margin": _mode_payload(margin_counts, simulations),
        "outcome": _mode_payload(outcome_counts, simulations),
    }
    handicap = {
        "home_minus_1_5": float(np.mean(home - away >= 2)),
        "away_minus_1_5": float(np.mean(away - home >= 2)),
        "home_plus_1_5": float(np.mean(home - away >= -1)),
        "away_plus_1_5": float(np.mean(away - home >= -1)),
    }
    market_handicap = _market_handicap(home - away, simulations, headline_home_spread)
    # The reference points the model reaches on its own, independent of anything the market
    # posted. Stored on every forecast so the two can be compared once the game is final.
    model_fair_lines = {
        "total_line": _fair_total_line(total_counts, simulations),
        "home_spread": _fair_home_spread(margin_counts, simulations),
        "market_total_line": float(headline_total_line) if headline_total_line is not None
        and math.isfinite(float(headline_total_line)) else None,
        "market_home_spread": float(headline_home_spread) if headline_home_spread is not None
        and math.isfinite(float(headline_home_spread)) and float(headline_home_spread) != 0 else None,
        "market_total_over_probability": (
            float(headline_total_over_probability)
            if headline_total_over_probability is not None
            and math.isfinite(float(headline_total_over_probability)) else None),
        "market_home_spread_probability": (
            float(headline_spread_probability) if headline_spread_probability is not None
            and math.isfinite(float(headline_spread_probability)) else None),
    }
    # Stage two. Every derived market on the card is priced here, inside the branch where the
    # club stage one named actually wins, and the headline score is selected under the same
    # decisions so the score and the picks can never contradict each other.
    winner_conditional_market = _winner_conditional_market(
        score_counts, simulations, home_two_way, away_two_way,
        headline_total_line, headline_home_spread, headline_spread_probability,
        headline_total_over_probability,
    )
    projected_score, projected_score_candidates = _coherent_scenario_score_projection(
        score_counts, total_counts, margin_counts, simulations, league,
        float(home.mean()), float(away.mean()), home_two_way, away_two_way, handicap,
        headline_total_line, headline_home_spread, winner_conditional_market,
    )
    projected_score = with_trajectory(projected_score)
    projected_score_candidates = [
        with_trajectory(score) if index < 3 else score
        for index, score in enumerate(projected_score_candidates)
    ]
    totals = {
        str(line): {
            "over": float(np.mean(total > line)),
            "under": float(np.mean(total < line)),
            "push": float(np.mean(total == line)),
        }
        for line in TOTAL_MARKET_LINES
    }
    regulation = slice(0, 9)
    extras_played = (away_innings[:, 9:] >= 0).any(axis=1) if away_innings.shape[1] > 9 else np.zeros(1, dtype=bool)
    home_favored = home_two_way >= away_two_way
    home_through_five = np.maximum(home_innings[:, :5], 0).sum(axis=1)
    away_through_five = np.maximum(away_innings[:, :5], 0).sum(axis=1)
    favorite_runs = home if home_favored else away
    favorite_led_after_five = home_through_five > away_through_five if home_favored else away_through_five > home_through_five
    underdog_led_after_five = away_through_five > home_through_five if home_favored else home_through_five > away_through_five
    favorite_lost = away > home if home_favored else home > away
    underdog_won = favorite_lost
    result = {
        "engine": engine,
        # A club's season run rate counts extra-inning runs, so the full-game mean is what the
        # engines calibrate against; the regulation mean is kept alongside it as a diagnostic.
        "mean_runs": {"home": round(float(home.mean()), 3), "away": round(float(away.mean()), 3)},
        "regulation_mean_runs": {
            "home": round(float(np.maximum(home_innings[:, regulation], 0).sum(axis=1).mean()), 3),
            "away": round(float(np.maximum(away_innings[:, regulation], 0).sum(axis=1).mean()), 3),
        },
        "bullpen_usage": bullpen_usage,
        "extra_innings": {
            "rule": "MLB_GHOST_RUNNER_UNTIL_DECIDED" if league == "MLB" else "KBO_MAX_11_TIES_STAND",
            "probability": float(extras_played.mean()) if away_innings.shape[1] > 9 else 0.0,
        },
        "home_win_probability": home_win_probability,
        "away_win_probability": away_win_probability,
        "home_two_way_probability": home_two_way,
        "away_two_way_probability": away_two_way,
        "tie_probability": tie_probability,
        "handicap": handicap,
        "market_handicap": market_handicap,
        "model_fair_lines": model_fair_lines,
        "winner_conditional_market": winner_conditional_market,
        # Preserve the two legacy keys for callers that have not migrated to the handicap object.
        "home_minus_1_5": handicap["home_minus_1_5"],
        "away_plus_1_5": handicap["away_plus_1_5"],
        "totals": totals,
        "top_scores": top_scores,
        "full_distribution_score": full_distribution_score,
        "projected_score": projected_score,
        "winner_conditional_score": projected_score,
        "projected_score_candidates": projected_score_candidates,
        "outcome_scores": outcome_scores,
        "probability_calibration": calibration_summary,
        # Per-club run histograms, so the shape can be compared against real results.
        "team_run_distribution": {
            "home": _run_histogram(home, simulations), "away": _run_histogram(away, simulations),
        },
        # Compact full-population tables make every final-score comparison exact even when the
        # observed score was not one of the 16 most common candidates shown on the card.
        "frequency_tables": {
            "scores": {f"{a}:{h}": count for (h, a), count in score_counts.items()},
            "totals": {str(value): count for value, count in total_counts.items()},
            "margins": {str(value): count for value, count in margin_counts.items()},
            "outcomes": dict(outcome_counts),
        },
        "simulation_modes": simulation_modes,
        "total_quantiles": {
            "p10": float(np.quantile(total, .10)), "p50": float(np.quantile(total, .50)), "p90": float(np.quantile(total, .90)),
        },
        "team_dense_intervals": {
            "away": highest_density_interval(away), "home": highest_density_interval(home),
        },
        "total_dense_interval": highest_density_interval(total),
        "team_quantiles": {
            "away": {"p10": float(np.quantile(away, .10)), "p50": float(np.quantile(away, .50)), "p90": float(np.quantile(away, .90))},
            "home": {"p10": float(np.quantile(home, .10)), "p50": float(np.quantile(home, .50)), "p90": float(np.quantile(home, .90))},
        },
        "game_shape": {
            "one_run_probability": float(np.mean(np.abs(home - away) <= 1)),
            "blowout_probability": float(np.mean(np.abs(home - away) >= 5)),
            "either_shutout_probability": float(np.mean((home == 0) | (away == 0))),
            "favorite_side": "HOME" if home_favored else "AWAY",
            "favorite_scores_0_to_2_and_loses": float(np.mean((favorite_runs <= 2) & favorite_lost)),
            "favorite_leads_after_five_then_loses": float(np.mean(favorite_led_after_five & favorite_lost)),
            "underdog_leads_after_five_and_wins": float(np.mean(underdog_led_after_five & underdog_won)),
        },
    }
    if observed_result is not None:
        result["observed_evaluation"] = _observed_evaluation(
            observed_result, simulations, home, away, home_innings, away_innings,
            score_counts, total_counts, margin_counts, outcome_counts, trajectory_of,
        )
    validate_simulation_summary(result)
    return result


def _observed_evaluation(observed: dict[str, Any], simulations: int, home: np.ndarray, away: np.ndarray,
                         home_innings: np.ndarray, away_innings: np.ndarray,
                         score_counts: Counter[Any], total_counts: Counter[Any], margin_counts: Counter[Any],
                         outcome_counts: Counter[Any], trajectory_of: Any) -> dict[str, Any]:
    actual_away, actual_home = int(observed["away_score"]), int(observed["home_score"])
    outcome = "HOME_WIN" if actual_home > actual_away else ("AWAY_WIN" if actual_away > actual_home else "TIE")
    score_count = int(score_counts[(actual_home, actual_away)])
    total_count = int(total_counts[actual_home + actual_away])
    margin_count = int(margin_counts[actual_home - actual_away])
    outcome_count = int(outcome_counts[outcome])
    inning_count: int | None = None
    innings = observed.get("innings")
    if isinstance(innings, dict) and isinstance(innings.get("away"), list) and isinstance(innings.get("home"), list):
        size = max(len(innings["away"]), len(innings["home"]))
        actual_path = tuple(
            (int(innings["away"][index] or 0) if index < len(innings["away"]) else 0,
             int(innings["home"][index] or 0) if index < len(innings["home"]) else 0)
            for index in range(size)
        )
        # Only simulations with the same number of played innings can be an exact flow match.
        inning_count = sum(trajectory_of(index) == actual_path for index in range(simulations))
    return {
        "simulation_count": simulations,
        "actual_score": {"away": actual_away, "home": actual_home},
        "actual_score_count": score_count,
        "actual_score_probability": round(score_count / simulations, 6),
        "actual_outcome": outcome,
        "actual_outcome_count": outcome_count,
        "actual_outcome_probability": round(outcome_count / simulations, 6),
        "actual_total": actual_home + actual_away,
        "actual_total_count": total_count,
        "actual_total_probability": round(total_count / simulations, 6),
        "actual_margin": actual_home - actual_away,
        "actual_margin_count": margin_count,
        "actual_margin_probability": round(margin_count / simulations, 6),
        "actual_inning_path_count": inning_count,
        "actual_inning_path_probability": round(inning_count / simulations, 6) if inning_count is not None else None,
        "inning_data_available": inning_count is not None,
    }


def _staff_profile(staff: dict[str, Any] | None, rng: np.random.Generator, simulations: int) -> dict[str, Any]:
    """Resolve one club's pitching plan: starter workload plus per-tier relief multipliers."""
    staff = staff or {}
    bullpen = {**DEFAULT_BULLPEN, **(staff.get("bullpen") or {})}
    starter_multiplier = float(staff.get("starter_multiplier", 1.0))
    starter_innings = float(staff.get("starter_innings") or LEAGUE_AVERAGE_STARTER_INNINGS)
    # The multipliers describe how runs are distributed across innings, not how many are
    # scored: dividing by the whole-game average keeps each club's expected total intact.
    relief_blend = sum(BULLPEN_USAGE_MIX[tier] * bullpen[tier] for tier in BULLPEN_USAGE_MIX)
    starter_share = min(1.0, max(.2, starter_innings / 9))
    normalizer = starter_share * starter_multiplier + (1 - starter_share) * relief_blend
    exit_inning = np.clip(np.rint(rng.normal(starter_innings, STARTER_EXIT_SPREAD, simulations)), 2, 9)
    return {
        "starter_multiplier": starter_multiplier, "starter_innings": starter_innings,
        "bullpen": bullpen, "normalizer": max(.3, normalizer), "exit_inning": exit_inning.astype(np.int64),
    }


def _play_extras(rng: np.random.Generator, home: np.ndarray, away: np.ndarray,
                 home_extra_rate: np.ndarray, away_extra_rate: np.ndarray, ghost_bonus: float,
                 max_extra_innings: int, league: str, home_ratio: float, away_ratio: float,
                 simulations: int,
                 collect_columns: bool = True) -> tuple[np.ndarray, np.ndarray,
                                                        list[np.ndarray], list[np.ndarray]]:
    """Play the tiebreaker on a regulation population, returning the runs each club added.

    `home` and `away` are updated in place when columns are collected, which is how the real
    pass uses it; the calibration pass works on copies because it only needs the totals.
    """
    if not collect_columns:
        home, away = home.copy(), away.copy()
    start_home, start_away = home.copy(), away.copy()
    home_columns: list[np.ndarray] = []
    away_columns: list[np.ndarray] = []
    tied = home == away
    for _ in range(max_extra_innings):
        if not tied.any():
            break
        indices = np.flatnonzero(tied)
        away_inning_runs = _draw_runs(rng, away_extra_rate[indices] + ghost_bonus, away_ratio)
        home_inning_runs = _draw_runs(rng, home_extra_rate[indices] + ghost_bonus, home_ratio)
        # The home half ends the moment the winning run scores (walk-off), capping the margin.
        home_inning_runs = np.minimum(home_inning_runs, away_inning_runs + 1)
        if collect_columns:
            away_column = np.full(simulations, -1, dtype=np.int64)
            home_column = np.full(simulations, -1, dtype=np.int64)
            away_column[indices] = away_inning_runs
            home_column[indices] = home_inning_runs
            away_columns.append(away_column)
            home_columns.append(home_column)
        away[indices] += away_inning_runs
        home[indices] += home_inning_runs
        tied = home == away

    if league == "MLB" and tied.any():
        # Beyond the practical cap, decide by relative extra-inning scoring rates.
        indices = np.flatnonzero(tied)
        home_strength = home_extra_rate[indices] + ghost_bonus
        away_strength = away_extra_rate[indices] + ghost_bonus
        home_walkoff = rng.random(indices.size) < home_strength / (home_strength + away_strength)
        away_added = np.where(home_walkoff, 0, 1)
        home_added = np.where(home_walkoff, 1, 0)
        if collect_columns:
            away_column = np.full(simulations, -1, dtype=np.int64)
            home_column = np.full(simulations, -1, dtype=np.int64)
            away_column[indices] = away_added
            home_column[indices] = home_added
            away_columns.append(away_column)
            home_columns.append(home_column)
        away[indices] += away_added
        home[indices] += home_added
    return home - start_home, away - start_away, home_columns, away_columns


def _rate_correction(target: float, calibration_line: np.ndarray) -> float:
    """Scale a club's rate so the ninth-inning rules do not silently lower its expected runs.

    A club's season run rate already counts the home ninths it never batted, so the simulation
    has to bat harder in the innings it does play to reproduce that rate.
    """
    realized = float(calibration_line.sum(axis=1).mean())
    if realized <= 0:
        return 1.0
    return float(np.clip(target / realized, .8, 1.35))


def _apply_realized_normalizer(profile: dict[str, Any], counts: Counter[str]) -> None:
    """Replace the assumed usage mix with the tier shares the leverage rules actually produced."""
    innings = sum(counts.values())
    if not innings:
        return
    weighted = counts["starter"] * profile["starter_multiplier"] + sum(
        counts[tier] * profile["bullpen"][tier] for tier in BULLPEN_TIERS)
    profile["normalizer"] = max(.3, weighted / innings)


def _normalized_tier(profile: dict[str, Any], tier: str) -> float:
    return profile["bullpen"][tier] / profile["normalizer"]


def _relief_tier(inning: int, lead: np.ndarray) -> dict[str, np.ndarray]:
    """Which relief group warms up, judged from the pitching club's own scoreboard position."""
    late = inning >= LATE_INNING_INDEX
    decided = np.abs(lead) >= BLOWOUT_MARGIN
    high_leverage = late & ~decided & (lead >= HIGH_LEVERAGE_LEAD[0]) & (lead <= HIGH_LEVERAGE_LEAD[1])
    chase = late & ~decided & (lead >= CHASE_LEAD[0]) & (lead <= CHASE_LEAD[1])
    return {
        "mop_up": decided,
        "high_leverage": high_leverage,
        "chase": chase,
        "middle": ~decided & ~high_leverage & ~chase,
    }


def _inning_multiplier(profile: dict[str, Any], inning: int, lead: np.ndarray,
                       calibrating: bool = False) -> tuple[np.ndarray, dict[str, int]]:
    """Pick starter or relief tier per simulation, then scale the inning's run rate."""
    bullpen = profile["bullpen"]
    tiers = _relief_tier(inning, lead)
    relief = np.full(lead.size, bullpen["middle"], dtype=float)
    for tier in ("chase", "mop_up", "high_leverage"):
        relief[tiers[tier]] = bullpen[tier]
    starter_in = inning < profile["exit_inning"]
    multiplier = np.ones(lead.size) if calibrating else (
        np.where(starter_in, profile["starter_multiplier"], relief) / profile["normalizer"])
    counts = {"starter": int(np.count_nonzero(starter_in))}
    counts.update({tier: int(np.count_nonzero(~starter_in & mask)) for tier, mask in tiers.items()})
    return multiplier, counts


def _bullpen_usage(profile: dict[str, Any], counts: Counter[str]) -> dict[str, Any]:
    innings = max(1, counts["starter"] + sum(counts[tier] for tier in BULLPEN_TIERS))
    usage = {f"{tier}_share": round(counts[tier] / innings, 4) for tier in BULLPEN_TIERS}
    return {
        "starter_innings": round(profile["starter_innings"], 1),
        "starter_share": round(counts["starter"] / innings, 4),
        **usage,
        "multipliers": {tier: round(profile["bullpen"][tier], 3) for tier in BULLPEN_TIERS},
    }


def _mode_payload(counts: Counter[Any], simulations: int) -> dict[str, Any]:
    value, count = counts.most_common(1)[0]
    return {"value": value, "count": count, "probability": round(count / simulations, 4)}
