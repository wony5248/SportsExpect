from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np


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


def simulate_scores(home_expected: float, away_expected: float, simulations: int, seed: int,
                    environment_variance: float = .08, team_variance: float = .12,
                    league: str = "MLB", home_staff: dict[str, Any] | None = None,
                    away_staff: dict[str, Any] | None = None, home_lineup: np.ndarray | None = None,
                    away_lineup: np.ndarray | None = None) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    if home_lineup is not None and away_lineup is not None:
        # Both lineups have collected splits, so play the game out plate appearance by plate
        # appearance instead of drawing inning run totals.
        return _summarize(*_plate_appearance_game(
            rng, home_expected, away_expected, simulations, league, home_staff, away_staff,
            home_lineup, away_lineup))
    # A shared gamma run environment creates realistic over-dispersion and correlation
    # (weather/umpire/park conditions affect both clubs) while preserving expected means.
    variance = min(.18, max(.02, environment_variance))
    shape = 1 / variance
    shared_environment = rng.gamma(shape, variance, simulations)
    # Baseball scoring is more dispersed than a Poisson process even after shared weather/park effects.
    # Independent gamma shocks represent sequencing, defense and bullpen execution specific to each club.
    independent_variance = min(.24, max(.04, team_variance))
    independent_shape = 1 / independent_variance
    home_environment = rng.gamma(independent_shape, independent_variance, simulations)
    away_environment = rng.gamma(independent_shape, independent_variance, simulations)
    # Split each expected total across nine innings. Slightly higher late-inning weights reflect
    # starter fatigue and bullpen exposure while preserving each team's full-game expectation.
    away_weights = np.array([.105, .105, .105, .108, .110, .112, .115, .118, .122])
    home_weights = np.array([.108, .106, .105, .108, .110, .112, .114, .117, .120])
    away_weights /= away_weights.sum()
    home_weights /= home_weights.sum()
    # Who is on the mound depends on the score, so innings are drawn one at a time. The away
    # staff suppresses home scoring and vice versa.
    home_rate = home_expected * shared_environment * home_environment
    away_rate = away_expected * shared_environment * away_environment
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
            away_runs = rng.poisson(away_rate * away_scale * away_weights[inning] * home_multiplier)
            away_line[:, inning] = away_runs
            away_total += away_runs
            # Bottom half, with the top half already on the board.
            away_multiplier, away_used = _inning_multiplier(
                away_staff_profile, inning, away_total - home_total, calibrating)
            away_tiers.update(away_used)
            home_runs = rng.poisson(home_rate * home_scale * home_weights[inning] * away_multiplier)
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
    home_scale = _rate_correction(home_expected, calibration_home_line)
    away_scale = _rate_correction(away_expected, calibration_away_line)
    home_innings, away_innings, home_tier_counts, away_tier_counts = play_nine(False, home_scale, away_scale)
    home = home_innings.sum(axis=1)
    away = away_innings.sum(axis=1)
    # League-accurate extra innings. MLB plays the automatic-runner tiebreaker until every game
    # is decided; KBO plays innings 10-11 without a tiebreaker and lets remaining ties stand.
    # Extra innings are the definition of high leverage: both managers are down to their best
    # available arms, so the high-leverage multiplier applies for the rest of the game.
    home_extra_rate = home_rate * EXTRA_INNING_WEIGHT * _normalized_tier(away_staff_profile, "high_leverage")
    away_extra_rate = away_rate * EXTRA_INNING_WEIGHT * _normalized_tier(home_staff_profile, "high_leverage")
    ghost_bonus = MLB_GHOST_RUNNER_BONUS if league == "MLB" else 0.0
    max_extra_innings = MLB_MAX_EXTRA_INNINGS if league == "MLB" else KBO_MAX_EXTRA_INNINGS
    extra_away_columns: list[np.ndarray] = []
    extra_home_columns: list[np.ndarray] = []
    tied = home == away
    for _ in range(max_extra_innings):
        if not tied.any():
            break
        indices = np.flatnonzero(tied)
        away_inning_runs = rng.poisson(away_extra_rate[indices] + ghost_bonus)
        home_inning_runs = rng.poisson(home_extra_rate[indices] + ghost_bonus)
        # The home half ends the moment the winning run scores (walk-off), capping the margin.
        home_inning_runs = np.minimum(home_inning_runs, away_inning_runs + 1)
        away_column = np.full(simulations, -1, dtype=np.int64)
        home_column = np.full(simulations, -1, dtype=np.int64)
        away_column[indices] = away_inning_runs
        home_column[indices] = home_inning_runs
        extra_away_columns.append(away_column)
        extra_home_columns.append(home_column)
        away[indices] += away_inning_runs
        home[indices] += home_inning_runs
        tied = home == away

    if league == "MLB" and tied.any():
        # Beyond the practical cap, decide by relative extra-inning scoring rates.
        indices = np.flatnonzero(tied)
        home_strength = home_extra_rate[indices] + ghost_bonus
        away_strength = away_extra_rate[indices] + ghost_bonus
        home_walkoff = rng.random(indices.size) < home_strength / (home_strength + away_strength)
        away_column = np.full(simulations, -1, dtype=np.int64)
        home_column = np.full(simulations, -1, dtype=np.int64)
        away_column[indices] = np.where(home_walkoff, 0, 1)
        home_column[indices] = np.where(home_walkoff, 1, 0)
        extra_away_columns.append(away_column)
        extra_home_columns.append(home_column)
        away[indices] += away_column[indices]
        home[indices] += home_column[indices]
    if extra_away_columns:
        away_innings = np.concatenate([away_innings, np.stack(extra_away_columns, axis=1)], axis=1)
        home_innings = np.concatenate([home_innings, np.stack(extra_home_columns, axis=1)], axis=1)
    usage = {"home": _bullpen_usage(home_staff_profile, home_tier_counts),
             "away": _bullpen_usage(away_staff_profile, away_tier_counts)}
    return _summarize(home_innings, away_innings, home, away, simulations, league, usage, "INNING_RATE")


