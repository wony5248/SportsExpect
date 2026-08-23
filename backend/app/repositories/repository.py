from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, joinedload

from backend.app.config import KST, settings
from backend.app.database import database_datetime, database_now
from backend.app.models import (BatterSplit, Game, GameResult, LineupEntry, MarketConsensus, MarketSnapshot,
                                ModelVersion, PitcherStat, Prediction, PredictionEvaluation, PredictionHistory, PredictionSnapshot,
                                Team, TeamStat)


def upsert_team(session: Session, league: str, code: str, name: str) -> Team:
    team = session.scalar(select(Team).where(Team.league == league, Team.code == code))
    if team is None:
        team = Team(league=league, code=code, name=name)
        session.add(team)
        session.flush()
    elif team.name != name:
        team.name = name
    return team


def upsert_game(session: Session, raw: dict[str, Any], source_url: str, collected_at: datetime, league: str = "KBO") -> Game:
    away = upsert_team(session, league, raw["away_code"], raw["away_name"])
    home = upsert_team(session, league, raw["home_code"], raw["home_name"])
    game = session.scalar(select(Game).where(Game.external_id == raw["external_id"]))
    start = time.fromisoformat(raw["start_time"]) if raw.get("start_time") else None
    start_at = raw.get("start_at")
    if start_at:
        start_at = database_datetime(start_at)
    values = dict(league=league, game_date=raw["game_date"], venue_date=raw.get("venue_date") or raw["game_date"],
                  start_time=start, start_at=start_at, away_team_id=away.id,
                  home_team_id=home.id, stadium=raw.get("stadium"), status=raw["status"],
                  source=f"{league} official game data", source_url=source_url, collected_at=collected_at)
    if game is None:
        game = Game(external_id=raw["external_id"], **values)
        session.add(game)
        session.flush()
    else:
        for key, value in values.items():
            setattr(game, key, value)
    if game.status == "CANCELLED":
        purge_cancelled_game_pregame_data(session, game, repair=True)
    if raw.get("status") == "FINAL" and raw.get("away_score") is not None and raw.get("home_score") is not None:
        result = session.get(GameResult, game.id)
        if result is None:
            result = GameResult(game_id=game.id, away_score=raw["away_score"], home_score=raw["home_score"],
                                innings=raw.get("innings"),
                                finalized_at=collected_at, source_url=source_url)
            session.add(result)
        else:
            away_score, home_score = int(raw["away_score"]), int(raw["home_score"])
            score_changed = result.away_score != away_score or result.home_score != home_score
            result.away_score, result.home_score = away_score, home_score
            if raw.get("innings") is not None:
                result.innings = raw["innings"]
            if score_changed:
                result.finalized_at = collected_at
            result.source_url = source_url
    return game


def upsert_games_bulk(session: Session, rows: list[dict[str, Any]], source_url: str,
                      collected_at: datetime, league: str) -> list[Game]:
    """Upsert a season schedule without thousands of database round trips."""
    if not rows:
        return []
    teams = {(team.code): team for team in session.scalars(select(Team).where(Team.league == league)).all()}
    for raw in rows:
        for side in ("away", "home"):
            code, name = str(raw[f"{side}_code"]), str(raw[f"{side}_name"])
            team = teams.get(code)
            if team is None:
                team = Team(league=league, code=code, name=name)
                session.add(team); teams[code] = team
            elif team.name != name:
                team.name = name
    session.flush()

    external_ids = [str(row["external_id"]) for row in rows]
    games: dict[str, Game] = {}
    for offset in range(0, len(external_ids), 500):
        chunk = external_ids[offset:offset + 500]
        games.update({
            game.external_id: game
            for game in session.scalars(select(Game).where(Game.external_id.in_(chunk))).all()
        })
    output: list[Game] = []
    for raw in rows:
        start = time.fromisoformat(raw["start_time"]) if raw.get("start_time") else None
        start_at = database_datetime(raw["start_at"]) if raw.get("start_at") else None
        values = {
            "league": league, "game_date": raw["game_date"],
            "venue_date": raw.get("venue_date") or raw["game_date"], "start_time": start,
            "start_at": start_at, "away_team_id": teams[str(raw["away_code"])].id,
            "home_team_id": teams[str(raw["home_code"])].id, "stadium": raw.get("stadium"),
            "status": raw["status"], "source": f"{league} official season schedule",
            "source_url": source_url, "collected_at": collected_at,
        }
        game = games.get(str(raw["external_id"]))
        if game is None:
            game = Game(external_id=str(raw["external_id"]), **values)
            session.add(game); games[game.external_id] = game
        else:
            # Season schedule pages are useful for archive discovery but are less authoritative
            # than the per-game feed. Never let a partial-score schedule row regress or finalize
            # a state already established by the live endpoint.
            if game.status in {"LIVE", "FINAL", "CANCELLED"}:
                values["status"] = game.status
            for key, value in values.items():
                setattr(game, key, value)
        output.append(game)
    session.flush()

    results: dict[int, GameResult] = {}
    game_ids = [game.id for game in output]
    for offset in range(0, len(game_ids), 500):
        chunk = game_ids[offset:offset + 500]
        results.update({
            result.game_id: result
            for result in session.scalars(select(GameResult).where(GameResult.game_id.in_(chunk))).all()
        })
    for raw, game in zip(rows, output, strict=True):
        if game.status == "CANCELLED":
            purge_cancelled_game_pregame_data(session, game, repair=True)
        if game.status != "FINAL" or raw.get("status") != "FINAL" or raw.get("away_score") is None or raw.get("home_score") is None:
            continue
        result = results.get(game.id)
        if result is None:
            result = GameResult(game_id=game.id, away_score=int(raw["away_score"]),
                                home_score=int(raw["home_score"]), finalized_at=collected_at,
                                innings=raw.get("innings"), source_url=source_url)
            session.add(result); results[game.id] = result
        else:
            score_changed = (result.away_score != int(raw["away_score"]) or
                             result.home_score != int(raw["home_score"]))
            result.away_score, result.home_score = int(raw["away_score"]), int(raw["home_score"])
            if raw.get("innings") is not None:
                result.innings = raw["innings"]
            if score_changed:
                result.finalized_at = collected_at
            result.source_url = source_url
    return output


