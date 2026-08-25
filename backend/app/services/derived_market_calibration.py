"""Accumulated comparison of the model's own derived reference points against the market's.

Every forecast now stores two things side by side: the run line and total the model arrived at
on its own, with the probabilities it gives each side, and the line and de-vigged price the book
actually posted. Once a game is final the realised outcome joins them, which turns each finished
game into one labelled training row for the derived markets.

Nothing here changes a forecast. A model that disagreed with the book is not thereby wrong, and
a handful of games cannot tell the two apart, so this service only measures - it reports how far
our lines sit from the posted ones, whether our probabilities beat the closing price as a
forecast, and whether the games we claimed an edge on actually landed. A correction is only
worth fitting once those numbers say the disagreement is skill rather than bias, and the gate
below stays HOLD until then.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Game, GameResult, Prediction


# Below this a bias estimate is indistinguishable from noise, and an edge hit rate certainly is.
MIN_DERIVED_MARKET_SAMPLES = 100
# The model must beat the closing price as a forecast, not merely differ from it, before any
# correction fitted here is allowed to touch a prediction.
DERIVED_MARKET_GUARDRAIL = {"brier_improvement": 0.0, "minimum_samples": MIN_DERIVED_MARKET_SAMPLES}
# Edge buckets reported back, in probability points.
EDGE_BUCKETS = ((0.0, .02), (.02, .05), (.05, .10), (.10, 1.0))


@dataclass(frozen=True)
class DerivedMarketObservation:
    """One finished game, as the derived markets saw it before first pitch."""

    game_id: int
    league: str
    season: int
    available_at: datetime
    market: str
    # The line each side would have posted. None when the market did not publish one.
    model_line: float | None
    market_line: float | None
    # Probabilities for the same event at the market's own line, both two-way.
    model_probability: float | None
    market_probability: float | None
    # 1 when the priced side won, 0 when it lost, None on a push.
    outcome: float | None


class DerivedMarketHistory:
    """Finished games paired only with forecasts that could not have seen their result."""

    def __init__(self, observations: list[DerivedMarketObservation]):
        self.observations = sorted(observations, key=lambda row: (row.available_at, row.game_id))

    @classmethod
    def from_session(cls, session: Session, league: str = "ALL") -> DerivedMarketHistory:
        query = (
            select(Prediction, Game, GameResult)
            .join(Game, Game.id == Prediction.game_id)
            .join(GameResult, GameResult.game_id == Game.id)
            .where(Game.start_at.is_not(None))
            .order_by(Game.start_at, Prediction.created_at)
        )
        if league != "ALL":
            query = query.where(Game.league == league)
        by_game: dict[int, tuple[Prediction, Game, GameResult]] = {}
        for prediction, game, result in session.execute(query).all():
            cutoff = prediction.data_cutoff or prediction.created_at
            if _naive(cutoff) > _naive(game.start_at):
                continue
            # A replay that has not passed its leakage audit may have seen the result it is being
            # scored against, which would make every number here flattering and meaningless.
            if prediction.origin == "HISTORICAL_REPLAY" and (
                not prediction.training_eligible
                or not bool((prediction.leakage_audit or {}).get("passed"))
            ):
                continue
            current = by_game.get(game.id)
            if current is None or _prefer(prediction, current[0]):
                by_game[game.id] = (prediction, game, result)

        observations: list[DerivedMarketObservation] = []
        for prediction, game, result in by_game.values():
            observations.extend(_observations_for(prediction, game, result))
        return cls(observations)

    def report(self) -> dict[str, Any]:
        """Everything accumulated so far, split by market and by league."""
        by_market: dict[str, list[DerivedMarketObservation]] = defaultdict(list)
        by_league: dict[str, list[DerivedMarketObservation]] = defaultdict(list)
        for row in self.observations:
            by_market[row.market].append(row)
            by_league[row.league].append(row)
        return {
            "sample_size": len(self.observations),
            "collected_through": (max(row.available_at for row in self.observations).isoformat()
                                  if self.observations else None),
            "guardrail": DERIVED_MARKET_GUARDRAIL,
            "markets": {market: _metrics(rows) for market, rows in sorted(by_market.items())},
            "leagues": {
                name: {market: _metrics([row for row in rows if row.market == market])
                       for market in sorted({row.market for row in rows})}
                for name, rows in sorted(by_league.items())
            },
        }


def _observations_for(prediction: Prediction, game: Game,
                      result: GameResult) -> list[DerivedMarketObservation]:
    payload = prediction.payload or {}
    conditional = payload.get("winner_conditional_market")
    fair = payload.get("model_fair_lines") or {}
    # Forecasts saved before the model recorded its own reference points have nothing to compare.
    if not isinstance(conditional, dict) or not fair:
        return []
    home_score, away_score = int(result.home_score), int(result.away_score)
    total, margin = home_score + away_score, home_score - away_score
    season = game.game_date.year
    available_at = result.finalized_at
    rows: list[DerivedMarketObservation] = []

    headline_total = conditional.get("headline_total") or {}
    market_total_line = fair.get("market_total_line")
    priced_total = (headline_total.get("market_over_probability")
                    if headline_total.get("line_source") == "MARKET" else None)
    rows.append(DerivedMarketObservation(
        game_id=game.id, league=game.league, season=season, available_at=available_at,
        market="TOTAL",
        model_line=_number(fair.get("total_line")),
        market_line=_number(market_total_line),
        model_probability=_number(headline_total.get("model_over_probability")),
        market_probability=_number(priced_total),
        outcome=_side_outcome(total, _number(headline_total.get("line"))),
    ))

    # The moneyline joins them because an upset call is a claim about this market specifically:
    # that the club the book made the underdog wins more often than the price says. Scored on
    # the home club so every row reads the same way.
    watch = payload.get("upset_watch") or {}
    market_home = None
    if watch.get("market_probability") is not None:
        market_home = (float(watch["market_probability"]) if watch.get("underdog") == "HOME"
                       else 1 - float(watch["market_probability"]))
    rows.append(DerivedMarketObservation(
        game_id=game.id, league=game.league, season=season, available_at=available_at,
        market="MONEYLINE",
        model_line=None, market_line=None,
        model_probability=float(prediction.home_win_probability),
        market_probability=market_home,
        outcome=_side_outcome(margin, 0),
    ))

    handicap = conditional.get("handicap") or {}
    run_line = _number(handicap.get("run_line"))
    minus_side = handicap.get("minus_side")
    # The market prices the club laying the runs, so the outcome is read from that club's margin.
    minus_margin = margin if minus_side == "HOME" else -margin
    rows.append(DerivedMarketObservation(
        game_id=game.id, league=game.league, season=season, available_at=available_at,
        market="RUN_LINE",
        model_line=_number(fair.get("home_spread")),
        market_line=_number(fair.get("market_home_spread")),
        model_probability=_number(handicap.get("model_minus_probability")),
        market_probability=_number(handicap.get("market_minus_probability")),
        outcome=_side_outcome(minus_margin, run_line),
    ))
    return rows


def _metrics(rows: list[DerivedMarketObservation]) -> dict[str, Any]:
    lines = [row for row in rows if row.model_line is not None and row.market_line is not None]
    judged = [row for row in rows if row.outcome is not None and row.model_probability is not None]
    priced = [row for row in judged if row.market_probability is not None]
    model_brier = _mean([(row.model_probability - row.outcome) ** 2 for row in priced])
    market_brier = _mean([(row.market_probability - row.outcome) ** 2 for row in priced])
    improvement = (round(market_brier - model_brier, 6)
                   if model_brier is not None and market_brier is not None else None)
    return {
        "sample_size": len(rows),
        "line_comparison": {
            "sample_size": len(lines),
            # Positive means we post a higher number than the book: a longer total, or a home
            # run line further toward the away club.
            "mean_difference": _round(_mean([row.model_line - row.market_line for row in lines])),
            "mean_absolute_difference": _round(
                _mean([abs(row.model_line - row.market_line) for row in lines])),
            "agreement_rate": _round(_mean([float(row.model_line == row.market_line) for row in lines])),
        },
        "probability_comparison": {
            "sample_size": len(priced),
            # Positive means we give the priced side a better chance than the book does.
            "mean_difference": _round(
                _mean([row.model_probability - row.market_probability for row in priced])),
            "model_brier": _round(model_brier, 6),
            "market_brier": _round(market_brier, 6),
            # Positive means the model beat the posted price as a forecast.
            "brier_improvement": improvement,
        },
        "realized": {
            "sample_size": len(judged),
            "priced_side_win_rate": _round(_mean([row.outcome for row in judged])),
            "model_mean_probability": _round(_mean([row.model_probability for row in judged])),
        },
        # The question a disagreement is actually making: when we claimed an edge of this size,
        # how often did the side we preferred win?
        "edge_buckets": _edge_buckets(priced),
        "status": _status(len(priced), improvement),
    }


def _edge_buckets(rows: list[DerivedMarketObservation]) -> list[dict[str, Any]]:
    buckets = []
    for low, high in EDGE_BUCKETS:
        selected = [row for row in rows
                    if low <= abs(row.model_probability - row.market_probability) < high]
        if not selected:
            buckets.append({"edge_low": low, "edge_high": high, "sample_size": 0})
            continue
        # Our preferred side, which is the priced side when we rate it above the book.
        hits = [row.outcome if row.model_probability > row.market_probability else 1 - row.outcome
                for row in selected]
        expected = [max(row.model_probability, 1 - row.model_probability) for row in selected]
        buckets.append({
            "edge_low": low, "edge_high": high, "sample_size": len(selected),
            "picked_side_win_rate": _round(_mean(hits)),
            "model_expected_win_rate": _round(_mean(expected)),
        })
    return buckets


def _status(sample_size: int, improvement: float | None) -> dict[str, Any]:
    if sample_size < MIN_DERIVED_MARKET_SAMPLES:
        return {"state": "COLLECTING", "reason": "INSUFFICIENT_PRICED_FINALS",
                "sample_size": sample_size, "minimum_samples": MIN_DERIVED_MARKET_SAMPLES}
    if improvement is None or improvement <= DERIVED_MARKET_GUARDRAIL["brier_improvement"]:
        # Measured and not yet better than the closing price, so no correction is fitted from it.
        return {"state": "HOLD", "reason": "NO_BRIER_IMPROVEMENT_OVER_MARKET",
                "sample_size": sample_size, "brier_improvement": improvement}
    return {"state": "READY", "reason": "BEATS_MARKET_ON_BRIER",
            "sample_size": sample_size, "brier_improvement": improvement}


def _side_outcome(value: float, line: float | None) -> float | None:
    """1 when the value clears the line, 0 when it falls short, None on an exact push."""
    if line is None:
        return None
    if value > line:
        return 1.0
    if value < line:
        return 0.0
    return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _prefer(candidate: Prediction, current: Prediction) -> bool:
    """A genuine pregame forecast outranks a replay; otherwise the later one wins."""
    if candidate.origin != current.origin:
        return candidate.origin == "LIVE_PREGAME"
    return _naive(candidate.created_at) > _naive(current.created_at)


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
