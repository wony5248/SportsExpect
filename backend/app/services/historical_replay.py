from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.app.config import KST
from backend.app.models import Game, GameResult, LineupEntry, PitcherStat, Prediction, Team
from backend.app.repositories.repository import save_prediction
from backend.app.services.prediction import predict_game
from backend.app.services.prediction_evaluation import evaluate_game_predictions


def run_historical_replay(session: Session, league: str, start_date: date | None = None,
                          end_date: date | None = None, limit: int = 20) -> dict[str, Any]:
    """Recreate archive forecasts using only results and observations available before first pitch."""
    query = select(Game, GameResult).join(GameResult, GameResult.game_id == Game.id).options(
        joinedload(Game.home_team), joinedload(Game.away_team),
    ).where(Game.league == league, Game.start_at.is_not(None))
    if start_date:
        query = query.where(Game.game_date >= start_date)
    if end_date:
        query = query.where(Game.game_date <= end_date)
    candidates = session.execute(query.order_by(Game.start_at.desc())).all()

    history = session.execute(
        select(Game, GameResult).join(GameResult, GameResult.game_id == Game.id).options(
            joinedload(Game.home_team), joinedload(Game.away_team),
        ).where(Game.league == league).order_by(Game.start_at, Game.id)
    ).all()
    predictions = session.scalars(select(Prediction).join(Game, Game.id == Prediction.game_id).where(
        Game.league == league,
    )).all()
    by_game: dict[int, list[Prediction]] = defaultdict(list)
    for prediction in predictions:
        by_game[prediction.game_id].append(prediction)

    created = evaluated = skipped_live = skipped_existing = skipped_history = 0
    processed: list[str] = []
    for game, result in candidates:
        stored = by_game.get(game.id, [])
        if any(row.origin == "LIVE_PREGAME" and _before_start(row, game) for row in stored):
            evaluated += evaluate_game_predictions(session, game, result)
            skipped_live += 1
            continue
        if any(row.origin == "HISTORICAL_REPLAY" for row in stored):
            evaluated += evaluate_game_predictions(session, game, result)
            skipped_existing += 1
            continue
        prior = [(prior_game, prior_result) for prior_game, prior_result in history
                 if prior_game.game_date.year == game.game_date.year and _game_before(prior_game, game)]
        home = _reconstructed_team_stat(game.home_team, game, prior)
        away = _reconstructed_team_stat(game.away_team, game, prior)
        if home.games < 5 or away.games < 5:
            skipped_history += 1
            continue
        cutoff = game.start_at
        pitchers = session.scalars(select(PitcherStat).where(
            PitcherStat.game_id == game.id, PitcherStat.collected_at <= cutoff,
        )).all()
        lineups = session.scalars(select(LineupEntry).where(
            LineupEntry.game_id == game.id, LineupEntry.collected_at <= cutoff,
        ).order_by(LineupEntry.side, LineupEntry.batting_order)).all()
        by_side = {pitcher.side: pitcher for pitcher in pitchers}
        league_average_runs = _league_average(prior, league)
        audit = {
            "passed": True,
            "method": "PRIOR_FINAL_RESULTS_ONLY",
            "data_cutoff": cutoff.isoformat(),
            "target_result_used_as_input": False,
            "future_games_used": 0,
            "home_prior_games": home.games,
            "away_prior_games": away.games,
            "pregame_pitchers_used": len(pitchers),
            "pregame_lineup_entries_used": len(lineups),
            "official_metric": False,
            "note": "현재 코드의 회고 재현이며 당시 실시간 저장 예측은 아닙니다.",
        }
        prediction_result = predict_game(
            game, home, away, by_side.get("home"), by_side.get("away"), lineups,
            {"home_games_today": 1, "away_games_today": 1,
             "league_average_runs": league_average_runs},
            model_runtime=None, bullpens={}, lineup_tables={},
            prediction_context={
                "origin": "HISTORICAL_REPLAY", "data_cutoff": cutoff.isoformat(),
                "model_name": f"{league}_HISTORICAL_REPLAY_V1",
            },
        )
        prediction = save_prediction(
            session, game, prediction_result, stage="HISTORICAL_REPLAY", trigger="archive_replay",
            captured_at=datetime.now(KST), origin="HISTORICAL_REPLAY", data_cutoff=cutoff,
            training_eligible=True, leakage_audit=audit,
        )
        session.flush()
        by_game[game.id].append(prediction)
        evaluated += evaluate_game_predictions(session, game, result)
        created += 1
        processed.append(game.external_id)
        if created >= limit:
            break
    return {
        "league": league, "created": created, "evaluations_created": evaluated,
        "skipped_live": skipped_live, "skipped_existing_replay": skipped_existing,
        "skipped_insufficient_history": skipped_history, "limit": limit,
        "games": processed,
        "disclosure": "과거 재현은 경기 전 데이터만 복원한 회고 시뮬레이션이며 당시 저장된 실전 예측과 분리됩니다.",
    }


