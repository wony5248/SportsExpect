from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, joinedload

from backend.app.config import KST, settings
from backend.app.database import database_datetime, database_now
from backend.app.models import (Game, GameResult, LineupEntry, MarketConsensus, MarketSnapshot, ModelVersion,
                                PitcherStat, Prediction, PredictionHistory, PredictionSnapshot, Team, TeamStat)


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
    values = dict(league=league, game_date=raw["game_date"], start_time=start, start_at=start_at, away_team_id=away.id,
                  home_team_id=home.id, stadium=raw.get("stadium"), status=raw["status"],
                  source=f"{league} official game data", source_url=source_url, collected_at=collected_at)
    if game is None:
        game = Game(external_id=raw["external_id"], **values)
        session.add(game)
        session.flush()
    else:
        for key, value in values.items():
            setattr(game, key, value)
    if raw.get("away_score") is not None and raw.get("home_score") is not None:
        result = session.get(GameResult, game.id)
        if result is None:
            result = GameResult(game_id=game.id, away_score=raw["away_score"], home_score=raw["home_score"],
                                finalized_at=collected_at, source_url=source_url)
            session.add(result)
        else:
            result.away_score, result.home_score = raw["away_score"], raw["home_score"]
            result.source_url = source_url
    return game


def upsert_team_stat(session: Session, team: Team, effective_date: date, raw: dict[str, Any], recent: dict[str, Any], source_url: str, collected_at: datetime) -> TeamStat:
    stat = session.scalar(select(TeamStat).where(TeamStat.team_id == team.id, TeamStat.effective_date == effective_date))
    fields = ("games", "wins", "losses", "draws", "win_rate", "home_win_rate", "away_win_rate", "runs_per_game",
              "runs_allowed_per_game", "avg", "obp", "slg", "ops", "home_runs", "walks", "strikeouts", "era", "whip")
    values = {field: raw.get(field) for field in fields}
    values.update(recent=recent, source=f"{team.league} official records", source_url=source_url, collected_at=collected_at)
    if stat is None:
        stat = TeamStat(team_id=team.id, effective_date=effective_date, **values)
        session.add(stat)
    else:
        for key, value in values.items():
            setattr(stat, key, value)
    return stat


