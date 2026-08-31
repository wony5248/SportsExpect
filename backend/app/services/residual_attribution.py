from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


# Shares mirror the normalized inning weights in simulation.py.  Keeping the split coarse is
# intentional: innings 1-5 are predominantly the starter regime and 6+ the leverage bullpen
# regime, while pretending to identify a single responsible pitcher from a team score would be
# false precision without the postgame pitching ledger.
EARLY_RUN_SHARE = {"away": .533, "home": .537}


def attribute_score_residual(prediction: Any, result: Any) -> dict[str, Any]:
    """Decompose a final score miss into auditable, non-causal diagnostic channels."""
    payload = prediction.payload or {}
    residual = payload.get("residual_calibration") or {}
    expected = {
        "home": float(prediction.home_expected_runs),
        "away": float(prediction.away_expected_runs),
    }
    actual = {"home": int(result.home_score), "away": int(result.away_score)}
    innings = result.innings if isinstance(result.innings, dict) else {}
    channels: dict[str, Any] = {}
    for side in ("home", "away"):
        values = innings.get(side)
        early_actual = sum(int(value or 0) for value in values[:5]) if isinstance(values, list) else None
        late_actual = sum(int(value or 0) for value in values[5:]) if isinstance(values, list) else None
        early_expected = expected[side] * EARLY_RUN_SHARE[side]
        late_expected = expected[side] - early_expected
        miss = actual[side] - expected[side]
        projection = residual.get(side) or {}
        persistent = bool(
            projection.get("matchup_residual_flag")
            or abs(float(projection.get("structure") or 0.0)) >= .20
        )
        large = abs(miss) >= max(3.0, 1.25 * float(residual.get("league_residual_sd") or 2.4))
        route = "DIRECTIONAL_CANDIDATE" if persistent and not large else (
            "VARIANCE_ONLY" if large else "MEAN_REVERSION"
        )
        channels[side] = {
            "expected_runs": round(expected[side], 4), "actual_runs": actual[side],
            "score_residual": round(miss, 4),
            "early_starter_phase": ({
                "innings": "1-5", "expected_runs": round(early_expected, 4),
                "actual_runs": early_actual,
                "residual": round(early_actual - early_expected, 4),
            } if early_actual is not None else {"available": False}),
            "late_bullpen_phase": ({
                "innings": "6+", "expected_runs": round(late_expected, 4),
                "actual_runs": late_actual,
                "residual": round(late_actual - late_expected, 4),
            } if late_actual is not None else {"available": False}),
            "routing": route,
            "large_residual": large,
            "persistent_pregame_evidence": persistent,
        }
    expected_total = expected["home"] + expected["away"]
    actual_total = actual["home"] + actual["away"]
    expected_margin = expected["home"] - expected["away"]
    actual_margin = actual["home"] - actual["away"]
    model_favorite = "home" if float(prediction.home_win_probability) >= .5 else "away"
    actual_winner = "home" if actual_margin > 0 else ("away" if actual_margin < 0 else "tie")
    return {
        "schema_version": 1,
        "home": channels["home"], "away": channels["away"],
        "total_residual": round(actual_total - expected_total, 4),
        "margin_residual": round(actual_margin - expected_margin, 4),
        "favorite_lost": actual_winner not in {model_favorite, "tie"},
        "inning_split_available": all(isinstance(innings.get(side), list) for side in ("home", "away")),
        "interpretation_guard": (
            "Score and inning residuals are diagnostics, not causal skill labels. Directional "
            "carry requires repeated matchup/structure evidence; isolated tails widen variance."
        ),
    }


def residual_attribution_report(rows: Iterable[tuple[Any, Any, Any, Any]]) -> dict[str, Any]:
    attributions = [attribute_score_residual(prediction, result)
                    for prediction, _game, result, _snapshot in rows]
    if not attributions:
        return {"sample_size": 0}
    routes: dict[str, int] = defaultdict(int)
    early_errors: list[float] = []
    late_errors: list[float] = []
    for attribution in attributions:
        for side in ("home", "away"):
            row = attribution[side]
            routes[row["routing"]] += 1
            if attribution["inning_split_available"]:
                early_errors.append(float(row["early_starter_phase"]["residual"]))
                late_errors.append(float(row["late_bullpen_phase"]["residual"]))

    def mae(values: list[float]) -> float | None:
        return round(sum(abs(value) for value in values) / len(values), 4) if values else None

    return {
        "sample_size": len(attributions),
        "team_observations": len(attributions) * 2,
        "routing_counts": dict(routes),
        "inning_split_games": sum(row["inning_split_available"] for row in attributions),
        "starter_phase_mae": mae(early_errors),
        "bullpen_phase_mae": mae(late_errors),
        "favorite_loss_rate": round(sum(row["favorite_lost"] for row in attributions) / len(attributions), 4),
    }
