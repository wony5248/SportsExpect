"""Per-batter plate-appearance outcome tables, built only from collected splits.

Each hitter gets one probability row per base state, derived from the counting stats both
leagues publish for that exact state (bases empty through bases loaded). Exact states are small
samples — a hitter may have eleven bases-loaded at-bats all season — so every state is shrunk
toward a larger observed sample from the same hitter: the scoring-position aggregate for states
with a runner in scoring position, then the hitter's own season line. Nothing is invented; a
hitter with no collected splits simply has no table and the caller falls back.
"""
from __future__ import annotations

from typing import Any

import numpy as np

# Base-state index is a bit field: first + 2*second + 4*third.
STATE_INDEX = {
    "BASES_EMPTY": 0, "RUNNER_1": 1, "RUNNER_2": 2, "RUNNER_12": 3,
    "RUNNER_3": 4, "RUNNER_13": 5, "RUNNER_23": 6, "BASES_LOADED": 7,
}
STATE_NAMES = {index: name for name, index in STATE_INDEX.items()}
# States where the shrink target is the hitter's scoring-position line rather than their overall
# line: any state with a runner on second or third.
SCORING_POSITION_STATES = {2, 3, 4, 5, 6, 7}

# Plate-appearance outcomes, in the order the probability rows are stored.
OUT, STRIKEOUT, WALK, SINGLE, DOUBLE, TRIPLE, HOMER = range(7)
OUTCOMES = ("out", "strikeout", "walk", "single", "double", "triple", "homer")

# Shrinkage strength in plate appearances. An exact state needs roughly this many observations
# before it outweighs the larger sample it is shrunk toward.
STATE_PRIOR_PA = 60.0
SCORING_PRIOR_PA = 120.0
# A hitter with fewer than this many total plate appearances is not modelled individually.
MINIMUM_SEASON_PA = 40


def build_batter_table(splits: dict[str, dict[str, int]]) -> np.ndarray | None:
    """Return an 8x7 probability table for one hitter, or None when the sample is too thin."""
    exact = {STATE_INDEX[name]: counts for name, counts in splits.items() if name in STATE_INDEX}
    if not exact:
        return None
    overall = _sum_counts(exact.values())
    if _plate_appearances(overall) < MINIMUM_SEASON_PA:
        return None
    overall_rates = _rates(overall)
    scoring = splits.get("SCORING_POSITION")
    if scoring:
        scoring_rates = _blend(_rates(scoring), _plate_appearances(scoring), overall_rates, SCORING_PRIOR_PA)
    else:
        scoring_rates = _blend(
            _rates(_sum_counts(counts for index, counts in exact.items() if index in SCORING_POSITION_STATES)),
            sum(_plate_appearances(counts) for index, counts in exact.items() if index in SCORING_POSITION_STATES),
            overall_rates, SCORING_PRIOR_PA)
    table = np.zeros((8, len(OUTCOMES)))
    for index in range(8):
        counts = exact.get(index)
        prior = scoring_rates if index in SCORING_POSITION_STATES else overall_rates
        if counts is None:
            table[index] = prior
            continue
        table[index] = _blend(_rates(counts), _plate_appearances(counts), prior, STATE_PRIOR_PA)
    return table


def league_average_table(tables: list[np.ndarray]) -> np.ndarray | None:
    """Average of the hitters we do have, used only for lineup slots with no collected splits."""
    return np.mean(np.stack(tables), axis=0) if tables else None


def lineup_tables(order: list[dict[str, Any]], splits_by_player: dict[str, dict[str, dict[str, int]]],
                  fallback: np.ndarray | None) -> tuple[np.ndarray, int] | None:
    """Stack nine hitters into one (9, 8, 7) table. Returns the table and how many are real."""
    rows: list[np.ndarray] = []
    covered = 0
    for entry in order:
        player_id = entry.get("player_id")
        table = build_batter_table(splits_by_player.get(str(player_id), {})) if player_id else None
        if table is not None:
            covered += 1
        elif fallback is not None:
            table = fallback
        else:
            return None
        rows.append(table)
    if len(rows) != 9:
        return None
    return np.stack(rows), covered


def _plate_appearances(counts: dict[str, int]) -> float:
    return float(counts.get("at_bats", 0) + counts.get("walks", 0)
                 + counts.get("hit_by_pitch", 0) + counts.get("sacrifice_flies", 0))


def _sum_counts(rows: Any) -> dict[str, int]:
    total: dict[str, int] = {}
    for counts in rows:
        for key, value in counts.items():
            total[key] = total.get(key, 0) + int(value or 0)
    return total


def _rates(counts: dict[str, int]) -> np.ndarray:
    plate_appearances = _plate_appearances(counts)
    if plate_appearances <= 0:
        return np.zeros(len(OUTCOMES))
    hits = int(counts.get("hits", 0))
    doubles, triples, homers = (int(counts.get(key, 0)) for key in ("doubles", "triples", "home_runs"))
    singles = max(0, hits - doubles - triples - homers)
    walks = int(counts.get("walks", 0)) + int(counts.get("hit_by_pitch", 0))
    strikeouts = int(counts.get("strikeouts", 0))
    row = np.zeros(len(OUTCOMES))
    row[STRIKEOUT] = strikeouts / plate_appearances
    row[WALK] = walks / plate_appearances
    row[SINGLE] = singles / plate_appearances
    row[DOUBLE] = doubles / plate_appearances
    row[TRIPLE] = triples / plate_appearances
    row[HOMER] = homers / plate_appearances
    # Everything else is a ball put in play for an out, including sacrifice flies and grounders.
    row[OUT] = max(0.0, 1.0 - row.sum())
    total = row.sum()
    return row / total if total > 0 else row


def _blend(rates: np.ndarray, sample: float, prior: np.ndarray, prior_strength: float) -> np.ndarray:
    weight = sample / (sample + prior_strength) if sample > 0 else 0.0
    blended = weight * rates + (1 - weight) * prior
    total = blended.sum()
    return blended / total if total > 0 else prior
