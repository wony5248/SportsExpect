from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from html import unescape
from typing import Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from backend.app.config import KST, settings


TEAM_CODES = {
    "KIA": "HT", "키움": "WO", "롯데": "LT", "두산": "OB", "KT": "KT",
    "SSG": "SK", "삼성": "SS", "NC": "NC", "LG": "LG", "한화": "HH",
}
# The 상황별 기록 base-state rows, mapped to the league-neutral state names the engine uses.
KBO_BASE_STATES = {
    "주자없음": "BASES_EMPTY", "1루": "RUNNER_1", "2루": "RUNNER_2", "3루": "RUNNER_3",
    "1,2루": "RUNNER_12", "1,3루": "RUNNER_13", "2,3루": "RUNNER_23", "만루": "BASES_LOADED",
    "득점권": "SCORING_POSITION",
}


@dataclass
class SourcePayload:
    data: Any
    source_url: str
    collected_at: datetime


class KboClient:
    """Low-frequency client for public pages used by the official KBO website."""

    def __init__(self, timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self.base_url = settings.kbo_base_url
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": "DugoutLab/0.1 (+low-frequency educational collector)",
                "Referer": f"{self.base_url}/Schedule/GameCenter/Main.aspx",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _post_json(self, path: str, data: dict[str, str]) -> SourcePayload:
        response = self.client.post(path, content=urlencode(data), headers={"Content-Type": "application/x-www-form-urlencoded"})
        response.raise_for_status()
        return SourcePayload(response.json(), f"{self.base_url}{path}", datetime.now(KST))

    def _get_html(self, path: str) -> SourcePayload:
        response = self.client.get(path)
        response.raise_for_status()
        return SourcePayload(response.text, str(response.url), datetime.now(KST))

    def games(self, game_date: date) -> SourcePayload:
        raw = self._post_json(
            "/ws/Main.asmx/GetKboGameList",
            {"leId": "1", "srId": "0,1,3,4,5,6,7,8,9", "date": game_date.strftime("%Y%m%d")},
        )
        games = []
        for item in raw.data.get("game", []):
            if int(item.get("SR_ID", -1)) != 0:
                continue
            cancel_id = str(item.get("CANCEL_SC_ID", "0"))
            state = str(item.get("GAME_STATE_SC", "1"))
            # KBO sends this flag as a string.  bool("0") is True in Python, which used to
            # make an in-progress game look final as soon as the field appeared in the feed.
            result = _flag(item.get("GAME_RESULT_CK"))
            status = "CANCELLED" if cancel_id != "0" else ("FINAL" if result or state == "3" else ("LIVE" if state in {"2", "5"} else "SCHEDULED"))
            start_text = item.get("G_TM")
            start_at = datetime.combine(game_date, time.fromisoformat(start_text), tzinfo=KST) if start_text else None
            games.append({
                "external_id": item["G_ID"],
                "game_date": game_date,
                "venue_date": game_date,
                "start_time": item.get("G_TM"),
                "start_at": start_at,
                "away_code": item["AWAY_ID"],
                "away_name": item["AWAY_NM"].strip(),
                "home_code": item["HOME_ID"],
                "home_name": item["HOME_NM"].strip(),
                "stadium": item.get("S_NM"),
                "status": status,
                "away_score": _as_int(item.get("T_SCORE_CN")) if status == "FINAL" else None,
                "home_score": _as_int(item.get("B_SCORE_CN")) if status == "FINAL" else None,
                "away_pitcher_id": _clean_id(item.get("T_PIT_P_ID")),
                "away_pitcher_name": (item.get("T_PIT_P_NM") or "").strip() or None,
                "home_pitcher_id": _clean_id(item.get("B_PIT_P_ID")),
                "home_pitcher_name": (item.get("B_PIT_P_NM") or "").strip() or None,
                "starter_confirmed": bool(item.get("START_PIT_CK")),
                "lineup_confirmed": bool(item.get("LINEUP_CK")),
            })
        return SourcePayload(games, raw.source_url, raw.collected_at)

    def score_innings(self, game_id: str, season: int) -> SourcePayload:
        """Return official inning runs from the KBO GameCenter scoreboard."""
        raw = self._post_json(
            "/ws/Schedule.asmx/GetScoreBoardScroll",
            {"leId": "1", "srId": "0", "seasonId": str(season), "gameId": game_id},
        )
        return SourcePayload(_scoreboard_innings(raw.data), raw.source_url, raw.collected_at)

    def lineups(self, game: dict[str, Any]) -> SourcePayload:
        path = "/ws/Schedule.asmx/GetLineUpAnalysis"
        raw = self._post_json(path, {
            "leId": "1", "srId": "0", "seasonId": str(game["game_date"].year), "gameId": game["external_id"],
        })
        groups = raw.data
        confirmed = bool(groups and groups[0] and groups[0][0].get("LINEUP_CK"))
        entries: list[dict[str, Any]] = []
        for side, group_index in (("home", 3), ("away", 4)):
            if len(groups) <= group_index or not groups[group_index]:
                continue
            table = groups[group_index][0]
            if isinstance(table, str):
                import json
                table = json.loads(table)
            for wrapper in table.get("rows", []):
                values = [_text(cell.get("Text", "")) for cell in wrapper.get("row", [])]
                if len(values) < 3:
                    continue
                entries.append({
                    "side": side, "batting_order": _as_int(values[0], len(entries) + 1),
                    "player_id": None, "player_name": values[2], "position": values[1],
                    "value": _as_float(values[3]) if len(values) > 3 else None,
                    "value_metric": "WAR", "confirmed": confirmed,
                })
        return SourcePayload(entries, raw.source_url, raw.collected_at)

    def monthly_schedule(self, year: int, month: int) -> SourcePayload:
        """Return every published regular-season game, including future fixtures."""
        raw = self._post_json(
            "/ws/Schedule.asmx/GetScheduleList",
            {"leId": "1", "srIdList": "0,9,6", "seasonId": str(year), "gameMonth": f"{month:02d}", "teamId": ""},
        )
        current_day: int | None = None
        games: list[dict[str, Any]] = []
        matchup_sequences: dict[tuple[date, str, str], int] = {}
        for wrapper in raw.data.get("rows", []):
            cells = wrapper.get("row", [])
            day_cell = next((c for c in cells if c.get("Class") == "day"), None)
            if day_cell:
                match = re.search(r"\d{2}\.(\d{2})", _text(day_cell.get("Text", "")))
                current_day = int(match.group(1)) if match else None
            play_cell = next((c for c in cells if c.get("Class") == "play"), None)
            if not play_cell or current_day is None:
                continue
            soup = BeautifulSoup(play_cell.get("Text", ""), "html.parser")
            direct = soup.find_all("span", recursive=False)
            if len(direct) < 2:
                continue
            away_name, home_name = direct[0].get_text(strip=True), direct[-1].get_text(strip=True)
            scores = [int(s.get_text(strip=True)) for s in soup.select("em span") if s.get_text(strip=True).isdigit()]
            game_date = date(year, month, current_day)
            away_code, home_code = TEAM_CODES.get(away_name, away_name), TEAM_CODES.get(home_name, home_name)
            key = (game_date, away_code, home_code)
            sequence = matchup_sequences.get(key, 0)
            matchup_sequences[key] = sequence + 1
            time_cell = next((c for c in cells if c.get("Class") == "time"), None)
            time_match = re.search(r"\d{1,2}:\d{2}", _text(time_cell.get("Text", ""))) if time_cell else None
            start_time = time_match.group(0) if time_match else None
            status_text = " ".join(_text(cell.get("Text", "")) for cell in cells)
            collected_day = raw.collected_at.astimezone(KST).date()
            explicit_final = any(label in status_text for label in ("경기종료", "종료"))
            # The monthly page shows both clubs' *current* scores during a live game. Scores
            # alone therefore prove completion only for a past date; today's authoritative
            # state comes from GetKboGameList above.
            status = "CANCELLED" if "취소" in status_text else (
                "FINAL" if len(scores) == 2 and (game_date < collected_day or explicit_final)
                else "LIVE" if len(scores) == 2 else "SCHEDULED"
            )
            stadium = _text(cells[-2].get("Text", "")) if len(cells) >= 2 else ""
            games.append({
                "external_id": f"{game_date:%Y%m%d}{away_code}{home_code}{sequence}",
                "game_date": game_date, "venue_date": game_date,
                "away_name": away_name,
                "away_code": away_code, "home_name": home_name, "home_code": home_code,
                "away_score": scores[0] if status == "FINAL" else None,
                "home_score": scores[1] if status == "FINAL" else None,
                "start_time": start_time,
                "start_at": datetime.combine(game_date, time.fromisoformat(start_time), tzinfo=KST) if start_time else None,
                "stadium": stadium or None,
                "status": status,
            })
        return SourcePayload(games, raw.source_url, raw.collected_at)

    def monthly_results(self, year: int, month: int) -> SourcePayload:
        schedule = self.monthly_schedule(year, month)
        return SourcePayload(
            [game for game in schedule.data if game["status"] == "FINAL"],
            schedule.source_url,
            schedule.collected_at,
        )

    def team_stats(self) -> SourcePayload:
        rank_path = "/Record/TeamRank/TeamRank.aspx"
        hit1_path = "/Record/Team/Hitter/Basic1.aspx"
        hit2_path = "/Record/Team/Hitter/Basic2.aspx"
        pitch_path = "/Record/Team/Pitcher/Basic1.aspx"
        pages = [self._get_html(p) for p in (rank_path, hit1_path, hit2_path, pitch_path)]
        ranks = _rank_table(pages[0].data)
        hit1 = _data_id_table(pages[1].data)
        hit2 = _data_id_table(pages[2].data)
        pitching = _data_id_table(pages[3].data)
        teams: dict[str, dict[str, Any]] = {}
        for name, rank in ranks.items():
            games = int(rank.get("경기", 0))
            h1, h2, pit = hit1.get(name, {}), hit2.get(name, {}), pitching.get(name, {})
            runs = _as_float(h1.get("RUN_CN"))
            allowed = _as_float(pit.get("R_CN"))
            teams[name] = {
                "code": TEAM_CODES.get(name, name), "name": name, "games": games,
                "wins": int(rank.get("승", 0)), "losses": int(rank.get("패", 0)), "draws": int(rank.get("무", 0)),
                "win_rate": _as_float(rank.get("승률"), 0.5),
                "home_win_rate": _record_rate(rank.get("홈")), "away_win_rate": _record_rate(rank.get("방문")),
                "runs_per_game": runs / games if games and runs is not None else None,
                "runs_allowed_per_game": allowed / games if games and allowed is not None else None,
                "avg": _as_float(h1.get("HRA_RT")), "obp": _as_float(h2.get("OBP_RT")),
                "slg": _as_float(h2.get("SLG_RT")), "ops": _as_float(h2.get("OPS_RT")),
                "home_runs": _as_int(h1.get("HR_CN")), "walks": _as_int(h2.get("BB_CN")),
                "strikeouts": _as_int(h2.get("KK_CN")), "era": _as_float(pit.get("ERA_RT")),
                "whip": _as_float(pit.get("WHIP_RT")),
                "advanced": {
                    "available": any(key in h1 or key in h2 for key in ("SB_CN", "CS_CN")),
                    "source": "KBO_OFFICIAL_TEAM_HITTING",
                    "stolen_bases": _as_int(h1.get("SB_CN", h2.get("SB_CN"))),
                    "caught_stealing": _as_int(h1.get("CS_CN", h2.get("CS_CN"))),
                    "fielding_available": False,
                    "catcher_available": False,
                },
            }
        source_url = ", ".join(page.source_url for page in pages)
        return SourcePayload(teams, source_url, max(page.collected_at for page in pages))

    def starter_stats(self, game: dict[str, Any]) -> SourcePayload:
        path = "/ws/Schedule.asmx/GetPitcherRecordAnalysis"
        if not game.get("away_pitcher_id") or not game.get("home_pitcher_id"):
            return SourcePayload([], f"{self.base_url}{path}", datetime.now(KST))
        raw = self._post_json(path, {
            "leId": "1", "srId": "0", "seasonId": str(game["game_date"].year),
            "awayTeamId": game["away_code"], "awayPitId": game["away_pitcher_id"],
            "homeTeamId": game["home_code"], "homePitId": game["home_pitcher_id"], "groupSc": "SEASON",
        })
        player_ids = [str(game[f"{side}_pitcher_id"]) for side in ("away", "home")]
        logs = self.pitcher_game_logs(player_ids, game["game_date"].year)
        boundary = game.get("venue_date") or game["game_date"]
        output = []
        source_urls = [raw.source_url]
        if logs.source_url:
            source_urls.append(logs.source_url)
        for side, row in zip(("away", "home"), raw.data.get("rows", []), strict=False):
            cells = row.get("row", [])
            values = [_text(cell.get("Text", "")) for cell in cells]
            if len(values) < 7:
                continue
            name_node = BeautifulSoup(cells[0].get("Text", ""), "html.parser").select_one(".name")
            opponent = game.get("home_name" if side == "away" else "away_name")
            opponent_split: dict[str, Any] = {}
            player_id = game.get(f"{side}_pitcher_id")
            log_summary = _pitcher_log_summary(
                logs.data.get(str(player_id), []), boundary,
            )
            if player_id and opponent:
                try:
                    detail = self._get_html(f"/Record/Player/PitcherDetail/Game.aspx?playerId={player_id}")
                    source_urls.append(detail.source_url)
                    opponent_split = _pitcher_opponent_split(detail.data, opponent)
                except httpx.HTTPError:
                    # Season starter values remain usable when the optional detail page is unavailable.
                    opponent_split = {}
            output.append({
                "side": side, "player_id": game.get(f"{side}_pitcher_id"),
                "name": name_node.get_text(strip=True) if name_node else game.get(f"{side}_pitcher_name"),
                "confirmed": game.get("starter_confirmed", False), "era": _as_float(values[1]),
                "war": _as_float(values[2]),
                # QS is a percentage of starts, not of every pitching appearance.
                "games": log_summary["starts"] or _as_int(values[3]),
                "avg_start_innings": _as_float(values[4]), "quality_starts": _as_int(values[5]),
                "whip": _as_float(values[6]),
                "fip": log_summary["fip"], "k_bb_rate": log_summary["k_bb_rate"],
                "rest_days": log_summary["rest_days"],
                "recent_pitches": log_summary["recent_pitches"],
                "recent": log_summary["recent"],
                **opponent_split,
            })
        return SourcePayload(output, ", ".join(source_urls), raw.collected_at)

    def archived_starters(self, game_dates: list[date]) -> SourcePayload:
        """Who actually started each finished game on the given dates.

        The daily feed keeps its starting pitchers after the game, so this is the same
        pre-game fact for a past date that the live collector reads for today.
        """
        output: list[dict[str, Any]] = []
        urls: list[str] = []
        for game_date in sorted(set(game_dates)):
            try:
                payload = self.games(game_date)
            except httpx.HTTPError:
                continue
            urls.append(payload.source_url)
            for game in payload.data:
                if game.get("status") != "FINAL":
                    continue
                for side in ("away", "home"):
                    player_id = game.get(f"{side}_pitcher_id")
                    if not player_id:
                        continue
                    output.append({"external_id": game["external_id"], "side": side,
                                   "player_id": str(player_id),
                                   "name": game.get(f"{side}_pitcher_name")})
        return SourcePayload(output, ", ".join(dict.fromkeys(urls)) or self.base_url,
                             datetime.now(KST))

    def pitcher_game_logs(self, player_ids: list[str], season: int) -> SourcePayload:
        """Per-appearance pitching lines, so season-to-date rates can be rebuilt for any date.

        Mirrors the MLB game log shape so both leagues share one accumulation routine.
        """
        path = "/Record/Player/PitcherDetail/Daily.aspx"
        output: dict[str, list[dict[str, Any]]] = {}
        urls: list[str] = []
        for player_id in dict.fromkeys(str(row) for row in player_ids):
            try:
                page = self._get_html(f"{path}?playerId={player_id}")
            except httpx.HTTPError:
                continue
            urls.append(page.source_url)
            appearances = _pitcher_daily_log(page.data, season)
            if appearances:
                output[player_id] = appearances
        return SourcePayload(output, ", ".join(dict.fromkeys(urls)) or f"{self.base_url}{path}",
                             datetime.now(KST))

    def hitter_directory(self, team_codes: list[str]) -> SourcePayload:
        """Map hitter names to official player ids, one team at a time.

        The lineup feed carries names only, but the hitter record pages link every player id.
        A name that appears twice on one club is dropped rather than guessed at.
        """
        path = "/Record/Player/HitterBasic/Basic1.aspx"
        team_control = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ddlTeam$ddlTeam"
        pager_prefix = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ucPager$btnNo"
        directory: dict[str, dict[str, str]] = {}
        urls: list[str] = []
        for code in team_codes:
            names: dict[str, str | None] = {}
            try:
                initial = self._get_html(path)
                urls.append(initial.source_url)
                page = self._webforms_post(initial.data, path, team_control, "", {team_control: code})
                for page_number in (1, 2, 3):
                    soup = BeautifulSoup(page, "html.parser")
                    rows = soup.select("a[href*='playerId=']")
                    if not rows:
                        break
                    for anchor in rows:
                        name = anchor.get_text(strip=True)
                        match = re.search(r"playerId=(\d+)", anchor.get("href", ""))
                        if not name or not match:
                            continue
                        # Two hitters with the same name on one club cannot be told apart by
                        # the lineup feed, so neither gets an id.
                        names[name] = None if name in names and names[name] != match.group(1) else match.group(1)
                    pager = soup.select_one(f"[id$='btnNo{page_number + 1}']")
                    if pager is None:
                        break
                    page = self._webforms_post(page, path, f"{pager_prefix}{page_number + 1}", "",
                                               {team_control: code})
            except httpx.HTTPError:
                continue
            directory[code] = {name: player_id for name, player_id in names.items() if player_id}
        return SourcePayload(directory, ", ".join(dict.fromkeys(urls)) or f"{self.base_url}{path}",
                             datetime.now(KST))

    def batter_splits(self, player_ids: list[str], season: int) -> SourcePayload:
        """Read each hitter's 상황별 기록 base-state table.

        One page per hitter, so callers should pass only the lineup they are about to predict.
        A hitter whose page is unavailable is skipped rather than guessed at.
        """
        output: dict[str, dict[str, Any]] = {}
        urls: list[str] = []
        for player_id in player_ids:
            path = f"/Record/Player/HitterDetail/Situation.aspx?playerId={player_id}"
            try:
                page = self._get_html(path)
            except httpx.HTTPError:
                continue
            urls.append(page.source_url)
            states = _batter_base_states(page.data)
            if states:
                output[player_id] = {"name": _hitter_name(page.data), "states": states}
        return SourcePayload(output, ", ".join(urls) or f"{self.base_url}/Record/Player/HitterDetail/Situation.aspx",
                             datetime.now(KST))

    def batter_vs_pitcher(self, entries: list[dict[str, Any]], game: dict[str, Any]) -> SourcePayload:
        """Read KBO's WebForms-backed 투수 VS 타자 table for confirmed lineup pairs."""
        path = "/Record/Etc/HitVsPit.aspx"
        output: dict[tuple[str, str], dict[str, Any]] = {}
        collected_at = datetime.now(KST)
        for side in ("away", "home"):
            side_entries = [entry for entry in entries if entry.get("side") == side and entry.get("confirmed")]
            pitcher_id = game.get("home_pitcher_id" if side == "away" else "away_pitcher_id")
            pitcher_team = game.get("home_code" if side == "away" else "away_code")
            hitter_team = game.get("away_code" if side == "away" else "home_code")
            if not side_entries or not pitcher_id or not pitcher_team or not hitter_team:
                continue
            initial = self._get_html(path)
            pitcher_page = self._webforms_post(
                initial.data, path, _PITCHER_TEAM, "", {_PITCHER_TEAM: str(pitcher_team)},
            )
            hitter_page = self._webforms_post(
                pitcher_page, path, _HITTER_TEAM, "",
                {_PITCHER_TEAM: str(pitcher_team), _PITCHER_PLAYER: str(pitcher_id),
                 _HITTER_TEAM: str(hitter_team)},
            )
            soup = BeautifulSoup(hitter_page, "html.parser")
            hitter_options = {
                option.get_text(" ", strip=True): str(option.get("value"))
                for option in soup.select(f"#{_control_id(_HITTER_PLAYER)} option")
                if option.get("value") not in (None, "0")
            }
            for entry in side_entries:
                hitter_id = hitter_options.get(str(entry.get("player_name", "")).strip())
                if not hitter_id:
                    continue
                result_html = self._webforms_post(hitter_page, path, "", "", {
                    _PITCHER_TEAM: str(pitcher_team), _PITCHER_PLAYER: str(pitcher_id),
                    _HITTER_TEAM: str(hitter_team), _HITTER_PLAYER: hitter_id,
                    _SEARCH_BUTTON: "검색",
                })
                split = _batter_pitcher_split(result_html)
                output[(hitter_id, str(pitcher_id))] = {
                    "player_id": hitter_id, "player_name": entry.get("player_name"),
                    "opponent_pitcher_id": str(pitcher_id),
                    "matchup_plate_appearances": 0, **split,
                }
        return SourcePayload(output, f"{self.base_url}{path}", collected_at)

    def _webforms_post(self, html: str, path: str, event_target: str, event_value: str,
                       overrides: dict[str, str] | None = None) -> str:
        soup = BeautifulSoup(html, "html.parser")
        fields = {
            str(node.get("name")): str(node.get("value", ""))
            for node in soup.select("form#mainForm input[name]")
            if node.get("name") and node.get("type") in {"hidden", "text"}
        }
        fields["__EVENTTARGET"] = event_target
        fields["__EVENTARGUMENT"] = event_value
        fields.update(overrides or {})
        response = self.client.post(path, content=urlencode(fields), headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{self.base_url}{path}",
        })
        response.raise_for_status()
        return response.text


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"1", "Y", "YES", "TRUE"}


