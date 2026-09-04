from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
import time
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, Session

from backend.app.collectors.kbo import KboClient
from backend.app.collectors.mlb import MlbClient
from backend.app.collectors.odds import OddsClient
from backend.app.config import KST, settings
from backend.app.database import SessionLocal, database_now, init_db, session_scope
from backend.app.models import (CrawlLog, Game, GameResult, LineupEntry, MarketConsensus,
                                MarketSnapshot, PitcherStat, Prediction, Team)
from backend.app.repositories.repository import (
    fresh_batter_split_ids,
    latest_team_stat,
    load_batter_splits,
    replace_lineups,
    save_prediction,
    team_stats_fresh,
    upsert_batter_splits,
    upsert_game,
    upsert_games_bulk,
    upsert_market_consensus,
    upsert_pitcher,
    upsert_team,
    upsert_team_stat,
)
from backend.app.services.prediction import predict_game
from backend.app.services.batting import build_batter_table, league_average_table, lineup_tables
from backend.app.services.bullpen import load_profiles, seed_league
from backend.app.services.model_lifecycle import load_champion_runtime
from backend.app.services.prediction_evaluation import evaluate_pending_predictions
from backend.app.services.team_strength import TeamStrengthHistory
from backend.app.services.prediction_history_cache import load_prediction_histories
from backend.app.services.operations import job_lock
from backend.app.services.pregame_context import prediction_context


def discover_schedule(league: str, target_date: date) -> dict[str, Any]:
    """Store one KST slate without running enrichments or simulations."""
    init_db()
    client = KboClient() if league == "KBO" else MlbClient()
    errors: list[str] = []
    try:
        source = _tracked(
            f"{league.lower()}_games", "/ws/Main.asmx/GetKboGameList" if league == "KBO" else "/api/v1/schedule",
            lambda: client.games(target_date), errors,
        )
        rows = source.data if source else []
        if source:
            with session_scope() as session:
                for raw in rows:
                    upsert_game(session, raw, source.source_url, source.collected_at, league)
        return {
            "league": league, "date": target_date.isoformat(), "games": len(rows),
            "scheduled": sum(row.get("status") == "SCHEDULED" for row in rows),
            "errors": errors,
        }
    finally:
        client.close()


def refresh_kbo(target_date: date, force: bool = False, client: KboClient | None = None,
                game_ids: set[str] | None = None, trigger: str = "manual",
                checkpoint_stage: str | None = None, include_inning_backfill: bool = True,
                include_lineups: bool = True, only_changed: bool = False) -> dict[str, Any]:
    init_db()
    own_client = client is None
    client = client or KboClient()
    errors: list[str] = []
    fetched_games: list[dict[str, Any]] = []
    split_budget = _split_budget()
    try:
        games_source = _tracked("kbo_games", "/ws/Main.asmx/GetKboGameList", lambda: client.games(target_date), errors)
        if games_source:
            fetched_games = games_source.data
            with session_scope() as session:
                for raw in fetched_games:
                    upsert_game(session, raw, games_source.source_url, games_source.collected_at, "KBO")
            scheduled = [row for row in fetched_games if row.get("status") == "SCHEDULED"
                         and (not game_ids or row["external_id"] in game_ids)]
            context_source = _tracked(
                "kbo_pregame_context", "/ws/Schedule.asmx/GetTodayGames + GetBoxScoreScroll",
                lambda: client.slate_context(target_date, scheduled), errors,
            ) if scheduled else None
            if context_source:
                with session_scope() as session:
                    for external_id, context in context_source.data.items():
                        stored_game = session.scalar(select(Game).where(Game.external_id == external_id))
                        if stored_game:
                            stored_game.pregame_context = context
                            stored_game.context_collected_at = context_source.collected_at

        with session_scope() as session:
            fresh = team_stats_fresh(session, target_date, "KBO")
        if force or not fresh:
            stats_source = _tracked("kbo_team_stats", "/Record/Team", client.team_stats, errors)
            schedule_sources = []
            for month in range(1, 13):
                payload = _tracked(
                    f"kbo_schedule_{target_date.year}{month:02d}", "/ws/Schedule.asmx/GetScheduleList",
                    lambda m=month: client.monthly_schedule(target_date.year, m), errors,
                )
                if payload:
                    schedule_sources.append(payload)
            season_rows = [row for source in schedule_sources for row in source.data]
            history_rows = [row for row in season_rows if row.get("away_score") is not None and row.get("home_score") is not None]
            if season_rows:
                with session_scope() as session:
                    upsert_games_bulk(
                        session, season_rows, ", ".join(source.source_url for source in schedule_sources),
                        max(source.collected_at for source in schedule_sources), "KBO",
                    )
            if stats_source:
                recent = _recent_by_team(history_rows, target_date)
                with session_scope() as session:
                    for name, raw in stats_source.data.items():
                        team = upsert_team(session, "KBO", raw["code"], name)
                        upsert_team_stat(session, team, target_date, raw, recent.get(name, {}), stats_source.source_url, stats_source.collected_at)

        now = datetime.now(KST)
        if fetched_games:
            for raw in fetched_games:
                if game_ids and raw["external_id"] not in game_ids:
                    continue
                if raw["status"] != "SCHEDULED":
                    # Live polling only needs the authoritative state/result feed. Pregame
                    # pitcher, lineup and split enrichment stops at first pitch.
                    continue
                starter_source = _tracked(
                    f"kbo_starters_{raw['external_id']}", "/ws/Schedule.asmx/GetPitcherRecordAnalysis",
                    lambda item=raw: client.starter_stats(item), errors,
                )
                if not starter_source:
                    continue
                with session_scope() as session:
                    game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
                    if game:
                        for pitcher in starter_source.data:
                            upsert_pitcher(session, game, pitcher, starter_source.source_url, starter_source.collected_at)
                minutes_to_start = (raw["start_at"].astimezone(KST) - now).total_seconds() / 60
                # KBO does not publish an official batting order the day before the game. Calling
                # every lineup and hitter-detail endpoint anyway is the slowest part of a manual
                # refresh and can exhaust Vercel's full 300-second request limit. The dedicated
                # T-40 dispatcher supplies game_ids, so it always enters this branch when lineups
                # are expected; an ordinary page refresh enters only inside the live-data window.
                should_fetch_lineup = include_lineups and (
                    bool(game_ids) or -30 <= minutes_to_start <= settings.live_update_window_minutes
                )
                if should_fetch_lineup:
                    lineup_source = _tracked(
                        f"kbo_lineups_{raw['external_id']}", "/ws/Schedule.asmx/GetLineUpAnalysis",
                        lambda item=raw: client.lineups(item), errors,
                    )
                    if lineup_source and lineup_source.data:
                        _enrich_batter_matchups(client, raw, lineup_source.data, errors, "KBO")
                        _resolve_kbo_player_ids(client, lineup_source.data, raw, errors)
                        _enrich_kbo_platoon(client, raw, lineup_source.data, errors)
                        _collect_batter_splits(client, lineup_source.data, target_date.year, "KBO", errors, split_budget)
                        with session_scope() as session:
                            game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
                            if game:
                                replace_lineups(session, game, lineup_source.data, lineup_source.source_url, lineup_source.collected_at)

        # A pre-game refresh must not spend its request budget fetching completed-game innings.
        # That backfill can make a user-triggered latest-data request wait long enough to time
        # out even though none of its work changes an upcoming game's forecast.
        inning_backfill = (backfill_kbo_innings(10, target_date=target_date, client=client)
                           if include_inning_backfill else {"status": "skipped", "reason": "pregame refresh"})
        market_status = _refresh_market("KBO", errors)
        predicted = _predict_games(
            "KBO", target_date, game_ids, errors, trigger, checkpoint_stage,
            only_changed=only_changed,
        )
        with session_scope() as session:
            evaluations = evaluate_pending_predictions(session, "KBO", target_date)
        return {"date": target_date.isoformat(), "games": len(fetched_games) or predicted, "predictions": predicted,
                "evaluations": evaluations, "inning_results": inning_backfill,
                "market": market_status, "errors": errors,
                "used_cached_team_stats": fresh and not force}
    finally:
        if own_client:
            client.close()


