from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select

from backend.app.config import KST
from backend.app.database import SessionLocal, database_now, session_scope
from backend.app.models import CrawlLog, Game, LineupEntry, PredictionSnapshot
from backend.app.services.operations import job_lock
from backend.app.services.refresh import (backfill_batter_splits, backfill_kbo_innings,
                                          backfill_mlb_innings, refresh_kbo, refresh_market,
                                          refresh_mlb, refresh_mlb_starters, discover_schedule,
                                          predict_stored_games)
from backend.app.services.model_lifecycle import run_model_lifecycle
from backend.app.services.historical_replay import run_historical_replay
from backend.app.services.prediction_evaluation import evaluate_pending_predictions


RefreshOperation = Callable[..., dict[str, Any]]
CHECKPOINTS = {
    "T_MINUS_40M": 40,
}
CHECKPOINT_TOLERANCE_MINUTES = 2.5
REPLAY_START_DATE = date(2026, 1, 1)
REPLAY_END_DATE = date(2026, 12, 31)
LIVE_REFRESH_LOOKBACK = timedelta(hours=6)
LIVE_REFRESH_LOOKAHEAD = timedelta(hours=3)
LINEUP_RETRY_LOOKBACK = timedelta(minutes=5)
LINEUP_RETRY_LOOKAHEAD = timedelta(minutes=90)
LINEUP_RETRY_COOLDOWN = timedelta(minutes=10)


def checkpoint_stage_for_minutes(minutes_to_start: float) -> str | None:
    for stage, target in CHECKPOINTS.items():
        if abs(minutes_to_start - target) <= CHECKPOINT_TOLERANCE_MINUTES:
            return stage
    return None


def _operation(league: str) -> RefreshOperation:
    return refresh_kbo if league == "KBO" else refresh_mlb


def _missing_leagues_for_date(session, target_date: date) -> set[str]:
    stored = set(session.scalars(select(Game.league).where(Game.game_date == target_date)).all())
    return {"KBO", "MLB"} - stored


def run_full_refresh(league: str, target_date: date | None = None, *, force: bool = False,
                     trigger: str = "supabase_cron", include_inning_backfill: bool = True,
                     game_ids: set[str] | None = None, include_lineups: bool = True,
                     only_changed: bool = False) -> dict[str, Any]:
    target_date = target_date or datetime.now(KST).date()
    lock_scope = ",".join(sorted(game_ids)) if game_ids else "all"
    with job_lock(f"refresh:{league}:{target_date.isoformat()}:{lock_scope}"):
        if league == "KBO":
            return refresh_kbo(
                target_date, force=force, trigger=trigger,
                include_inning_backfill=include_inning_backfill,
                game_ids=game_ids, include_lineups=include_lineups,
                only_changed=only_changed,
            )
        return refresh_mlb(
            target_date, force=force, trigger=trigger, game_ids=game_ids,
            include_lineups=include_lineups, only_changed=only_changed,
        )


def run_nearby_refresh(league: str) -> dict[str, Any]:
    """Synchronize authoritative game states without rerunning pregame enrichment.

    This path runs every five minutes while games are nearby.  Keeping it schedule-only makes
    live polling cheap and prevents a status check from repeatedly fetching starters, lineups,
    batter splits and Monte Carlo forecasts.
    """
    now = database_now()
    with SessionLocal() as session:
        games = session.scalars(select(Game).where(
            Game.league == league,
            Game.status.in_(("SCHEDULED", "LIVE")),
            # Keep polling throughout a normal game, and retain enough headroom for long
            # extra-inning or delayed starts. Stale SCHEDULED rows stay eligible so the same
            # job can discover that first pitch already happened.
            Game.start_at >= now - LIVE_REFRESH_LOOKBACK,
            Game.start_at <= now + LIVE_REFRESH_LOOKAHEAD,
        )).all()
    grouped: dict[date, set[str]] = {}
    for game in games:
        grouped.setdefault(game.game_date, set()).add(game.external_id)
    results = []
    for game_date, ids in sorted(grouped.items()):
        with job_lock(f"live-state:{league}:{game_date.isoformat()}"):
            result = discover_schedule(league, game_date)
            with session_scope() as session:
                evaluations = evaluate_pending_predictions(session, league, game_date)
            results.append({**result, "requested_games": len(ids), "evaluations": evaluations})
    return {"league": league, "scope": "nearby", "matched_games": len(games), "runs": results}


