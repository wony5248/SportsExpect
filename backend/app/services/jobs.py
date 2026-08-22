from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select

from backend.app.config import KST
from backend.app.database import SessionLocal, database_now
from backend.app.models import Game
from backend.app.services.operations import job_lock
from backend.app.services.refresh import refresh_kbo, refresh_mlb


RefreshOperation = Callable[..., dict[str, Any]]


def _operation(league: str) -> RefreshOperation:
    return refresh_kbo if league == "KBO" else refresh_mlb


def _missing_leagues_for_date(session, target_date: date) -> set[str]:
    stored = set(session.scalars(select(Game.league).where(Game.game_date == target_date)).all())
    return {"KBO", "MLB"} - stored


def run_full_refresh(league: str, target_date: date | None = None, *, force: bool = False,
                     trigger: str = "supabase_cron") -> dict[str, Any]:
    target_date = target_date or datetime.now(KST).date()
    with job_lock(f"refresh:{league}:{target_date.isoformat()}"):
        return _operation(league)(target_date, force=force, trigger=trigger)


def run_nearby_refresh(league: str) -> dict[str, Any]:
    now = database_now()
    with SessionLocal() as session:
        games = session.scalars(select(Game).where(
            Game.league == league,
            Game.status.in_(("SCHEDULED", "LIVE")),
            Game.start_at >= now - timedelta(minutes=30),
            Game.start_at <= now + timedelta(minutes=180),
        )).all()
    grouped: dict[date, set[str]] = {}
    for game in games:
        grouped.setdefault(game.game_date, set()).add(game.external_id)
    results = []
    for game_date, ids in sorted(grouped.items()):
        with job_lock(f"refresh:{league}:{game_date.isoformat()}"):
            results.append(_operation(league)(game_date, game_ids=ids, trigger="supabase_nearby_30m"))
    return {"league": league, "scope": "nearby", "matched_games": len(games), "runs": results}


def run_tomorrow_discovery(league: str) -> dict[str, Any]:
    target_date = datetime.now(KST).date() + timedelta(days=1)
    return run_full_refresh(league, target_date, trigger="supabase_tomorrow_discovery")


def run_cron_refresh(league: str, scope: str) -> dict[str, Any]:
    if scope == "full":
        return run_full_refresh(league)
    if scope == "nearby":
        return run_nearby_refresh(league)
    if scope == "tomorrow":
        return run_tomorrow_discovery(league)
    raise ValueError(f"Unsupported refresh scope: {scope}")
