from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np


def simulate_scores(home_expected: float, away_expected: float, simulations: int, seed: int,
                    environment_variance: float = .08, team_variance: float = .12) -> dict[str, Any]:
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
    total = home + away
    tie_probability = float(np.mean(home == away))
    home_two_way = float(np.mean(home > away) + tie_probability / 2)
    score_counts = Counter(zip(home.tolist(), away.tolist(), strict=False))
    top_scores = []
    # Keep enough candidates for the representative-score selector to honor both winner and total.
    for (h, a), count in score_counts.most_common(16):
        indices = np.flatnonzero((home == h) & (away == a))
        trajectory_counts = Counter(
            tuple(away_innings[index].tolist() + home_innings[index].tolist()) for index in indices
        )
        trajectory, trajectory_count = trajectory_counts.most_common(1)[0]
        away_line, home_line = trajectory[:9], trajectory[9:]
        away_cumulative = home_cumulative = 0
        inning_line = []
        for inning, (away_runs, home_runs) in enumerate(zip(away_line, home_line, strict=True), 1):
            away_cumulative += away_runs
            home_cumulative += home_runs
            inning_line.append({
                "inning": inning, "away": away_runs, "home": home_runs,
                "away_cumulative": away_cumulative, "home_cumulative": home_cumulative,
            })
        top_scores.append({
            "home": h, "away": a, "probability": round(count / simulations, 4),
            "inning_line": inning_line,
            "trajectory_probability_given_score": round(trajectory_count / count, 4),
        })
    return {
        "home_two_way_probability": home_two_way,
        "away_two_way_probability": 1 - home_two_way,
        "tie_probability": tie_probability,
        "home_minus_1_5": float(np.mean(home - away > 1.5)),
        "away_plus_1_5": float(np.mean(away - home + 1.5 > 0)),
        "totals": {
            str(line): {"over": float(np.mean(total > line)), "under": float(np.mean(total < line))}
            for line in (6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13)
        },
        "top_scores": top_scores,
        "total_quantiles": {
            "p10": float(np.quantile(total, .10)), "p50": float(np.quantile(total, .50)), "p90": float(np.quantile(total, .90)),
        },
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