def purge_cancelled_game_pregame_data(session: Session, game: Game, *, repair: bool) -> dict[str, Any]:
    """Keep a cancelled fixture, but invalidate/remove its stale pregame material.

    The schedule row remains useful for travel and doubleheader context.  Starter and lineup
    snapshots are not: a postponed fixture can be replayed the next day with the same pitcher,
    which otherwise looks like a duplicate player record.  Immutable prediction snapshots stay
    for audit, while their parent prediction is excluded from training and UI display.
    """
    if session.get(GameResult, game.id) is not None:
        return {"game_id": game.external_id, "skipped": "HAS_FINAL_RESULT"}
    pitcher_count = session.scalar(select(func.count(PitcherStat.id)).where(PitcherStat.game_id == game.id)) or 0
    lineup_count = session.scalar(select(func.count(LineupEntry.id)).where(LineupEntry.game_id == game.id)) or 0
    predictions = session.scalars(select(Prediction).where(Prediction.game_id == game.id)).all()
    report = {
        "game_id": game.external_id, "pitcher_rows": int(pitcher_count), "lineup_rows": int(lineup_count),
        "predictions_invalidated": sum(bool(row.training_eligible) for row in predictions),
    }
    if not repair:
        return report
    session.execute(delete(PitcherStat).where(PitcherStat.game_id == game.id))
    session.execute(delete(LineupEntry).where(LineupEntry.game_id == game.id))
    for prediction in predictions:
        if prediction.training_eligible:
            prediction.training_eligible = False
        payload = dict(prediction.payload or {})
        payload.setdefault("cancellation", {
            "reason": "OFFICIAL_CANCELLED_OR_POSTPONED",
            "game_id": game.external_id,
        })
        prediction.payload = payload
    game.pregame_context = {}
    game.context_collected_at = None
    return report


def cancelled_game_pregame_integrity(session: Session, repair: bool = False) -> dict[str, Any]:
    """Audit or clean all currently cancelled fixtures without deleting their schedule history."""
    games = session.scalars(select(Game).where(Game.status == "CANCELLED").order_by(Game.game_date, Game.id)).all()
    rows = [purge_cancelled_game_pregame_data(session, game, repair=repair) for game in games]
    return {
        "cancelled_games": len(games), "repair": repair,
        "pitcher_rows": sum(int(row.get("pitcher_rows", 0)) for row in rows),
        "lineup_rows": sum(int(row.get("lineup_rows", 0)) for row in rows),
        "predictions_invalidated": sum(int(row.get("predictions_invalidated", 0)) for row in rows),
        "skipped_final_results": sum(row.get("skipped") == "HAS_FINAL_RESULT" for row in rows),
        "samples": rows[:30],
    }


def upsert_team_stat(session: Session, team: Team, effective_date: date, raw: dict[str, Any], recent: dict[str, Any], source_url: str, collected_at: datetime) -> TeamStat:
    stat = session.scalar(select(TeamStat).where(TeamStat.team_id == team.id, TeamStat.effective_date == effective_date))
    fields = ("games", "wins", "losses", "draws", "win_rate", "home_win_rate", "away_win_rate", "runs_per_game",
              "runs_allowed_per_game", "avg", "obp", "slg", "ops", "home_runs", "walks", "strikeouts", "era", "whip")
    values = {field: raw.get(field) for field in fields}
    values.update(recent=recent, advanced=raw.get("advanced") or {},
                  source=f"{team.league} official records", source_url=source_url, collected_at=collected_at)
    if stat is None:
        stat = TeamStat(team_id=team.id, effective_date=effective_date, **values)
        session.add(stat)
    else:
        for key, value in values.items():
            setattr(stat, key, value)
    return stat


BATTER_SPLIT_COUNTS = ("at_bats", "hits", "doubles", "triples", "home_runs", "walks",
                       "hit_by_pitch", "strikeouts", "sacrifice_flies", "grounded_into_double_play")


def upsert_batter_splits(session: Session, league: str, season: int, rows: dict[str, dict[str, Any]],
                         source_url: str, collected_at: datetime) -> int:
    """Replace one season's base-state splits for the supplied hitters."""
    if not rows:
        return 0
    existing = {
        (split.player_id, split.state): split
        for split in session.scalars(select(BatterSplit).where(
            BatterSplit.league == league, BatterSplit.season == season,
            BatterSplit.player_id.in_(list(rows)),
        )).all()
    }
    written = 0
    for player_id, payload in rows.items():
        for state, counts in (payload.get("states") or {}).items():
            values = {field: int(counts.get(field, 0) or 0) for field in BATTER_SPLIT_COUNTS}
            split = existing.get((player_id, state))
            if split is None:
                session.add(BatterSplit(
                    league=league, season=season, player_id=player_id, state=state,
                    player_name=payload.get("name"), source=f"{league} official records",
                    source_url=source_url, collected_at=collected_at, **values))
            else:
                for key, value in values.items():
                    setattr(split, key, value)
                split.player_name = payload.get("name") or split.player_name
                split.source_url, split.collected_at = source_url, collected_at
            written += 1
    return written