def refresh_mlb(target_date: date, force: bool = False, client: MlbClient | None = None,
                game_ids: set[str] | None = None, trigger: str = "manual",
                checkpoint_stage: str | None = None, include_lineups: bool = True,
                only_changed: bool = False) -> dict[str, Any]:
    init_db()
    own_client = client is None
    client = client or MlbClient()
    errors: list[str] = []
    fetched_games: list[dict[str, Any]] = []
    split_budget = _split_budget()
    try:
        games_source = _tracked("mlb_games", "/api/v1/schedule", lambda: client.games(target_date), errors)
        if games_source:
            fetched_games = games_source.data
            with session_scope() as session:
                for raw in fetched_games:
                    upsert_game(session, raw, games_source.source_url, games_source.collected_at, "MLB")
            # Every per-game worker needs the same weather/bullpen slate. Serialize this one
            # provider collection by league/date and write all scheduled games at once; workers
            # arriving behind the lock reuse that exact committed snapshot.
            all_scheduled = [row for row in fetched_games if row.get("status") == "SCHEDULED"]
            _refresh_mlb_slate_context(client, target_date, all_scheduled, errors)
        with session_scope() as session:
            fresh = team_stats_fresh(session, target_date, "MLB")
        if force or not fresh:
            stats_source = _tracked("mlb_team_stats", "/api/v1/standings + /teams/{id}/stats", lambda: client.team_stats(target_date.year), errors)
            season_source = _tracked("mlb_season_schedule", "/api/v1/schedule", lambda: client.season_games(target_date.year), errors)
            season_rows = season_source.data if season_source else []
            if season_source:
                with session_scope() as session:
                    upsert_games_bulk(session, season_rows, season_source.source_url,
                                      season_source.collected_at, "MLB")
            if stats_source:
                completed_rows = [row for row in season_rows if row.get("away_score") is not None and row.get("home_score") is not None]
                recent = _recent_by_team(completed_rows, target_date)
                with session_scope() as session:
                    for name, raw in stats_source.data.items():
                        team = upsert_team(session, "MLB", raw["code"], name)
                        upsert_team_stat(session, team, target_date, raw, recent.get(name, {}), stats_source.source_url, stats_source.collected_at)

        now = datetime.now(KST)
        for raw in fetched_games:
            if game_ids and raw["external_id"] not in game_ids:
                continue
            if raw["status"] != "SCHEDULED":
                continue
            starter_source = _tracked(
                f"mlb_starters_{raw['external_id']}", "/api/v1/people/{id}/stats",
                lambda item=raw: client.starter_stats(item), errors,
            )
            if starter_source:
                with session_scope() as session:
                    game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
                    if game:
                        for pitcher in starter_source.data:
                            upsert_pitcher(session, game, pitcher, starter_source.source_url, starter_source.collected_at)
            minutes_to_start = (raw["start_at"].astimezone(KST) - now).total_seconds() / 60
            should_fetch_lineup = include_lineups and (
                bool(game_ids) or raw["status"] == "LIVE"
                or -30 <= minutes_to_start <= settings.live_update_window_minutes
            )
            if should_fetch_lineup:
                lineup_source = _tracked(
                    f"mlb_lineups_{raw['external_id']}", "/api/v1.1/game/{gamePk}/feed/live",
                    lambda item=raw: client.lineups(item), errors,
                )
                if lineup_source and lineup_source.data:
                    _enrich_batter_matchups(client, raw, lineup_source.data, errors, "MLB")
                    _enrich_mlb_platoon(client, raw, lineup_source.data, errors)
                    _enrich_mlb_statcast(client, raw, lineup_source.data, errors)
                    _collect_batter_splits(client, lineup_source.data, target_date.year, "MLB", errors, split_budget)
                    with session_scope() as session:
                        game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
                        if game:
                            replace_lineups(session, game, lineup_source.data, lineup_source.source_url, lineup_source.collected_at)
        market_status = _refresh_market("MLB", errors)
        predicted = _predict_games(
            "MLB", target_date, game_ids, errors, trigger, checkpoint_stage,
            only_changed=only_changed,
        )
        with session_scope() as session:
            evaluations = evaluate_pending_predictions(session, "MLB", target_date)
        return {"date": target_date.isoformat(), "league": "MLB", "games": len(fetched_games) or predicted,
                "predictions": predicted, "evaluations": evaluations,
                "market": market_status, "errors": errors,
                "used_cached_team_stats": fresh and not force}
    finally:
        if own_client:
            client.close()


