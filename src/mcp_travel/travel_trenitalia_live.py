"""Trenitalia (Le Frecce) live train tickets + prices via the BFF API.

Discovered 2026-05-04 by Stu — the BFF (Backend-For-Frontend) endpoint
at lefrecce.it/Channels.Website.BFF.WEB/website is what the React SPA
talks to internally. NOT the same as Channels.Website.WEB (the SPA
itself, which uses hash routing and exposes nothing useful in URLs).

Two-step CSRF flow:
  1. POST /whitelist/enabled → returns {"token": "..."}
  2. POST /ticket/solutions with X-CSRF-Token header set to that token

Cookies must persist across the two calls. Responses are gzip-compressed
(httpx auto-decompresses).

Returns Trenitalia's full structured pricing — every class (STANDARD /
PREMIUM / BUSINESS / BUSINESS AREA SILENZIO / EXECUTIVE on Frecciarossa,
2ª CLASSE / 1ª CLASSE on regional/intercity) and every offer tier
(BASE / Economy / Super Economy / FrecciaYOUNG / etc.) per class.

Reference script at /zfs/tank/home/stu/trenitalia.py.
"""

import re
import urllib.parse
from datetime import date as _date
from typing import Any

import httpx

API_BASE = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.lefrecce.it",
    "Referer": "https://www.lefrecce.it/",
}

# Curated station ID map — large 9-10 digit numbers prefixed 8300...
# (UIC station codes). Substring match falls back to the live
# locations/search endpoint for unknown stations.
STATIONS: dict[str, int] = {
    "milano centrale":         830001700,
    "milano":                  830001700,
    "roma termini":            830008409,
    "roma":                    830008409,
    "firenze":                 830006421,
    "firenze smn":             830006421,
    "firenze s.m. novella":    830006421,
    "napoli":                  830009218,
    "napoli centrale":         830009218,
    "torino":                  830000219,
    "torino porta nuova":      830000219,
    "venezia":                 830002593,
    "venezia s. lucia":        830002593,
    "venezia santa lucia":     830002593,
    "bologna":                 830005043,
    "bologna centrale":        830005043,
    "bari":                    830011119,
    "bari centrale":           830011119,
    "genova":                  830004700,
    "genova piazza principe":  830004700,
    "palermo":                 830012055,
    "catania":                 830012661,
    "torino porta susa":       830000222,
    "venezia mestre":          830002589,
    "napoli afragola":         830009988,
    "reggio calabria":         830010791,
    "salerno":                 830010012,
    "ancona":                  830007721,
    "verona":                  830003048,
    "verona porta nuova":      830003048,
    "padova":                  830002682,
    "trieste":                 830003627,
    "trieste centrale":        830003627,
    "brescia":                 830001463,
    "bergamo":                 830001393,
    "pisa":                    830006623,
    "perugia":                 830008052,
    "pescara":                 830008978,
    "taranto":                 830011393,
    "lecce":                   830011502,
}


class TrenitaliaLiveError(RuntimeError):
    pass


def _parse_duration(d: str | None) -> int | None:
    """Parse Trenitalia's '3h 10min' / '2h' / '45min' format → minutes."""
    if not d:
        return None
    h = re.search(r"(\d+)h", d)
    m = re.search(r"(\d+)min", d)
    total = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
    return total or None


