"""Irish Ferries — async wrapper around the mcp-travel-scraper sidecar.

Irish Ferries doesn't expose a public booking API and the website is
gated by Imperva Incapsula WAF, so live data has to come from a real
browser session. The actual Playwright work happens in the
`mcp-travel-scraper` container; this module is a thin async wrapper
that POSTs to its `/irish-ferries/sailings` endpoint and reshapes the
response to match the operator pattern used by `travel_dfds`,
`travel_brittany_ferries`, etc.

Sidecar URL is configurable via TRAVEL_SCRAPER_URL (default
`http://mcp-travel-scraper:8080`). A scrape takes 15-25 seconds, so
upstream callers should cache aggressively (the `travel_ferry_check`
tool's 24h `query_cache` TTL is the right answer).

Routes:
  IRLUK  Dublin/Rosslare → Holyhead/Pembroke
  UKIRL  Holyhead/Pembroke → Dublin/Rosslare
  IRLFRA Rosslare → Cherbourg
  FRAIRL Cherbourg → Rosslare
"""

import os
from typing import Any, Literal

import httpx

_SCRAPER_URL = os.environ.get("TRAVEL_SCRAPER_URL", "http://mcp-travel-scraper:8080").rstrip("/")
_HTTP_TIMEOUT = 60.0  # one scrape takes 15-25s; buffer for cold-start + WAF challenges


# (origin, destination) → Irish Ferries route code. Lower-cased keys.
_PORT_PAIR_TO_ROUTE: dict[tuple[str, str], str] = {
    # Ireland → Britain
    ("dublin",       "holyhead"):     "IRLUK",
    ("rosslare",     "pembroke"):     "IRLUK",
    ("rosslare",     "pembroke dock"): "IRLUK",
    # Britain → Ireland
    ("holyhead",     "dublin"):       "UKIRL",
    ("pembroke",     "rosslare"):     "UKIRL",
    ("pembroke dock", "rosslare"):    "UKIRL",
    # Ireland ↔ France
    ("rosslare",     "cherbourg"):    "IRLFRA",
    ("cherbourg",    "rosslare"):     "FRAIRL",
}


class IrishFerriesError(RuntimeError):
    pass


def _key(s: str) -> str:
    return s.strip().lower()


# Port name aliases used when filtering scraper output. The scraper returns
# the port names exactly as Irish Ferries renders them ("Pembroke Dock",
# "Holyhead", ...), but callers may pass "pembroke" — fold both to a single
# canonical form before comparing.
_PORT_ALIASES: dict[str, str] = {
    "pembroke dock": "pembroke",
}


def _canon_port(s: str) -> str:
    k = _key(s)
    return _PORT_ALIASES.get(k, k)


def is_known_route(origin: str, destination: str) -> bool:
    """True if the (origin, destination) port pair maps to an Irish Ferries route."""
    return (_key(origin), _key(destination)) in _PORT_PAIR_TO_ROUTE


def resolve_route(origin: str, destination: str) -> str:
    code = _PORT_PAIR_TO_ROUTE.get((_key(origin), _key(destination)))
    if not code:
        raise IrishFerriesError(
            f"unknown Irish Ferries route {origin!r} → {destination!r}; "
            f"known: {sorted(_PORT_PAIR_TO_ROUTE.keys())}"
        )
    return code


# Map our static-table vehicle vocabulary → (transport, vehicle_height) pair
# accepted by Stu's irish_ferries.py.
def _map_transport(vehicle: str) -> tuple[str, str]:
    v = vehicle.lower()
    # Tall cars + caravans flow through as cars with height adjustments.
    if v == "car":
        return "car", "standard"
    if v == "high-vehicle":
        return "car", "high"
    if v == "caravan-trailer":
        # No first-class caravan-trailer category in the form; tall car is closest.
        return "car", "high"
    if v == "motorhome":
        return "motorhome", "standard"
    if v == "motorcycle":
        return "motorcycle", "standard"
    if v == "van":
        return "van", "standard"
    # foot / bicycle / unknown → foot pricing (no dimensions field).
    return "foot", "standard"


def _normalize_sailing(s: dict[str, Any]) -> dict[str, Any]:
    """Map Stu's reference field names → the unified key names used by the
    other operator modules (DFDS, Brittany, Stena, P&O)."""
    return {
        "departure_port": s.get("departure_port", ""),
        "arrival_port":   s.get("arrival_port", ""),
        "departure":      s.get("departure_time", s.get("departure", "")),
        "arrival":        s.get("arrival_time",   s.get("arrival", "")),
        "ship":           s.get("vessel",         s.get("ship", "")),
        "available":      s.get("available", True),
        "prices":         s.get("prices", {}),
        "best_price":     s.get("best_price"),
        "currency":       s.get("currency"),
    }


async def get_sailings(
    client: httpx.AsyncClient,
    date: str,
    origin: str,
    destination: str,
    adults: int = 1,
    children: int = 0,
    vehicle: Literal["foot", "car", "motorhome", "motorcycle", "van", "bicycle", "high-vehicle", "caravan-trailer"] = "car",
) -> list[dict[str, Any]]:
    """Live Irish Ferries sailings + prices for one date.

    Returns a list of sailing dicts shaped like the other operators:
        {departure_port, arrival_port, departure (HH:MM), arrival (HH:MM),
         ship, available, prices (raw map), best_price (numeric or None),
         currency ('GBP' or 'EUR')}
    """
    route_code = resolve_route(origin, destination)
    transport, vehicle_height = _map_transport(vehicle)

    body = {
        "date": date,
        "route": route_code,
        "adults": adults,
        "children": children,
        "transport": transport,
        "vehicle_height": vehicle_height,
    }
    try:
        resp = await client.post(
            f"{_SCRAPER_URL}/irish-ferries/sailings",
            json=body,
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise IrishFerriesError(f"scraper unreachable: {e}") from e

    if resp.status_code >= 400:
        # FastAPI puts the message under "detail"; surface it cleanly.
        try:
            detail = resp.json().get("detail", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        raise IrishFerriesError(f"scraper {resp.status_code}: {detail}")

    payload = resp.json()
    sailings = [_normalize_sailing(s) for s in payload.get("sailings", [])]

    # Irish Ferries route codes (IRLUK / UKIRL) cover BOTH port-pairs on each
    # corridor — a Dublin→Holyhead query also returns Rosslare→Pembroke
    # sailings. Filter back down to the requested origin/destination.
    want_dep = _canon_port(origin)
    want_arr = _canon_port(destination)
    return [
        s for s in sailings
        if _canon_port(s.get("departure_port", "")) == want_dep
        and _canon_port(s.get("arrival_port", "")) == want_arr
    ]
