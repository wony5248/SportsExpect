from __future__ import annotations

import math
from typing import Any

from backend.app.services.simulation import simulate_scores


ABLATION_SIMULATIONS = 1000
ABLATION_MAX_GAMES = 120


def paired_ablation_report(rows: list[tuple[Any, Any, Any, Any]]) -> dict[str, Any]:
    """Replay bounded counterfactuals with common random numbers on identical games."""
    eligible = [row for row in rows if isinstance((row[0].payload or {}).get("simulation_recipe"), dict)
                and row[2].home_score != row[2].away_score][-ABLATION_MAX_GAMES:]
    variants: dict[str, list[dict[str, float]]] = {
        name: [] for name in ("production", "no_residual", "no_market", "no_upset_volatility")
    }
    for prediction, _game, result, _snapshot in eligible:
        for name, recipe in _variant_recipes(prediction).items():
            summary = _simulate(recipe)
            variants[name].append(_score(summary, result))
    metrics = {name: _metrics(values) for name, values in variants.items()}
    production = metrics["production"]
    deltas = {
        name: {metric: round(values[metric] - production[metric], 6)
               for metric in ("brier", "log_loss", "team_run_mae", "run_line_brier", "total_brier")}
        for name, values in metrics.items() if name != "production" and values.get("sample_size")
    }
    return {
        "sample_size": len(eligible), "simulations_per_variant": ABLATION_SIMULATIONS,
        "common_random_numbers": True, "metrics": metrics,
        "delta_vs_production": deltas,
        "interpretation": "Negative delta beats production; diagnostics remain shadow-only.",
    }


def _variant_recipes(prediction: Any) -> dict[str, dict[str, Any]]:
    payload = prediction.payload or {}
    production = dict(payload["simulation_recipe"])
    production["simulations"] = min(ABLATION_SIMULATIONS, int(production.get("simulations") or ABLATION_SIMULATIONS))
    variants = {"production": production}

    market = payload.get("headline_market") or {}
    residual = payload.get("residual_calibration") or {}
    market_calibration = payload.get("market_calibration") or {}

    # Remove the residual layer but preserve the same market treatment so only one component
    # changes.  Import locally to avoid prediction.py importing this diagnostic module back.
    from backend.app.services.prediction import market_reference_audit
    baseline_home = residual.get("baseline_home_expected_runs")
    baseline_away = residual.get("baseline_away_expected_runs")
    if baseline_home is not None and baseline_away is not None:
        home, away, _ = market_reference_audit(
            float(baseline_home), float(baseline_away), market, str(production.get("league") or "MLB"),
        )
        variants["no_residual"] = {**production, "home_expected": home, "away_expected": away}
    else:
        variants["no_residual"] = dict(production)

    before_home = market_calibration.get("model_home_before")
    before_away = market_calibration.get("model_away_before")
    variants["no_market"] = ({**production, "home_expected": float(before_home),
                               "away_expected": float(before_away)}
                              if before_home is not None and before_away is not None else dict(production))

    volatility = payload.get("upset_volatility") or {}
    shared = float(volatility.get("shared_volatility") or 0.0)
    base_variance = max(.01, float(production.get("team_variance") or .06) - shared)
    home_multiplier = float(residual.get("home_variance_multiplier") or 1.0)
    away_multiplier = float(residual.get("away_variance_multiplier") or 1.0)
    variants["no_upset_volatility"] = {
        **production,
        "team_variance": base_variance,
        "home_team_variance": max(.01, base_variance * home_multiplier),
        "away_team_variance": max(.01, base_variance * away_multiplier),
    }
    return variants


def _simulate(recipe: dict[str, Any]) -> dict[str, Any]:
    return simulate_scores(
        float(recipe["home_expected"]), float(recipe["away_expected"]),
        int(recipe["simulations"]), int(recipe["seed"]),
        float(recipe.get("environment_variance", 0.0)), float(recipe.get("team_variance", .06)),
        league=str(recipe.get("league") or "MLB"),
        home_staff=recipe.get("home_staff"), away_staff=recipe.get("away_staff"),
        # The bounded diagnostic intentionally uses the inning engine.  It isolates the global
        # layer under test without turning a report into thousands of expensive PA replays.
        home_lineup=None, away_lineup=None,
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


def _score(summary: dict[str, Any], result: Any) -> dict[str, float]:
    probability = min(.999, max(.001, float(summary["home_two_way_probability"])))
    outcome = 1.0 if result.home_score > result.away_score else 0.0
    means = summary["mean_runs"]
    actual_margin = result.home_score - result.away_score
    actual_total = result.home_score + result.away_score
    total_line = "8.5" if "8.5" in summary["totals"] else next(iter(summary["totals"]))
    return {
        "brier": (probability - outcome) ** 2,
        "log_loss": -(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability)),
        "team_run_absolute_error": (
            abs(float(means["home"]) - result.home_score)
            + abs(float(means["away"]) - result.away_score)
        ) / 2,
        "run_line_brier": (float(summary["handicap"]["home_minus_1_5"])
                           - float(actual_margin >= 2)) ** 2,
        "total_brier": (float(summary["totals"][total_line]["over"])
                        - float(actual_total > float(total_line))) ** 2,
    }


def _metrics(rows: list[dict[str, float]]) -> dict[str, Any]:
    if not rows:
        return {"sample_size": 0, "brier": 0.0, "log_loss": 0.0, "team_run_mae": 0.0,
                "run_line_brier": 0.0, "total_brier": 0.0}
    return {
        "sample_size": len(rows),
        "brier": round(sum(row["brier"] for row in rows) / len(rows), 6),
        "log_loss": round(sum(row["log_loss"] for row in rows) / len(rows), 6),
        "team_run_mae": round(sum(row["team_run_absolute_error"] for row in rows) / len(rows), 6),
        "run_line_brier": round(sum(row["run_line_brier"] for row in rows) / len(rows), 6),
        "total_brier": round(sum(row["total_brier"] for row in rows) / len(rows), 6),
    }