def refresh_mlb_starters(target_date: date) -> dict[str, Any]:
    """Refresh probable starters and regenerate affected MLB forecasts without full enrichment."""
    init_db()
    client = MlbClient()
    errors: list[str] = []
    updated_ids: set[str] = set()
    try:
        games_source = _tracked("mlb_games", "/api/v1/schedule", lambda: client.games(target_date), errors)
        games = games_source.data if games_source else []
        if games_source:
            with session_scope() as session:
                for raw in games:
                    upsert_game(session, raw, games_source.source_url, games_source.collected_at, "MLB")
        scheduled_ids = {
            raw["external_id"] for raw in games if raw.get("status") == "SCHEDULED"
        }
        for raw in games:
            if raw.get("status") != "SCHEDULED":
                continue
            starter_source = _tracked(
                f"mlb_starters_{raw['external_id']}", "/api/v1/people/{id}/stats",
                lambda item=raw: client.starter_stats(item), errors,
            )
            if not starter_source:
                continue
            with session_scope() as session:
                game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
                if game:
                    for pitcher in starter_source.data:
                        upsert_pitcher(session, game, pitcher, starter_source.source_url,
                                       starter_source.collected_at)
                    updated_ids.add(raw["external_id"])
        # Refresh the once-daily market reference before regenerating forecasts, so the saved
        # card compares its new representative score with the current line. Market data remains
        # excluded from all score and probability inputs.
        market_status = _refresh_market("MLB", errors)
        predicted = _predict_games(
            "MLB", target_date, scheduled_ids or None, errors, "supabase_mlb_21_starters",
            None, only_changed=True,
        ) if scheduled_ids else 0
        return {
            "league": "MLB", "date": target_date.isoformat(), "scope": "starters",
            "games": len(games), "starters_updated_games": len(updated_ids),
            "predictions": predicted, "market": market_status, "errors": errors,
        }
    finally:
        client.close()


MLB_SLATE_CONTEXT_BURST_TTL = timedelta(seconds=60)


def _refresh_mlb_slate_context(client: MlbClient, target_date: date,
                               scheduled: list[dict[str, Any]], errors: list[str]) -> None:
    """Collect one immutable MLB common-input snapshot for a concurrent refresh burst."""
    if not scheduled:
        return
    external_ids = [row["external_id"] for row in scheduled]
    with job_lock(f"mlb-slate-context:{target_date.isoformat()}", blocking=True):
        cutoff = database_now() - MLB_SLATE_CONTEXT_BURST_TTL
        with SessionLocal() as session:
            collected = dict(session.execute(select(Game.external_id, Game.context_collected_at).where(
                Game.external_id.in_(external_ids),
            )).all())
        if len(collected) == len(external_ids) and all(
            value is not None and _comparable_datetime(value, cutoff) >= cutoff for value in collected.values()
        ):
            return
        context_source = _tracked(
            "mlb_pregame_context", "/api/v1.1/game/{gamePk}/feed/live + prior box scores",
            lambda: client.slate_context(target_date, scheduled), errors,
        )
        if not context_source:
            return
        with session_scope() as session:
            stored = {
                game.external_id: game
                for game in session.scalars(select(Game).where(Game.external_id.in_(external_ids))).all()
            }
            for external_id, context in context_source.data.items():
                if external_id in stored:
                    stored[external_id].pregame_context = context
                    stored[external_id].context_collected_at = context_source.collected_at