def _clean_id(value: Any) -> str | None:
    return str(value) if value not in (None, "", 0, "0") else None


def _text(value: str) -> str:
    return " ".join(BeautifulSoup(unescape(value), "html.parser").stripped_strings)


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        text = str(value).strip()
        return float(text) if text not in {"", "-", "None"} else default
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        text = str(value).strip()
        return int(float(text)) if text not in {"", "-", "None"} else default
    except (TypeError, ValueError):
        return default


def _record_rate(value: Any) -> float | None:
    match = re.match(r"(\d+)-(\d+)-(\d+)", str(value or ""))
    if not match:
        return None
    wins, draws, losses = map(int, match.groups())
    decided = wins + losses
    return wins / decided if decided else 0.5


def _rank_table(html: str) -> dict[str, dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table.tData"):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if "팀명" not in headers or "승률" not in headers or "경기" not in headers:
            continue
        result = {}
        for row in table.select("tbody tr"):
            values = [td.get_text(strip=True) for td in row.find_all("td", recursive=False)]
            if len(values) == len(headers):
                record = dict(zip(headers, values, strict=False))
                result[record["팀명"]] = record
        return result
    raise ValueError("KBO team ranking table schema not found")


def _data_id_table(html: str) -> dict[str, dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table.tData"):
        rows = table.select("tbody tr")
        if not rows or not table.select_one("td[data-id]"):
            continue
        result = {}
        for row in rows:
            cells = row.find_all("td", recursive=False)
            if len(cells) < 3:
                continue
            name = cells[1].get_text(strip=True)
            result[name] = {cell.get("data-id"): cell.get_text(strip=True) for cell in cells if cell.get("data-id")}
        return result
    raise ValueError("KBO data-id team table schema not found")


def _hitter_name(html: str) -> str | None:
    node = BeautifulSoup(html, "html.parser").select_one("[id$='playerProfile_lblName']")
    return node.get_text(strip=True) if node else None


def _batter_base_states(html: str) -> dict[str, dict[str, int]]:
    """Pull the base-state rows out of the 상황별 기록 page.

    The page stacks several situational tables that share one header, so rows are matched by
    their 구분 label instead of by table position.
    """
    soup = BeautifulSoup(html, "html.parser")
    states: dict[str, dict[str, int]] = {}
    for table in soup.select("table"):
        columns = [th.get_text(strip=True) for th in table.select("thead th")]
        if not columns or columns[0] != "구분":
            continue
        index = {name: position for position, name in enumerate(columns)}
        for row in table.select("tbody tr"):
            cells = [cell.get_text(strip=True) for cell in row.select("td")]
            state = KBO_BASE_STATES.get(cells[0]) if cells else None
            if not state or state in states:
                continue

            def count(column: str, cells: list[str] = cells, index: dict[str, int] = index) -> int:
                position = index.get(column)
                return _as_int(cells[position], 0) or 0 if position is not None and position < len(cells) else 0

            states[state] = {
                "at_bats": count("AB"), "hits": count("H"), "doubles": count("2B"),
                "triples": count("3B"), "home_runs": count("HR"), "walks": count("BB"),
                "hit_by_pitch": count("HBP"), "strikeouts": count("SO"),
                "sacrifice_flies": 0, "grounded_into_double_play": count("GDP"),
            }
    return states


def _pitcher_opponent_split(html: str, opponent: str) -> dict[str, Any]:
    """Parse the official '상대팀별' table and return a strongly sample-aware split."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one('table[summary^="상대팀별 기록"]')
    if not table:
        return {}
    for row in table.select("tbody tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
        if len(cells) < 18 or cells[0] != opponent:
            continue
        innings = _baseball_innings(cells[9])
        hits = _as_int(cells[10], 0) or 0
        walks = _as_int(cells[12], 0) or 0
        return {
            "opponent_games": _as_int(cells[1]), "opponent_innings": innings,
            "opponent_era": _as_float(cells[2]),
            "opponent_whip": round((hits + walks) / innings, 3) if innings else None,
        }
    return {}


def _pitcher_daily_log(html: str, season: int) -> list[dict[str, Any]]:
    """Parse the 일자별 기록 tables into one row per appearance, oldest first.

    Each month gets its own table whose first column holds a "MM.DD" date, so the rows carry the
    calendar information the season splits page lacks.
    """
    appearances: list[dict[str, Any]] = []
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table"):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if not headers or "IP" not in headers:
            continue
        index = {name: position for position, name in enumerate(headers)}
        if not {"TBF", "H", "BB", "SO", "ER"} <= set(index):
            continue
        for row in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in row.select("td")]
            if len(cells) < len(headers):
                continue
            match = re.match(r"^(\d{2})\.(\d{2})$", cells[0])
            if not match:
                continue

            def number(column: str, cells: list[str] = cells, index: dict[str, int] = index) -> int:
                return _as_int(cells[index[column]], 0) or 0

            appearances.append({
                "date": f"{season}-{match.group(1)}-{match.group(2)}",
                "innings": _baseball_innings(cells[index["IP"]]),
                "earned_runs": number("ER"),
                "hits": number("H"),
                "walks": number("BB"),
                "hit_batters": number("HBP") if "HBP" in index else 0,
                "batters_faced": number("TBF"),
                "strikeouts": number("SO"),
                "home_runs": number("HR"),
                "pitches": next((number(column) for column in ("NP", "PIT", "투구수")
                                 if column in index), None),
                # 구분 marks the role; anything else is a relief outing.
                "started": cells[index["구분"]] == "선발" if "구분" in index else False,
            })
    appearances.sort(key=lambda row: row["date"])
    return appearances


def _pitcher_log_summary(appearances: list[dict[str, Any]], boundary: date) -> dict[str, Any]:
    """Build leakage-safe KBO starter form from official rows strictly before first pitch."""
    prior = [row for row in appearances if date.fromisoformat(row["date"]) < boundary]
    starts = [row for row in prior if row.get("started")]
    recent = starts[-3:]
    innings = sum(float(row.get("innings") or 0) for row in prior)
    batters = sum(int(row.get("batters_faced") or 0) for row in prior)
    strikeouts = sum(int(row.get("strikeouts") or 0) for row in prior)
    walks = sum(int(row.get("walks") or 0) for row in prior)
    hit_batters = sum(int(row.get("hit_batters") or 0) for row in prior)
    homers = sum(int(row.get("home_runs") or 0) for row in prior)
    recent_innings = sum(float(row.get("innings") or 0) for row in recent)
    recent_batters = sum(int(row.get("batters_faced") or 0) for row in recent)
    recent_walks = sum(int(row.get("walks") or 0) for row in recent)
    recent_strikeouts = sum(int(row.get("strikeouts") or 0) for row in recent)
    pitch_rows = [row for row in prior if row.get("pitches") is not None and
                  (boundary - date.fromisoformat(row["date"])).days <= 5]
    recent_start_pitches = [int(row["pitches"]) for row in recent if row.get("pitches") is not None]
    last_date = date.fromisoformat(prior[-1]["date"]) if prior else None
    return {
        "starts": len(starts),
        "fip": round((13 * homers + 3 * (walks + hit_batters) - 2 * strikeouts) / innings + 3.1, 3)
        if innings else None,
        "k_bb_rate": round((strikeouts - walks) / batters, 4) if batters else None,
        "rest_days": (boundary - last_date).days if last_date else None,
        "recent_pitches": sum(int(row["pitches"]) for row in pitch_rows) if pitch_rows else None,
        "recent": {
            "available": bool(recent), "starts": len(recent),
            "era": round(9 * sum(int(row.get("earned_runs") or 0) for row in recent) / recent_innings, 3)
            if recent_innings else None,
            "whip": round((sum(int(row.get("hits") or 0) for row in recent) + recent_walks) /
                          recent_innings, 3) if recent_innings else None,
            "k_bb_rate": round((recent_strikeouts - recent_walks) / recent_batters, 4)
            if recent_batters else None,
            "avg_pitches": round(sum(recent_start_pitches) / len(recent_start_pitches), 1)
            if recent_start_pitches else None,
            "max_pitches": max(recent_start_pitches) if recent_start_pitches else None,
            "derived_pitch_limit": min(115, max(70, round(
                sum(recent_start_pitches) / len(recent_start_pitches) + 8
            ))) if recent_start_pitches else None,
            "velocity_available": False, "source": "KBO_OFFICIAL_DAILY_PITCHER_LOG",
        },
    }


def _baseball_innings(value: Any) -> float:
    text = str(value or "0").strip()
    match = re.match(r"^(\d+)(?:\s+([12])/3)?$", text)
    if not match:
        return _as_float(text, 0.0) or 0.0
    return float(match.group(1)) + (int(match.group(2)) / 3 if match.group(2) else 0.0)


def _scoreboard_innings(payload: dict[str, Any]) -> dict[str, list[int | None]] | None:
    if payload.get("code") != "100" or not payload.get("table2"):
        return None
    table = json.loads(payload["table2"])
    rows = table.get("rows") or []
    if len(rows) < 2:
        return None

    def runs(row: dict[str, Any]) -> list[int | None]:
        values: list[int | None] = []
        for cell in row.get("row") or []:
            text = str(cell.get("Text", "")).strip()
            values.append(int(text) if text.isdigit() else None)
        while values and values[-1] is None:
            values.pop()
        return values

    away, home = runs(rows[0]), runs(rows[1])
    return {"away": away, "home": home} if away and home else None


_PREFIX = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$"
_PITCHER_TEAM = f"{_PREFIX}ddlPitcherTeam"
_PITCHER_PLAYER = f"{_PREFIX}ddlPitcherPlayer"
_HITTER_TEAM = f"{_PREFIX}ddlHitterTeam"
_HITTER_PLAYER = f"{_PREFIX}ddlHitterPlayer"
_SEARCH_BUTTON = f"{_PREFIX}btnSearch"


def _control_id(name: str) -> str:
    return name.replace("ctl00$ctl00$ctl00$", "").replace("$", "_")


def _batter_pitcher_split(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    table = next((table for table in soup.select("table.tData") if
                  [node.get_text(strip=True) for node in table.select("thead th")][:4] == ["AVG", "PA", "AB", "H"]), None)
    if not table:
        return {}
    cells = [cell.get_text(" ", strip=True) for cell in table.select("tbody tr:first-child td")]
    if len(cells) < 14 or "기록이 없습니다" in " ".join(cells):
        return {}
    return {
        "matchup_avg": _as_float(cells[0]), "matchup_plate_appearances": _as_int(cells[1], 0) or 0,
        "matchup_at_bats": _as_int(cells[2], 0) or 0, "matchup_hits": _as_int(cells[3], 0) or 0,
        "matchup_doubles": _as_int(cells[4], 0) or 0, "matchup_triples": _as_int(cells[5], 0) or 0,
        "matchup_home_runs": _as_int(cells[6], 0) or 0, "matchup_walks": _as_int(cells[8], 0) or 0,
        "matchup_hit_by_pitch": _as_int(cells[9], 0) or 0, "matchup_strikeouts": _as_int(cells[10], 0) or 0,
        "matchup_slg": _as_float(cells[11]), "matchup_obp": _as_float(cells[12]), "matchup_ops": _as_float(cells[13]),
    }
