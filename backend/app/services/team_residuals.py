from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from statistics import NormalDist
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Game, GameResult, Prediction


# The residual layer is active for forecasts dated August 23, 2026 and later. Earlier
# predictions remain immutable, but their leakage-safe pregame baselines may seed the EWMA.
RESIDUAL_FEATURE_START_DATE = date(2026, 8, 23)
RESIDUAL_POLICY_VERSION = 2
RESIDUAL_ENABLED_LEAGUES = {"KBO"}
EWMA_HALF_LIFE_GAMES = 12.0
RESIDUAL_WINSOR_LIMIT = 6.0
MAX_RUN_ADJUSTMENT = .45
# The base model already includes recent form. KBO's 550-game walk-forward replay showed that
# unexplained run residuals mean-revert; carrying them forward double-counted form and worsened
# every tracked metric. Half of the shrunk signal is therefore pulled back toward the baseline.
RESIDUAL_MEAN_REVERSION_WEIGHT = -.50


@dataclass(frozen=True)
class ResidualObservation:
    game_id: int
    started_at: datetime
    finalized_at: datetime
    home_team_id: int
    away_team_id: int
    home_expected: float
    away_expected: float
    home_actual: int
    away_actual: int


class TeamResidualHistory:
    """Leakage-safe residual history built from one pregame baseline per final game."""

    def __init__(self, observations: list[ResidualObservation]):
        self.observations = sorted(observations, key=lambda row: (row.started_at, row.game_id))

    @classmethod
    def from_session(cls, session: Session, league: str) -> TeamResidualHistory:
        rows = session.execute(
            select(Prediction, Game, GameResult)
            .join(Game, Game.id == Prediction.game_id)
            .join(GameResult, GameResult.game_id == Game.id)
            .where(Game.league == league, Game.status == "FINAL", Game.start_at.is_not(None))
            .order_by(Game.start_at, Prediction.created_at)
        ).all()
        candidates: dict[int, tuple[tuple[int, datetime], Prediction, Game, GameResult]] = {}
        for prediction, game, result in rows:
            cutoff = prediction.data_cutoff or prediction.created_at
            if _naive(cutoff) > _naive(game.start_at):
                continue
            if prediction.origin == "LIVE_PREGAME" and _naive(prediction.created_at) > _naive(game.start_at):
                continue
            if prediction.origin == "HISTORICAL_REPLAY" and not bool(
                (prediction.leakage_audit or {}).get("passed")
            ):
                continue
            payload = prediction.payload or {}
            schema = int(payload.get("summary_schema_version") or 0)
            # A current audited replay is a better comparable baseline than a legacy live
            # forecast. Current live forecasts retain priority over retrospective replays.
            quality = 3 if prediction.origin == "LIVE_PREGAME" and schema >= 10 else (
                2 if prediction.origin == "HISTORICAL_REPLAY" and schema >= 10 else 1
            )
            rank = (quality, prediction.created_at)
            if game.id not in candidates or rank > candidates[game.id][0]:
                candidates[game.id] = (rank, prediction, game, result)

        observations = []
        for _, prediction, game, result in candidates.values():
            baseline = baseline_expected_runs(prediction)
            observations.append(ResidualObservation(
                game_id=game.id,
                started_at=_naive(game.start_at),
                finalized_at=_naive(result.finalized_at),
                home_team_id=game.home_team_id,
                away_team_id=game.away_team_id,
                home_expected=baseline[0],
                away_expected=baseline[1],
                home_actual=result.home_score,
                away_actual=result.away_score,
            ))
        return cls(observations)

    def context_for(self, game: Game) -> dict[str, Any]:
        if game.league not in RESIDUAL_ENABLED_LEAGUES:
            return _disabled_context(game.game_date, reason="LEAGUE_NOT_ENABLED")
        if game.game_date < RESIDUAL_FEATURE_START_DATE or game.start_at is None:
            return _disabled_context(game.game_date, reason="BEFORE_EFFECTIVE_DATE")
        cutoff = _naive(game.start_at)
        prior = [row for row in self.observations if available_before(row, cutoff)]
        return residual_context(
            prior, game.home_team_id, game.away_team_id, game.game_date,
            latest_game_id=prior[-1].game_id if prior else None,
        )