def _reconstructed_team_stat(team: Team, target: Game,
                             history: list[tuple[Game, GameResult]]) -> SimpleNamespace:
    rows: list[dict[str, Any]] = []
    home_wins = home_games = away_wins = away_games = 0
    for game, result in history:
        if team.id not in (game.home_team_id, game.away_team_id):
            continue
        is_home = game.home_team_id == team.id
        runs = result.home_score if is_home else result.away_score
        allowed = result.away_score if is_home else result.home_score
        if runs > allowed:
            outcome = "W"
        elif runs < allowed:
            outcome = "L"
        else:
            outcome = "D"
        opponent = game.away_team if is_home else game.home_team
        rows.append({"date": game.game_date.isoformat(), "opponent": opponent.name,
                     "runs": runs, "allowed": allowed, "result": outcome})
        if is_home:
            home_games += 1; home_wins += outcome == "W"
        else:
            away_games += 1; away_wins += outcome == "W"
    games = len(rows)
    wins = sum(row["result"] == "W" for row in rows)
    losses = sum(row["result"] == "L" for row in rows)
    draws = games - wins - losses
    recent: dict[str, Any] = {}
    for window in (5, 10, 20):
        sample = rows[-window:]
        points = sum(1 if row["result"] == "W" else (.5 if row["result"] == "D" else 0) for row in sample)
        recent[str(window)] = {
            "games": len(sample), "wins": sum(row["result"] == "W" for row in sample),
            "draws": sum(row["result"] == "D" for row in sample),
            "win_rate": points / len(sample) if sample else .5,
            "avg_runs": sum(row["runs"] for row in sample) / len(sample) if sample else None,
            "avg_runs_allowed": sum(row["allowed"] for row in sample) / len(sample) if sample else None,
            "games_detail": sample,
        }
    matchups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows[-20:]:
        matchups[row["opponent"]].append(row)
    recent["matchups"] = {
        name: {
            "games": len(sample),
            "win_rate": sum(1 if row["result"] == "W" else (.5 if row["result"] == "D" else 0)
                            for row in sample) / len(sample),
            "avg_run_diff": sum(row["runs"] - row["allowed"] for row in sample) / len(sample),
        }
        for name, sample in matchups.items()
    }
    return SimpleNamespace(
        team=team, games=games, wins=wins, losses=losses, draws=draws,
        win_rate=(wins + .5 * draws) / games if games else .5,
        home_win_rate=home_wins / home_games if home_games else .5,
        away_win_rate=away_wins / away_games if away_games else .5,
        runs_per_game=sum(row["runs"] for row in rows) / games if games else None,
        runs_allowed_per_game=sum(row["allowed"] for row in rows) / games if games else None,
        avg=None, obp=None, slg=None, ops=None, home_runs=None, walks=None,
        strikeouts=None, era=None, whip=None, recent=recent,
    )


def _league_average(history: list[tuple[Game, GameResult]], league: str) -> float:
    scores = [score for _, result in history for score in (result.away_score, result.home_score)]
    return sum(scores) / len(scores) if scores else (5.15 if league == "KBO" else 4.45)


def _game_before(candidate: Game, target: Game) -> bool:
    if candidate.start_at and target.start_at:
        return _naive(candidate.start_at) < _naive(target.start_at)
    if candidate.game_date != target.game_date:
        return candidate.game_date < target.game_date
    if candidate.start_time and target.start_time:
        return candidate.start_time < target.start_time
    # Unknown ordering on the same date is not safe enough for a replay feature.
    return False


def _before_start(prediction: Prediction, game: Game) -> bool:
    return bool(game.start_at and _naive(prediction.data_cutoff or prediction.created_at) <= _naive(game.start_at))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
