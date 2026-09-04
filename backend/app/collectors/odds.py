from __future__ import annotations

from datetime import date, datetime
from statistics import median
from typing import Any

import httpx

from backend.app.collectors.kbo.client import SourcePayload, _as_float
from backend.app.config import KST, settings


class OddsClient:
    """API-Sports Baseball odds feed, used only as an external comparison benchmark."""

    def __init__(self, timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self.base_url = "https://v1.baseball.api-sports.io"
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout, follow_redirects=True,
                                   transport=transport, headers={
                                       "User-Agent": "DugoutLab/0.3",
                                       "x-apisports-key": settings.api_sports_key or "",
                                   })
        self.last_usage: dict[str, int] = {}
        self.request_count = 0

    def close(self) -> None:
        self.client.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.client.get(path, params=params)
        self.request_count += 1
        self.last_usage = {
            key.removeprefix("x-ratelimit-requests-"): int(response.headers[key])
            for key in ("x-ratelimit-requests-limit", "x-ratelimit-requests-remaining")
            if response.headers.get(key, "").isdigit()
        }
        if response.is_error:
            raise RuntimeError(f"API-Sports returned HTTP {response.status_code}")
        payload = response.json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise RuntimeError(f"API-Sports returned an application error: {errors}")
        return payload

    def consensus(self, league: str, target_dates: list[date]) -> SourcePayload:
        if not settings.api_sports_key:
            return SourcePayload([], self.base_url, datetime.now(KST))
        league_id = (settings.api_sports_kbo_league_id if league == "KBO"
                     else settings.api_sports_mlb_league_id)
        game_index: dict[str, dict[str, Any]] = {}
        seasons: set[int] = set()
        for target in sorted(set(target_dates)):
            seasons.add(target.year)
            payload = self._get("/games", {
                "date": target.isoformat(), "league": league_id, "season": target.year,
                "timezone": "Asia/Seoul",
            })
            for game in payload.get("response") or []:
                game_index[str(game.get("id") or "")] = game

        rows: list[dict[str, Any]] = []
        for season in sorted(seasons):
            payload = self._get("/odds", {"league": league_id, "season": season})
            for event in payload.get("response") or []:
                game_id = str((event.get("game") or {}).get("id") or event.get("game_id") or "")
                row = _api_sports_consensus(event, game_index.get(game_id))
                if row["home_name"] and row["away_name"]:
                    rows.append(row)
        return SourcePayload(rows, f"{self.base_url}/odds", datetime.now(KST))