def run_tomorrow_discovery(league: str) -> dict[str, Any]:
    target_date = datetime.now(KST).date() + timedelta(days=1)
    return run_full_refresh(league, target_date, trigger="supabase_tomorrow_discovery")


def run_market_refresh(league: str) -> dict[str, Any]:
    slot_date = datetime.now(KST).date()
    with job_lock(f"market:{league}:{slot_date.isoformat()}"):
        return refresh_market(league)


def run_checkpoint_refresh(league: str) -> dict[str, Any]:
    """Capture T-40 forecasts and recover lineups missed by the one-shot checkpoint."""
    now = database_now()
    with SessionLocal() as session:
        games = session.scalars(select(Game).where(
            Game.league == league,
            Game.status == "SCHEDULED",
            Game.start_at.is_not(None),
            Game.start_at >= now - LINEUP_RETRY_LOOKBACK,
            Game.start_at <= now + LINEUP_RETRY_LOOKAHEAD,
        )).all()
        game_ids = [game.id for game in games]
        captured = set(session.execute(select(PredictionSnapshot.game_id, PredictionSnapshot.stage).where(
            PredictionSnapshot.game_id.in_(game_ids),
            PredictionSnapshot.trigger == "checkpoint_exact",
        )).all()) if game_ids else set()
        lineup_counts = dict(session.execute(
            select(LineupEntry.game_id, func.count(LineupEntry.id)).where(
                LineupEntry.game_id.in_(game_ids), LineupEntry.confirmed.is_(True),
            ).group_by(LineupEntry.game_id)
        ).all()) if game_ids else {}
        collectors = {game.id: f"{league.lower()}_lineups_{game.external_id}" for game in games}
        latest_attempts = dict(session.execute(
            select(CrawlLog.collector, func.max(CrawlLog.finished_at)).where(
                CrawlLog.collector.in_(list(collectors.values()))
            ).group_by(CrawlLog.collector)
        ).all()) if collectors else {}
    grouped: dict[tuple[str, date], set[str]] = {}
    retry_grouped: dict[date, set[str]] = {}
    for game in games:
        minutes = (game.start_at - now).total_seconds() / 60
        stage = checkpoint_stage_for_minutes(minutes)
        if stage and (game.id, stage) not in captured:
            grouped.setdefault((stage, game.game_date), set()).add(game.external_id)
            continue
        last_attempt = latest_attempts.get(collectors[game.id])
        if _lineup_retry_needed(minutes, lineup_counts.get(game.id, 0), last_attempt, now):
            retry_grouped.setdefault(game.game_date, set()).add(game.external_id)
    results = []
    for (stage, game_date), ids in sorted(grouped.items()):
        with job_lock(f"checkpoint:{league}:{stage}:{game_date.isoformat()}"):
            results.append(_operation(league)(
                game_date, game_ids=ids, trigger="checkpoint_exact", checkpoint_stage=stage,
            ))
    for game_date, ids in sorted(retry_grouped.items()):
        with job_lock(f"lineup-retry:{league}:{game_date.isoformat()}"):
            results.append(_operation(league)(
                game_date, game_ids=ids, trigger="pregame_lineup_retry",
            ))
    return {
        "league": league, "scope": "checkpoints",
        "matched_games": sum(len(ids) for ids in grouped.values()) + sum(len(ids) for ids in retry_grouped.values()),
        "checkpoint_games": sum(len(ids) for ids in grouped.values()),
        "lineup_retry_games": sum(len(ids) for ids in retry_grouped.values()),
        "checkpoint_windows": CHECKPOINTS, "tolerance_minutes": CHECKPOINT_TOLERANCE_MINUTES, "runs": results,
    }