def _comparable_datetime(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def refresh_all(target_date: date, force: bool = False, trigger: str = "manual") -> dict[str, Any]:
    kbo = refresh_kbo(target_date, force=force, trigger=trigger)
    mlb = refresh_mlb(target_date, force=force, trigger=trigger)
    return {"date": target_date.isoformat(), "leagues": {"KBO": kbo, "MLB": mlb},
            "games": kbo["games"] + mlb["games"], "predictions": kbo["predictions"] + mlb["predictions"],
            "errors": kbo["errors"] + mlb["errors"]}


def predict_stored_games(league: str, target_date: date, *, trigger: str = "stored_prediction",
                         game_ids: set[str] | None = None) -> dict[str, Any]:
    """Create forecasts from data already committed to the database, without network collection.

    This deliberately runs separately from the slower MLB starter/stat enrichment path. It gives
    every scheduled game a usable baseline forecast even when an upstream provider or serverless
    worker reaches its request deadline; later per-game refreshes append a fresher forecast.
    """
    init_db()
    started = datetime.now(KST)
    errors: list[str] = []
    with SessionLocal() as session:
        query = select(Game.external_id).where(
            Game.league == league, Game.game_date == target_date, Game.status == "SCHEDULED",
        ).order_by(Game.start_at, Game.external_id)
        if game_ids:
            query = query.where(Game.external_id.in_(game_ids))
        scheduled_ids = list(session.scalars(query).all())

    # Load the expensive residual/calibration histories once for the slate. `_predict_games`
    # commits each completed game independently, so a later failure or request timeout cannot
    # roll back predictions that have already finished.
    completed_ids: set[str] = set()
    try:
        predicted = _predict_games(
            league, target_date, set(scheduled_ids), errors, trigger,
            completed_game_ids=completed_ids,
        )
    except Exception as exc:
        # The normal path shares one history snapshot across the slate. If a single fixture has
        # malformed inputs, retry only the unfinished fixtures in isolated sessions so that game
        # cannot prevent the remainder from being forecast.
        errors.append(f"shared prediction pass: {type(exc).__name__}: {exc}")
        predicted = len(completed_ids)
        for external_id in scheduled_ids:
            if external_id in completed_ids:
                continue
            try:
                predicted += _predict_games(
                    league, target_date, {external_id}, errors, trigger,
                    completed_game_ids=completed_ids,
                )
            except Exception as game_exc:
                errors.append(f"{external_id}: {type(game_exc).__name__}: {game_exc}")

    with session_scope() as session:
        evaluate_pending_predictions(session, league, target_date)
    failed = bool(scheduled_ids) and predicted == 0
    _log(
        f"{league.lower()}_stored_prediction",
        "FAILED" if failed else "SUCCESS",
        "database://stored-pregame-inputs",
        started,
        "; ".join(errors)[:4000] if errors else None,
    )
    return {
        "date": target_date.isoformat(),
        "league": league,
        "scheduled": len(scheduled_ids),
        "predictions": predicted,
        "errors": errors,
        "source": "STORED_DATA",
    }


def _predict_games(league: str, target_date: date, game_ids: set[str] | None, errors: list[str], trigger: str,
                   checkpoint_stage: str | None = None, *, only_changed: bool = False,
                   completed_game_ids: set[str] | None = None) -> int:
    predicted = 0
    residual_history, probability_history, market_offset_history = load_prediction_histories(league)
    with session_scope() as session:
        query = select(Game).options(joinedload(Game.home_team), joinedload(Game.away_team)).where(
            Game.game_date == target_date, Game.league == league, Game.status == "SCHEDULED"
        )
        if game_ids:
            query = query.where(Game.external_id.in_(game_ids))
        games = session.scalars(query).all()
        model_runtime = load_champion_runtime(session, league)
        strength_history = TeamStrengthHistory.from_session(session, league)
        # Seed any team that has no profile yet, then read them all back, so a bullpen update
        # made since the last refresh reaches this slate's predictions. Both are optional
        # enrichments: if their tables are not migrated yet the slate still gets predictions.
        bullpen_profiles = _optional(
            lambda: (seed_league(session, league, target_date), load_profiles(session, league))[1],
            {}, "bullpen profiles", errors)
        all_day_games = session.scalars(select(Game).where(Game.game_date == target_date, Game.league == league)).all()
        team_ids = {team_id for day_game in all_day_games for team_id in (day_game.home_team_id, day_game.away_team_id)}
        stats_by_team = {team_id: latest_team_stat(session, team_id, target_date) for team_id in team_ids}
        environment_stats = [stat for stat in stats_by_team.values() if stat and stat.games and
                             stat.runs_per_game is not None and stat.runs_allowed_per_game is not None]
        environment_weight = sum(2 * stat.games for stat in environment_stats)
        league_average_runs = (
            sum(stat.games * (stat.runs_per_game + stat.runs_allowed_per_game) for stat in environment_stats) /
            environment_weight
        ) if environment_weight else (5.15 if league == "KBO" else 4.45)
        appearances: dict[int, int] = defaultdict(int)
        for day_game in all_day_games:
            appearances[day_game.home_team_id] += 1
            appearances[day_game.away_team_id] += 1
        for game in games:
            home = stats_by_team.get(game.home_team_id)
            away = stats_by_team.get(game.away_team_id)
            if not home or not away:
                errors.append(f"{game.external_id}: 팀 통계 부족으로 예측 생략")
                continue
            pitchers = session.scalars(select(PitcherStat).where(PitcherStat.game_id == game.id)).all()
            lineups = session.scalars(select(LineupEntry).where(LineupEntry.game_id == game.id).order_by(LineupEntry.side, LineupEntry.batting_order)).all()
            by_side = {p.side: p for p in pitchers}
            lineup_table_data = _optional(
                lambda: _lineup_split_tables(session, game, league, target_date.year, lineups),
                {}, "batter splits", errors)
            lineups_confirmed = len(lineups) >= 18 and all(item.confirmed for item in lineups)
            scoring_seed = sum(float(value or league_average_runs) for value in (
                home.runs_per_game, away.runs_per_game, home.runs_allowed_per_game, away.runs_allowed_per_game,
            )) / 2
            regime = {
                "engine": "PLATE_APPEARANCE" if lineup_table_data.get("home") is not None and lineup_table_data.get("away") is not None else "INNING_RATE",
                "confirmation": "CONFIRMED" if lineups_confirmed and len(pitchers) >= 2 and all(p.confirmed for p in pitchers) else "PARTIAL",
                "scoring_band": "LOW" if scoring_seed < 8.5 else ("HIGH" if scoring_seed > 10.5 else "MID"),
                "season_phase": "EARLY" if target_date.month <= 5 else ("LATE" if target_date.month >= 8 else "MID"),
            }
            market = session.scalar(select(MarketConsensus).where(
                MarketConsensus.game_id == game.id,
            ).order_by(MarketConsensus.collected_at.desc()).limit(1))
            market_context = ({
                "total_line": market.total_line,
                "home_spread": market.home_spread,
                # Priced only in `raw`: neither derived market has a dedicated price column, and
                # their de-vigged prices are what the second-stage reads compare themselves against.
                "home_spread_probability": (market.raw or {}).get("home_spread_probability"),
                "total_over_probability": (market.raw or {}).get("total_over_probability"),
                "home_implied_probability": market.home_implied_probability,
                "away_implied_probability": market.away_implied_probability,
                "home_decimal_odds": (market.raw or {}).get("home_decimal_odds"),
                "away_decimal_odds": (market.raw or {}).get("away_decimal_odds"),
                "bookmaker_count": market.bookmaker_count,
                "provider": market.provider,
                "collected_at": market.collected_at.isoformat(),
            } if market and (game.start_at is None or
                             market.collected_at.replace(tzinfo=None) <= game.start_at.replace(tzinfo=None)) else {})
            context = {
                "home_games_today": appearances[game.home_team_id],
                "away_games_today": appearances[game.away_team_id],
                "league_average_runs": league_average_runs,
                "team_residuals": residual_history.context_for(game, regime),
                "team_strength": strength_history.context_for(game),
                "probability_calibration": probability_history.context_for(game),
                "market_offset": market_offset_history.context_for(game),
                "pregame": prediction_context(session, game),
                "market": market_context,
            }
            captured_at = datetime.now(KST)
            latest_input_hash = session.scalar(select(Prediction.input_hash).where(
                Prediction.game_id == game.id,
                Prediction.origin == "LIVE_PREGAME",
            ).order_by(Prediction.created_at.desc()).limit(1)) if only_changed else None
            result = predict_game(
                game, home, away, by_side.get("home"), by_side.get("away"), lineups, context,
                model_runtime=model_runtime,
                bullpens={"home": bullpen_profiles.get(game.home_team_id),
                          "away": bullpen_profiles.get(game.away_team_id)},
                lineup_tables=lineup_table_data,
                known_input_hash=latest_input_hash,
            )
            if result.get("unchanged"):
                continue
            save_prediction(
                session, game, result, stage=checkpoint_stage or _prediction_stage(game, captured_at),
                trigger=trigger, captured_at=captured_at,
            )
            # Persist progress before moving to the next fixture. Supabase's async HTTP worker
            # can terminate the request at 290 seconds; without this checkpoint the surrounding
            # transaction rolled the entire slate back even after several forecasts completed.
            session.commit()
            if completed_game_ids is not None:
                completed_game_ids.add(game.external_id)
            predicted += 1
    return predicted


def _prediction_stage(game: Game, captured_at: datetime) -> str:
    if game.start_at is None:
        return "TIME_UNCONFIRMED"
    start_at = game.start_at if game.start_at.tzinfo else game.start_at.replace(tzinfo=KST)
    captured = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=KST)
    minutes = (start_at - captured).total_seconds() / 60
    if minutes > 12 * 60:
        return "T_MINUS_24H"
    if minutes > 120:
        return "T_MINUS_3H"
    if minutes > 35:
        return "T_MINUS_60M"
    return "T_MINUS_15M"


def _tracked(name: str, source_url: str, operation: Callable[[], Any], errors: list[str]):
    started = datetime.now(KST)
    last_error: Exception | None = None
    for attempt in range(1, settings.retry_attempts + 1):
        try:
            result = operation()
            _log(name, "SUCCESS", getattr(result, "source_url", source_url), started, None)
            return result
        except Exception as exc:  # One source must not stop the entire refresh.
            last_error = exc
            if attempt < settings.retry_attempts:
                time.sleep(settings.retry_base_seconds * (2 ** (attempt - 1)))
    assert last_error is not None
    message = f"{name}: {type(last_error).__name__}: {last_error} ({settings.retry_attempts}회 시도)"
    errors.append(message)
    _log(name, "FAILED", source_url, started, message)
    return None


def _log(collector: str, status: str, source_url: str, started_at: datetime, error: str | None) -> None:
    with session_scope() as session:
        session.add(CrawlLog(collector=collector, status=status, source_url=source_url,
                             started_at=started_at, finished_at=datetime.now(KST), error=error))


