from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
import time
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from backend.app.collectors.kbo import KboClient
from backend.app.collectors.mlb import MlbClient
from backend.app.collectors.odds import OddsClient
from backend.app.config import KST, settings
from backend.app.database import database_now, init_db, session_scope
from backend.app.models import CrawlLog, Game, LineupEntry, PitcherStat, Team
from backend.app.repositories.repository import (
    latest_team_stat,
    replace_lineups,
    save_prediction,
    team_stats_fresh,
    upsert_game,
    upsert_games_bulk,
    upsert_market_consensus,
    upsert_pitcher,
    upsert_team,
    upsert_team_stat,
)
from backend.app.services.prediction import predict_game
from backend.app.services.model_lifecycle import load_champion_runtime


def refresh_kbo(target_date: date, force: bool = False, client: KboClient | None = None,
                game_ids: set[str] | None = None, trigger: str = "manual",
                checkpoint_stage: str | None = None) -> dict[str, Any]:
    init_db()
    own_client = client is None
    client = client or KboClient()
    errors: list[str] = []
    fetched_games: list[dict[str, Any]] = []
    try:
        games_source = _tracked("kbo_games", "/ws/Main.asmx/GetKboGameList", lambda: client.games(target_date), errors)
        if games_source:
            fetched_games = games_source.data
            with session_scope() as session:
                for raw in fetched_games:
                    upsert_game(session, raw, games_source.source_url, games_source.collected_at, "KBO")

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

        if fetched_games:
            for raw in fetched_games:
                if game_ids and raw["external_id"] not in game_ids:
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
                lineup_source = _tracked(
                    f"kbo_lineups_{raw['external_id']}", "/ws/Schedule.asmx/GetLineUpAnalysis",
                    lambda item=raw: client.lineups(item), errors,
                )
                if lineup_source and lineup_source.data:
                    _enrich_batter_matchups(client, raw, lineup_source.data, errors, "KBO")
                    with session_scope() as session:
                        game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
                        if game:
                            replace_lineups(session, game, lineup_source.data, lineup_source.source_url, lineup_source.collected_at)

        _refresh_market("KBO", errors)
        predicted = _predict_games("KBO", target_date, game_ids, errors, trigger, checkpoint_stage)
        return {"date": target_date.isoformat(), "games": len(fetched_games) or predicted, "predictions": predicted, "errors": errors, "used_cached_team_stats": fresh and not force}
    finally:
        if own_client:
            client.close()


def refresh_mlb(target_date: date, force: bool = False, client: MlbClient | None = None,
                game_ids: set[str] | None = None, trigger: str = "manual",
                checkpoint_stage: str | None = None) -> dict[str, Any]:
    init_db()
    own_client = client is None
    client = client or MlbClient()
    errors: list[str] = []
    fetched_games: list[dict[str, Any]] = []
    try:
        games_source = _tracked("mlb_games", "/api/v1/schedule", lambda: client.games(target_date), errors)
        if games_source:
            fetched_games = games_source.data
            with session_scope() as session:
                for raw in fetched_games:
                    upsert_game(session, raw, games_source.source_url, games_source.collected_at, "MLB")
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
            should_fetch_lineup = bool(game_ids) or raw["status"] == "LIVE" or -30 <= minutes_to_start <= settings.live_update_window_minutes
            if should_fetch_lineup:
                lineup_source = _tracked(
                    f"mlb_lineups_{raw['external_id']}", "/api/v1.1/game/{gamePk}/feed/live",
                    lambda item=raw: client.lineups(item), errors,
                )
                if lineup_source and lineup_source.data:
                    _enrich_batter_matchups(client, raw, lineup_source.data, errors, "MLB")
                    with session_scope() as session:
                        game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
                        if game:
                            replace_lineups(session, game, lineup_source.data, lineup_source.source_url, lineup_source.collected_at)
        _refresh_market("MLB", errors)
        predicted = _predict_games("MLB", target_date, game_ids, errors, trigger, checkpoint_stage)
        return {"date": target_date.isoformat(), "league": "MLB", "games": len(fetched_games) or predicted,
                "predictions": predicted, "errors": errors, "used_cached_team_stats": fresh and not force}
    finally:
        if own_client:
            client.close()