def residual_context(observations: list[ResidualObservation], home_team_id: int, away_team_id: int,
                     target_date: date, latest_game_id: int | None = None,
                     force_enabled: bool = False) -> dict[str, Any]:
    """Build run adjustments and variance multipliers from observations available at cutoff."""
    if target_date < RESIDUAL_FEATURE_START_DATE and not force_enabled:
        return _disabled_context(target_date)
    league_sd = _league_residual_sd(observations)
    home = _team_projection(observations, home_team_id, away_team_id, True, league_sd)
    away = _team_projection(observations, away_team_id, home_team_id, False, league_sd)

    # A scoring residual contains both the batting club and opposing prevention signal. Their
    # weights sum to one, avoiding double-counting the same game error. Matchup residuals get
    # only a small final weight after their much stronger sample shrinkage.
    home_signal = .70 * home["offense"] - .30 * away["defense"] + .10 * home["matchup"]
    away_signal = .70 * away["offense"] - .30 * home["defense"] + .10 * away["matchup"]
    home_adjustment = _clip(RESIDUAL_MEAN_REVERSION_WEIGHT * home_signal,
                            -MAX_RUN_ADJUSTMENT, MAX_RUN_ADJUSTMENT)
    away_adjustment = _clip(RESIDUAL_MEAN_REVERSION_WEIGHT * away_signal,
                            -MAX_RUN_ADJUSTMENT, MAX_RUN_ADJUSTMENT)
    home_volatility = _clip(math.sqrt(home["offense_volatility"] * away["defense_volatility"]), .82, 1.35)
    away_volatility = _clip(math.sqrt(away["offense_volatility"] * home["defense_volatility"]), .82, 1.35)
    return {
        "enabled": True,
        "policy_version": RESIDUAL_POLICY_VERSION,
        "effective_from": RESIDUAL_FEATURE_START_DATE.isoformat(),
        "source_game_count": len(observations),
        "latest_source_game_id": latest_game_id,
        "league_residual_sd": round(league_sd, 4),
        "home_run_adjustment": round(home_adjustment, 6),
        "away_run_adjustment": round(away_adjustment, 6),
        "home_variance_multiplier": round(home_volatility ** 2, 6),
        "away_variance_multiplier": round(away_volatility ** 2, 6),
        "home": _public_projection(home),
        "away": _public_projection(away),
        "mean_reversion_weight": RESIDUAL_MEAN_REVERSION_WEIGHT,
        "method": ("walk-forward selected mean reversion of team offense/defense EWMA + "
                   "venue split + shrunk matchup residual"),
    }


def apply_residual_adjustment(home_runs: float, away_runs: float,
                              context: dict[str, Any] | None) -> tuple[float, float]:
    if not context or not context.get("enabled"):
        return home_runs, away_runs
    return (
        _clip(home_runs + float(context.get("home_run_adjustment") or 0), .6, 10.0),
        _clip(away_runs + float(context.get("away_run_adjustment") or 0), .6, 10.0),
    )


def probability_from_run_means(home_runs: float, away_runs: float) -> float:
    """Stable two-way approximation used for residual walk-forward comparisons."""
    margin_sd = max(1.0, (1.6 * (home_runs + away_runs)) ** .5)
    return _clip(NormalDist().cdf((home_runs - away_runs) / margin_sd), .02, .98)


def available_before(observation: ResidualObservation, cutoff: datetime) -> bool:
    """A prior-day KBO final is known even if a later bulk sync rewrote its collection time."""
    return observation.started_at < cutoff and (
        observation.started_at.date() < cutoff.date() or observation.finalized_at <= cutoff
    )


def baseline_expected_runs(prediction: Prediction) -> tuple[float, float]:
    calibration = (prediction.payload or {}).get("residual_calibration") or {}
    home = calibration.get("baseline_home_expected_runs")
    away = calibration.get("baseline_away_expected_runs")
    return (
        float(prediction.home_expected_runs if home is None else home),
        float(prediction.away_expected_runs if away is None else away),
    )


