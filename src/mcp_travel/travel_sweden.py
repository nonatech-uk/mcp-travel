"""Swedish rail / national journey planner via Trafiklab ResRobot v2.1.

ResRobot is the consolidated Swedish national journey-planner API
(formerly "Reseplanerare 2"). HAFAS-based — covers SJ (national rail),
regional operators, bus, tram, ferry, the Stockholm Tunnelbana, etc.

Auth: API key in `accessId` query parameter (not header — note the
unusual placement). Free tier 30k requests/month at trafiklab.se.

Endpoints:
  GET /location.name?input=...  — station / location autocomplete
  GET /trip?originId=...&destId=...&date=...&time=... — journey planner
  GET /departureBoard?id=...     — live departures (not exposed yet)

Response shape is HAFAS-Sweden flavoured: `Trip[]` of journeys, each
with `LegList.Leg` (which can be a list OR a single dict — defensive
handling needed). Times come back as `HH:MM:SS` plus a separate date.
"""

import os
from typing import Any

import httpx

RESROBOT_BASE = "https://api.resrobot.se/v2.1"


class SwedenError(RuntimeError):
    pass


def _api_key() -> str:
    k = os.environ.get("RESROBOT_API_KEY")
    if not k:
        raise SwedenError("RESROBOT_API_KEY is not set")
    return k


async def resolve_station(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    """Resolve free-text query to a ResRobot station extId. Prefers rail stations."""
    resp = await client.get(
        f"{RESROBOT_BASE}/location.name",
        params={"input": query, "format": "json", "accessId": _api_key()},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise SwedenError(f"resrobot /location.name {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    items = payload.get("stopLocationOrCoordLocation") or []
    # Each item is {"StopLocation": {...}} or {"CoordLocation": {...}}
    stops = []
    for it in items:
        if "StopLocation" in it:
            stops.append(it["StopLocation"])
    if not stops:
        return None
    # Prefer entries with rail (cls 1=ICE, 2=IC, 4=Intercity, 8=Express, 16=Regional, etc.)
    # Just take the first stop
    s = stops[0]
    return {
        "id": s.get("extId") or s.get("id"),
        "name": s.get("name"),
    }


def _ensure_list(x: Any) -> list:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _summarise_leg(leg: dict) -> dict:
    o = leg.get("Origin") or {}
    d = leg.get("Destination") or {}
    prod_block = leg.get("Product")
    if isinstance(prod_block, list):
        prod = prod_block[0] if prod_block else {}
    elif isinstance(prod_block, dict):
        prod = prod_block
    else:
        prod = {}
    notes_block = leg.get("Notes") or {}
    return {
        "from": o.get("name"),
        "from_time": o.get("time"),
        "from_date": o.get("date"),
        "from_track": o.get("track"),
        "to": d.get("name"),
        "to_time": d.get("time"),
        "to_date": d.get("date"),
        "to_track": d.get("track"),
        "operator": prod.get("operator") or prod.get("operatorCode"),
        "category": prod.get("catOut") or prod.get("catIn") or prod.get("catOutS"),
        "line_name": prod.get("name") or leg.get("name"),
        "line_number": prod.get("num") or prod.get("displayNumber"),
        "is_walking": leg.get("type") == "WALK",
    }


def _summarise_trip(t: dict) -> dict:
    legs = _ensure_list((t.get("LegList") or {}).get("Leg"))
    summarised = [_summarise_leg(l) for l in legs]
    pt_legs = [l for l in summarised if not l.get("is_walking")]
    o = t.get("Origin") or {}
    d = t.get("Destination") or {}
    duration_iso = t.get("duration", "")
    # duration format: "PnDTnHnM" or "HH:MM" depending on response
    return {
        "depart": f"{o.get('date','')}T{o.get('time','')}".rstrip("T"),
        "arrive": f"{d.get('date','')}T{d.get('time','')}".rstrip("T"),
        "duration_iso": duration_iso,
        "transfers": max(len(pt_legs) - 1, 0),
        "legs": summarised,
    }


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
        raise SwedenError(f"could not resolve origin={origin!r} or destination={destination!r}")

    # ResRobot expects date YYYY-MM-DD and time HH:MM separately
    if "T" in datetime_iso:
        date_part, time_part = datetime_iso.split("T", 1)
        time_part = time_part[:5]   # HH:MM
    else:
        date_part = datetime_iso
        time_part = "08:00"

    params = {
        "originId": o["id"],
        "destId": d["id"],
        "date": date_part,
        "time": time_part,
        "format": "json",
        "numF": max_journeys,
        "accessId": _api_key(),
    }
    if is_arrival:
        params["searchForArrival"] = 1

    resp = await client.get(
        f"{RESROBOT_BASE}/trip",
        params=params,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise SwedenError(f"resrobot /trip {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    trips = payload.get("Trip") or []
    journeys = [_summarise_trip(t) for t in trips[:max_journeys]]

    return {
        "ok": True,
        "mode": "rail",
        "country": "SE",
        "operator_data_source": "Trafiklab ResRobot v2.1",
        "data_sources": ["resrobot-live"],
        "from": o["name"],
        "from_id": o["id"],
        "to": d["name"],
        "to_id": d["id"],
        "datetime": datetime_iso,
        "journeys": journeys,
        "booking_deeplink": (
            f"https://www.sj.se/en?from={o['name'].replace(' ','+')}&to={d['name'].replace(' ','+')}"
        ),
    }