def _api_sports_consensus(event: dict[str, Any], game: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize API-Sports' bookmaker/bet/value nesting into the app's consensus schema."""
    game = game or event.get("game") or {}
    teams = game.get("teams") or event.get("teams") or {}
    home_name = str((teams.get("home") or {}).get("name") or event.get("home_name") or "")
    away_name = str((teams.get("away") or {}).get("name") or event.get("away_name") or "")
    converted = {
        "id": (event.get("game") or {}).get("id") or event.get("game_id") or game.get("id"),
        "commence_time": game.get("date") or event.get("date"),
        "home_team": home_name,
        "away_team": away_name,
        "bookmakers": [],
    }
    for bookmaker in event.get("bookmakers") or []:
        markets = []
        for bet in bookmaker.get("bets") or bookmaker.get("markets") or []:
            name = str(bet.get("name") or bet.get("key") or "").lower()
            values = bet.get("values") or bet.get("outcomes") or []
            if name in {"home/away", "match winner", "moneyline", "h2h"}:
                outcomes = _api_sports_outcomes(values, home_name, away_name, "h2h")
                key = "h2h"
            elif "over/under" in name or "total" in name:
                outcomes = _api_sports_outcomes(values, home_name, away_name, "totals")
                key = "totals"
            elif "handicap" in name or "run line" in name or "spread" in name:
                outcomes = _api_sports_outcomes(values, home_name, away_name, "spreads")
                key = "spreads"
            else:
                continue
            if outcomes:
                markets.append({"key": key, "outcomes": outcomes})
        if markets:
            converted["bookmakers"].append({
                "key": bookmaker.get("id") or bookmaker.get("name"), "markets": markets,
            })
    row = _consensus_event(converted)
    row["provider"] = "API-Sports Baseball"
    return row


def _api_sports_outcomes(values: list[dict[str, Any]], home_name: str, away_name: str,
                         market: str) -> list[dict[str, Any]]:
    outcomes = []
    for value in values:
        label = str(value.get("value") or value.get("name") or "").strip()
        price = _as_float(value.get("odd") if value.get("odd") is not None else value.get("price"))
        lower = label.lower()
        point = _number_from_label(label)
        if market == "h2h":
            name = home_name if lower in {"home", home_name.lower()} else away_name if lower in {"away", away_name.lower()} else ""
        elif market == "totals":
            name = "Over" if lower.startswith("over") else "Under" if lower.startswith("under") else ""
        else:
            name = home_name if lower.startswith("home") or home_name.lower() in lower else away_name if lower.startswith("away") or away_name.lower() in lower else ""
        if name and price is not None:
            outcome = {"name": name, "price": price}
            if point is not None:
                outcome["point"] = point
            outcomes.append(outcome)
    return outcomes


def _number_from_label(value: str) -> float | None:
    for token in reversed(value.replace("(", " ").replace(")", " ").split()):
        parsed = _as_float(token)
        if parsed is not None:
            return parsed
    return None


def _consensus_event(event: dict[str, Any]) -> dict[str, Any]:
    home_name, away_name = event.get("home_team", ""), event.get("away_team", "")
    home_probabilities: list[float] = []
    away_probabilities: list[float] = []
    home_decimal_odds: list[float] = []
    away_decimal_odds: list[float] = []
    total_lines: list[float] = []
    home_spreads: list[float] = []
    # A line on its own says where the market set the bar; the price beside it says how likely
    # the market thinks that bar is to be cleared. Without the price a model can only compare
    # itself against a flat 50%, which is not a market reading: a run line clears in nearly every
    # matchup once a winner is assumed, so every card would report the same side.
    #
    # A price belongs to the exact line it was quoted at, so both markets are collected as whole
    # quotes and de-vigged afterwards using only the books posting the consensus line.
    total_quotes: list[tuple[float, float, float]] = []
    spread_quotes: list[tuple[float, float, float]] = []
    used_books: set[str] = set()
    for bookmaker in event.get("bookmakers", []):
        book_used = False
        for market in bookmaker.get("markets", []):
            outcomes = market.get("outcomes", [])
            if market.get("key") == "h2h":
                prices = {row.get("name"): _as_float(row.get("price")) for row in outcomes}
                home_price, away_price = prices.get(home_name), prices.get(away_name)
                if home_price and away_price and home_price > 1 and away_price > 1:
                    raw_home, raw_away = 1 / home_price, 1 / away_price
                    home_probabilities.append(raw_home / (raw_home + raw_away))
                    away_probabilities.append(raw_away / (raw_home + raw_away))
                    # Keep the executable quote as well as the de-vigged probability.  The
                    # latter scores forecast quality; the former is required for honest ROI
                    # and closing-line-value evaluation after the game.
                    home_decimal_odds.append(home_price)
                    away_decimal_odds.append(away_price)
                    book_used = True
            elif market.get("key") == "totals":
                points = [_as_float(row.get("point")) for row in outcomes if row.get("name") in {"Over", "Under"}]
                points = [point for point in points if point is not None]
                if points:
                    total_lines.append(median(points)); book_used = True
                quote = _two_way_quote(outcomes, "Over", "Under")
                if quote:
                    total_quotes.append(quote); book_used = True
            elif market.get("key") == "spreads":
                point = next((_as_float(row.get("point")) for row in outcomes if row.get("name") == home_name), None)
                if point is not None:
                    home_spreads.append(point); book_used = True
                quote = _two_way_quote(outcomes, home_name, away_name)
                if quote:
                    spread_quotes.append(quote); book_used = True
        if book_used:
            used_books.add(str(bookmaker.get("key") or bookmaker.get("title") or len(used_books)))
    total_line = median(total_lines) if total_lines else None
    home_spread = median(home_spreads) if home_spreads else None
    return {
        "provider": "API-Sports Baseball", "external_event_id": str(event.get("id", "")),
        "commence_time": event.get("commence_time"), "home_name": home_name, "away_name": away_name,
        "bookmaker_count": len(used_books),
        "total_line": total_line,
        "home_spread": home_spread,
        # Vig removed, so each is the market's own probability for its side at the consensus
        # line. The opposite side is the complement: a half-run line cannot push.
        "total_over_probability": _devigged_at_line(total_quotes, total_line),
        "home_spread_probability": _devigged_at_line(spread_quotes, home_spread),
        "home_implied_probability": median(home_probabilities) if home_probabilities else None,
        "away_implied_probability": median(away_probabilities) if away_probabilities else None,
        "home_decimal_odds": median(home_decimal_odds) if home_decimal_odds else None,
        "away_decimal_odds": median(away_decimal_odds) if away_decimal_odds else None,
    }


def _two_way_quote(outcomes: list[dict[str, Any]], first: str,
                   second: str) -> tuple[float, float, float] | None:
    """One book's complete two-way quote as (line, first price, second price).

    Both sides must be quoted at the same line. A book offering the two halves at different
    numbers is quoting two different bets, and de-vigging across them would invent a price.
    """
    rows = {row.get("name"): row for row in outcomes}
    first_row, second_row = rows.get(first), rows.get(second)
    if not first_row or not second_row:
        return None
    first_point, second_point = _as_float(first_row.get("point")), _as_float(second_row.get("point"))
    first_price, second_price = _as_float(first_row.get("price")), _as_float(second_row.get("price"))
    if first_point is None or first_price is None or second_price is None:
        return None
    # Totals quote the same number on both sides; a spread quotes mirrored numbers.
    if second_point is not None and second_point not in (first_point, -first_point):
        return None
    if not (first_price > 1 and second_price > 1):
        return None
    return first_point, first_price, second_price


def _devigged_at_line(quotes: list[tuple[float, float, float]], line: float | None) -> float | None:
    """Median de-vigged probability of the first side, across the books quoting `line`.

    A price only means anything next to the line it was quoted at, so books posting a different
    number are dropped rather than blended into a probability for a line none of them offered.
    """
    if line is None:
        return None
    values = [(1 / first) / ((1 / first) + (1 / second))
              for point, first, second in quotes if point == line]
    return round(median(values), 6) if values else None
