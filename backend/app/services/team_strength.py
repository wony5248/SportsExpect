from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Game, GameResult


@dataclass(frozen=True)
class _Result:
    game_id: int
    season: int
    start_at: datetime | None
    game_date: Any
    home_id: int
    away_id: int
    home_score: int
    away_score: int


class TeamStrengthHistory:
    """Leakage-safe opponent-adjusted ratings reconstructed from final scores.

    Every context is rebuilt from games that started strictly before the target game. This makes
    the same implementation safe for both today's slate and historical walk-forward replay.
    """

    def __init__(self, rows: list[_Result], league: str):
        self.rows = rows
        self.league = league
        self._cache: dict[tuple[int, str], dict[str, Any]] = {}

    @classmethod
    def from_session(cls, session: Session, league: str) -> "TeamStrengthHistory":
        rows = session.execute(
            select(Game, GameResult).join(GameResult, GameResult.game_id == Game.id)
            .where(Game.league == league, Game.status == "FINAL")
            .order_by(Game.start_at, Game.game_date, Game.id)
        ).all()
        return cls([
            _Result(game.id, game.game_date.year, game.start_at, game.game_date,
                    game.home_team_id, game.away_team_id, result.home_score, result.away_score)
            for game, result in rows
        ], league)

    def context_for(self, game: Game) -> dict[str, Any]:
        cutoff = game.start_at.isoformat() if game.start_at else game.game_date.isoformat()
        key = (game.game_date.year, cutoff)
        ratings = self._cache.get(key)
        if ratings is None:
            prior = [row for row in self.rows if row.season == game.game_date.year and _before(row, game)]
            ratings = _ratings(prior, self.league)
            self._cache[key] = ratings
        teams = ratings["teams"]
        home = teams.get(game.home_team_id, _neutral_team())
        away = teams.get(game.away_team_id, _neutral_team())
        return {
            "available": bool(home["games"] and away["games"]),
            "method": "PRIOR_RESULTS_OPPONENT_ADJUSTED_V1",
            "cutoff": cutoff,
            "league_average_runs": ratings["league_average_runs"],
            "home": home,
            "away": away,
            "elo_diff": home["elo"] - away["elo"],
            "srs_diff": home["srs"] - away["srs"],
            "pythagorean_diff": home["pythagorean"] - away["pythagorean"],
            "schedule_strength_diff": home["schedule_strength"] - away["schedule_strength"],
            "adjusted_offense_diff": home["adjusted_offense"] - away["adjusted_offense"],
            # Positive means the home defense has allowed fewer opponent-adjusted runs.
            "adjusted_defense_edge": away["adjusted_defense"] - home["adjusted_defense"],
        }


def _ratings(rows: list[_Result], league: str) -> dict[str, Any]:
    team_ids = sorted({team for row in rows for team in (row.home_id, row.away_id)})
    if not team_ids:
        return {"league_average_runs": 5.15 if league == "KBO" else 4.45, "teams": {}}
    games: dict[int, list[tuple[int, float, float]]] = defaultdict(list)
    runs_for: dict[int, float] = defaultdict(float)
    runs_against: dict[int, float] = defaultdict(float)
    elo = {team: 1500.0 for team in team_ids}
    home_advantage = 20.0 if league == "KBO" else 24.0
    for row in rows:
        games[row.home_id].append((row.away_id, row.home_score, row.away_score))
        games[row.away_id].append((row.home_id, row.away_score, row.home_score))
        runs_for[row.home_id] += row.home_score; runs_against[row.home_id] += row.away_score
        runs_for[row.away_id] += row.away_score; runs_against[row.away_id] += row.home_score
        expected = 1 / (1 + 10 ** ((elo[row.away_id] - (elo[row.home_id] + home_advantage)) / 400))
        actual = 1.0 if row.home_score > row.away_score else (0.0 if row.home_score < row.away_score else .5)
        margin = abs(row.home_score - row.away_score)
        multiplier = min(1.65, 1.0 + .10 * math.log1p(margin))
        change = 20.0 * multiplier * (actual - expected)
        elo[row.home_id] += change; elo[row.away_id] -= change

    league_average = sum(runs_for.values()) / max(1, sum(len(values) for values in games.values()))
    margin = {team: sum(scored - allowed for _, scored, allowed in games[team]) / len(games[team])
              for team in team_ids}
    srs = dict(margin)
    for _ in range(16):
        updated = {team: margin[team] + sum(srs[opponent] for opponent, _, _ in games[team]) / len(games[team])
                   for team in team_ids}
        center = sum(updated.values()) / len(updated)
        srs = {team: value - center for team, value in updated.items()}

    offense = {team: 0.0 for team in team_ids}
    defense = {team: 0.0 for team in team_ids}  # positive is more runs allowed (worse)
    for _ in range(20):
        offense = {team: sum(scored - league_average - defense[opponent]
                             for opponent, scored, _ in games[team]) / len(games[team]) for team in team_ids}
        center = sum(offense.values()) / len(offense)
        offense = {team: value - center for team, value in offense.items()}
        defense = {team: sum(allowed - league_average - offense[opponent]
                             for opponent, _, allowed in games[team]) / len(games[team]) for team in team_ids}
        center = sum(defense.values()) / len(defense)
        defense = {team: value - center for team, value in defense.items()}

    output: dict[int, dict[str, float | int]] = {}
    for team in team_ids:
        count = len(games[team])
        shrink = count / (count + 20.0)
        scored, allowed = runs_for[team], runs_against[team]
        pyth = scored ** 1.83 / (scored ** 1.83 + allowed ** 1.83) if scored + allowed else .5
        output[team] = {
            "games": count,
            "elo": round(1500 + (elo[team] - 1500) * shrink, 4),
            "srs": round(srs[team] * shrink, 4),
            "pythagorean": round(.5 + (pyth - .5) * shrink, 6),
            "schedule_strength": round(sum(srs[opponent] for opponent, _, _ in games[team]) / count * shrink, 4),
            "adjusted_offense": round(offense[team] * shrink, 4),
            "adjusted_defense": round(defense[team] * shrink, 4),
        }
    return {"league_average_runs": league_average, "teams": output}


def _neutral_team() -> dict[str, float | int]:
    return {"games": 0, "elo": 1500.0, "srs": 0.0, "pythagorean": .5,
            "schedule_strength": 0.0, "adjusted_offense": 0.0, "adjusted_defense": 0.0}


def _before(row: _Result, game: Game) -> bool:
    if row.game_id == game.id:
        return False
    if row.start_at is not None and game.start_at is not None:
        left = row.start_at.replace(tzinfo=None) if row.start_at.tzinfo else row.start_at
        right = game.start_at.replace(tzinfo=None) if game.start_at.tzinfo else game.start_at
        return left < right
    return row.game_date < game.game_date
