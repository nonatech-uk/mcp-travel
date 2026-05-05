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


async def find_station(
    client: httpx.AsyncClient, query: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to `limit` ResRobot StopLocations for `query`. Coordinate-only
    (CoordLocation) entries are dropped — they're addresses, not stops."""
    resp = await client.get(
        f"{RESROBOT_BASE}/location.name",
        params={"input": query, "format": "json", "accessId": _api_key()},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise SwedenError(f"resrobot /location.name {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    items = payload.get("stopLocationOrCoordLocation") or []
    out: list[dict[str, Any]] = []
    for it in items:
        s = it.get("StopLocation")
        if not s:
            continue
        out.append({
            "id": s.get("extId") or s.get("id"),
            "name": s.get("name"),
            "lat": s.get("lat"),
            "lon": s.get("lon"),
        })
        if len(out) >= limit:
            break
    return out


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


async def stationboard(
    client: httpx.AsyncClient,
    station: str,
    kind: str = "departure",
    limit: int = 10,
) -> dict[str, Any]:
    """Live departures or arrivals at a Swedish ResRobot station via
    /v2.1/{departureBoard|arrivalBoard}. `station` is a name or ResRobot
    extId."""
    if kind not in ("departure", "arrival"):
        raise SwedenError(f"kind must be 'departure' or 'arrival', got {kind!r}")
    resolved = await resolve_station(client, station)
    if not resolved or not resolved.get("id"):
        raise SwedenError(f"unknown ResRobot station {station!r}")
    sid = resolved["id"]

    endpoint = "departureBoard" if kind == "departure" else "arrivalBoard"
    resp = await client.get(
        f"{RESROBOT_BASE}/{endpoint}",
        params={"id": sid, "format": "json", "accessId": _api_key(), "maxJourneys": limit},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise SwedenError(f"resrobot /{endpoint} {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    rows_raw = payload.get("Departure") or payload.get("Arrival") or []

    rows = []
    for r in rows_raw[:limit]:
        product = (r.get("ProductAtStop") or (r.get("Product") or [{}])[0] if r.get("Product") else {}) or {}
        date = r.get("rtDate") or r.get("date")
        time_field = r.get("rtTime") or r.get("time")
        iso = f"{date}T{time_field}" if date and time_field else None
        rows.append({
            "time": iso,
            "planned_time": (
                f"{r.get('date')}T{r.get('time')}"
                if r.get("date") and r.get("time") else None
            ),
            "destination" if kind == "departure" else "origin": (
                r.get("direction") or r.get("origin")
            ),
            "platform": (r.get("rtTrack") or r.get("track")),
            "line_name": product.get("name") or product.get("displayNumber"),
            "category": product.get("catOut") or product.get("catCode"),
            "operator": product.get("operator"),
            "cancelled": bool(r.get("cancelled")),
        })
    return {
        "station": resolved["name"],
        "id": sid,
        "kind": kind,
        "row_count": len(rows),
        "rows": rows,
    }


def _summarise_leg(leg: dict) -> dict:
    """Canonical leg shape — see README §Leg shape (canonical).

    ResRobot ships date and time as separate fields; we compose them
    into ISO datetime to match every other rail wrapper.
    """
    o = leg.get("Origin") or {}
    d = leg.get("Destination") or {}
    prod_block = leg.get("Product")
    if isinstance(prod_block, list):
        prod = prod_block[0] if prod_block else {}
    elif isinstance(prod_block, dict):
        prod = prod_block
    else:
        prod = {}

    def _iso(date: str | None, time: str | None) -> str | None:
        if not date or not time:
            return None
        return f"{date}T{time}"

    return {
        "from": o.get("name"),
        "to": d.get("name"),
        "from_platform": o.get("track"),
        "to_platform": d.get("track"),
        "depart": _iso(o.get("date"), o.get("time")),
        "arrive": _iso(d.get("date"), d.get("time")),
        "duration_minutes": None,  # ResRobot doesn't expose per-leg duration
        "operator": prod.get("operator") or prod.get("operatorCode"),
        "category": prod.get("catOut") or prod.get("catIn") or prod.get("catOutS"),
        "train_number": prod.get("num") or prod.get("displayNumber"),
        "line_name": prod.get("name") or leg.get("name"),
        "is_walking": leg.get("type") == "WALK",
    }


def _is_internal_change(leg: dict) -> bool:
    """HAFAS internal platform-change marker — same-station zero-duration
    walking leg. Noise; filter out."""
    if not leg.get("is_walking"):
        return False
    if leg.get("from") and leg.get("to") and leg["from"] == leg["to"]:
        return True
    if leg.get("depart") and leg.get("arrive") and leg["depart"] == leg["arrive"]:
        return True
    return False


def _summarise_trip(t: dict) -> dict:
    legs = _ensure_list((t.get("LegList") or {}).get("Leg"))
    summarised = [_summarise_leg(l) for l in legs]
    summarised = [l for l in summarised if not _is_internal_change(l)]
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
