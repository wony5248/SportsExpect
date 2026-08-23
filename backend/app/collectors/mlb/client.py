from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from backend.app.collectors.kbo.client import SourcePayload, _as_float, _as_int
from backend.app.config import KST


# StatsAPI situational codes for the eight exact base states plus the scoring-position
# aggregate, mapped to the same league-neutral names the KBO collector produces.
MLB_BASE_STATES = {
    "r0": "BASES_EMPTY", "r1": "RUNNER_1", "r2": "RUNNER_2", "r3": "RUNNER_3",
    "r12": "RUNNER_12", "r13": "RUNNER_13", "r23": "RUNNER_23", "r123": "BASES_LOADED",
    "risp": "SCORING_POSITION",
}


class MlbClient:
    """Client for public JSON used by MLB.com Gameday and official stats pages."""

    def __init__(self, timeout: float = 25.0, transport: httpx.BaseTransport | None = None):
        self.base_url = "https://statsapi.mlb.com"
        self.client = httpx.Client(
            base_url=self.base_url, timeout=timeout, follow_redirects=True, transport=transport,
            headers={"User-Agent": "DugoutLab/0.2 (+low-frequency educational collector)"},
        )

    def close(self) -> None:
        self.client.close()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> SourcePayload:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return SourcePayload(response.json(), str(response.url), datetime.now(KST))

    def games(self, service_date: date) -> SourcePayload:
        games: dict[str, dict[str, Any]] = {}
        urls = []
        collected = datetime.now(KST)
        # MLB's schedule date is venue-local. Two dates cover one complete KST calendar day.
        for official_date in (service_date - timedelta(days=1), service_date):
            payload = self._get_json("/api/v1/schedule", {
                "sportId": 1, "date": official_date.isoformat(), "hydrate": "probablePitcher,team,venue,linescore",
            })
            urls.append(payload.source_url); collected = max(collected, payload.collected_at)
            for date_group in payload.data.get("dates", []):
                for item in date_group.get("games", []):
                    start_at = _parse_datetime(item["gameDate"])
                    if start_at.astimezone(KST).date() != service_date:
                        continue
                    game_pk = str(item["gamePk"])
                    games[game_pk] = _schedule_game(item, service_date)
        return SourcePayload(list(games.values()), ", ".join(urls), collected)

    def season_games(self, season: int) -> SourcePayload:
        """Return the complete regular-season schedule grouped by KST date."""
        payload = self._get_json("/api/v1/schedule", {
            "sportId": 1, "startDate": date(season, 1, 1).isoformat(),
            "endDate": date(season, 12, 31).isoformat(), "gameTypes": "R", "hydrate": "team,venue,linescore",
        })
        rows = []
        for date_group in payload.data.get("dates", []):
            for item in date_group.get("games", []):
                start_at = _parse_datetime(item["gameDate"])
                rows.append(_schedule_game(item, start_at.astimezone(KST).date()))
        return SourcePayload(rows, payload.source_url, payload.collected_at)

    def recent_results(self, target_date: date, days: int = 40) -> SourcePayload:
        payload = self._get_json("/api/v1/schedule", {
            "sportId": 1, "startDate": (target_date - timedelta(days=days + 1)).isoformat(),
            "endDate": target_date.isoformat(), "gameTypes": "R",
        })
        results = []
        for date_group in payload.data.get("dates", []):
            for item in date_group.get("games", []):
                if item.get("status", {}).get("abstractGameState") != "Final":
                    continue
                service_day = _parse_datetime(item["gameDate"]).astimezone(KST).date()
                if service_day >= target_date:
                    continue
                away, home = item["teams"]["away"], item["teams"]["home"]
                if away.get("score") is None or home.get("score") is None:
                    continue
                results.append({"game_date": service_day, "away_name": away["team"]["name"],
                                "home_name": home["team"]["name"], "away_score": int(away["score"]), "home_score": int(home["score"])})
        return SourcePayload(results, payload.source_url, payload.collected_at)

    def team_stats(self, season: int) -> SourcePayload:
        standings = self._get_json("/api/v1/standings", {
            "leagueId": "103,104", "season": season, "standingsTypes": "regularSeason", "hydrate": "team",
        })
        records = [record for division in standings.data.get("records", []) for record in division.get("teamRecords", [])]
        output: dict[str, dict[str, Any]] = {}
        by_id = {str(record["team"]["id"]): record for record in records}

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._team_stat, team_id, season): team_id for team_id in by_id}
            for future in as_completed(futures):
                team_id = futures[future]
                record = by_id[team_id]
                stat_groups = future.result()
                hitting = stat_groups.get("hitting", {})
                pitching = stat_groups.get("pitching", {})
                games = int(record.get("gamesPlayed", 0))
                split_records = {item.get("type"): item for item in record.get("records", {}).get("splitRecords", [])}
                name = record["team"]["name"]
                output[name] = {
                    "code": team_id, "name": name, "games": games, "wins": int(record.get("wins", 0)),
                    "losses": int(record.get("losses", 0)), "draws": 0,
                    "win_rate": _as_float(record.get("winningPercentage"), .5),
                    "home_win_rate": _split_rate(split_records.get("home")), "away_win_rate": _split_rate(split_records.get("away")),
                    "runs_per_game": _as_float(hitting.get("runs"), 0) / games if games else None,
                    "runs_allowed_per_game": _as_float(pitching.get("runs"), 0) / games if games else None,
                    "avg": _as_float(hitting.get("avg")), "obp": _as_float(hitting.get("obp")),
                    "slg": _as_float(hitting.get("slg")), "ops": _as_float(hitting.get("ops")),
                    "home_runs": _as_int(hitting.get("homeRuns")), "walks": _as_int(hitting.get("baseOnBalls")),
                    "strikeouts": _as_int(hitting.get("strikeOuts")), "era": _as_float(pitching.get("era")),
                    "whip": _as_float(pitching.get("whip")),
                }
        return SourcePayload(output, standings.source_url + ", /api/v1/teams/{id}/stats", datetime.now(KST))

    def _team_stat(self, team_id: str, season: int) -> dict[str, dict[str, Any]]:
        payload = self._get_json(f"/api/v1/teams/{team_id}/stats", {
            "stats": "season", "group": "hitting,pitching", "season": season,
        })
        groups = {}
        for group in payload.data.get("stats", []):
            name = group.get("group", {}).get("displayName")
            splits = group.get("splits", [])
            if name and splits:
                groups[name] = splits[0].get("stat", {})
        return groups

    def starter_stats(self, game: dict[str, Any]) -> SourcePayload:
        output = []
        urls = []
        for side in ("away", "home"):
            player_id = game.get(f"{side}_pitcher_id")
            if not player_id:
                continue
            payload = self._get_json(f"/api/v1/people/{player_id}/stats", {
                "stats": "season,gameLog", "group": "pitching", "season": game["start_at"].year,
            })
            urls.append(payload.source_url)
            groups = {group.get("type", {}).get("displayName"): group for group in payload.data.get("stats", [])}
            season_splits = groups.get("season", {}).get("splits", [])
            stat = season_splits[0].get("stat", {}) if season_splits else {}
            logs = groups.get("gameLog", {}).get("splits", [])
            starts = _as_int(stat.get("gamesStarted"), 0) or 0
            innings = _innings(stat.get("inningsPitched"))
            batters = _as_int(stat.get("battersFaced"), 0) or 0
            strikeouts = _as_int(stat.get("strikeOuts"), 0) or 0
            walks = _as_int(stat.get("baseOnBalls"), 0) or 0
            homers = _as_int(stat.get("homeRuns"), 0) or 0
            hit_batters = _as_int(stat.get("hitBatsmen"), 0) or 0
            fip = ((13 * homers + 3 * (walks + hit_batters) - 2 * strikeouts) / innings + 3.1) if innings else None
            prior_logs = [row for row in logs if row.get("date") and date.fromisoformat(row["date"][:10]) < game["game_date"]]
            prior_logs.sort(key=lambda row: row["date"])
            last_date = date.fromisoformat(prior_logs[-1]["date"][:10]) if prior_logs else None
            recent_pitches = sum(_as_int(row.get("stat", {}).get("numberOfPitches"), 0) or 0 for row in prior_logs
                                 if (game["game_date"] - date.fromisoformat(row["date"][:10])).days <= 5)
            person = self._get_json(f"/api/v1/people/{player_id}")
            urls.append(person.source_url)
            person_row = person.data.get("people", [{}])[0]
            opponent_id = str(game["home_code" if side == "away" else "away_code"])
            opponent_logs = [row for row in prior_logs if str(row.get("opponent", {}).get("id")) == opponent_id]
            opponent_innings = sum(_innings(row.get("stat", {}).get("inningsPitched")) for row in opponent_logs)
            opponent_er = sum(_as_int(row.get("stat", {}).get("earnedRuns"), 0) or 0 for row in opponent_logs)
            opponent_hits = sum(_as_int(row.get("stat", {}).get("hits"), 0) or 0 for row in opponent_logs)
            opponent_walks = sum(_as_int(row.get("stat", {}).get("baseOnBalls"), 0) or 0 for row in opponent_logs)
            output.append({
                "side": side, "player_id": player_id, "name": game.get(f"{side}_pitcher_name"),
                "confirmed": True, "era": _as_float(stat.get("era")), "whip": _as_float(stat.get("whip")),
                "war": None, "games": _as_int(stat.get("gamesPlayed")),
                "avg_start_innings": innings / starts if starts else None,
                "quality_starts": _as_int(stat.get("qualityStarts")),
                "fip": round(fip, 3) if fip is not None else None,
                "k_bb_rate": (strikeouts - walks) / batters if batters else None,
                "rest_days": (game["game_date"] - last_date).days if last_date else None,
                "recent_pitches": recent_pitches,
                "handedness": person_row.get("pitchHand", {}).get("code"),
                "opponent_games": len(opponent_logs), "opponent_innings": round(opponent_innings, 3),
                "opponent_era": round(9 * opponent_er / opponent_innings, 3) if opponent_innings else None,
                "opponent_whip": round((opponent_hits + opponent_walks) / opponent_innings, 3) if opponent_innings else None,
            })
        return SourcePayload(output, ", ".join(urls) or f"{self.base_url}/api/v1/people/{{id}}/stats", datetime.now(KST))

    def lineups(self, game: dict[str, Any]) -> SourcePayload:
        payload = self._get_json(f"/api/v1.1/game/{game['game_pk']}/feed/live")
        teams = payload.data.get("liveData", {}).get("boxscore", {}).get("teams", {})
        entries = []
        for side in ("away", "home"):
            team = teams.get(side, {})
            order = team.get("battingOrder", [])
            players = team.get("players", {})
            confirmed = len(order) >= 9
            for slot, person_id in enumerate(order[:9], 1):
                player = players.get(f"ID{person_id}", {})
                batting = player.get("seasonStats", {}).get("batting", {})
                entries.append({
                    "side": side, "batting_order": slot, "player_id": str(person_id),
                    "player_name": player.get("person", {}).get("fullName", str(person_id)),
                    "position": player.get("position", {}).get("abbreviation"), "value": _as_float(batting.get("ops")),
                    "value_metric": "OPS", "confirmed": confirmed,
                })
        return SourcePayload(entries, payload.source_url, payload.collected_at)

    def batter_splits(self, player_ids: list[str], season: int) -> SourcePayload:
        """Fetch each hitter's official base-state splits for the season.

        One request per hitter covering every state, so callers should pass only the lineup they
        are about to predict. A hitter the API has no splits for is skipped, never estimated.
        """
        output: dict[str, dict[str, Any]] = {}
        urls: list[str] = []

        def fetch(player_id: str) -> tuple[str, dict[str, Any], str]:
            payload = self._get_json(f"/api/v1/people/{player_id}/stats", {
                "stats": "statSplits", "group": "hitting", "season": str(season),
                "sitCodes": ",".join(MLB_BASE_STATES),
            })
            states: dict[str, dict[str, int]] = {}
            name: str | None = None
            for group in payload.data.get("stats", []):
                for split in group.get("splits", []):
                    state = MLB_BASE_STATES.get((split.get("split") or {}).get("code", ""))
                    if not state:
                        continue
                    name = name or _person_name(split.get("player"))
                    stat = split.get("stat", {})
                    states[state] = {
                        "at_bats": _as_int(stat.get("atBats"), 0) or 0,
                        "hits": _as_int(stat.get("hits"), 0) or 0,
                        "doubles": _as_int(stat.get("doubles"), 0) or 0,
                        "triples": _as_int(stat.get("triples"), 0) or 0,
                        "home_runs": _as_int(stat.get("homeRuns"), 0) or 0,
                        "walks": _as_int(stat.get("baseOnBalls"), 0) or 0,
                        "hit_by_pitch": _as_int(stat.get("hitByPitch"), 0) or 0,
                        "strikeouts": _as_int(stat.get("strikeOuts"), 0) or 0,
                        "sacrifice_flies": _as_int(stat.get("sacFlies"), 0) or 0,
                        "grounded_into_double_play": _as_int(stat.get("groundIntoDoublePlay"), 0) or 0,
                    }
            return player_id, {"name": name, "states": states}, payload.source_url

        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in as_completed([pool.submit(fetch, str(row)) for row in player_ids]):
                player_id, payload, url = future.result()
                urls.append(url)
                if payload["states"]:
                    output[player_id] = payload
        return SourcePayload(output, ", ".join(urls) or f"{self.base_url}/api/v1/people/{{id}}/stats?stats=statSplits",
                             datetime.now(KST))

    def batter_vs_pitcher(self, entries: list[dict[str, Any]], game: dict[str, Any]) -> SourcePayload:
        """Fetch official career batter-vs-probable-pitcher totals for uncached lineup pairs."""
        output: dict[tuple[str, str], dict[str, Any]] = {}
        urls: list[str] = []

        def fetch(entry: dict[str, Any]) -> tuple[tuple[str, str], dict[str, Any], str]:
            batter_id = str(entry["player_id"])
            pitcher_id = str(game["home_pitcher_id" if entry["side"] == "away" else "away_pitcher_id"])
            payload = self._get_json(f"/api/v1/people/{batter_id}/stats", {
                "stats": "vsPlayer", "group": "hitting", "opposingPlayerId": pitcher_id,
            })
            groups = {row.get("type", {}).get("displayName"): row for row in payload.data.get("stats", [])}
            splits = groups.get("vsPlayerTotal", {}).get("splits", [])
            stat = splits[0].get("stat", {}) if splits else {}
            return (batter_id, pitcher_id), {
                "player_id": batter_id, "player_name": entry.get("player_name"),
                "opponent_pitcher_id": pitcher_id,
                "matchup_plate_appearances": _as_int(stat.get("plateAppearances"), 0) or 0,
                "matchup_at_bats": _as_int(stat.get("atBats"), 0) or 0,
                "matchup_hits": _as_int(stat.get("hits"), 0) or 0,
                "matchup_doubles": _as_int(stat.get("doubles"), 0) or 0,
                "matchup_triples": _as_int(stat.get("triples"), 0) or 0,
                "matchup_home_runs": _as_int(stat.get("homeRuns"), 0) or 0,
                "matchup_walks": _as_int(stat.get("baseOnBalls"), 0) or 0,
                "matchup_hit_by_pitch": _as_int(stat.get("hitByPitch"), 0) or 0,
                "matchup_strikeouts": _as_int(stat.get("strikeOuts"), 0) or 0,
                "matchup_avg": _as_float(stat.get("avg")), "matchup_obp": _as_float(stat.get("obp")),
                "matchup_slg": _as_float(stat.get("slg")), "matchup_ops": _as_float(stat.get("ops")),
            }, payload.source_url

        eligible = [entry for entry in entries if entry.get("player_id") and game.get(
            "home_pitcher_id" if entry["side"] == "away" else "away_pitcher_id"
        )]
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(fetch, entry) for entry in eligible]
            for future in as_completed(futures):
                key, stats, url = future.result()
                output[key] = stats
                urls.append(url)
        return SourcePayload(output, ", ".join(urls) or f"{self.base_url}/api/v1/people/{{id}}/stats?stats=vsPlayer",
                             datetime.now(KST))


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _person_id(value: dict[str, Any] | None) -> str | None:
    return str(value["id"]) if value and value.get("id") else None