def load_batter_splits(session: Session, league: str, season: int,
                       player_ids: list[str]) -> dict[str, dict[str, dict[str, int]]]:
    """Return stored splits as {player_id: {state: counts}} for the requested hitters."""
    if not player_ids:
        return {}
    rows = session.scalars(select(BatterSplit).where(
        BatterSplit.league == league, BatterSplit.season == season,
        BatterSplit.player_id.in_(player_ids),
    )).all()
    output: dict[str, dict[str, dict[str, int]]] = {}
    for split in rows:
        output.setdefault(split.player_id, {})[split.state] = {
            field: getattr(split, field) for field in BATTER_SPLIT_COUNTS
        }
    return output


def fresh_batter_split_ids(session: Session, league: str, season: int,
                           player_ids: list[str], *, max_age: timedelta,
                           now: datetime | None = None) -> set[str]:
    """Return hitters whose stored base-state splits are still inside the refresh TTL."""
    if not player_ids:
        return set()
    cutoff = database_datetime((now or database_now()) - max_age)
    return set(session.scalars(select(BatterSplit.player_id).where(
        BatterSplit.league == league,
        BatterSplit.season == season,
        BatterSplit.player_id.in_(player_ids),
        BatterSplit.collected_at >= cutoff,
    ).distinct()).all())


def upsert_pitcher(session: Session, game: Game, raw: dict[str, Any], source_url: str, collected_at: datetime) -> PitcherStat:
    stat = session.scalar(select(PitcherStat).where(PitcherStat.game_id == game.id, PitcherStat.side == raw["side"]))
    values = {key: raw.get(key) for key in (
        "player_id", "name", "confirmed", "era", "whip", "war", "games", "avg_start_innings",
        "quality_starts", "fip", "k_bb_rate", "rest_days", "recent_pitches", "handedness",
        "opponent_games", "opponent_innings", "opponent_era", "opponent_whip", "recent",
    )}
    values.update(source=f"{game.league} official starter analysis", source_url=source_url, collected_at=collected_at)
    if stat is None:
        stat = PitcherStat(game_id=game.id, side=raw["side"], **values)
        session.add(stat)
    else:
        for key, value in values.items():
            setattr(stat, key, value)
    return stat


def latest_team_stat(session: Session, team_id: int, target_date: date) -> TeamStat | None:
    return session.scalar(select(TeamStat).options(joinedload(TeamStat.team)).where(
        TeamStat.team_id == team_id, TeamStat.effective_date <= target_date
    ).order_by(TeamStat.effective_date.desc(), TeamStat.collected_at.desc()).limit(1))


def team_stats_fresh(session: Session, target_date: date, league: str = "KBO") -> bool:
    ttl = settings.mlb_stats_ttl_minutes if league == "MLB" else settings.cache_ttl_minutes
    threshold = database_now() - timedelta(minutes=ttl)
    count = session.scalar(select(func.count(TeamStat.id)).join(Team).where(
        TeamStat.effective_date == target_date, TeamStat.collected_at >= threshold, Team.league == league
    )) or 0
    return count >= (30 if league == "MLB" else 10)


def replace_lineups(session: Session, game: Game, entries: list[dict[str, Any]], source_url: str, collected_at: datetime) -> list[LineupEntry]:
    if not entries:
        return session.scalars(select(LineupEntry).where(LineupEntry.game_id == game.id)).all()
    sides = {entry["side"] for entry in entries}
    session.execute(delete(LineupEntry).where(LineupEntry.game_id == game.id, LineupEntry.side.in_(sides)))
    output = []
    for raw in entries:
        matchup_source_url = raw.get("matchup_source_url")
        if not matchup_source_url:
            entry_source_url = source_url
        elif source_url in matchup_source_url:
            entry_source_url = matchup_source_url
        else:
            entry_source_url = f"{source_url}, {matchup_source_url}"
        item = LineupEntry(
            game_id=game.id, side=raw["side"], batting_order=int(raw["batting_order"]),
            player_id=raw.get("player_id"), player_name=raw["player_name"], position=raw.get("position"),
            value=raw.get("value"), value_metric=raw.get("value_metric"), confirmed=bool(raw.get("confirmed")),
            batting_side=raw.get("batting_side"), platoon_opponent_hand=raw.get("platoon_opponent_hand"),
            platoon_plate_appearances=raw.get("platoon_plate_appearances"), platoon_ops=raw.get("platoon_ops"),
            opponent_pitcher_id=raw.get("opponent_pitcher_id"),
            matchup_plate_appearances=raw.get("matchup_plate_appearances"), matchup_avg=raw.get("matchup_avg"),
            matchup_at_bats=raw.get("matchup_at_bats"), matchup_hits=raw.get("matchup_hits"),
            matchup_doubles=raw.get("matchup_doubles"), matchup_triples=raw.get("matchup_triples"),
            matchup_home_runs=raw.get("matchup_home_runs"), matchup_walks=raw.get("matchup_walks"),
            matchup_hit_by_pitch=raw.get("matchup_hit_by_pitch"), matchup_strikeouts=raw.get("matchup_strikeouts"),
            matchup_obp=raw.get("matchup_obp"), matchup_slg=raw.get("matchup_slg"), matchup_ops=raw.get("matchup_ops"),
            source=f"{game.league} official lineup + batter/pitcher matchup" if matchup_source_url else f"{game.league} official lineup",
            source_url=entry_source_url, collected_at=collected_at,
        )
        session.add(item); output.append(item)
    session.flush()
    return output


