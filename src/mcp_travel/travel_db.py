"""Deutsche Bahn (DB) — db-rest community API via transport.rest.

`v6.db.transport.rest` is Jannis R.'s public REST wrapper around DB's
HAFAS engine. No auth required. Maintained as a community service —
occasionally has downtime; we fail-soft and let the caller see the error.

Endpoints used:
  GET /locations  — station/poi search by free text (returns id + name)
  GET /journeys   — journey planner (origin id → destination id)

Free-text origin/destination is resolved via /locations (chooses the
top result of type 'stop') before /journeys is hit.

If you ever want a more reliable backend, self-host db-rest in a
container alongside mcp-travel and point DB_BASE at it.
"""

import os
from typing import Any

import httpx

DB_BASE_DEFAULT = "https://v6.db.transport.rest"
DB_UA = f"mcp-travel/1.0 ({os.environ.get('MCP_TRAVEL_CONTACT', 'mcp-travel@example.com')})"


class DBError(RuntimeError):
    pass


def _base() -> str:
    return os.environ.get("DB_REST_BASE", DB_BASE_DEFAULT).rstrip("/")


async def _locations(client: httpx.AsyncClient, query: str, results: int) -> list[dict]:
    resp = await client.get(
        f"{_base()}/locations",
        params={"query": query, "results": results, "stops": "true", "addresses": "false", "poi": "false"},
        headers={"User-Agent": DB_UA},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise DBError(f"db-rest /locations {resp.status_code}: {resp.text[:300]}")
    return resp.json() or []


async def find_station(
    client: httpx.AsyncClient, query: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to `limit` DB station matches via db-rest /locations.
    Stop-typed entries surface first; addresses/POIs trail."""
    items = await _locations(client, query, max(limit, 5))
    if not items:
        return []
    stops = [it for it in items if it.get("type") == "stop"]
    others = [it for it in items if it.get("type") != "stop"]
    out: list[dict[str, Any]] = []
    for it in (stops + others)[:limit]:
        loc = it.get("location") or {}
        out.append({
            "id": it.get("id"),
            "name": it.get("name"),
            "type": it.get("type"),
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
        })
    return out


async def resolve_station(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    """Returns top /locations match (preferring 'stop' type) or None."""
    items = await _locations(client, query, 5)
    if not items:
        return None
    stops = [it for it in items if it.get("type") == "stop"]
    pick = stops[0] if stops else items[0]
    return {"id": pick.get("id"), "name": pick.get("name"), "type": pick.get("type")}


def _summarise_leg(leg: dict) -> dict:
    """Canonical leg shape — see README §Leg shape (canonical).

    db-rest's `line.name` typically combines category + train number
    ('ICE 41', 'IC 2363'). We split those out so `category` and
    `train_number` are clean separate fields.
    """
    line = leg.get("line") or {}
    name = line.get("name") or ""
    # Try to split "ICE 41" → ("ICE", "41"); fall back to whole string as line_name
    train_number = None
    if " " in name:
        head, tail = name.rsplit(" ", 1)
        if tail.replace("-", "").replace("/", "").isalnum():
            train_number = tail
    return {
        "from": (leg.get("origin") or {}).get("name"),
        "to": (leg.get("destination") or {}).get("name"),
        "from_platform": leg.get("plannedDeparturePlatform"),
        "to_platform": leg.get("plannedArrivalPlatform"),
        "depart": leg.get("plannedDeparture"),
        "arrive": leg.get("plannedArrival"),
        "duration_minutes": None,  # db-rest doesn't expose per-leg duration
        "operator": (line.get("operator") or {}).get("name"),
        "category": line.get("product"),    # ICE / IC / RE / S / U / Bus etc.
        "train_number": train_number,
        "line_name": name,
        "is_walking": leg.get("walking", False),
    }


async def stationboard(
    client: httpx.AsyncClient,
    station: str,
    kind: str = "departure",
    limit: int = 10,
) -> dict[str, Any]:
    """Live departures or arrivals at a DB station via db-rest
    /stops/{id}/{departures|arrivals}. `station` is a name or db-rest id."""
    if kind not in ("departure", "arrival"):
        raise DBError(f"kind must be 'departure' or 'arrival', got {kind!r}")
    resolved = await resolve_station(client, station)
    if not resolved:
        raise DBError(f"unknown DB station {station!r}")
    sid = resolved["id"]

    endpoint = "departures" if kind == "departure" else "arrivals"
    resp = await client.get(
        f"{_base()}/stops/{sid}/{endpoint}",
        params={"results": limit, "duration": 60},
        headers={"User-Agent": DB_UA},
        timeout=45.0,
    )
    if resp.status_code >= 400:
        raise DBError(f"db-rest /{endpoint} {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    rows_raw = payload.get(endpoint) or payload if isinstance(payload, list) else (payload.get(endpoint) or [])

    rows = []
    for r in rows_raw[:limit]:
        line = r.get("line") or {}
        other = (r.get("destination") or r.get("origin") or {})
        rows.append({
            "time": r.get("when") or r.get("plannedWhen"),
            "planned_time": r.get("plannedWhen"),
            "delay_seconds": r.get("delay"),
            "destination" if kind == "departure" else "origin": other.get("name"),
            "platform": r.get("platform") or r.get("plannedPlatform"),
            "line_name": line.get("name"),
            "product": line.get("product"),
            "operator": (line.get("operator") or {}).get("name"),
            "trip_id": r.get("tripId"),
            "cancelled": bool(r.get("cancelled")),
        })
    return {
        "station": resolved["name"],
        "id": sid,
        "kind": kind,
        "row_count": len(rows),
        "rows": rows,
    }


def _summarise_journey(j: dict) -> dict:
    legs = [_summarise_leg(l) for l in (j.get("legs") or [])]
    pt_legs = [l for l in legs if not l.get("is_walking")]
    if pt_legs:
        depart = pt_legs[0]["depart"]
        arrive = pt_legs[-1]["arrive"]
    else:
        depart = legs[0]["depart"] if legs else None
        arrive = legs[-1]["arrive"] if legs else None
    # duration: take from first→last leg if exposed, else compute none
    return {
        "depart": depart,
        "arrive": arrive,
        "transfers": max(len(pt_legs) - 1, 0),
        "legs": legs,
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
        raise DBError(f"could not resolve origin={origin!r} or destination={destination!r}")

    # db-rest expects ISO 8601 with timezone; assume UTC if naive
    dt = datetime_iso if ("+" in datetime_iso or "Z" in datetime_iso) else datetime_iso + "Z"

    params: dict[str, Any] = {
        "from": o["id"],
        "to": d["id"],
        "results": max_journeys,
    }
    if is_arrival:
        params["arrival"] = dt
    else:
        params["departure"] = dt

    resp = await client.get(
        f"{_base()}/journeys",
        params=params,
        headers={"User-Agent": DB_UA},
        timeout=45.0,
    )
    if resp.status_code >= 400:
        raise DBError(f"db-rest /journeys {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    journeys_raw = payload.get("journeys") or []
    journeys = [_summarise_journey(j) for j in journeys_raw[:max_journeys]]

    return {
        "ok": True,
        "mode": "rail",
        "country": "DE",
        "operator_data_source": f"db-rest ({_base()})",
        "data_sources": ["hafas-live"],
        "from": o["name"],
        "from_id": o["id"],
        "to": d["name"],
        "to_id": d["id"],
        "datetime": datetime_iso,
        "journeys": journeys,
        "booking_deeplink": (
            f"https://www.bahn.com/en/buchung/start?S={o['name'].replace(' ','+')}&Z={d['name'].replace(' ','+')}"
        ),
    }