def _months_for_recent(target: date, days: int) -> list[tuple[int, int]]:
    cursor = target - timedelta(days=days)
    months = []
    while (cursor.year, cursor.month) <= (target.year, target.month):
        months.append((cursor.year, cursor.month))
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return months


MARKET_REFRESH_HOURS_KST = {"KBO": 12, "MLB": 21}
MARKET_CHECKPOINT_LOOKAHEAD_HOURS = 36


def _market_refresh_due(league: str, latest: datetime | None, now: datetime) -> bool:
    """Return true once per league-specific KST daily market slot, with missed-run catch-up."""
    scheduled = now.replace(hour=MARKET_REFRESH_HOURS_KST[league], minute=0, second=0, microsecond=0)
    if now < scheduled:
        scheduled -= timedelta(days=1)
    return latest is None or latest < scheduled


def _market_stage_started_at(game: Game, stage: str) -> datetime:
    offsets = {
        "T_MINUS_24H": timedelta(hours=MARKET_CHECKPOINT_LOOKAHEAD_HOURS),
        "T_MINUS_3H": timedelta(hours=12),
        "T_MINUS_60M": timedelta(minutes=120),
        "T_MINUS_15M": timedelta(minutes=35),
    }
    return game.start_at - offsets[stage]


def _market_checkpoint_due(session: Session, league: str, now: datetime,
                           latest: datetime | None = None) -> bool:
    """Whether any upcoming game lacks the quote checkpoint appropriate for `now`.

    This query is deliberately local and cheap.  Cron may ask frequently, but an external odds
    request is made only four times per game horizon at most: T-24h, T-3h, T-60m and T-15m.
    """
    cutoff = now + timedelta(hours=MARKET_CHECKPOINT_LOOKAHEAD_HOURS)
    games = session.scalars(select(Game).where(
        Game.league == league, Game.status == "SCHEDULED", Game.start_at.is_not(None),
        Game.start_at > now, Game.start_at <= cutoff,
    )).all()
    for game in games:
        stage = _prediction_stage(game, now)
        snapshots = session.scalars(select(MarketSnapshot).where(
            MarketSnapshot.game_id == game.id,
        )).all()
        has_snapshot = any((row.raw or {}).get("snapshot_stage") == stage for row in snapshots)
        # A successful league-wide provider request also counts as an attempt for this stage.
        # Otherwise an unavailable or unmatched event leaves no MarketSnapshot and the ten-minute
        # cron spends credits on the same missing quote for the entire stage.
        attempted_stage = latest is not None and _comparable_datetime(
            latest, _market_stage_started_at(game, stage),
        ) >= _market_stage_started_at(game, stage)
        if not has_snapshot and not attempted_stage:
            return True
    return False


def _odds_request_credit_cost(league: str) -> int:
    """Conservative normal-call estimate kept for operations/backward compatibility."""
    return 3


def _odds_credits_used_today(session: Session, now: datetime) -> int:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = session.execute(select(CrawlLog.collector, func.count()).where(
        CrawlLog.collector.in_(("kbo_market", "mlb_market")),
        # Old The Odds API checkpoint logs used the same collector names. Counting those rows
        # as API-Sports HTTP requests can instantly report hundreds of requests on migration day.
        CrawlLog.source_url.like("%api-sports.io%"),
        CrawlLog.finished_at >= day_start,
    ).group_by(CrawlLog.collector)).all()
    return sum(int(count) for _, count in rows)


def _market_event_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(KST).date()
    except ValueError:
        return None


def refresh_market(league: str, *, force: bool = False) -> dict[str, Any]:
    """Run the daily structured-market collector without refreshing baseball sources."""
    init_db()
    errors: list[str] = []
    result = _refresh_market(league, errors, force=force)
    return {"league": league, "collector": "market", **result, "errors": errors}


def _refresh_market(league: str, errors: list[str], *, force: bool = False) -> dict[str, Any]:
    if not settings.api_sports_key:
        return {"status": "disabled", "matched_games": 0}
    # Full-slate refreshes run in parallel workers. Serialize the shared paid provider across
    # both leagues, then re-check due state and budget after acquiring the lock.
    with job_lock("api-sports-odds-provider", blocking=True):
        return _refresh_market_locked(league, errors, force=force)


def _refresh_market_locked(league: str, errors: list[str], *, force: bool = False) -> dict[str, Any]:
    collector = f"{league.lower()}_market"
    now = database_now()
    with session_scope() as session:
        # CrawlLog gates provider calls even when no event matches a locally stored game.
        latest = session.scalar(select(func.max(CrawlLog.finished_at)).where(
            CrawlLog.collector == collector, CrawlLog.status == "SUCCESS",
            CrawlLog.source_url.like("%api-sports.io%"),
        ))
        used_today = _odds_credits_used_today(session, now)
        upcoming_games = session.scalars(select(Game).where(
            Game.league == league, Game.status == "SCHEDULED", Game.start_at.is_not(None),
            Game.start_at > now, Game.start_at <= now + timedelta(hours=MARKET_CHECKPOINT_LOOKAHEAD_HOURS),
        )).all()
        target_dates = sorted({game.game_date for game in upcoming_games})
    # API-Sports publishes pre-match odds once per day, so four intraday checkpoint requests
    # cannot reveal fresher provider data. One successful league collection per daily slot is
    # sufficient; frequent cron dispatches stop here locally.
    if not force and not _market_refresh_due(league, latest, now):
        return {"status": "already_collected", "matched_games": 0}
    if not target_dates:
        return {"status": "no_upcoming_games", "matched_games": 0}
    estimated_cost = len(target_dates) + len({day.year for day in target_dates})
    if used_today + estimated_cost > settings.api_sports_daily_request_budget:
        return {
            "status": "daily_request_budget_reached", "matched_games": 0,
            "requests_used_today": used_today,
            "daily_request_budget": settings.api_sports_daily_request_budget,
        }
    client = OddsClient()
    try:
        started = datetime.now(KST)
        try:
            source = client.consensus(league, target_dates)
        except Exception as exc:
            message = f"{collector}: {type(exc).__name__}: {exc} (1회 시도)"
            errors.append(message)
            for _ in range(max(1, client.request_count)):
                _log(collector, "FAILED", "https://v1.baseball.api-sports.io", started, message)
            return {"status": "failed", "matched_games": 0}
        for _ in range(max(1, client.request_count)):
            _log(collector, "SUCCESS", source.source_url, started, None)
        matched_games = 0
        matched_game_ids: list[str] = []
        with session_scope() as session:
            games = session.scalars(select(Game).options(
                joinedload(Game.home_team), joinedload(Game.away_team),
            ).where(
                Game.league == league, Game.status == "SCHEDULED", Game.start_at.is_not(None),
                Game.start_at > now,
                Game.start_at <= now + timedelta(hours=MARKET_CHECKPOINT_LOOKAHEAD_HOURS),
            )).all()
            by_matchup = {
                (game.game_date, _team_key(game.away_team.name), _team_key(game.home_team.name)): game
                for game in games
            }
            for row in source.data:
                game = by_matchup.get((
                    _market_event_date(row.get("commence_time")),
                    _team_key(row["away_name"]),
                    _team_key(row["home_name"]),
                ))
                if game:
                    upsert_market_consensus(session, game, row, source.source_url, source.collected_at)
                    matched_games += 1
                    matched_game_ids.append(game.external_id)
        return {
            "status": "collected",
            "provider_events": len(source.data),
            "matched_games": matched_games,
            "matched_game_ids": matched_game_ids,
            "provider_usage": client.last_usage,
            "requests_this_collection": client.request_count,
            "requests_used_today": used_today + client.request_count,
            "daily_request_budget": settings.api_sports_daily_request_budget,
        }
    finally:
        client.close()


