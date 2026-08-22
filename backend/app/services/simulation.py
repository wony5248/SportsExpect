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
                    league: str = "MLB") -> dict[str, Any]:
    rng = np.random.default_rng(seed)
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
    home_innings = rng.poisson(home_expected * shared_environment[:, None] * home_environment[:, None] * home_weights[None, :])
    away_innings = rng.poisson(away_expected * shared_environment[:, None] * away_environment[:, None] * away_weights[None, :])
    home = home_innings.sum(axis=1)
    away = away_innings.sum(axis=1)
    # League-accurate extra innings. MLB plays the automatic-runner tiebreaker until every game
    # is decided; KBO plays innings 10-11 without a tiebreaker and lets remaining ties stand.
    home_extra_rate = home_expected * shared_environment * home_environment * EXTRA_INNING_WEIGHT
    away_extra_rate = away_expected * shared_environment * away_environment * EXTRA_INNING_WEIGHT
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
        pairs = list(zip(away_innings[index].tolist(), home_innings[index].tolist(), strict=True))
        for away_column, home_column in zip(extra_away_columns, extra_home_columns, strict=True):
            if away_column[index] >= 0:
                pairs.append((int(away_column[index]), int(home_column[index])))
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
    result = {
        "extra_innings": {
            "rule": "MLB_GHOST_RUNNER_UNTIL_DECIDED" if league == "MLB" else "KBO_MAX_11_TIES_STAND",
            "probability": float(np.mean(extra_away_columns[0] >= 0)) if extra_away_columns else 0.0,
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


def _mode_payload(counts: Counter[Any], simulations: int) -> dict[str, Any]:
    value, count = counts.most_common(1)[0]
    return {"value": value, "count": count, "probability": round(count / simulations, 4)}
