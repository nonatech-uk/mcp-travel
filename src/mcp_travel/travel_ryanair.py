"""Ryanair — async wrapper around the mcp-travel-scraper sidecar.

Ryanair's availability API returns 409 when called directly (Imperva
WAF), so live data has to come from a real browser session that
intercepts the page's own fetch. The Playwright work runs in
`mcp-travel-scraper`; this module is a thin async wrapper that POSTs
to its `/ryanair/flights` endpoint.

Ryanair is the canonical hole in Duffel inventory (see
`mcp_travel_data_source_gaps.md`), so this is the only way to surface
its prices alongside flagship carriers.

Sidecar URL is configurable via `TRAVEL_SCRAPER_URL` (default
`http://mcp-travel-scraper:8080`). Per-call latency 10-20s; cache
aggressively.

Important: Ryanair's `prices` are *totals for all queried passengers
combined*, not per-person. Passing `adults=2` returns the 2-adult total.
"""

import os
from typing import Any

import httpx

_SCRAPER_URL = os.environ.get(
    "TRAVEL_SCRAPER_URL", "http://mcp-travel-scraper:8080"
).rstrip("/")
_HTTP_TIMEOUT = 60.0


class RyanairError(RuntimeError):
    pass


async def get_flights(
    client: httpx.AsyncClient,
    date: str,
    origin: str,
    destination: str,
    adults: int = 1,
    teens: int = 0,
    children: int = 0,
    infants: int = 0,
    include_sold_out: bool = False,
) -> list[dict[str, Any]]:
    """Live Ryanair flights for one date + IATA pair.

    Returns flight dicts with keys: date, flight_number, departure (HH:MM),
    arrival (HH:MM), duration_minutes, origin, destination, available,
    fares_left, currency, best_price (numeric), prices (raw map of
    fare_type → numeric total).

    Raises:
        RyanairError on sidecar unreachable / non-2xx response.
    """
    body = {
        "date": date,
        "origin": origin,
        "destination": destination,
        "adults": adults,
        "teens": teens,
        "children": children,
        "infants": infants,
        "include_sold_out": include_sold_out,
    }
    try:
        resp = await client.post(
            f"{_SCRAPER_URL}/ryanair/flights",
            json=body,
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise RyanairError(f"scraper unreachable: {e}") from e

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        raise RyanairError(f"scraper {resp.status_code}: {detail}")

    payload = resp.json()
    return payload.get("flights", [])
