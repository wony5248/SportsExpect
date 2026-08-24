"""Plate-appearance simulation: one game played out batter by batter.

The inning-level model draws a run total per inning and never knows who is at the plate or who
is on base. This engine walks the actual lineup through a base-out state machine, so a hitter's
scoring-position line is used exactly when there is a runner in scoring position, and the ninth
inning stops the moment the home club has won.

Where the numbers come from:

* Plate-appearance outcomes are the hitter's own collected splits for the current base state
  (see services/batting.py). Nothing about a hitter is invented.
* The pitcher on the mound scales those outcomes by the same leverage-tier multiplier the
  inning model uses, so the starter, the high-leverage group and the mop-up group each change
  what hitters do against them.
* Baserunning advancement uses fixed league-typical conventions. These are mechanics, not
  claims about any team, and any bias in them is absorbed by the offense calibration below,
  which scales each club until its simulated run total matches the expected runs the team-level
  model produced.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from backend.app.services.batting import DOUBLE, HOMER, OUT, SINGLE, STRIKEOUT, TRIPLE, WALK

# Baserunning conventions. Fixed mechanics shared by every club; the offense calibration absorbs
# any level bias they introduce.
SINGLE_SCORES_FROM_SECOND = .58
SINGLE_TAKES_EXTRA_BASE = .28
DOUBLE_SCORES_FROM_FIRST = .42
# Balls in play with a runner on first and fewer than two out.
DOUBLE_PLAY_RATE = .11
# Fly outs that score a runner from third with fewer than two out.
SACRIFICE_FLY_RATE = .16
# Run scoring is superlinear in on-base rate, so a run-rate multiplier of m corresponds to a
# smaller move in the underlying outcome rates.
RUN_TO_RATE_EXPONENT = .55
# The calibration pass runs at reduced volume purely to find each club's offense scalar.
CALIBRATION_SIMULATIONS = 4000
CALIBRATION_ROUNDS = 3
# The base-out process already supplies substantial batter-to-batter variance. Only part of the
# game-level team variance is added here, preventing the same uncertainty from being counted twice.
PLATE_GAME_SHOCK_WEIGHT = .35


def playable(plan: dict[str, Any] | None) -> bool:
    """True when a club has a nine-hitter table the engine can actually use."""
    return bool(plan and plan.get("tables") is not None)


def simulate_game(rng: np.random.Generator, simulations: int, home: dict[str, Any], away: dict[str, Any],
                  league: str, extra_innings: dict[str, Any], home_team_variance: float | None = None,
                  away_team_variance: float | None = None) -> dict[str, Any]:
    """Play `simulations` complete games and return per-inning run lines for both clubs."""
    home_scale, away_scale = _calibrate(
        rng, home, away, league, extra_innings, home_team_variance, away_team_variance)
    result = _play(rng, simulations, home, away, league, extra_innings, home_scale, away_scale,
                   home_team_variance, away_team_variance)
    result["offense_scale"] = {"home": round(home_scale, 4), "away": round(away_scale, 4)}
    return result


def _calibrate(rng: np.random.Generator, home: dict[str, Any], away: dict[str, Any],
               league: str, extra_innings: dict[str, Any], home_team_variance: float | None,
               away_team_variance: float | None) -> tuple[float, float]:
    """Find the offense scalars that land both clubs on the run totals the team model expects.

    The two are solved together: whether the home club bats in the ninth depends on how many
    runs the away club scored, so calibrating either one alone leaves the home club short.
    """
    targets = (float(home["expected_runs"]), float(away["expected_runs"]))
    scales = [1.0, 1.0]
    rounds = CALIBRATION_ROUNDS + (2 if home_team_variance is not None or away_team_variance is not None else 0)
    for _ in range(rounds):
        probe = _play(np.random.default_rng(int(rng.integers(2**32))), CALIBRATION_SIMULATIONS,
                      home, away, league, extra_innings, scales[0], scales[1],
                      home_team_variance, away_team_variance)
        for index, side in enumerate(("home", "away")):
            realized = float(np.maximum(probe[f"{side}_innings"], 0).sum(axis=1).mean())
            if realized > 0:
                scales[index] *= float(np.clip((targets[index] / realized) ** RUN_TO_RATE_EXPONENT, .75, 1.35))
    return float(np.clip(scales[0], .55, 1.8)), float(np.clip(scales[1], .55, 1.8))


def _play(rng: np.random.Generator, simulations: int, home: dict[str, Any], away: dict[str, Any],
          league: str, extra_innings: dict[str, Any], home_scale: float, away_scale: float,
          home_team_variance: float | None, away_team_variance: float | None) -> dict[str, Any]:
    from backend.app.services.simulation import _staff_profile

    # The pitcher plan draws one starter exit per simulation, so it is built at this pass's size.
    home_profile = _staff_profile(home["staff"], rng, simulations)
    away_profile = _staff_profile(away["staff"], rng, simulations)
    max_innings = 9 + int(extra_innings["max_extra"])
    home_runs_by_inning: list[np.ndarray] = []
    away_runs_by_inning: list[np.ndarray] = []
    home_total = np.zeros(simulations, dtype=np.int64)
    away_total = np.zeros(simulations, dtype=np.int64)
    home_batter = np.zeros(simulations, dtype=np.int64)
    away_batter = np.zeros(simulations, dtype=np.int64)
    live = np.ones(simulations, dtype=bool)
    tier_counts = {"home": {}, "away": {}}
    home_game_scale = _game_scale(rng, simulations, home_team_variance)
    away_game_scale = _game_scale(rng, simulations, away_team_variance)

    for inning in range(max_innings):
        extra = inning >= 9
        # Top half: the away club bats against the home staff.
        home_multiplier = _staff_multiplier(home_profile, inning, home_total - away_total, extra,
                                            None if extra else tier_counts["home"])
        runs, away_batter = _half_inning(
            rng, away["tables"], away_batter, away_scale * away_game_scale,
            home_multiplier, live, None, runner_on_second=(extra and league == "MLB"))
        away_total += runs
        away_runs_by_inning.append(np.where(live, runs, -1) if inning >= 9 else runs)
        # Bottom half. From the ninth on, the home club bats only while level or behind, and a
        # walk-off ends the inning at the winning run.
        deficit = away_total - home_total
        cap = np.where(inning >= 8, np.maximum(deficit + 1, 0), np.iinfo(np.int64).max)
        bats = live & ((inning < 8) | (deficit >= 0))
        away_multiplier = _staff_multiplier(away_profile, inning, away_total - home_total, extra,
                                            None if extra else tier_counts["away"])
        runs, home_batter = _half_inning(
            rng, home["tables"], home_batter, home_scale * home_game_scale,
            away_multiplier, bats, cap, runner_on_second=(extra and league == "MLB"))
        home_total += runs
        # A half-inning that was never played is recorded as -1 so a scorebook can show it as
        # skipped rather than as a scoreless inning.
        home_runs_by_inning.append(np.where(bats, runs, -1) if inning >= 8 else runs)
        if inning >= 8:
            live = home_total == away_total
            if not live.any():
                break

    return {
        "home_innings": np.stack(home_runs_by_inning, axis=1),
        "away_innings": np.stack(away_runs_by_inning, axis=1),
        "home": home_total, "away": away_total, "tier_counts": tier_counts,
    }


def _game_scale(rng: np.random.Generator, simulations: int,
                team_variance: float | None) -> float | np.ndarray:
    # None deliberately preserves historical plate-engine recipes created before this field.
    if team_variance is None:
        return 1.0
    variance = min(.12, max(.01, float(team_variance) * PLATE_GAME_SHOCK_WEIGHT))
    return rng.gamma(1 / variance, variance, simulations)


def _staff_multiplier(profile: dict[str, Any], inning: int, lead: np.ndarray, extra: bool,
                      counts: dict[str, int] | None) -> np.ndarray:
    """Reuse the inning model's pitcher plan, expressed as an outcome-rate multiplier."""
    from backend.app.services.simulation import _inning_multiplier

    if extra:
        # Extras are the definition of high leverage; both clubs are down to their best arms.
        multiplier = np.full(lead.size, profile["bullpen"]["high_leverage"] / profile["normalizer"])
    else:
        multiplier, used = _inning_multiplier(profile, inning, lead)
        if counts is not None:
            for tier, value in used.items():
                counts[tier] = counts.get(tier, 0) + value
    return np.power(multiplier, RUN_TO_RATE_EXPONENT)