def upsert_market_consensus(session: Session, game: Game, raw: dict[str, Any], source_url: str,
                            collected_at: datetime) -> MarketConsensus:
    provider = str(raw.get("provider", "The Odds API"))
    row = session.scalar(select(MarketConsensus).where(
        MarketConsensus.game_id == game.id, MarketConsensus.provider == provider,
    ))
    values = {
        "external_event_id": raw.get("external_event_id"), "bookmaker_count": int(raw.get("bookmaker_count", 0)),
        "total_line": raw.get("total_line"), "home_spread": raw.get("home_spread"),
        "home_implied_probability": raw.get("home_implied_probability"),
        "away_implied_probability": raw.get("away_implied_probability"), "raw": raw,
        "source_url": source_url, "collected_at": collected_at,
    }
    latest_snapshot = session.scalar(select(MarketSnapshot).where(
        MarketSnapshot.game_id == game.id, MarketSnapshot.provider == provider,
    ).order_by(MarketSnapshot.collected_at.desc()).limit(1))
    comparison = (values["bookmaker_count"], values["total_line"], values["home_implied_probability"],
                  values["away_implied_probability"])
    previous = ((latest_snapshot.bookmaker_count, latest_snapshot.total_line,
                 latest_snapshot.home_implied_probability, latest_snapshot.away_implied_probability)
                if latest_snapshot else None)
    if comparison != previous:
        session.add(MarketSnapshot(
            game_id=game.id, provider=provider, bookmaker_count=values["bookmaker_count"],
            total_line=values["total_line"], home_implied_probability=values["home_implied_probability"],
            away_implied_probability=values["away_implied_probability"], raw=raw,
            source_url=source_url, collected_at=collected_at,
        ))
    if row is None:
        row = MarketConsensus(game_id=game.id, provider=provider, **values)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    return row


def get_or_create_model(session: Session, name: str, algorithm: str) -> ModelVersion:
    model = session.scalar(select(ModelVersion).where(ModelVersion.name == name))
    if model is None:
        schema = {"version": 3, "features": [
            "season", "recent5_10", "offense", "defense", "starter_era_fip_kbb",
            "starter_workload_rest", "pitcher_vs_opponent", "batter_vs_pitcher", "bullpen_proxy",
            "team_rest_doubleheader", "shrunk_head_to_head_runs", "lineup", "home", "park",
        ]}
        checksum = hashlib.sha256(f"{name}:{algorithm}:{schema}".encode()).hexdigest()
        model = ModelVersion(name=name, algorithm=algorithm, feature_schema=schema, checksum=checksum)
        session.add(model)
        session.flush()
    return model


def save_prediction(session: Session, game: Game, result: dict[str, Any], *, stage: str = "UNSCHEDULED",
                    trigger: str = "manual", captured_at: datetime | None = None,
                    origin: str = "LIVE_PREGAME", data_cutoff: datetime | None = None,
                    training_eligible: bool = True,
                    leakage_audit: dict[str, Any] | None = None) -> Prediction:
    captured_at = captured_at or datetime.now(KST)
    latest = session.scalar(select(Prediction).where(
        Prediction.game_id == game.id, Prediction.origin == origin,
    ).order_by(Prediction.created_at.desc()).limit(1))
    if latest and latest.input_hash == result["input_hash"]:
        prediction = latest
    else:
        model_info = result["payload"]["model"]
        model = get_or_create_model(session, model_info["name"], model_info["algorithm"])
        prediction = Prediction(
            game_id=game.id, model_version_id=model.id, input_hash=result["input_hash"],
            home_win_probability=result["home_win_probability"], away_win_probability=result["away_win_probability"],
            home_expected_runs=result["home_expected_runs"], away_expected_runs=result["away_expected_runs"],
            confidence=result["confidence"], payload=result["payload"], created_at=captured_at,
            origin=origin, data_cutoff=data_cutoff or captured_at,
            training_eligible=training_eligible, leakage_audit=leakage_audit or {},
        )
        session.add(prediction)
        session.flush()
        session.add(PredictionHistory(prediction_id=prediction.id, recorded_at=captured_at, snapshot={
            "home_win_probability": prediction.home_win_probability,
            "away_win_probability": prediction.away_win_probability,
            "home_expected_runs": prediction.home_expected_runs,
            "away_expected_runs": prediction.away_expected_runs,
            "confidence": prediction.confidence,
        }))
    previous = session.scalar(select(PredictionSnapshot).where(
        PredictionSnapshot.game_id == game.id,
    ).order_by(PredictionSnapshot.captured_at.desc()).limit(1))
    # A scheduled checkpoint remains auditable even when its inputs match the prior hourly snapshot.
    duplicate_latest_snapshot = bool(
        previous and previous.stage == stage and previous.trigger == trigger and
        previous.input_hash == result["input_hash"]
    )
    if not duplicate_latest_snapshot:
        minutes = None
        if game.start_at and stage != "HISTORICAL_REPLAY":
            start_at = game.start_at if game.start_at.tzinfo else game.start_at.replace(tzinfo=KST)
            captured = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=KST)
            minutes = round((start_at - captured).total_seconds() / 60)
        session.add(PredictionSnapshot(
            game_id=game.id, prediction_id=prediction.id, stage=stage, trigger=trigger,
            minutes_to_start=minutes, input_hash=result["input_hash"], input_payload=result["input_payload"],
            changes=_prediction_changes(previous.input_payload if previous else None, result["input_payload"]),
            captured_at=captured_at,
        ))
    return prediction


