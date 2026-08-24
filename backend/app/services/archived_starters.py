"""Backfill archived game starters with the record they had before that game.

The replay refuses any observation collected after first pitch, which is the right leakage rule
but left the historical archive with no starter information at all: every starter feature was
constant across 1,571 training rows, so a standardized fit gave all of them a coefficient of
exactly zero and could not learn from them.

This closes that gap without weakening the rule. A starter's identity is announced days ahead
and is confirmed by the box score, so recording it for a finished game is pre-game information.
The rates stored beside it accumulate only that pitcher's appearances strictly BEFORE the game,
which is exactly what a forecaster standing at first pitch would have had.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.collectors.kbo import KboClient
from backend.app.collectors.mlb import MlbClient
from backend.app.config import KST
from backend.app.models import Game, GameStarter

# A quality start is six or more innings with three or fewer earned runs.
QUALITY_START_INNINGS = 6.0
QUALITY_START_EARNED_RUNS = 3


def backfill_archived_starters(session: Session, season: int, limit: int = 400,
                               league: str = "MLB", client: Any | None = None) -> dict[str, Any]:
    """Record starters for finished games that have none, newest first."""
    owned = client is None
    client = client or (MlbClient() if league == "MLB" else KboClient())
    try:
        pending = session.scalars(
            select(Game).where(
                Game.league == league, Game.status == "FINAL",
                Game.id.notin_(select(GameStarter.game_id)),
            ).order_by(Game.game_date.desc()).limit(limit)
        ).all()
        if not pending:
            return {"league": league, "season": season, "pending": 0, "written": 0, "games": 0}

        # MLB publishes a whole season of starters in one request; KBO only exposes them a day at
        # a time, so it is asked for exactly the dates this batch needs.
        if league == "MLB":
            feed = client.archived_starters(season)
        else:
            feed = client.archived_starters([game.venue_date or game.game_date for game in pending])
        by_game: dict[str, dict[str, dict[str, Any]]] = {}
        for row in feed.data:
            by_game.setdefault(row["external_id"], {})[row["side"]] = row

        wanted = {game.external_id: game for game in pending if game.external_id in by_game}
        if not wanted:
            return {"league": league, "season": season, "pending": len(pending), "written": 0,
                    "games": 0, "note": "일정 피드에 선발 정보가 없는 경기들입니다."}

        player_ids = sorted({row["player_id"]
                             for external_id in wanted
                             for row in by_game[external_id].values()})
        logs = client.pitcher_game_logs(player_ids, season)
        collected = datetime.now(KST)
        written = 0
        for external_id, game in wanted.items():
            # The venue-local date is the one a game log line is stamped with.
            boundary = (game.venue_date or game.game_date).isoformat()
            for side, row in by_game[external_id].items():
                totals = _totals_before(logs.data.get(row["player_id"], []), boundary)
                session.add(GameStarter(
                    game_id=game.id, side=side, player_id=row["player_id"], name=row.get("name"),
                    source=f"{league} official schedule + game log",
                    source_url=logs.source_url[:2000],
                    collected_at=collected, **totals))
                written += 1
        return {"league": league, "season": season, "pending": len(pending), "written": written,
                "games": len(wanted), "pitchers": len(player_ids)}
    finally:
        if owned:
            client.close()


def _totals_before(appearances: list[dict[str, Any]], boundary: str) -> dict[str, Any]:
    """Accumulate a pitcher's season only up to, and never including, the target game's date."""
    prior = [row for row in appearances if row["date"] < boundary]
    innings = sum(float(row["innings"]) for row in prior)
    return {
        "prior_games": len(prior),
        "prior_starts": sum(1 for row in prior if row["started"]),
        "prior_innings": round(innings, 2),
        "prior_earned_runs": sum(int(row["earned_runs"]) for row in prior),
        "prior_hits": sum(int(row["hits"]) for row in prior),
        "prior_walks": sum(int(row["walks"]) for row in prior),
        "prior_strikeouts": sum(int(row["strikeouts"]) for row in prior),
        "prior_home_runs": sum(int(row["home_runs"]) for row in prior),
        "prior_quality_starts": sum(
            1 for row in prior
            if row["started"] and float(row["innings"]) >= QUALITY_START_INNINGS
            and int(row["earned_runs"]) <= QUALITY_START_EARNED_RUNS
        ),
    }


def starter_view(record: GameStarter, league_era: float) -> Any:
    """Shape a stored archive starter like the live PitcherStat the feature builder expects.

    A pitcher with almost no prior work carries no usable rate, so those fields stay None and the
    feature builder falls back exactly as it does for a live game with a missing starter.
    """
    from types import SimpleNamespace

    innings = float(record.prior_innings or 0)
    thin = innings < 1.0
    era = None if thin else record.prior_earned_runs * 9 / innings
    whip = None if thin else (record.prior_hits + record.prior_walks) / innings
    # Defense-independent estimate on the same scale as ERA, matching the live collector.
    fip = None if thin else (
        (13 * record.prior_home_runs + 3 * record.prior_walks - 2 * record.prior_strikeouts) / innings + 3.1
    )
    return SimpleNamespace(
        player_id=record.player_id, name=record.name,
        # The starter was announced before first pitch, which is what "confirmed" means here.
        confirmed=True,
        era=era, whip=whip, fip=fip,
        war=None,
        games=record.prior_starts or record.prior_games,
        avg_start_innings=(innings / record.prior_starts) if record.prior_starts else None,
        quality_starts=record.prior_quality_starts,
        k_bb_rate=(record.prior_strikeouts / record.prior_walks) if record.prior_walks else None,
        rest_days=None, recent_pitches=None, handedness=None,
        opponent_games=None, opponent_innings=None, opponent_era=None, opponent_whip=None,
        recent=None,
    )