def upsert_pitcher(session: Session, game: Game, raw: dict[str, Any], source_url: str, collected_at: datetime) -> PitcherStat:
    stat = session.scalar(select(PitcherStat).where(PitcherStat.game_id == game.id, PitcherStat.side == raw["side"]))
    values = {key: raw.get(key) for key in (
        "player_id", "name", "confirmed", "era", "whip", "war", "games", "avg_start_innings",
        "quality_starts", "fip", "k_bb_rate", "rest_days", "recent_pitches", "handedness",
        "opponent_games", "opponent_innings", "opponent_era", "opponent_whip",
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
                    trigger: str = "manual", captured_at: datetime | None = None) -> Prediction:
    captured_at = captured_at or datetime.now(KST)
    latest = session.scalar(select(Prediction).where(Prediction.game_id == game.id).order_by(Prediction.created_at.desc()).limit(1))
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
    duplicate_latest_snapshot = bool(previous and previous.stage == stage and previous.input_hash == result["input_hash"])
    if not duplicate_latest_snapshot:
        minutes = None
        if game.start_at:
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
    if previous.get("ai_result") != current.get("ai_result"):
        changes.append({"type": "AI_ASSIST", "label": "Claude 보조 분석 결과 또는 사용 상태가 변경되었습니다."})
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
    return [_serialize_game(session, game) for game in games]


def game_detail(session: Session, external_id: str) -> dict[str, Any] | None:
    game = session.scalar(select(Game).options(joinedload(Game.away_team), joinedload(Game.home_team)).where(Game.external_id == external_id))
    return _serialize_game(session, game) if game else None


def _serialize_game(session: Session, game: Game) -> dict[str, Any]:
    prediction = session.scalar(select(Prediction).where(Prediction.game_id == game.id).order_by(Prediction.created_at.desc()).limit(1))
    pitchers = session.scalars(select(PitcherStat).where(PitcherStat.game_id == game.id)).all()
    lineups = session.scalars(select(LineupEntry).where(LineupEntry.game_id == game.id).order_by(LineupEntry.side, LineupEntry.batting_order)).all()
    pitcher_map = {p.side: p for p in pitchers}
    home_stat = latest_team_stat(session, game.home_team_id, game.game_date)
    away_stat = latest_team_stat(session, game.away_team_id, game.game_date)
    result = session.get(GameResult, game.id)
    market = session.scalar(select(MarketConsensus).where(
        MarketConsensus.game_id == game.id,
    ).order_by(MarketConsensus.collected_at.desc()).limit(1))
    snapshots = session.scalars(select(PredictionSnapshot).where(
        PredictionSnapshot.game_id == game.id,
    ).order_by(PredictionSnapshot.captured_at.desc()).limit(20)).all()
    source_objects = [value for value in (home_stat, away_stat, *pitchers, *lineups) if value]
    latest_update = max([game.collected_at, *(item.collected_at for item in source_objects)])
    age_minutes = max(0, round((datetime.now(KST).replace(tzinfo=None) - _naive(latest_update)).total_seconds() / 60))
    return {
        "id": game.external_id, "league": game.league, "date": game.game_date.isoformat(),
        "time": game.start_time.strftime("%H:%M") if game.start_time else None, "start_at": _iso(game.start_at) if game.start_at else None, "stadium": game.stadium,
        "status": game.status, "collected_at": _iso(game.collected_at),
        "away": _team_payload(game.away_team, away_stat, pitcher_map.get("away")),
        "home": _team_payload(game.home_team, home_stat, pitcher_map.get("home")),
        "result": {"away_score": result.away_score, "home_score": result.home_score} if result else None,
        "prediction": _prediction_payload(prediction) if prediction else None,
        "market": _market_payload(market, prediction) if market else None,
        "prediction_history": _history_payload(session, game.id, snapshots),
        "prediction_timeline": _timeline_payload(session, snapshots),
        "lineups": {side: [_lineup_payload(item) for item in lineups if item.side == side] for side in ("away", "home")},
        "sources": _sources(game, home_stat, away_stat, pitchers, lineups),
        "freshness": {"last_updated_at": _iso(latest_update), "age_minutes": age_minutes,
                      "status": "FRESH" if age_minutes <= settings.stale_after_minutes else "STALE"},
    }


def _team_payload(team: Team, stat: TeamStat | None, pitcher: PitcherStat | None) -> dict[str, Any]:
    data: dict[str, Any] = {"code": team.code, "name": team.name}
    data["stats"] = ({key: getattr(stat, key) for key in (
        "games", "wins", "losses", "draws", "win_rate", "home_win_rate", "away_win_rate", "runs_per_game",
        "runs_allowed_per_game", "avg", "obp", "slg", "ops", "era", "whip", "recent"
    )} if stat else None)
    data["starter"] = ({key: getattr(pitcher, key) for key in (
        "player_id", "name", "confirmed", "era", "whip", "war", "games", "avg_start_innings", "quality_starts",
        "fip", "k_bb_rate", "rest_days", "recent_pitches", "handedness"
        , "opponent_games", "opponent_innings", "opponent_era", "opponent_whip"
    )} if pitcher else None)
    return data


def _prediction_payload(p: Prediction) -> dict[str, Any]:
    payload = p.payload or {}
    display_score = payload.get("display_expected_score") or {
        "away": round(p.away_expected_runs, 1), "home": round(p.home_expected_runs, 1),
    }
    displayed_total = round(float(display_score["away"]) + float(display_score["home"]), 1)
    return {
        "home_win_probability": p.home_win_probability, "away_win_probability": p.away_win_probability,
        "home_expected_runs": p.home_expected_runs, "away_expected_runs": p.away_expected_runs,
        "expected_total": displayed_total,
        "statistical_expected_total": round(p.home_expected_runs + p.away_expected_runs, 2),
        "confidence": p.confidence, "created_at": _iso(p.created_at), **payload,
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


def _history_payload(session: Session, game_id: int, snapshots: list[PredictionSnapshot]) -> list[dict[str, Any]]:
    rows = session.scalars(select(Prediction).where(Prediction.game_id == game_id).order_by(Prediction.created_at.desc()).limit(10)).all()
    by_prediction = {snapshot.prediction_id: snapshot for snapshot in snapshots}
    return [{"created_at": _iso(p.created_at), "home_win_probability": p.home_win_probability,
             "away_win_probability": p.away_win_probability, "home_expected_runs": p.home_expected_runs,
             "away_expected_runs": p.away_expected_runs, "confidence": p.confidence,
             "model": (p.payload or {}).get("model", {}).get("name"),
             "stage": by_prediction[p.id].stage if p.id in by_prediction else None,
             "changes": by_prediction[p.id].changes if p.id in by_prediction else []} for p in rows]


def _timeline_payload(session: Session, snapshots: list[PredictionSnapshot]) -> list[dict[str, Any]]:
    predictions = {p.id: p for p in session.scalars(select(Prediction).where(
        Prediction.id.in_([s.prediction_id for s in snapshots if s.prediction_id is not None])
    )).all()} if snapshots else {}
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
    ).join(Game, Game.id == Prediction.game_id)).all()
    # One evaluation per game: retain the final prediction created before the result was stored.
    latest: dict[int, tuple[Prediction, GameResult]] = {}
    for prediction, result, game in rows:
        before_start = game.start_at is None or prediction.created_at <= game.start_at
        if before_start and prediction.created_at <= result.finalized_at and (prediction.game_id not in latest or prediction.created_at > latest[prediction.game_id][0].created_at):
            latest[prediction.game_id] = (prediction, result)
    if not latest:
        return {"sample_size": 0, "message": "종료 경기와 경기 전 예측이 쌓이면 평가 지표가 표시됩니다.", "calibration": []}
    probs, outcomes, run_errors = [], [], []
    for p, r in latest.values():
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
