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
        self._feed_cache: dict[str, SourcePayload] = {}

    def close(self) -> None:
        self.client.close()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> SourcePayload:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return SourcePayload(response.json(), str(response.url), datetime.now(KST))

    def _game_feed(self, game_pk: str) -> SourcePayload:
        if game_pk not in self._feed_cache:
            self._feed_cache[game_pk] = self._get_json(f"/api/v1.1/game/{game_pk}/feed/live")
        return self._feed_cache[game_pk]

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
                fielding = stat_groups.get("fielding", {})
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
                    "advanced": {
                        "available": bool(fielding or hitting), "source": "MLB_OFFICIAL_TEAM_STATS",
                        "stolen_bases": _as_int(hitting.get("stolenBases")),
                        "caught_stealing": _as_int(hitting.get("caughtStealing")),
                        "stolen_base_percentage": _as_float(hitting.get("stolenBasePercentage")),
                        "fielding_percentage": _as_float(fielding.get("fielding")),
                        "errors": _as_int(fielding.get("errors")),
                        "double_plays": _as_int(fielding.get("doublePlays")),
                        "opponent_stolen_bases": _as_int(fielding.get("stolenBases")),
                        "opponent_caught_stealing": _as_int(fielding.get("caughtStealing")),
                        "opponent_stolen_base_percentage": _as_float(fielding.get("stolenBasePercentage")),
                        "passed_balls": _as_int(fielding.get("passedBall")),
                        "wild_pitches": _as_int(fielding.get("wildPitches")),
                    },
                }
        return SourcePayload(output, standings.source_url + ", /api/v1/teams/{id}/stats", datetime.now(KST))

    def _team_stat(self, team_id: str, season: int) -> dict[str, dict[str, Any]]:
        payload = self._get_json(f"/api/v1/teams/{team_id}/stats", {
            "stats": "season", "group": "hitting,pitching,fielding", "season": season,
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
            boundary = game.get("venue_date") or game["game_date"]
            prior_logs = [row for row in logs if row.get("date") and date.fromisoformat(row["date"][:10]) < boundary]
            prior_logs.sort(key=lambda row: row["date"])
            last_date = date.fromisoformat(prior_logs[-1]["date"][:10]) if prior_logs else None
            recent_pitches = sum(_as_int(row.get("stat", {}).get("numberOfPitches"), 0) or 0 for row in prior_logs
                                 if (boundary - date.fromisoformat(row["date"][:10])).days <= 5)
            prior_starts = [row for row in prior_logs if (_as_int(row.get("stat", {}).get("gamesStarted"), 0) or 0) > 0]
            recent_starts = prior_starts[-3:]
            recent_innings = sum(_innings(row.get("stat", {}).get("inningsPitched")) for row in recent_starts)
            recent_er = sum(_as_int(row.get("stat", {}).get("earnedRuns"), 0) or 0 for row in recent_starts)
            recent_hits = sum(_as_int(row.get("stat", {}).get("hits"), 0) or 0 for row in recent_starts)
            recent_walks = sum(_as_int(row.get("stat", {}).get("baseOnBalls"), 0) or 0 for row in recent_starts)
            recent_strikeouts = sum(_as_int(row.get("stat", {}).get("strikeOuts"), 0) or 0 for row in recent_starts)
            recent_batters = sum(_as_int(row.get("stat", {}).get("battersFaced"), 0) or 0 for row in recent_starts)
            start_pitches = [_as_int(row.get("stat", {}).get("numberOfPitches"), 0) or 0 for row in recent_starts]
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
                "war": None, "games": starts,
                "avg_start_innings": innings / starts if starts else None,
                "quality_starts": _as_int(stat.get("qualityStarts")),
                "fip": round(fip, 3) if fip is not None else None,
                "k_bb_rate": (strikeouts - walks) / batters if batters else None,
                "rest_days": (boundary - last_date).days if last_date else None,
                "recent_pitches": recent_pitches,
                "handedness": person_row.get("pitchHand", {}).get("code"),
                "opponent_games": len(opponent_logs), "opponent_innings": round(opponent_innings, 3),
                "opponent_era": round(9 * opponent_er / opponent_innings, 3) if opponent_innings else None,
                "opponent_whip": round((opponent_hits + opponent_walks) / opponent_innings, 3) if opponent_innings else None,
                "recent": {
                    "available": bool(recent_starts), "starts": len(recent_starts),
                    "era": round(9 * recent_er / recent_innings, 3) if recent_innings else None,
                    "whip": round((recent_hits + recent_walks) / recent_innings, 3) if recent_innings else None,
                    "k_bb_rate": round((recent_strikeouts - recent_walks) / recent_batters, 4) if recent_batters else None,
                    "avg_pitches": round(sum(start_pitches) / len(start_pitches), 1) if start_pitches else None,
                    "max_pitches": max(start_pitches) if start_pitches else None,
                    # This is explicitly a workload-derived ceiling, not an announced manager limit.
                    "derived_pitch_limit": min(115, max(70, round(sum(start_pitches) / len(start_pitches) + 8))) if start_pitches else None,
                    "velocity_available": False, "source": "OFFICIAL_GAME_LOG",
                },
            })
        return SourcePayload(output, ", ".join(urls) or f"{self.base_url}/api/v1/people/{{id}}/stats", datetime.now(KST))

    def lineups(self, game: dict[str, Any]) -> SourcePayload:
        payload = self._game_feed(str(game["game_pk"]))
        teams = payload.data.get("liveData", {}).get("boxscore", {}).get("teams", {})
        people = payload.data.get("gameData", {}).get("players", {})
        entries = []
        for side in ("away", "home"):
            team = teams.get(side, {})
            order = team.get("battingOrder", [])
            players = team.get("players", {})
            confirmed = len(order) >= 9
            for slot, person_id in enumerate(order[:9], 1):
                player = players.get(f"ID{person_id}", {})
                person = people.get(f"ID{person_id}", {})
                batting = player.get("seasonStats", {}).get("batting", {})
                entries.append({
                    "side": side, "batting_order": slot, "player_id": str(person_id),
                    "player_name": player.get("person", {}).get("fullName", str(person_id)),
                    "position": player.get("position", {}).get("abbreviation"), "value": _as_float(batting.get("ops")),
                    "value_metric": "OPS", "confirmed": confirmed,
                    "batting_side": (person.get("batSide") or {}).get("code"),
                })
        return SourcePayload(entries, payload.source_url, payload.collected_at)

    def batter_platoon(self, entries: list[dict[str, Any]], game: dict[str, Any]) -> SourcePayload:
        """Fetch official current-season OPS versus the opposing starter's throwing hand."""
        feed = self._game_feed(str(game["game_pk"]))
        people = feed.data.get("gameData", {}).get("players", {})
        pitcher_hands = {
            side: (people.get(f"ID{game.get(f'{side}_pitcher_id')}", {}).get("pitchHand") or {}).get("code")
            for side in ("away", "home")
        }
        output: dict[str, dict[str, Any]] = {}
        urls: list[str] = []

        def fetch(entry: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
            player_id = str(entry["player_id"])
            opponent_side = "home" if entry["side"] == "away" else "away"
            hand = pitcher_hands.get(opponent_side)
            sit_code = "vl" if hand == "L" else "vr" if hand == "R" else None
            if not sit_code:
                return player_id, {}, feed.source_url
            payload = self._get_json(f"/api/v1/people/{player_id}/stats", {
                "stats": "statSplits", "group": "hitting", "season": str(game["game_date"].year),
                "sitCodes": sit_code,
            })
            split = next((split for group in payload.data.get("stats", []) for split in group.get("splits", [])
                          if (split.get("split") or {}).get("code") == sit_code), None)
            stat = (split or {}).get("stat", {})
            return player_id, {
                "platoon_opponent_hand": hand,
                "platoon_plate_appearances": _as_int(stat.get("plateAppearances"), 0) or 0,
                "platoon_ops": _as_float(stat.get("ops")),
            }, payload.source_url

        eligible = [entry for entry in entries if entry.get("player_id") and entry.get("confirmed")]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in as_completed([pool.submit(fetch, entry) for entry in eligible]):
                player_id, data, url = future.result()
                urls.append(url)
                if data:
                    output[player_id] = data
        return SourcePayload(output, ", ".join(dict.fromkeys(urls)) or feed.source_url, datetime.now(KST))

    def slate_context(self, target_date: date, games: list[dict[str, Any]]) -> SourcePayload:
        """Official weather/venue and prior-three-day relief usage for one KST slate."""
        schedule = self._get_json("/api/v1/schedule", {
            "sportId": 1, "startDate": (target_date - timedelta(days=4)).isoformat(),
            "endDate": (target_date - timedelta(days=1)).isoformat(), "gameTypes": "R",
        })
        prior = [item for group in schedule.data.get("dates", []) for item in group.get("games", [])
                 if item.get("status", {}).get("abstractGameState") == "Final"]
        workload: dict[str, dict[str, dict[str, Any]]] = {}
        urls = [schedule.source_url]

        def relief_rows(item: dict[str, Any]) -> tuple[list[tuple[str, str, str, int]], str]:
            payload = self._game_feed(str(item["gamePk"]))
            rows: list[tuple[str, str, str, int]] = []
            game_day = _parse_datetime(item["gameDate"]).astimezone(KST).date()
            days_ago = (target_date - game_day).days
            if not 1 <= days_ago <= 3:
                return rows, payload.source_url
            for side in ("away", "home"):
                team = payload.data.get("gameData", {}).get("teams", {}).get(side, {})
                box = payload.data.get("liveData", {}).get("boxscore", {}).get("teams", {}).get(side, {})
                for player_id in box.get("pitchers", []):
                    player = box.get("players", {}).get(f"ID{player_id}", {})
                    stat = player.get("stats", {}).get("pitching", {})
                    if (_as_int(stat.get("gamesStarted"), 0) or 0) > 0:
                        continue
                    rows.append((str(team.get("id")), str(player_id),
                                 player.get("person", {}).get("fullName", str(player_id)),
                                 _as_int(stat.get("numberOfPitches"), 0) or 0, days_ago))
            return rows, payload.source_url

        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in as_completed([pool.submit(relief_rows, item) for item in prior]):
                rows, url = future.result(); urls.append(url)
                for team_id, player_id, name, pitches, days_ago in rows:
                    arm = workload.setdefault(team_id, {}).setdefault(player_id, {"name": name, "days": {}})
                    arm["days"][str(days_ago)] = arm["days"].get(str(days_ago), 0) + pitches

        bullpen: dict[str, dict[str, Any]] = {}
        for team_id, arms in workload.items():
            day_totals = {day: sum(int(arm["days"].get(str(day), 0)) for arm in arms.values()) for day in (1, 2, 3)}
            high_load = [arm["name"] for arm in arms.values()
                         if int(arm["days"].get("1", 0)) >= 30 or
                         (int(arm["days"].get("1", 0)) > 0 and int(arm["days"].get("2", 0)) > 0)]
            fatigue = min(1.0, day_totals[1] / 140 * .65 + day_totals[2] / 160 * .25 + len(high_load) * .06)
            bullpen[team_id] = {"available": True, "source": "OFFICIAL_BOX_SCORE", "pitches": day_totals,
                                 "high_load_arms": high_load, "confirmed_unavailable_arms": [],
                                 "fatigue_index": round(fatigue, 4),
                                 "availability_basis": "official workload; manager availability is not inferred"}

        output: dict[str, dict[str, Any]] = {}
        for game in games:
            feed = self._game_feed(str(game["game_pk"])); urls.append(feed.source_url)
            game_data = feed.data.get("gameData", {})
            venue = game_data.get("venue", {})
            location = venue.get("location", {})
            weather = game_data.get("weather") or {}
            output[game["external_id"]] = {
                "version": 1, "league": "MLB",
                "weather": _weather_context(weather, venue.get("fieldInfo") or {}),
                "venue": {"available": bool(venue), "name": venue.get("name"),
                          "latitude": (location.get("defaultCoordinates") or {}).get("latitude"),
                          "longitude": (location.get("defaultCoordinates") or {}).get("longitude"),
                          "roof_type": (venue.get("fieldInfo") or {}).get("roofType"),
                          "turf_type": (venue.get("fieldInfo") or {}).get("turfType"),
                          "time_zone": (venue.get("timeZone") or {}).get("id")},
                "bullpen": {side: bullpen.get(str(game[f"{side}_code"]), {
                    "available": False, "source": "OFFICIAL_BOX_SCORE", "reason": "NO_PRIOR_RELIEF_USAGE",
                    "fatigue_index": 0.0, "pitches": {1: 0, 2: 0, 3: 0},
                    "high_load_arms": [], "confirmed_unavailable_arms": [],
                }) for side in ("away", "home")},
            }
        return SourcePayload(output, ", ".join(dict.fromkeys(urls)), datetime.now(KST))

    def inning_lines(self, game_pks: list[str]) -> SourcePayload:
        """Fetch official inning lines from individual Gameday feeds in parallel."""
        output: dict[str, dict[str, list[int | None]]] = {}
        urls: list[str] = []
        collected = datetime.now(KST)

        def fetch(game_pk: str) -> tuple[str, dict[str, list[int | None]] | None, str, datetime]:
            payload = self._game_feed(str(game_pk))
            innings = _linescore(payload.data.get("liveData", {}).get("linescore"))
            return game_pk, innings, payload.source_url, payload.collected_at

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(game_pks)))) as executor:
            futures = [executor.submit(fetch, game_pk) for game_pk in game_pks]
            for future in as_completed(futures):
                game_pk, innings, source_url, fetched_at = future.result()
                urls.append(source_url)
                collected = max(collected, fetched_at)
                if innings:
                    output[f"MLB-{game_pk}"] = innings
        return SourcePayload(output, ", ".join(urls), collected)

    def archived_starters(self, season: int) -> SourcePayload:
        """Return the starting pitcher of every regular-season game in one request.

        A starter's identity is announced days before first pitch, so recording it for a finished
        game is pre-game information, not hindsight.
        """
        payload = self._get_json("/api/v1/schedule", {
            "sportId": 1, "startDate": date(season, 1, 1).isoformat(),
            "endDate": date(season, 12, 31).isoformat(), "gameTypes": "R",
            "hydrate": "probablePitcher",
        })
        rows: list[dict[str, Any]] = []
        for date_group in payload.data.get("dates", []):
            for item in date_group.get("games", []):
                for side in ("away", "home"):
                    pitcher = (item.get("teams", {}).get(side) or {}).get("probablePitcher") or {}
                    if not pitcher.get("id"):
                        continue
                    rows.append({
                        # Matches _schedule_game's identifier so the archive joins on it.
                        "external_id": f"MLB-{item['gamePk']}", "side": side,
                        "player_id": str(pitcher["id"]), "name": pitcher.get("fullName"),
                    })
        return SourcePayload(rows, payload.source_url, payload.collected_at)

    def pitcher_game_logs(self, player_ids: list[str], season: int) -> SourcePayload:
        """Per-appearance pitching lines, so season-to-date rates can be rebuilt for any date."""
        output: dict[str, list[dict[str, Any]]] = {}
        urls: list[str] = []

        def fetch(player_id: str) -> tuple[str, list[dict[str, Any]], str]:
            payload = self._get_json(f"/api/v1/people/{player_id}/stats", {
                "stats": "gameLog", "group": "pitching", "season": str(season),
            })
            appearances = []
            for group in payload.data.get("stats", []):
                for split in group.get("splits", []):
                    stat = split.get("stat", {})
                    if not split.get("date"):
                        continue
                    appearances.append({
                        "date": split["date"],
                        "innings": _innings(stat.get("inningsPitched")),
                        "earned_runs": _as_int(stat.get("earnedRuns"), 0) or 0,
                        "hits": _as_int(stat.get("hits"), 0) or 0,
                        "walks": _as_int(stat.get("baseOnBalls"), 0) or 0,
                        "hit_batters": _as_int(stat.get("hitBatsmen"), 0) or 0,
                        "batters_faced": _as_int(stat.get("battersFaced"), 0) or 0,
                        "strikeouts": _as_int(stat.get("strikeOuts"), 0) or 0,
                        "home_runs": _as_int(stat.get("homeRuns"), 0) or 0,
                        "pitches": _as_int(stat.get("numberOfPitches"), 0) or 0,
                        "started": bool(_as_int(stat.get("gamesStarted"), 0)),
                    })
            appearances.sort(key=lambda row: row["date"])
            return player_id, appearances, payload.source_url

        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in as_completed([pool.submit(fetch, str(row)) for row in player_ids]):
                player_id, appearances, url = future.result()
                urls.append(url)
                if appearances:
                    output[player_id] = appearances
        return SourcePayload(output, ", ".join(dict.fromkeys(urls))
                             or f"{self.base_url}/api/v1/people/{{id}}/stats?stats=gameLog",
                             datetime.now(KST))

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


def _weather_context(weather: dict[str, Any], field: dict[str, Any]) -> dict[str, Any]:
    roof = str(field.get("roofType") or "")
    if not weather:
        return {"available": False, "reason": "PREGAME_WEATHER_NOT_PUBLISHED", "run_multiplier": 1.0}
    temperature = _as_float(weather.get("temp"))
    wind_text = str(weather.get("wind") or "")
    speed = _as_float(wind_text.split(" ", 1)[0], 0.0) or 0.0
    controlled = roof.lower() in {"dome", "closed"}
    temp_effect = 0.0 if controlled or temperature is None else max(-.04, min(.04, (temperature - 70) * .002))
    wind_effect = 0.0
    if not controlled and "out" in wind_text.lower():
        wind_effect = min(.045, speed * .003)
    elif not controlled and "in " in wind_text.lower():
        wind_effect = -min(.04, speed * .0025)
    return {"available": True, "temperature_f": temperature, "condition": weather.get("condition"),
            "wind": wind_text, "controlled_roof": controlled,
            "run_multiplier": round(max(.92, min(1.08, 1 + temp_effect + wind_effect)), 4),
            "method": "conservative temperature/wind adjustment capped at +/-8%"}