def _prediction_changes(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    if previous is None:
        return [{"type": "INITIAL", "label": "첫 경기 전 예측이 저장되었습니다."}]
    changes: list[dict[str, Any]] = []
    if previous.get("pitchers") != current.get("pitchers"):
        changes.append({"type": "STARTER", "label": "선발투수 정보가 변경되었습니다."})
    old_lineups, new_lineups = previous.get("lineups", []), current.get("lineups", [])
    if old_lineups != new_lineups:
        old_confirmed = sum(bool(row[7] if len(row) > 7 else row[-1]) for row in old_lineups)
        new_confirmed = sum(bool(row[7] if len(row) > 7 else row[-1]) for row in new_lineups)
        label = "라인업과 타순이 변경되었습니다."
        if old_confirmed < 18 <= new_confirmed:
            label = "양 팀 확정 라인업이 반영되었습니다."
        changes.append({"type": "LINEUP", "label": label})
    old_features, new_features = previous.get("features", {}), current.get("features", {})
    feature_labels = {
        "recent_10_win_rate_diff": "최근 경기 흐름", "recent_run_diff": "최근 득점력",
        "recent_run_allowed_diff": "최근 실점 억제력", "bullpen_proxy_diff": "불펜 가용성 추정치",
        "rest_days_diff": "휴식일", "doubleheader_diff": "더블헤더 조건",
        "head_to_head_diff": "최근 상대 전적",
        "ops_diff": "시즌 공격력", "starter_era_diff": "선발 성적",
    }
    changed_features = [label for key, label in feature_labels.items() if old_features.get(key) != new_features.get(key)]
    if changed_features:
        changes.append({"type": "STATS", "label": f"{', '.join(changed_features)} 데이터가 갱신되었습니다."})
    return changes or [{"type": "STABLE", "label": "핵심 입력 변화 없이 시점 스냅샷을 저장했습니다."}]


def game_cards(session: Session, target_date: date, league: str | None = "KBO") -> list[dict[str, Any]]:
    query = select(Game).options(joinedload(Game.away_team), joinedload(Game.home_team)).where(Game.game_date == target_date)
    if league and league != "ALL":
        query = query.where(Game.league == league)
    games = session.scalars(query.order_by(Game.start_at, Game.start_time, Game.external_id)).all()
    context = _game_serialization_context(session, games)
    return [_serialize_game(game, context) for game in games]


def game_dates(session: Session, year: int, league: str | None = "ALL") -> list[dict[str, Any]]:
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    query = select(Game.game_date, Game.league, func.count(Game.id)).where(
        Game.game_date >= start, Game.game_date < end,
    ).group_by(Game.game_date, Game.league).order_by(Game.game_date)
    if league and league != "ALL":
        query = query.where(Game.league == league)
    grouped: dict[date, dict[str, int]] = {}
    for game_date, game_league, count in session.execute(query):
        row = grouped.setdefault(game_date, {"KBO": 0, "MLB": 0})
        row[game_league] = int(count)
    return [{"date": day.isoformat(), "games": values["KBO"] + values["MLB"],
             "kbo": values["KBO"], "mlb": values["MLB"]} for day, values in grouped.items()]


def game_detail(session: Session, external_id: str) -> dict[str, Any] | None:
    game = session.scalar(select(Game).options(joinedload(Game.away_team), joinedload(Game.home_team)).where(Game.external_id == external_id))
    return _serialize_game(game, _game_serialization_context(session, [game])) if game else None


def _game_serialization_context(session: Session, games: list[Game]) -> dict[str, Any]:
    """Fetch card dependencies in bounded batches instead of once per game.

    Supabase round trips dominate response time, so the former per-card query
    pattern made the ALL board take tens of seconds. This stays at eight
    dependency queries regardless of the number of games on the board.
    """
    game_ids = [game.id for game in games]
    team_ids = list({team_id for game in games for team_id in (game.away_team_id, game.home_team_id)})
    empty: dict[Any, Any] = {}
    if not game_ids:
        return {
            "predictions": empty, "replay_predictions": empty,
            "prediction_history": empty, "predictions_by_id": empty,
            "pitchers": empty, "lineups": empty, "team_stats": empty, "results": empty,
            "markets": empty, "snapshots": empty, "evaluations": empty,
        }

    predictions_by_game: dict[int, list[Prediction]] = defaultdict(list)
    predictions_by_id: dict[int, Prediction] = {}
    for row in session.scalars(select(Prediction).options(joinedload(Prediction.evaluation)).where(
        Prediction.game_id.in_(game_ids),
    ).order_by(Prediction.game_id, Prediction.created_at.desc())).all():
        predictions_by_game[row.game_id].append(row)
        predictions_by_id[row.id] = row

    pitchers_by_game: dict[int, list[PitcherStat]] = defaultdict(list)
    for row in session.scalars(select(PitcherStat).where(PitcherStat.game_id.in_(game_ids))).all():
        pitchers_by_game[row.game_id].append(row)

    lineups_by_game: dict[int, list[LineupEntry]] = defaultdict(list)
    for row in session.scalars(select(LineupEntry).where(
        LineupEntry.game_id.in_(game_ids),
    ).order_by(LineupEntry.game_id, LineupEntry.side, LineupEntry.batting_order)).all():
        lineups_by_game[row.game_id].append(row)

    # Sorted newest-first; keep only the first row for each team.
    team_stats: dict[int, TeamStat] = {}
    max_target_date = max(game.game_date for game in games)
    for row in session.scalars(select(TeamStat).where(
        TeamStat.team_id.in_(team_ids), TeamStat.effective_date <= max_target_date,
    ).order_by(TeamStat.team_id, TeamStat.effective_date.desc(), TeamStat.collected_at.desc())).all():
        team_stats.setdefault(row.team_id, row)

    results = {row.game_id: row for row in session.scalars(select(GameResult).where(GameResult.game_id.in_(game_ids))).all()}

    evaluations = {row.id: row.evaluation for row in predictions_by_id.values() if row.evaluation is not None}

    markets: dict[int, MarketConsensus] = {}
    for row in session.scalars(select(MarketConsensus).where(
        MarketConsensus.game_id.in_(game_ids),
    ).order_by(MarketConsensus.game_id, MarketConsensus.collected_at.desc())).all():
        markets.setdefault(row.game_id, row)

    snapshots_by_game: dict[int, list[PredictionSnapshot]] = defaultdict(list)
    for row in session.scalars(select(PredictionSnapshot).where(
        PredictionSnapshot.game_id.in_(game_ids),
    ).order_by(PredictionSnapshot.game_id, PredictionSnapshot.captured_at.desc())).all():
        if len(snapshots_by_game[row.game_id]) < 20:
            snapshots_by_game[row.game_id].append(row)

    display_predictions = {
        game.id: prediction
        for game in games
        if (prediction := _display_prediction(
            game, predictions_by_game.get(game.id, []), results.get(game.id) if game.status == "FINAL" else None,
        )) is not None
    }
    replay_predictions = {
        game.id: replay
        for game in games
        if (replay := next((row for row in predictions_by_game.get(game.id, [])
                            if row.origin == "HISTORICAL_REPLAY" and
                            bool((row.leakage_audit or {}).get("passed"))), None)) is not None
    }

    return {
        "predictions": display_predictions, "replay_predictions": replay_predictions,
        "prediction_history": {game_id: rows[:10] for game_id, rows in predictions_by_game.items()},
        "predictions_by_id": predictions_by_id,
        "pitchers": pitchers_by_game,
        "lineups": lineups_by_game,
        "team_stats": team_stats,
        "results": results,
        "markets": markets,
        "snapshots": snapshots_by_game,
        "evaluations": evaluations,
    }


def _display_prediction(game: Game, predictions: list[Prediction], result: GameResult | None) -> Prediction | None:
    """Use the last genuinely pre-game prediction when comparing a final result."""
    if not predictions:
        return None
    if result is None:
        return predictions[0]
    cutoff = game.start_at or result.finalized_at
    live = next((row for row in predictions if row.origin == "LIVE_PREGAME" and
                 _naive(row.data_cutoff or row.created_at) <= _naive(cutoff)), None)
    if live:
        return live
    return next((row for row in predictions if row.origin == "HISTORICAL_REPLAY" and
                 bool((row.leakage_audit or {}).get("passed")) and
                 _naive(row.data_cutoff or row.created_at) <= _naive(cutoff)), None)


def _serialize_game(game: Game, context: dict[str, Any]) -> dict[str, Any]:
    prediction = context["predictions"].get(game.id)
    replay_prediction = context["replay_predictions"].get(game.id)
    pitchers = context["pitchers"].get(game.id, [])
    lineups = context["lineups"].get(game.id, [])
    pitcher_map = {p.side: p for p in pitchers}
    home_stat = context["team_stats"].get(game.home_team_id)
    away_stat = context["team_stats"].get(game.away_team_id)
    # A score row is only an official result after the source marks the game final. This also
    # hides and later repairs any partial result written by an older collector version.
    result = context["results"].get(game.id) if game.status == "FINAL" else None
    market = context["markets"].get(game.id)
    snapshots = context["snapshots"].get(game.id, [])
    source_objects = [value for value in (home_stat, away_stat, *pitchers, *lineups) if value]
    latest_update = max([game.collected_at, *(item.collected_at for item in source_objects)])
    age_minutes = max(0, round((datetime.now(KST).replace(tzinfo=None) - _naive(latest_update)).total_seconds() / 60))
    return {
        "id": game.external_id, "league": game.league, "date": game.game_date.isoformat(),
        "venue_date": game.venue_date.isoformat() if game.venue_date else game.game_date.isoformat(),
        "time": game.start_time.strftime("%H:%M") if game.start_time else None, "start_at": _iso(game.start_at) if game.start_at else None, "stadium": game.stadium,
        "status": game.status, "collected_at": _iso(game.collected_at),
        "away": _team_payload(game.away_team, away_stat, pitcher_map.get("away")),
        "home": _team_payload(game.home_team, home_stat, pitcher_map.get("home")),
        "result": ({"away_score": result.away_score, "home_score": result.home_score,
                    **({"innings": result.innings} if result.innings is not None else {})}
                   if result else None),
        "prediction": _prediction_payload(prediction, context["evaluations"].get(prediction.id)) if prediction else None,
        "replay_prediction": (_prediction_payload(
            replay_prediction, context["evaluations"].get(replay_prediction.id),
        ) if replay_prediction and (not prediction or replay_prediction.id != prediction.id) else None),
        "market": _market_payload(market, prediction) if market else None,
        "prediction_history": _history_payload(context["prediction_history"].get(game.id, []), snapshots),
        "prediction_timeline": _timeline_payload(context["predictions_by_id"], snapshots),
        "lineups": {side: [_lineup_payload(item) for item in lineups if item.side == side] for side in ("away", "home")},
        "sources": _sources(game, home_stat, away_stat, pitchers, lineups),
        "freshness": {"last_updated_at": _iso(latest_update), "age_minutes": age_minutes,
                      "status": "FRESH" if age_minutes <= settings.stale_after_minutes else "STALE"},
    }


def _team_payload(team: Team, stat: TeamStat | None, pitcher: PitcherStat | None) -> dict[str, Any]:
    data: dict[str, Any] = {"code": team.code, "name": team.name}
    data["stats"] = ({key: getattr(stat, key) for key in (
        "games", "wins", "losses", "draws", "win_rate", "home_win_rate", "away_win_rate", "runs_per_game",
        "runs_allowed_per_game", "avg", "obp", "slg", "ops", "era", "whip"
    )} if stat else None)
    if data["stats"] is not None:
        # The model consumes full season matchup/game logs before persistence.
        # Cards only need the aggregate recent windows; omitting hidden raw rows
        # keeps a 15-game MLB board small enough for mobile connections.
        recent = stat.recent or {}
        data["stats"]["recent"] = {
            window: {key: value.get(key) for key in (
                "games", "wins", "draws", "win_rate", "avg_runs", "avg_runs_allowed"
            )}
            for window in ("5", "10", "20")
            if isinstance((value := recent.get(window)), dict)
        }
        data["stats"]["advanced"] = stat.advanced or {}
    data["starter"] = ({key: getattr(pitcher, key) for key in (
        "player_id", "name", "confirmed", "era", "whip", "war", "games", "avg_start_innings", "quality_starts",
        "fip", "k_bb_rate", "rest_days", "recent_pitches", "handedness"
        , "opponent_games", "opponent_innings", "opponent_era", "opponent_whip", "recent"
    )} if pitcher else None)
    return data


def _prediction_payload(p: Prediction, evaluation: PredictionEvaluation | None = None) -> dict[str, Any]:
    payload = {key: value for key, value in (p.payload or {}).items() if key != "simulation_recipe"}
    display_score = payload.get("display_expected_score") or {
        "away": round(p.away_expected_runs, 1), "home": round(p.home_expected_runs, 1),
    }
    displayed_total = round(float(display_score["away"]) + float(display_score["home"]), 1)
    return {
        "home_win_probability": p.home_win_probability, "away_win_probability": p.away_win_probability,
        "home_expected_runs": p.home_expected_runs, "away_expected_runs": p.away_expected_runs,
        "expected_total": displayed_total,
        "statistical_expected_total": round(p.home_expected_runs + p.away_expected_runs, 2),
        "confidence": p.confidence, "created_at": _iso(p.created_at),
        "origin": p.origin, "data_cutoff": _iso(p.data_cutoff) if p.data_cutoff else None,
        "training_eligible": p.training_eligible, "leakage_audit": p.leakage_audit or {},
        "evaluation": _evaluation_payload(evaluation) if evaluation else None,
        **payload,
    }


def _evaluation_payload(row: PredictionEvaluation) -> dict[str, Any]:
    return {
        "simulation_count": row.simulation_count,
        "actual_score_count": row.actual_score_count,
        "actual_score_probability": row.actual_score_probability,
        "actual_outcome_count": row.actual_outcome_count,
        "actual_outcome_probability": row.actual_outcome_probability,
        "actual_total_count": row.actual_total_count,
        "actual_total_probability": row.actual_total_probability,
        "actual_margin_count": row.actual_margin_count,
        "actual_margin_probability": row.actual_margin_probability,
        "actual_inning_path_count": row.actual_inning_path_count,
        "actual_inning_path_probability": row.actual_inning_path_probability,
        **(row.details or {}),
    }


def _lineup_payload(item: LineupEntry) -> dict[str, Any]:
    return {"order": item.batting_order, "player_id": item.player_id, "name": item.player_name, "position": item.position,
            "value": item.value, "value_metric": item.value_metric, "confirmed": item.confirmed,
            "opponent_pitcher_id": item.opponent_pitcher_id,
            "matchup_plate_appearances": item.matchup_plate_appearances, "matchup_avg": item.matchup_avg,
            "matchup_at_bats": item.matchup_at_bats, "matchup_hits": item.matchup_hits,
            "matchup_doubles": item.matchup_doubles, "matchup_triples": item.matchup_triples,
            "matchup_home_runs": item.matchup_home_runs, "matchup_walks": item.matchup_walks,
            "matchup_hit_by_pitch": item.matchup_hit_by_pitch, "matchup_strikeouts": item.matchup_strikeouts,
            "matchup_obp": item.matchup_obp, "matchup_slg": item.matchup_slg, "matchup_ops": item.matchup_ops,
            "batting_side": item.batting_side, "platoon_opponent_hand": item.platoon_opponent_hand,
            "platoon_plate_appearances": item.platoon_plate_appearances, "platoon_ops": item.platoon_ops,
            "collected_at": _iso(item.collected_at)}


def _market_payload(market: MarketConsensus, prediction: Prediction | None) -> dict[str, Any]:
    output = {
        "provider": market.provider, "bookmaker_count": market.bookmaker_count,
        "total_line": market.total_line, "home_spread": market.home_spread,
        "home_implied_probability": market.home_implied_probability,
        "away_implied_probability": market.away_implied_probability,
        "source_url": market.source_url, "collected_at": _iso(market.collected_at),
    }
    if prediction:
        output["model_total_difference"] = round(
            prediction.home_expected_runs + prediction.away_expected_runs - market.total_line, 2,
        ) if market.total_line is not None else None
        output["model_home_probability_difference"] = round(
            prediction.home_win_probability - market.home_implied_probability, 4,
        ) if market.home_implied_probability is not None else None
    return output


def _history_payload(rows: list[Prediction], snapshots: list[PredictionSnapshot]) -> list[dict[str, Any]]:
    by_prediction = {snapshot.prediction_id: snapshot for snapshot in snapshots}
    return [{"created_at": _iso(p.created_at), "home_win_probability": p.home_win_probability,
             "away_win_probability": p.away_win_probability, "home_expected_runs": p.home_expected_runs,
             "away_expected_runs": p.away_expected_runs, "confidence": p.confidence,
             "model": (p.payload or {}).get("model", {}).get("name"),
             "stage": by_prediction[p.id].stage if p.id in by_prediction else None,
             "changes": by_prediction[p.id].changes if p.id in by_prediction else []} for p in rows]


def _timeline_payload(predictions: dict[int, Prediction], snapshots: list[PredictionSnapshot]) -> list[dict[str, Any]]:
    rows = []
    for snapshot in reversed(snapshots):
        prediction = predictions.get(snapshot.prediction_id)
        if not prediction:
            continue
        rows.append({
            "captured_at": _iso(snapshot.captured_at), "stage": snapshot.stage, "trigger": snapshot.trigger,
            "minutes_to_start": snapshot.minutes_to_start, "changes": snapshot.changes,
            "home_win_probability": prediction.home_win_probability,
            "away_win_probability": prediction.away_win_probability,
            "home_expected_runs": prediction.home_expected_runs, "away_expected_runs": prediction.away_expected_runs,
        })
    return rows


def _sources(game: Game, home: TeamStat | None, away: TeamStat | None, pitchers: list[PitcherStat], lineups: list[LineupEntry]) -> list[dict[str, Any]]:
    entries = [{"name": game.source, "url": game.source_url, "collected_at": _iso(game.collected_at)}]
    for stat in (home, away, *pitchers, *lineups):
        if stat:
            entries.append({"name": stat.source, "url": stat.source_url, "collected_at": _iso(stat.collected_at)})
    unique = {}
    for item in entries:
        unique[(item["name"], item["url"])] = item
    return list(unique.values())


def performance_metrics(session: Session) -> dict[str, Any]:
    rows = session.execute(select(Prediction, GameResult, Game).join(
        GameResult, GameResult.game_id == Prediction.game_id
    ).join(Game, Game.id == Prediction.game_id).where(Game.status == "FINAL")).all()
    # One evaluation per game and source. Retrospective replay never changes the official live metric.
    latest: dict[int, tuple[Prediction, GameResult]] = {}
    replay: dict[int, tuple[Prediction, GameResult]] = {}
    for prediction, result, game in rows:
        cutoff = prediction.data_cutoff or prediction.created_at
        before_start = game.start_at is None or _naive(cutoff) <= _naive(game.start_at)
        origin = prediction.origin or "LIVE_PREGAME"
        if not before_start:
            continue
        if origin == "HISTORICAL_REPLAY":
            if not bool((prediction.leakage_audit or {}).get("passed")):
                continue
            target = replay
        elif origin == "LIVE_PREGAME" and _naive(prediction.created_at) <= _naive(result.finalized_at):
            target = latest
        else:
            continue
        if prediction.game_id not in target or prediction.created_at > target[prediction.game_id][0].created_at:
            target[prediction.game_id] = (prediction, result)
    if not latest:
        output = {"sample_size": 0, "message": "종료 경기와 경기 전 실전 예측이 쌓이면 평가 지표가 표시됩니다.",
                  "calibration": []}
    else:
        output = _performance_summary(latest)
    output["historical_replay"] = {
        **(_performance_summary(replay) if replay else {"sample_size": 0, "calibration": []}),
        "official_live_metric": False,
        "disclosure": "현재 모델로 다시 계산한 회고 재현이며 실전 성과와 분리됩니다.",
    }
    return output


def _performance_summary(rows: dict[int, tuple[Prediction, GameResult]]) -> dict[str, Any]:
    probs, outcomes, run_errors = [], [], []
    for p, r in rows.values():
        outcome = 1.0 if r.home_score > r.away_score else (0.5 if r.home_score == r.away_score else 0.0)
        probs.append(p.home_win_probability); outcomes.append(outcome)
        run_errors.extend([p.home_expected_runs - r.home_score, p.away_expected_runs - r.away_score])
    n = len(probs)
    eps = 1e-9
    brier = sum((p-y)**2 for p, y in zip(probs, outcomes, strict=False)) / n
    logloss = -sum(y*math.log(max(eps,p)) + (1-y)*math.log(max(eps,1-p)) for p,y in zip(probs,outcomes,strict=False)) / n
    bins = []
    for low in [i/10 for i in range(10)]:
        group = [(p,y) for p,y in zip(probs,outcomes,strict=False) if low <= p < low+.1 or (low == .9 and p == 1)]
        if group:
            bins.append({"predicted": round(sum(p for p,_ in group)/len(group),3), "observed": round(sum(y for _,y in group)/len(group),3), "count": len(group)})
    return {
        "sample_size": n, "accuracy": round(sum((p >= .5) == (y >= .5) for p,y in zip(probs,outcomes,strict=False))/n, 4),
        "brier_score": round(brier, 4), "log_loss": round(logloss, 4),
        "runs_mae": round(sum(abs(e) for e in run_errors)/len(run_errors), 3),
        "runs_rmse": round(math.sqrt(sum(e*e for e in run_errors)/len(run_errors)), 3), "calibration": bins,
    }


def _iso(value: datetime) -> str:
    return value.isoformat() if value.tzinfo else value.replace(tzinfo=KST).isoformat()


def _naive(value: datetime) -> datetime:
    return value.astimezone(KST).replace(tzinfo=None) if value.tzinfo else value