def _team_key(name: str) -> str:
    aliases = {
        "kiatigers": "kia", "kiwoomheroes": "키움", "lottegiants": "롯데", "doosanbears": "두산",
        "ktwiz": "kt", "ssglanders": "ssg", "samsunglions": "삼성", "ncdinos": "nc",
        "lgtwins": "lg", "hanwhaeagles": "한화",
    }
    compact = "".join(character.lower() for character in name if character.isalnum())
    return aliases.get(compact, compact)


# Backfilling every hitter's splits in one refresh does not fit a serverless invocation, so a
# run spends only part of its budget on it and later runs finish the job. Until a club is fully
# covered the inning-rate engine handles it, which is a degraded forecast rather than a failed one.
SPLIT_FETCH_BUDGET = 40
SPLIT_DEADLINE_SECONDS = 120
BATTER_SPLIT_REFRESH_HOURS = 24


def _split_budget() -> dict[str, Any]:
    return {"remaining": SPLIT_FETCH_BUDGET, "deadline": time.monotonic() + SPLIT_DEADLINE_SECONDS}


def backfill_batter_splits(league: str, target_date: date) -> dict[str, Any]:
    """Fetch base-state splits for today's lineup hitters, outside the full refresh.

    The full refresh cannot afford to backfill hundreds of hitters inside one serverless
    invocation, so this runs on its own schedule and works through the queue over several runs.
    """
    init_db()
    errors: list[str] = []
    budget = _split_budget()
    with session_scope() as session:
        entries = [
            {"player_id": row.player_id}
            for row in session.scalars(
                select(LineupEntry).join(Game, Game.id == LineupEntry.game_id)
                .where(Game.league == league, Game.game_date == target_date,
                       LineupEntry.player_id.is_not(None))
            ).all()
        ]
    if not entries:
        return {"league": league, "scope": "splits", "hitters": 0, "fetched": 0, "errors": errors}
    client = KboClient() if league == "KBO" else MlbClient()
    try:
        _collect_batter_splits(client, entries, target_date.year, league, errors, budget)
    finally:
        client.close()
    return {"league": league, "scope": "splits", "hitters": len(entries),
            "fetched": SPLIT_FETCH_BUDGET - budget["remaining"], "errors": errors}


def backfill_kbo_innings(limit: int = 10, target_date: date | None = None,
                         client: KboClient | None = None) -> dict[str, Any]:
    """Fill missing KBO inning lines from the official GameCenter scoreboard."""
    init_db()
    own_client = client is None
    client = client or KboClient()
    query = select(Game.external_id, Game.game_date).join(
        GameResult, GameResult.game_id == Game.id,
    ).where(Game.league == "KBO", GameResult.innings.is_(None))
    if target_date:
        query = query.where(Game.game_date == target_date)
    with SessionLocal() as session:
        targets = session.execute(query.order_by(Game.game_date.desc()).limit(limit)).all()
    written = 0
    errors: list[str] = []
    try:
        for external_id, game_date in targets:
            try:
                source = client.score_innings(external_id, game_date.year)
                if not source.data:
                    continue
                with session_scope() as session:
                    game = session.scalar(select(Game).where(Game.external_id == external_id))
                    result = session.get(GameResult, game.id) if game else None
                    if result and result.innings is None:
                        result.innings = source.data
                        result.source_url = source.source_url
                        written += 1
            except Exception as exc:
                errors.append(f"{external_id}: {type(exc).__name__}: {exc}")
    finally:
        if own_client:
            client.close()
    return {"requested": len(targets), "written": written, "errors": errors}


def backfill_mlb_innings(limit: int = 50, client: MlbClient | None = None) -> dict[str, Any]:
    """Fill missing MLB inning lines from the official season schedule linescore."""
    init_db()
    own_client = client is None
    client = client or MlbClient()
    query = select(Game.external_id, Game.game_date).join(
        GameResult, GameResult.game_id == Game.id,
    ).where(Game.league == "MLB", GameResult.innings.is_(None))
    with SessionLocal() as session:
        targets = session.execute(query.order_by(Game.game_date.desc()).limit(limit)).all()
    if not targets:
        if own_client:
            client.close()
        return {"requested": 0, "written": 0, "errors": []}

    target_ids = {external_id for external_id, _ in targets}
    official: dict[str, tuple[dict[str, Any], str]] = {}
    errors: list[str] = []
    try:
        for season in sorted({game_date.year for _, game_date in targets}):
            try:
                source = client.season_games(season)
                official.update({
                    row["external_id"]: (row["innings"], source.source_url)
                    for row in source.data
                    if row["external_id"] in target_ids and row.get("innings")
                })
            except Exception as exc:
                errors.append(f"{season}: {type(exc).__name__}: {exc}")
        unresolved = [external_id.removeprefix("MLB-") for external_id in target_ids - set(official)]
        if unresolved:
            try:
                source = client.inning_lines(unresolved)
                official.update({
                    external_id: (innings, source.source_url)
                    for external_id, innings in source.data.items()
                })
            except Exception as exc:
                errors.append(f"individual feeds: {type(exc).__name__}: {exc}")
    finally:
        if own_client:
            client.close()

    written = 0
    if official:
        with session_scope() as session:
            games = {
                game.external_id: game
                for game in session.scalars(select(Game).where(Game.external_id.in_(list(official)))).all()
            }
            for external_id, (innings, source_url) in official.items():
                game = games.get(external_id)
                result = session.get(GameResult, game.id) if game else None
                if result and result.innings is None:
                    result.innings = innings
                    result.source_url = source_url
                    written += 1
    return {"requested": len(targets), "written": written, "errors": errors}