def _team_projection(observations: list[ResidualObservation], team_id: int, opponent_id: int,
                     target_is_home: bool, league_sd: float) -> dict[str, float | int]:
    offense_all: list[float] = []
    defense_all: list[float] = []
    offense_venue: list[float] = []
    defense_venue: list[float] = []
    matchup: list[float] = []
    for row in observations:
        if team_id == row.home_team_id:
            offense = _winsor(row.home_actual - row.home_expected)
            defense = _winsor(row.away_expected - row.away_actual)
            is_home = True
            opponent = row.away_team_id
        elif team_id == row.away_team_id:
            offense = _winsor(row.away_actual - row.away_expected)
            defense = _winsor(row.home_expected - row.home_actual)
            is_home = False
            opponent = row.home_team_id
        else:
            continue
        offense_all.append(offense)
        defense_all.append(defense)
        if is_home == target_is_home:
            offense_venue.append(offense)
            defense_venue.append(defense)
        if opponent == opponent_id:
            matchup.append(offense)

    off_mean, off_sd = _ewma(offense_all)
    def_mean, def_sd = _ewma(defense_all)
    venue_off, _ = _ewma(offense_venue)
    venue_def, _ = _ewma(defense_venue)
    matchup_mean, _ = _ewma(matchup)
    general_off = _shrink(off_mean, len(offense_all), 10)
    general_def = _shrink(def_mean, len(defense_all), 10)
    venue_off = _shrink(venue_off, len(offense_venue), 15)
    venue_def = _shrink(venue_def, len(defense_venue), 15)
    offense_signal = .90 * general_off + .10 * venue_off
    defense_signal = .90 * general_def + .10 * venue_def
    matchup_signal = _shrink(matchup_mean, len(matchup), 40)
    # Variance is shrunk toward league noise more aggressively than the mean. With very small
    # samples this makes the multiplier exactly neutral instead of treating one blowout as a trait.
    off_ratio = _variance_ratio(off_sd, len(offense_all), league_sd)
    def_ratio = _variance_ratio(def_sd, len(defense_all), league_sd)
    return {
        "offense": offense_signal,
        "defense": defense_signal,
        "venue_offense": venue_off,
        "venue_defense": venue_def,
        "matchup": matchup_signal,
        "offense_volatility": off_ratio,
        "defense_volatility": def_ratio,
        "games": len(offense_all),
        "venue_games": len(offense_venue),
        "matchup_games": len(matchup),
    }


def _ewma(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    alpha = 1 - math.exp(math.log(.5) / EWMA_HALF_LIFE_GAMES)
    mean = values[0]
    variance = 0.0
    for value in values[1:]:
        delta = value - mean
        mean += alpha * delta
        variance = (1 - alpha) * (variance + alpha * delta * delta)
    return mean, math.sqrt(max(variance, 0.0))


def _league_residual_sd(observations: list[ResidualObservation]) -> float:
    values = [
        residual
        for row in observations
        for residual in (
            _winsor(row.home_actual - row.home_expected),
            _winsor(row.away_actual - row.away_expected),
        )
    ]
    if len(values) < 8:
        return 3.0
    average = sum(values) / len(values)
    return max(1.5, math.sqrt(sum((value - average) ** 2 for value in values) / len(values)))


def _variance_ratio(sd: float, games: int, league_sd: float) -> float:
    if games < 2 or sd <= 0:
        return 1.0
    raw = (sd / max(league_sd, 1e-6)) ** 2
    weight = games / (games + 16)
    return _clip((1 - weight) + weight * raw, .67, 1.82)


def _public_projection(value: dict[str, float | int]) -> dict[str, float | int]:
    return {key: (round(item, 6) if isinstance(item, float) else item) for key, item in value.items()}


def _disabled_context(target_date: date, reason: str = "BEFORE_EFFECTIVE_DATE") -> dict[str, Any]:
    return {
        "enabled": False,
        "policy_version": RESIDUAL_POLICY_VERSION,
        "effective_from": RESIDUAL_FEATURE_START_DATE.isoformat(),
        "target_date": target_date.isoformat(),
        "home_run_adjustment": 0.0,
        "away_run_adjustment": 0.0,
        "home_variance_multiplier": 1.0,
        "away_variance_multiplier": 1.0,
        "source_game_count": 0,
        "reason": reason,
    }


def _shrink(value: float, games: int, prior_games: float) -> float:
    return value * games / (games + prior_games) if games else 0.0


def _winsor(value: float) -> float:
    return _clip(float(value), -RESIDUAL_WINSOR_LIMIT, RESIDUAL_WINSOR_LIMIT)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