def refresh_all(target_date: date, force: bool = False, trigger: str = "manual") -> dict[str, Any]:
    kbo = refresh_kbo(target_date, force=force, trigger=trigger)
    mlb = refresh_mlb(target_date, force=force, trigger=trigger)
    return {"date": target_date.isoformat(), "leagues": {"KBO": kbo, "MLB": mlb},
            "games": kbo["games"] + mlb["games"], "predictions": kbo["predictions"] + mlb["predictions"],
            "errors": kbo["errors"] + mlb["errors"]}


def _predict_games(league: str, target_date: date, game_ids: set[str] | None, errors: list[str], trigger: str,
                   checkpoint_stage: str | None = None) -> int:
    predicted = 0
    with session_scope() as session:
        query = select(Game).options(joinedload(Game.home_team), joinedload(Game.away_team)).where(
            Game.game_date == target_date, Game.league == league, Game.status == "SCHEDULED"
        )
        if game_ids:
            query = query.where(Game.external_id.in_(game_ids))
        games = session.scalars(query).all()
        model_runtime = load_champion_runtime(session, league)
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
            context = {
                "home_games_today": appearances[game.home_team_id],
                "away_games_today": appearances[game.away_team_id],
                "league_average_runs": league_average_runs,
            }
            captured_at = datetime.now(KST)
            result = predict_game(
                game, home, away, by_side.get("home"), by_side.get("away"), lineups, context,
                model_runtime=model_runtime,
            )
            save_prediction(
                session, game, result, stage=checkpoint_stage or _prediction_stage(game, captured_at),
                trigger=trigger, captured_at=captured_at,
            )
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


MARKET_REFRESH_HOURS_KST = {"KBO": 12, "MLB": 0}


def _market_refresh_due(league: str, latest: datetime | None, now: datetime) -> bool:
    """Return true once per league-specific KST daily market slot, with missed-run catch-up."""
    scheduled = now.replace(hour=MARKET_REFRESH_HOURS_KST[league], minute=0, second=0, microsecond=0)
    if now < scheduled:
        scheduled -= timedelta(days=1)
    return latest is None or latest < scheduled


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


def refresh_market(league: str) -> dict[str, Any]:
    """Run the daily structured-market collector without refreshing baseball sources."""
    init_db()
    errors: list[str] = []
    result = _refresh_market(league, errors)
    return {"league": league, "collector": "market", **result, "errors": errors}


def _refresh_market(league: str, errors: list[str]) -> dict[str, Any]:
    if not settings.odds_api_key:
        return {"status": "disabled", "matched_games": 0}
    collector = f"{league.lower()}_market"
    now = database_now()
    with session_scope() as session:
        # CrawlLog gates provider calls even when no event matches a locally stored game.
        latest = session.scalar(select(func.max(CrawlLog.finished_at)).where(
            CrawlLog.collector == collector, CrawlLog.status == "SUCCESS",
        ))
    if not _market_refresh_due(league, latest, now):
        return {"status": "already_collected", "matched_games": 0}
    client = OddsClient()
    try:
        source = _tracked(collector, "https://api.the-odds-api.com/v4/sports",
                          lambda: client.consensus(league), errors)
        if not source:
            return {"status": "failed", "matched_games": 0}
        matched_games = 0
        with session_scope() as session:
            games = session.scalars(select(Game).options(joinedload(Game.home_team), joinedload(Game.away_team)).where(
                Game.league == league, Game.status.in_(("SCHEDULED", "LIVE")),
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
        return {
            "status": "collected",
            "provider_events": len(source.data),
            "matched_games": matched_games,
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