def _optional(action: Callable[[], Any], default: Any, label: str, errors: list[str]) -> Any:
    """Run an optional enrichment, degrading instead of taking the whole refresh down.

    Bullpen profiles and batter splits live in tables added after the core schema. Until a
    deployment has run those migrations the queries fail, and a slate with no predictions is
    far worse than a slate predicted by the inning-rate model.
    """
    try:
        return action()
    except SQLAlchemyError as exc:
        errors.append(f"{label} 사용 불가(마이그레이션 대기 중일 수 있음): {type(exc).__name__}")
        return default


def _resolve_kbo_player_ids(client: KboClient, entries: list[dict[str, Any]], game: dict[str, Any],
                            errors: list[str]) -> None:
    """Fill KBO lineup entries' player ids from the official hitter directory.

    The KBO lineup feed carries names only. The record pages link every hitter's official id,
    so names are resolved against the two clubs actually playing; an unresolved or ambiguous
    name simply stays id-less and that hitter is not modelled individually.
    """
    if all(entry.get("player_id") for entry in entries):
        return
    team_codes = [str(game.get("away_code")), str(game.get("home_code"))]
    source = _tracked(
        f"kbo_hitter_directory_{game['external_id']}", "/Record/Player/HitterBasic/Basic1.aspx",
        lambda: client.hitter_directory(team_codes), errors,
    )
    if not source or not source.data:
        return
    by_side = {"away": source.data.get(team_codes[0], {}), "home": source.data.get(team_codes[1], {})}
    for entry in entries:
        if not entry.get("player_id"):
            entry["player_id"] = by_side.get(str(entry.get("side")), {}).get(
                str(entry.get("player_name", "")).strip())


def _collect_batter_splits(client: KboClient | MlbClient, entries: list[dict[str, Any]],
                           season: int, league: str, errors: list[str],
                           budget: dict[str, Any] | None = None) -> None:
    """Fetch missing or day-old base-state splits for lineup hitters.

    One request per hitter is required. Fresh rows are reused throughout the day, while the
    twice-hourly split cron and pregame refreshes replace records once they are 24 hours old.
    """
    player_ids = {str(entry["player_id"]) for entry in entries if entry.get("player_id")}
    if not player_ids:
        return

    def fresh_ids() -> set[str]:
        with session_scope() as session:
            return fresh_batter_split_ids(
                session, league, season, sorted(player_ids),
                max_age=timedelta(hours=BATTER_SPLIT_REFRESH_HOURS),
            )

    fresh = _optional(fresh_ids, None, "batter splits", errors)
    if fresh is None:
        return
    due = sorted(player_ids - fresh)
    if not due:
        return
    if budget is not None:
        if budget["remaining"] <= 0 or time.monotonic() > budget["deadline"]:
            return
        due = due[:budget["remaining"]]
        budget["remaining"] -= len(due)
    source = _tracked(
        f"{league.lower()}_batter_splits",
        "/Record/Player/HitterDetail/Situation.aspx" if league == "KBO"
        else "/api/v1/people/{id}/stats?stats=statSplits",
        lambda: client.batter_splits(due, season), errors,
    )
    if not source or not source.data:
        return

    def store() -> bool:
        with session_scope() as session:
            upsert_batter_splits(session, league, season, source.data, source.source_url, source.collected_at)
        return True

    _optional(store, False, "batter splits", errors)


def _lineup_split_tables(session: Session, game: Game, league: str, season: int,
                         lineups: list[LineupEntry]) -> dict[str, Any]:
    """Build the per-club hitter tables the plate-appearance engine needs.

    A club is only handed to the engine after both official batting orders are confirmed and
    every one of its nine slots resolves to a hitter with collected splits or, failing that, to
    the average of the hitters this game does have. Projected orders stay on the inning engine.
    """
    confirmed = {"home": [], "away": []}
    for entry in lineups:
        if entry.side in confirmed:
            confirmed[entry.side].append(bool(entry.confirmed))
    if any(len(values) < 9 or not all(values[:9]) for values in confirmed.values()):
        return {}
    by_side: dict[str, list[dict[str, Any]]] = {"home": [], "away": []}
    for entry in lineups:
        if entry.side in by_side and len(by_side[entry.side]) < 9:
            by_side[entry.side].append({"player_id": entry.player_id})
    if any(len(rows) < 9 for rows in by_side.values()):
        return {}
    player_ids = [str(row["player_id"]) for rows in by_side.values() for row in rows if row["player_id"]]
    splits = load_batter_splits(session, league, season, player_ids)
    known = [table for table in (build_batter_table(value) for value in splits.values()) if table is not None]
    fallback = league_average_table(known)
    tables: dict[str, Any] = {}
    coverage: dict[str, int] = {}
    for side, rows in by_side.items():
        built = lineup_tables(rows, splits, fallback)
        if built is None:
            return {}
        tables[side], coverage[side] = built
    # Half a lineup of stand-ins is not a plate-appearance model; fall back to the inning engine.
    if min(coverage.values()) < 5:
        return {}
    return {**tables, "coverage": coverage}