def _plate_appearance_game(rng: np.random.Generator, home_expected: float, away_expected: float,
                           simulations: int, league: str, home_staff: dict[str, Any] | None,
                           away_staff: dict[str, Any] | None, home_lineup: np.ndarray,
                           away_lineup: np.ndarray) -> tuple[Any, ...]:
    from backend.app.services.plate_engine import simulate_game

    max_extra = MLB_MAX_EXTRA_INNINGS if league == "MLB" else KBO_MAX_EXTRA_INNINGS
    played = simulate_game(
        rng, simulations,
        {"tables": home_lineup, "staff": home_staff or {}, "expected_runs": home_expected},
        {"tables": away_lineup, "staff": away_staff or {}, "expected_runs": away_expected},
        league, {"max_extra": max_extra})
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


def _summarize(home_innings: np.ndarray, away_innings: np.ndarray, home: np.ndarray, away: np.ndarray,
               simulations: int, league: str, bullpen_usage: dict[str, Any], engine: str) -> dict[str, Any]:
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
    top_scores = []
    for rank, ((h, a), count) in enumerate(score_counts.most_common(16), 1):
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
        top_scores.append({
            "rank": rank, "home": h, "away": a, "count": count,
            "probability": round(count / simulations, 4),
            "inning_line": inning_line,
            "trajectory_count": trajectory_count,
            "trajectory_probability_given_score": round(trajectory_count / count, 4),
        })
    outcome_counts = Counter({
        "HOME_WIN": int(np.count_nonzero(home > away)),
        "AWAY_WIN": int(np.count_nonzero(away > home)),
        "TIE": int(np.count_nonzero(home == away)),
    })
    simulation_modes = {
        "home_runs": _mode_payload(Counter(home.tolist()), simulations),
        "away_runs": _mode_payload(Counter(away.tolist()), simulations),
        "total_runs": _mode_payload(Counter(total.tolist()), simulations),
        "run_margin": _mode_payload(Counter((home - away).tolist()), simulations),
        "outcome": _mode_payload(outcome_counts, simulations),
    }
    handicap = {
        "home_minus_1_5": float(np.mean(home - away >= 2)),
        "away_minus_1_5": float(np.mean(away - home >= 2)),
        "home_plus_1_5": float(np.mean(home - away >= -1)),
        "away_plus_1_5": float(np.mean(away - home >= -1)),
    }
    totals = {
        str(line): {
            "over": float(np.mean(total > line)),
            "under": float(np.mean(total < line)),
            "push": float(np.mean(total == line)),
        }
        for line in (6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13)
    }
    regulation = slice(0, 9)
    extras_played = (away_innings[:, 9:] >= 0).any(axis=1) if away_innings.shape[1] > 9 else np.zeros(1, dtype=bool)
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
        # Preserve the two legacy keys for callers that have not migrated to the handicap object.
        "home_minus_1_5": handicap["home_minus_1_5"],
        "away_plus_1_5": handicap["away_plus_1_5"],
        "totals": totals,
        "top_scores": top_scores,
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
        },
    }
    validate_simulation_summary(result)
    return result


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
