from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.models import Game


CONTEXT_POLICY_VERSION = 1


def prediction_context(session: Session, game: Game) -> dict[str, Any]:
    """Merge immutable official feed fields with leakage-safe schedule fatigue."""
    stored = dict(game.pregame_context or {})
    schedule = {
        "home": _schedule_side(session, game, game.home_team_id),
        "away": _schedule_side(session, game, game.away_team_id),
    }
    return {**stored, "policy_version": CONTEXT_POLICY_VERSION, "schedule": schedule,
            "availability": {
                "weather": bool((stored.get("weather") or {}).get("available")),
                "bullpen": all(bool((stored.get("bullpen") or {}).get(side, {}).get("available"))
                                for side in ("home", "away")),
                "schedule": True,
            }}


def _schedule_side(session: Session, game: Game, team_id: int) -> dict[str, Any]:
    cutoff = game.start_at or datetime.combine(game.game_date, game.start_time or datetime.min.time())
    prior = session.scalars(select(Game).where(
        Game.id != game.id,
        Game.status == "FINAL",
        or_(Game.home_team_id == team_id, Game.away_team_id == team_id),
        or_(Game.start_at < cutoff, Game.start_at.is_(None) & (Game.game_date < game.game_date)),
    ).order_by(Game.game_date.desc(), Game.start_at.desc()).limit(10)).all()
    games_3d = sum(1 for row in prior if game.game_date - row.game_date <= timedelta(days=3))
    games_7d = sum(1 for row in prior if game.game_date - row.game_date <= timedelta(days=7))
    played_dates = {row.game_date for row in prior}
    consecutive = 0
    cursor = game.game_date - timedelta(days=1)
    while cursor in played_dates:
        consecutive += 1; cursor -= timedelta(days=1)
    previous = prior[0] if prior else None
    hours_since = None
    if previous and previous.start_at and game.start_at:
        hours_since = max(0.0, (_naive(game.start_at) - _naive(previous.start_at)).total_seconds() / 3600)
    travel_km = _travel(previous, game)
    fatigue = max(0.0, consecutive - 2) * .12 + max(0, games_3d - 3) * .12
    if hours_since is not None and hours_since < 22:
        fatigue += (22 - hours_since) / 22 * .25
    if travel_km is not None:
        fatigue += min(.20, travel_km / 6000)
    return {
        "games_last_3d": games_3d, "games_last_7d": games_7d,
        "consecutive_days": consecutive, "hours_since_previous_start": round(hours_since, 1) if hours_since is not None else None,
        "travel_km": round(travel_km, 1) if travel_km is not None else None,
        "fatigue_index": round(min(1.0, fatigue), 4),
        "travel_available": travel_km is not None,
        "method": "prior finalized schedule only; no same-game result data",
    }


def _travel(previous: Game | None, current: Game) -> float | None:
    if previous is None:
        return None
    left = (previous.pregame_context or {}).get("venue") or {}
    right = (current.pregame_context or {}).get("venue") or {}
    coords = (left.get("latitude"), left.get("longitude"), right.get("latitude"), right.get("longitude"))
    if any(value is None for value in coords):
        return None
    lat1, lon1, lat2, lon2 = (math.radians(float(value)) for value in coords)
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(value))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