def _enrich_batter_matchups(client: KboClient | MlbClient, game: dict[str, Any],
                             entries: list[dict[str, Any]], errors: list[str], league: str) -> None:
    """Reuse stored BvP pairs and fetch only new confirmed batter/pitcher combinations."""
    if not entries or not all(game.get(f"{side}_pitcher_id") for side in ("away", "home")):
        return
    if league == "KBO" and not all(bool(entry.get("confirmed")) for entry in entries):
        # Expected KBO lineups can change materially; collect BvP only after the official lineup is confirmed.
        return
    for entry in entries:
        entry["opponent_pitcher_id"] = str(game["home_pitcher_id" if entry["side"] == "away" else "away_pitcher_id"])
    pitcher_ids = [str(game["away_pitcher_id"]), str(game["home_pitcher_id"])]
    cache_hours = 12 if league == "KBO" else 24 * 7
    cache_threshold = database_now() - timedelta(hours=cache_hours)
    with session_scope() as session:
        cached_rows = session.scalars(select(LineupEntry).where(
            LineupEntry.opponent_pitcher_id.in_(pitcher_ids),
            LineupEntry.matchup_plate_appearances.is_not(None),
            LineupEntry.collected_at >= cache_threshold,
        )).all()
    stat_fields = (
        "matchup_plate_appearances", "matchup_at_bats", "matchup_hits", "matchup_doubles",
        "matchup_triples", "matchup_home_runs", "matchup_walks", "matchup_hit_by_pitch",
        "matchup_strikeouts", "matchup_avg", "matchup_obp", "matchup_slg", "matchup_ops",
    )

    def cached_payload(row: LineupEntry) -> dict[str, Any]:
        return {
            "player_id": row.player_id, "player_name": row.player_name,
            "opponent_pitcher_id": row.opponent_pitcher_id,
            "matchup_source_url": row.source_url,
            **{field: getattr(row, field) for field in stat_fields},
        }

    cached_by_id = {(str(row.player_id), str(row.opponent_pitcher_id)): cached_payload(row)
                    for row in cached_rows if row.player_id}
    cached_by_name = {(row.player_name.strip(), str(row.opponent_pitcher_id)): cached_payload(row)
                      for row in cached_rows if row.player_name}
    missing: list[dict[str, Any]] = []
    for entry in entries:
        pitcher_id = str(entry.get("opponent_pitcher_id"))
        id_key = (str(entry.get("player_id")), pitcher_id)
        name_key = (str(entry.get("player_name", "")).strip(), pitcher_id)
        cached = cached_by_id.get(id_key) if entry.get("player_id") else None
        cached = cached or cached_by_name.get(name_key)
        if cached:
            entry.update(cached)
        elif entry.get("player_id") or (league == "KBO" and name_key[0]):
            missing.append(entry)
    if not missing:
        return
    source = _tracked(
        f"{league.lower()}_batter_vs_pitcher_{game['external_id']}",
        "/Record/Etc/HitVsPit.aspx" if league == "KBO" else "/api/v1/people/{id}/stats?stats=vsPlayer",
        lambda: client.batter_vs_pitcher(missing, game), errors,
    )
    if not source:
        return
    by_id = {(str(stats.get("player_id")), str(stats.get("opponent_pitcher_id"))): stats
             for stats in source.data.values() if stats.get("player_id")}
    by_name = {(str(stats.get("player_name", "")).strip(), str(stats.get("opponent_pitcher_id"))): stats
               for stats in source.data.values() if stats.get("player_name")}
    for entry in missing:
        pitcher_id = str(entry["opponent_pitcher_id"])
        stats = by_id.get((str(entry.get("player_id")), pitcher_id)) if entry.get("player_id") else None
        stats = stats or by_name.get((str(entry.get("player_name", "")).strip(), pitcher_id))
        if stats:
            entry.update(stats)
            entry["matchup_source_url"] = source.source_url


def _enrich_mlb_platoon(client: MlbClient, game: dict[str, Any], entries: list[dict[str, Any]],
                        errors: list[str]) -> None:
    """Attach only official, current-season batter splits versus the probable starter hand."""
    source = _tracked(
        f"mlb_platoon_{game['external_id']}", "/api/v1/people/{id}/stats?stats=statSplits&sitCodes=vl,vr",
        lambda: client.batter_platoon(entries, game), errors,
    )
    if not source:
        return
    for entry in entries:
        stats = source.data.get(str(entry.get("player_id")))
        if stats:
            entry.update(stats)
            existing = entry.get("matchup_source_url")
            entry["matchup_source_url"] = f"{existing}, {source.source_url}" if existing else source.source_url


def _enrich_kbo_platoon(client: KboClient, game: dict[str, Any], entries: list[dict[str, Any]],
                        errors: list[str]) -> None:
    """Attach official current-season KBO splits versus the starter's throwing hand."""
    source = _tracked(
        f"kbo_platoon_{game['external_id']}", "/Record/Player/HitterDetail/Situation.aspx",
        lambda: client.batter_platoon(entries, game), errors,
    )
    if not source:
        return
    for entry in entries:
        stats = source.data.get(str(entry.get("player_id")))
        if stats:
            entry.update(stats)
            existing = entry.get("matchup_source_url")
            entry["matchup_source_url"] = f"{existing}, {source.source_url}" if existing else source.source_url


def _enrich_mlb_statcast(client: MlbClient, game: dict[str, Any], entries: list[dict[str, Any]],
                         errors: list[str]) -> None:
    """Attach official expected hitting, pitch-type matchup, defense and catcher context."""
    source = _tracked(
        f"mlb_statcast_lineup_{game['external_id']}",
        "https://baseballsavant.mlb.com/leaderboard",
        lambda: client.statcast_lineup_context(entries, game), errors,
    )
    if not source:
        for entry in entries:
            entry.setdefault("advanced", {"available": False, "reason": "STATCAST_COLLECTION_FAILED"})
        return
    for entry in entries:
        player_id = str(entry.get("player_id") or "")
        entry["advanced"] = source.data.get(player_id, {"available": False, "reason": "PLAYER_NOT_MATCHED"})
        existing = entry.get("matchup_source_url")
        entry["matchup_source_url"] = f"{existing}, {source.source_url}" if existing else source.source_url


def _recent_by_team(results: list[dict[str, Any]], target: date) -> dict[str, dict[str, Any]]:
    logs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for game in sorted((g for g in results if g["game_date"] < target), key=lambda g: g["game_date"]):
        away_score, home_score = game["away_score"], game["home_score"]
        logs[game["away_name"]].append({"date": game["game_date"].isoformat(), "opponent": game["home_name"], "runs": away_score, "allowed": home_score,
                                          "result": "W" if away_score > home_score else ("D" if away_score == home_score else "L")})
        logs[game["home_name"]].append({"date": game["game_date"].isoformat(), "opponent": game["away_name"], "runs": home_score, "allowed": away_score,
                                          "result": "W" if home_score > away_score else ("D" if home_score == away_score else "L")})
    output = {}
    for team, games in logs.items():
        windows = {}
        for size in (5, 10, 20):
            sample = games[-size:]
            n = len(sample)
            wins = sum(g["result"] == "W" for g in sample)
            draws = sum(g["result"] == "D" for g in sample)
            windows[str(size)] = {
                "games": n, "wins": wins, "draws": draws,
                "win_rate": round((wins + .5 * draws) / n, 4) if n else .5,
                "avg_runs": round(sum(g["runs"] for g in sample) / n, 3) if n else None,
                "avg_runs_allowed": round(sum(g["allowed"] for g in sample) / n, 3) if n else None,
                "games_detail": sample,
            }
        matchups: dict[str, dict[str, Any]] = {}
        opponents = {game["opponent"] for game in games}
        for opponent in opponents:
            sample = [game for game in games if game["opponent"] == opponent]
            n = len(sample)
            wins = sum(game["result"] == "W" for game in sample)
            draws = sum(game["result"] == "D" for game in sample)
            matchups[opponent] = {
                "games": n,
                "wins": wins,
                "draws": draws,
                "win_rate": round((wins + .5 * draws) / n, 4),
                "avg_runs": round(sum(game["runs"] for game in sample) / n, 3),
                "avg_runs_allowed": round(sum(game["allowed"] for game in sample) / n, 3),
                "avg_run_diff": round(sum(game["runs"] - game["allowed"] for game in sample) / n, 3),
            }
        windows["matchups"] = matchups
        output[team] = windows
    return output
