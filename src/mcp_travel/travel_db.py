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


async def resolve_station(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    """Returns top /locations match (preferring 'stop' type) or None."""
    resp = await client.get(
        f"{_base()}/locations",
        params={"query": query, "results": 5, "stops": "true", "addresses": "false", "poi": "false"},
        headers={"User-Agent": DB_UA},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise DBError(f"db-rest /locations {resp.status_code}: {resp.text[:300]}")
    items = resp.json()
    if not items:
        return None
    # Prefer entries marked as 'stop' (i.e. actual stations)
    stops = [it for it in items if it.get("type") == "stop"]
    pick = stops[0] if stops else items[0]
    return {"id": pick.get("id"), "name": pick.get("name"), "type": pick.get("type")}


def _summarise_leg(leg: dict) -> dict:
    line = leg.get("line") or {}
    return {
        "from": (leg.get("origin") or {}).get("name"),
        "to": (leg.get("destination") or {}).get("name"),
        "depart": leg.get("plannedDeparture"),
        "arrive": leg.get("plannedArrival"),
        "depart_platform": leg.get("plannedDeparturePlatform"),
        "arrive_platform": leg.get("plannedArrivalPlatform"),
        "operator": (line.get("operator") or {}).get("name"),
        "line_name": line.get("name"),
        "product": line.get("product"),    # ICE / IC / RE / S / U / Bus etc.
        "is_walking": leg.get("walking", False),
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
