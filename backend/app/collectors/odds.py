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
    total_lines: list[float] = []
    home_spreads: list[float] = []
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
                    book_used = True
            elif market.get("key") == "totals":
                points = [_as_float(row.get("point")) for row in outcomes if row.get("name") in {"Over", "Under"}]
                points = [point for point in points if point is not None]
                if points:
                    total_lines.append(median(points)); book_used = True
            elif market.get("key") == "spreads":
                point = next((_as_float(row.get("point")) for row in outcomes if row.get("name") == home_name), None)
                if point is not None:
                    home_spreads.append(point); book_used = True
        if book_used:
            used_books.add(str(bookmaker.get("key") or bookmaker.get("title") or len(used_books)))
    return {
        "provider": "The Odds API", "external_event_id": str(event.get("id", "")),
        "commence_time": event.get("commence_time"), "home_name": home_name, "away_name": away_name,
        "bookmaker_count": len(used_books),
        "total_line": median(total_lines) if total_lines else None,
        "home_spread": median(home_spreads) if home_spreads else None,
        "home_implied_probability": median(home_probabilities) if home_probabilities else None,
        "away_implied_probability": median(away_probabilities) if away_probabilities else None,
    }