def _lineup_retry_needed(minutes_to_start: float, confirmed_entries: int,
                         last_attempt: datetime | None, now: datetime) -> bool:
    """Retry only near first pitch, with a cooldown after either success or failure."""
    if confirmed_entries >= 18 or not (-LINEUP_RETRY_LOOKBACK.total_seconds() / 60
                                       <= minutes_to_start
                                       <= LINEUP_RETRY_LOOKAHEAD.total_seconds() / 60):
        return False
    if last_attempt is None:
        return True
    if last_attempt.tzinfo is None and now.tzinfo is not None:
        last_attempt = last_attempt.replace(tzinfo=now.tzinfo)
    elif last_attempt.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=last_attempt.tzinfo)
    return now - last_attempt >= LINEUP_RETRY_COOLDOWN


def run_lifecycle_refresh(league: str) -> dict[str, Any]:
    with job_lock(f"model-lifecycle:{league}"):
        with session_scope() as session:
            return run_model_lifecycle(session, league)


def run_replay_refresh(league: str, limit: int = 10, *, only_missing: bool = False) -> dict[str, Any]:
    """Regenerate archive forecasts. Deliberately does only that.

    This used to also backfill inning-by-inning scores (an unbounded external-API loop) and
    retrain the model from scratch, every single call, inside the same lock — so a `limit=1`
    request paid for all three regardless of how little replay work it actually needed, and held
    the lock for however long the network calls happened to take. Both now run on their own cron
    scope (`innings`, `lifecycle`) at their own cadence instead of piggybacking on every replay.
    """
    with job_lock(f"historical-replay:{league}"):
        with session_scope() as session:
            replay = run_historical_replay(
                session, league,
                start_date=REPLAY_START_DATE,
                end_date=REPLAY_END_DATE,
                limit=limit,
                only_missing=only_missing,
            )
            return {
                **replay,
                "replay_window": {
                    "start": REPLAY_START_DATE.isoformat(),
                    "end": REPLAY_END_DATE.isoformat(),
                },
            }


def run_innings_backfill(league: str, limit: int = 50) -> dict[str, Any]:
    """Fill missing inning-by-inning scores from the official schedule/scoreboard.

    Split out of run_replay_refresh: this is an external-API loop with no bound on wall time,
    and coupling it to replay meant every replay batch paid for it regardless of `limit`.
    """
    with job_lock(f"innings-backfill:{league}"):
        return backfill_kbo_innings(limit) if league == "KBO" else backfill_mlb_innings(limit)


def run_cron_refresh(league: str, scope: str, *, target_date: date | None = None,
                     game_ids: set[str] | None = None, only_changed: bool = False) -> dict[str, Any]:
    if scope == "full":
        return run_full_refresh(league, target_date)
    if scope == "nearby":
        return run_nearby_refresh(league)
    if scope == "tomorrow":
        return run_tomorrow_discovery(league)
    if scope == "discover":
        if target_date is None:
            raise ValueError("discover scope requires target_date")
        with job_lock(f"discover:{league}:{target_date.isoformat()}"):
            return discover_schedule(league, target_date)
    if scope == "games":
        if target_date is None or not game_ids:
            raise ValueError("games scope requires target_date and game_ids")
        return run_full_refresh(
            league, target_date, trigger="supabase_changed_2h" if only_changed else "supabase_chunked_daily",
            include_inning_backfill=False, game_ids=game_ids, include_lineups=False,
            only_changed=only_changed,
        )
    if scope == "predict":
        if target_date is None:
            raise ValueError("predict scope requires target_date")
        with job_lock(f"predict:{league}:{target_date.isoformat()}"):
            return predict_stored_games(
                league, target_date, trigger="supabase_stored_prediction",
                game_ids=game_ids,
            )
    if scope == "market":
        return run_market_refresh(league)
    if scope == "starters":
        if league != "MLB" or target_date is None:
            raise ValueError("starters scope requires MLB and target_date")
        with job_lock(f"mlb-starters:{target_date.isoformat()}"):
            return refresh_mlb_starters(target_date)
    if scope == "checkpoints":
        return run_checkpoint_refresh(league)
    if scope == "lifecycle":
        return run_lifecycle_refresh(league)
    if scope == "replay":
        return run_replay_refresh(league)
    if scope == "innings":
        return run_innings_backfill(league)
    if scope == "splits":
        return backfill_batter_splits(league, datetime.now(KST).date())
    raise ValueError(f"Unsupported refresh scope: {scope}")
