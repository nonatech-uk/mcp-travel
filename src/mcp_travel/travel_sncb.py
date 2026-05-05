"""SNCB / NMBS (Belgian Railways) — iRail community API.

iRail (api.irail.be) is the public REST wrapper around SNCB data.
No auth required; just send a sensible User-Agent. The free service is
informally tolerated by SNCB.

Endpoints used:
  GET /v1/connections — journey planner
  GET /v1/stations    — full station list (cached)
  GET /v1/liveboard   — live departures (not exposed yet)

Free-text origin/destination resolved against station list cache.
Station names follow iRail's English-dash convention ("Brussels-South",
"Antwerp-Central", "Liège-Guillemins"). Substring match handles common
variants.
"""

import os
from datetime import datetime, timezone
from typing import Any

import httpx


def _epoch_to_iso(t: Any) -> str | None:
    """iRail returns Unix epoch seconds as a string. Convert → ISO 8601 UTC."""
    if t is None or t == "":
        return None
    try:
        return datetime.fromtimestamp(int(t), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None

IRAIL_BASE = "https://api.irail.be/v1"
IRAIL_UA = f"mcp-travel/1.0 ({os.environ.get('MCP_TRAVEL_CONTACT', 'mcp-travel@example.com')})"
_STATIONS_CACHE: list[dict] = []


class SNCBError(RuntimeError):
    pass


async def _stations(client: httpx.AsyncClient) -> list[dict]:
    global _STATIONS_CACHE
    if _STATIONS_CACHE:
        return _STATIONS_CACHE
    resp = await client.get(
        f"{IRAIL_BASE}/stations/",
        params={"format": "json"},
        headers={"User-Agent": IRAIL_UA},
        follow_redirects=True,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise SNCBError(f"irail /stations {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    _STATIONS_CACHE = payload.get("station") or []
    return _STATIONS_CACHE


async def resolve_station(client: httpx.AsyncClient, query: str) -> str | None:
    """Return iRail station name (e.g. 'Brussels-South') for free text or None."""
    q = query.strip().lower()
    if not q:
        return None
    stations = await _stations(client)
    # iRail station entries have 'standardname' (English-ish) and 'name' (local)
    for key in ("standardname", "name"):
        for s in stations:
            if (s.get(key) or "").lower() == q:
                return s.get("standardname") or s.get("name")
    for key in ("standardname", "name"):
        for s in stations:
            if (s.get(key) or "").lower().startswith(q):
                return s.get("standardname") or s.get("name")
    for key in ("standardname", "name"):
        for s in stations:
            if q in (s.get(key) or "").lower():
                return s.get("standardname") or s.get("name")
    return None


def _summarise_via(via: dict) -> dict:
    arr = via.get("arrival") or {}
    dep = via.get("departure") or {}
    return {
        "station": via.get("station"),
        "arrive": _epoch_to_iso(arr.get("time")),
        "depart": _epoch_to_iso(dep.get("time")),
        "platform_arr": arr.get("platform"),
        "platform_dep": dep.get("platform"),
    }


def _summarise_connection(conn: dict) -> dict:
    dep = conn.get("departure") or {}
    arr = conn.get("arrival") or {}
    vias_block = conn.get("vias") or {}
    vias = vias_block.get("via", []) if isinstance(vias_block, dict) else []
    if vias is None:
        vias = []
    if not isinstance(vias, list):
        vias = [vias]
    return {
        "depart": _epoch_to_iso(dep.get("time")),
        "depart_station": dep.get("station"),
        "depart_platform": dep.get("platform"),
        "arrive": _epoch_to_iso(arr.get("time")),
        "arrive_station": arr.get("station"),
        "arrive_platform": arr.get("platform"),
        "duration_seconds": int(conn.get("duration") or 0),
        "duration_minutes": int(conn.get("duration") or 0) // 60,
        "transfers": len(vias),
        "vias": [_summarise_via(v) for v in vias],
        "operator": (dep.get("vehicleinfo") or {}).get("type"),  # IC / S / etc.
    }


def _format_date(iso: str) -> str:
    """ISO 'YYYY-MM-DD' → iRail 'DDMMYY'."""
    d = datetime.fromisoformat(iso)
    return d.strftime("%d%m%y")


def _format_time(iso: str) -> str:
    """ISO 'HH:MM' or 'HH:MM:SS' → iRail 'HHMM'."""
    return iso.replace(":", "")[:4]


async def search_journey(
    client: httpx.AsyncClient,
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> dict[str, Any]:
    o = await resolve_station(client, origin)
    d = await resolve_station(client, destination)
    if not o or not d:
        raise SNCBError(f"could not resolve origin={origin!r} or destination={destination!r}")

    if "T" in datetime_iso:
        date_part, time_part = datetime_iso.split("T", 1)
    else:
        date_part, time_part = datetime_iso, "08:00"
    params = {
        "from": o,
        "to": d,
        "date": _format_date(date_part),
        "time": _format_time(time_part),
        "timeSel": "arrival" if is_arrival else "depart",
        "format": "json",
    }
    resp = await client.get(
        f"{IRAIL_BASE}/connections/",
        params=params,
        headers={"User-Agent": IRAIL_UA},
        follow_redirects=True,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise SNCBError(f"irail /connections {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    conns = payload.get("connection") or []
    journeys = [_summarise_connection(c) for c in conns[:max_journeys]]

    return {
        "ok": True,
        "mode": "rail",
        "country": "BE",
        "operator_data_source": "iRail (SNCB/NMBS)",
        "data_sources": ["irail-live"],
        "from": o,
        "to": d,
        "datetime": datetime_iso,
        "journeys": journeys,
        "booking_deeplink": (
            f"https://www.belgiantrain.be/en/travel-info/route-planner?fromName={o}&toName={d}"
        ),
    }
