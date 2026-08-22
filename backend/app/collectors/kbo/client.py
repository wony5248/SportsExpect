from __future__ import annotations

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
            result = bool(item.get("GAME_RESULT_CK"))
            status = "CANCELLED" if cancel_id != "0" else ("FINAL" if result or state == "3" else ("LIVE" if state in {"2", "5"} else "SCHEDULED"))
            start_text = item.get("G_TM")
            start_at = datetime.combine(game_date, time.fromisoformat(start_text), tzinfo=KST) if start_text else None
            games.append({
                "external_id": item["G_ID"],
                "game_date": game_date,
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

    def monthly_results(self, year: int, month: int) -> SourcePayload:
        raw = self._post_json(
            "/ws/Schedule.asmx/GetScheduleList",
            {"leId": "1", "srIdList": "0,9,6", "seasonId": str(year), "gameMonth": f"{month:02d}", "teamId": ""},
        )
        current_day: int | None = None
        results: list[dict[str, Any]] = []
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
            if len(scores) != 2:
                continue
            results.append({
                "game_date": date(year, month, current_day),
                "away_name": away_name,
                "home_name": home_name,
                "away_score": scores[0],
                "home_score": scores[1],
            })
        return SourcePayload(results, raw.source_url, raw.collected_at)

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
        output = []
        source_urls = [raw.source_url]
        for side, row in zip(("away", "home"), raw.data.get("rows", []), strict=False):
            cells = row.get("row", [])
            values = [_text(cell.get("Text", "")) for cell in cells]
            if len(values) < 7:
                continue
            name_node = BeautifulSoup(cells[0].get("Text", ""), "html.parser").select_one(".name")
            opponent = game.get("home_name" if side == "away" else "away_name")
            opponent_split: dict[str, Any] = {}
            player_id = game.get(f"{side}_pitcher_id")
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
                "war": _as_float(values[2]), "games": _as_int(values[3]),
                "avg_start_innings": _as_float(values[4]), "quality_starts": _as_int(values[5]),
                "whip": _as_float(values[6]),
                **opponent_split,
            })
        return SourcePayload(output, ", ".join(source_urls), raw.collected_at)

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


def _baseball_innings(value: Any) -> float:
    text = str(value or "0").strip()
    match = re.match(r"^(\d+)(?:\s+([12])/3)?$", text)
    if not match:
        return _as_float(text, 0.0) or 0.0
    return float(match.group(1)) + (int(match.group(2)) / 3 if match.group(2) else 0.0)


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
