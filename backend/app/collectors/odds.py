from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

import httpx

from backend.app.collectors.kbo.client import SourcePayload, _as_float
from backend.app.config import KST, settings


class OddsClient:
    """Optional structured feed used for both comparison and conservative consensus anchoring."""

    def __init__(self, timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self.base_url = "https://api.the-odds-api.com"
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout, follow_redirects=True,
                                   transport=transport, headers={"User-Agent": "DugoutLab/0.3"})

    def close(self) -> None:
        self.client.close()

    def consensus(self, league: str) -> SourcePayload:
        if not settings.odds_api_key:
            return SourcePayload([], "https://the-odds-api.com", datetime.now(KST))
        sport = "baseball_kbo" if league == "KBO" else "baseball_mlb"
        regions = settings.odds_api_regions_kbo if league == "KBO" else settings.odds_api_regions
        # spreads (the run line) reveals which club the market makes the -1.5 favorite.
        # Note: each extra market or region raises The Odds API credit cost per request.
        response = self.client.get(f"/v4/sports/{sport}/odds", params={
            "apiKey": settings.odds_api_key, "regions": regions,
            "markets": "h2h,spreads,totals", "oddsFormat": "decimal", "dateFormat": "iso",
        })
        if response.is_error:
            # Do not let httpx include the apiKey-bearing request URL in logs.
            raise RuntimeError(f"Odds provider returned HTTP {response.status_code}")
        rows = [_consensus_event(event) for event in response.json()]
        return SourcePayload(rows, str(response.url).split("?", 1)[0], datetime.now(KST))


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
        "provider": "The Odds API", "external_event_id": str(event.get("id", "")),
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