async def _get_csrf_token(client: httpx.AsyncClient) -> str:
    """First leg of the CSRF dance: empty POST to /whitelist/enabled
    yields a session-bound token + cookies (managed by the client)."""
    resp = await client.post(
        f"{API_BASE}/whitelist/enabled",
        json={}, headers=_HEADERS, timeout=20.0,
    )
    if resp.status_code >= 400:
        raise TrenitaliaLiveError(f"CSRF init {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    token = data.get("token")
    if not token:
        raise TrenitaliaLiveError(f"no token in CSRF response: {data}")
    return token


async def _resolve_station_async(
    client: httpx.AsyncClient, token: str, name_or_id,
) -> int:
    """Resolve a station name to its numeric ID, hitting the live
    locations/search endpoint as a last resort."""
    if isinstance(name_or_id, int):
        return name_or_id
    try:
        return int(name_or_id)
    except (ValueError, TypeError):
        pass
    key = str(name_or_id).strip().lower()
    if key in STATIONS:
        return STATIONS[key]
    for k, v in STATIONS.items():
        if key in k or k in key:
            return v
    # Live lookup
    resp = await client.get(
        f"{API_BASE}/locations/search",
        params={"name": name_or_id},
        headers={**_HEADERS, "X-CSRF-Token": token},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise TrenitaliaLiveError(
            f"locations/search {resp.status_code}: {resp.text[:200]}"
        )
    results = resp.json()
    if not results:
        raise TrenitaliaLiveError(f"unknown Trenitalia station: {name_or_id!r}")
    return results[0]["id"]


async def find_station(client: httpx.AsyncClient, name: str) -> list[dict[str, Any]]:
    """Search Trenitalia stations by name. Returns id / name / multistation."""
    token = await _get_csrf_token(client)
    resp = await client.get(
        f"{API_BASE}/locations/search",
        params={"name": name},
        headers={**_HEADERS, "X-CSRF-Token": token},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise TrenitaliaLiveError(f"find_station {resp.status_code}: {resp.text[:200]}")
    return [
        {
            "id": r.get("id"),
            "name": r.get("displayName") or r.get("name"),
            "multistation": r.get("multistation", False),
        }
        for r in resp.json() or []
    ]


async def get_solutions(
    client: httpx.AsyncClient,
    date: str,
    origin,
    destination,
    adults: int = 1,
    children: int = 0,
    departure_after: str = "00:00",
    limit: int = 10,
    offset: int = 0,
    order: str = "DEPARTURE_DATE",
    frecce_only: bool = False,
    regional_only: bool = False,
    intercity_only: bool = False,
) -> list[dict[str, Any]]:
    """Live Trenitalia solutions for a date + route. Returns the full
    list of trains with per-class per-offer pricing.

    Fields per solution: departure / arrival / duration_minutes /
    trains (list of {category, number}) / changes / status / currency
    ('EUR') / best_price / prices (class → cheapest) / offers (class →
    {offer_name: amount})."""
    try:
        _date.fromisoformat(date)
    except ValueError as e:
        raise TrenitaliaLiveError(f"invalid date {date!r}: {e}") from e

    # The httpx.AsyncClient handles cookies persistently within its scope.
    # We need cookies to cross the whitelist/enabled → ticket/solutions
    # boundary, so use a fresh sub-client with cookies enabled.
    cookies: httpx.Cookies = httpx.Cookies()
    headers_with_cookies = {**_HEADERS}

    # We can't share the parent client's cookies cleanly across calls
    # without ensuring it has its own cookie jar; httpx.AsyncClient
    # already does. So just use the parent client.

    token = await _get_csrf_token(client)
    origin_id = await _resolve_station_async(client, token, origin)
    dest_id = await _resolve_station_async(client, token, destination)

    h, m = (departure_after.split(":") + ["0", "0"])[:2] if ":" in departure_after else ("0", "0")
    dep_time = f"{date}T{int(h):02d}:{int(m):02d}:00.000"

    body = {
        "departureLocationId": origin_id,
        "arrivalLocationId": dest_id,
        "departureTime": dep_time,
        "adults": adults,
        "children": children,
        "criteria": {
            "frecceOnly": frecce_only,
            "regionalOnly": regional_only,
            "intercityOnly": intercity_only,
            "tourismOnly": False,
            "noChanges": False,
            "order": order,
            "offset": offset,
            "limit": limit,
        },
        "advancedSearchRequest": {
            "bestFare": False,
            "bikeFilter": False,
        },
    }

    resp = await client.post(
        f"{API_BASE}/ticket/solutions",
        json=body,
        headers={**_HEADERS, "X-CSRF-Token": token},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise TrenitaliaLiveError(
            f"ticket/solutions {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()

    out: list[dict[str, Any]] = []
    for sol in data.get("solutions", []) or []:
        s = sol.get("solution") or {}
        dep_str = s.get("departureTime", "")
        arr_str = s.get("arrivalTime", "")

        trains = [
            {
                "category": t.get("trainCategory") or t.get("denomination") or "",
                "number": t.get("name") or t.get("description") or "",
            }
            for t in s.get("trains", []) or []
        ]

        class_prices: dict[str, float] = {}
        class_offers: dict[str, dict[str, float]] = {}
        for grid in sol.get("grids", []) or []:
            for svc in grid.get("services", []) or []:
                svc_name = svc.get("name", "")
                avail = {
                    o.get("name"): (o.get("price") or {}).get("amount")
                    for o in svc.get("offers", []) or []
                    if o.get("status") == "SALEABLE"
                       and (o.get("price") or {}).get("amount") is not None
                       and o.get("name")
                }
                avail = {k: v for k, v in avail.items() if v is not None}
                if avail and svc_name not in class_prices:
                    class_prices[svc_name] = min(avail.values())
                    class_offers[svc_name] = avail

        best = min(class_prices.values()) if class_prices else None

        out.append({
            "departure_date": dep_str[:10] if dep_str else date,
            "departure": dep_str[11:16] if dep_str else "",
            "arrival_date": arr_str[:10] if arr_str else date,
            "arrival": arr_str[11:16] if arr_str else "",
            "duration_minutes": _parse_duration(s.get("duration")),
            "trains": trains,
            "changes": max(0, len(trains) - 1),
            "status": s.get("status", ""),
            "currency": "EUR",
            "best_price": best,
            "prices": class_prices,
            "offers": class_offers,
        })
    return out