def _half_inning(rng: np.random.Generator, tables: np.ndarray, batter: np.ndarray,
                 offense_scale: float | np.ndarray,
                 pitcher_multiplier: np.ndarray, active: np.ndarray,
                 run_cap: np.ndarray | None,
                 runner_on_second: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Bat until three outs, or until a walk-off caps the inning."""
    simulations = batter.size
    runs = np.zeros(simulations, dtype=np.int64)
    outs = np.zeros(simulations, dtype=np.int64)
    first = np.zeros(simulations, dtype=bool)
    # MLB extra innings begin with an automatic runner on second. This must be represented in
    # the base-out state itself so RISP-specific hitter probabilities and advancement rules run.
    second = active.copy() if runner_on_second else np.zeros(simulations, dtype=bool)
    third = np.zeros(simulations, dtype=bool)
    batting = active.copy()
    if run_cap is not None:
        batting &= run_cap > 0

    while batting.any():
        index = np.flatnonzero(batting)
        state = first[index].astype(np.int64) + 2 * second[index] + 4 * third[index]
        active_scale = offense_scale[index] if isinstance(offense_scale, np.ndarray) else offense_scale
        row = tables[batter[index], state] * _adjustment(pitcher_multiplier[index], active_scale)
        row /= row.sum(axis=1, keepdims=True)
        draw = rng.random(index.size)
        outcome = (np.cumsum(row, axis=1) < draw[:, None]).sum(axis=1).clip(0, row.shape[1] - 1)
        scored, first_next, second_next, third_next, outs_added = _advance(
            rng, outcome, first[index], second[index], third[index], outs[index])
        runs[index] += scored
        first[index], second[index], third[index] = first_next, second_next, third_next
        outs[index] += outs_added
        batter[index] = (batter[index] + 1) % 9
        batting = active & (outs < 3)
        if run_cap is not None:
            batting &= runs < run_cap
    if run_cap is not None:
        runs = np.minimum(runs, np.maximum(run_cap, 0))
    return runs, batter


def _adjustment(pitcher_multiplier: np.ndarray, offense_scale: float | np.ndarray) -> np.ndarray:
    """Scale on-base outcomes; the balance flows into outs so each row still sums to one."""
    factor = pitcher_multiplier * offense_scale
    weights = np.ones((factor.size, 7))
    weights[:, [WALK, SINGLE, DOUBLE, TRIPLE, HOMER]] = factor[:, None]
    # A tougher pitcher converts the missing on-base chances into outs, mostly strikeouts.
    weights[:, STRIKEOUT] = np.power(factor, -.35)
    return weights


def _advance(rng: np.random.Generator, outcome: np.ndarray, first: np.ndarray, second: np.ndarray,
             third: np.ndarray, outs: np.ndarray) -> tuple[np.ndarray, ...]:
    """Move the runners for one plate appearance across every simulation at once."""
    size = outcome.size
    runs = np.zeros(size, dtype=np.int64)
    outs_added = np.zeros(size, dtype=np.int64)
    new_first = first.copy()
    new_second = second.copy()
    new_third = third.copy()
    chance = rng.random((size, 3))

    homer = outcome == HOMER
    runs[homer] = 1 + first[homer].astype(np.int64) + second[homer] + third[homer]
    new_first[homer] = new_second[homer] = new_third[homer] = False

    triple = outcome == TRIPLE
    runs[triple] = first[triple].astype(np.int64) + second[triple] + third[triple]
    new_first[triple] = new_second[triple] = False
    new_third[triple] = True

    double = outcome == DOUBLE
    if double.any():
        first_scores = double & first & (chance[:, 0] < DOUBLE_SCORES_FROM_FIRST)
        runs[double] += second[double].astype(np.int64) + third[double]
        runs[first_scores] += 1
        new_third[double] = first[double] & ~first_scores[double]
        new_second[double] = True
        new_first[double] = False

    single = outcome == SINGLE
    if single.any():
        second_scores = single & second & (chance[:, 1] < SINGLE_SCORES_FROM_SECOND)
        extra_base = single & first & (chance[:, 2] < SINGLE_TAKES_EXTRA_BASE)
        runs[single] += third[single].astype(np.int64)
        runs[second_scores] += 1
        new_third[single] = (second[single] & ~second_scores[single]) | extra_base[single]
        new_second[single] = first[single] & ~extra_base[single]
        new_first[single] = True

    walk = outcome == WALK
    if walk.any():
        forced_third = walk & first & second & third
        runs[forced_third] += 1
        new_third[walk] = third[walk] | (first[walk] & second[walk])
        new_second[walk] = second[walk] | first[walk]
        new_first[walk] = True

    strikeout = outcome == STRIKEOUT
    outs_added[strikeout] = 1

    in_play_out = outcome == OUT
    if in_play_out.any():
        double_play = in_play_out & first & (outs < 2) & (chance[:, 0] < DOUBLE_PLAY_RATE)
        sacrifice = in_play_out & third & ~double_play & (outs < 2) & (chance[:, 1] < SACRIFICE_FLY_RATE)
        outs_added[in_play_out] = 1
        outs_added[double_play] = 2
        runs[sacrifice] += 1
        new_third[sacrifice] = False
        new_first[double_play] = False
    return runs, new_first, new_second, new_third, outs_added