def _person_name(value: dict[str, Any] | None) -> str | None:
    return value.get("fullName") if value else None


def _schedule_game(item: dict[str, Any], service_date: date) -> dict[str, Any]:
    start_at = _parse_datetime(item["gameDate"])
    away, home = item["teams"]["away"], item["teams"]["home"]
    detailed = item["status"].get("detailedState", "")
    abstract = item["status"].get("abstractGameState", "Preview")
    status = "CANCELLED" if detailed in {"Cancelled", "Postponed", "Suspended"} else (
        "FINAL" if abstract == "Final" else ("LIVE" if abstract == "Live" else "SCHEDULED")
    )
    official_date = date.fromisoformat(str(item.get("officialDate") or start_at.date()))
    game_pk = str(item["gamePk"])
    return {
        "external_id": f"MLB-{game_pk}", "game_pk": game_pk, "game_date": service_date,
        "venue_date": official_date,
        "start_time": start_at.astimezone(KST).strftime("%H:%M"), "start_at": start_at,
        "away_code": str(away["team"]["id"]), "away_name": away["team"]["name"],
        "home_code": str(home["team"]["id"]), "home_name": home["team"]["name"],
        "stadium": item.get("venue", {}).get("name"), "status": status,
        "away_score": away.get("score") if status == "FINAL" else None,
        "home_score": home.get("score") if status == "FINAL" else None,
        "innings": _linescore(item.get("linescore")) if status == "FINAL" else None,
        "away_pitcher_id": _person_id(away.get("probablePitcher")),
        "away_pitcher_name": _person_name(away.get("probablePitcher")),
        "home_pitcher_id": _person_id(home.get("probablePitcher")),
        "home_pitcher_name": _person_name(home.get("probablePitcher")),
        "starter_confirmed": bool(away.get("probablePitcher") and home.get("probablePitcher")),
        "lineup_confirmed": False,
    }


def _linescore(value: dict[str, Any] | None) -> dict[str, list[int | None]] | None:
    innings = (value or {}).get("innings") or []
    if not innings:
        return None
    return {
        side: [inning.get(side, {}).get("runs") for inning in innings]
        for side in ("away", "home")
    }


def _split_rate(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    wins, losses = int(record.get("wins", 0)), int(record.get("losses", 0))
    return wins / (wins + losses) if wins + losses else .5


def _innings(value: Any) -> float:
    text = str(value or "0")
    whole, _, outs = text.partition(".")
    return float(whole or 0) + int(outs or 0) / 3
