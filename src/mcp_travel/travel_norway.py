"""Norwegian rail journey planner via Entur.

Entur is Norway's national journey-planner data hub. Entirely public,
no auth required — just a sensible User-Agent header. GraphQL endpoint
covers all Norwegian public transport, with focus here on Vy (state
rail operator) trains.

Endpoints used:
  GET https://api.entur.io/geocoder/v1/autocomplete  — text → stop place
  POST https://api.entur.io/journey-planner/v3/graphql — trip planning

Entur is the gold standard of European rail APIs — well-documented,
free, stable, no key. If only every country were like Norway.
"""

import os
from typing import Any

import httpx

ENTUR_GRAPHQL = "https://api.entur.io/journey-planner/v3/graphql"
ENTUR_GEOCODER = "https://api.entur.io/geocoder/v1/autocomplete"
ENTUR_UA = (
    f"mcp-travel/1.0 ({os.environ.get('MCP_TRAVEL_CONTACT', 'mcp-travel@example.com')}) "
    f"ET-Client-Name={os.environ.get('ENTUR_CLIENT_NAME', 'mcp-travel')}"
)


class NorwayError(RuntimeError):
    pass


async def find_stop(
    client: httpx.AsyncClient, query: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to `limit` Entur stop candidates for `query`. Rail / metro /
    tram surface first; other categories trail."""
    resp = await client.get(
        ENTUR_GEOCODER,
        params={"text": query, "size": max(limit, 5), "layers": "venue"},
        headers={"User-Agent": ENTUR_UA, "ET-Client-Name": "nonatech-mcp-travel"},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise NorwayError(f"entur geocoder {resp.status_code}: {resp.text[:300]}")
    feats = resp.json().get("features") or []
    rail_like = ("railStation", "onstreetTram", "metroStation")

    def shape(f: dict) -> dict:
        props = f.get("properties") or {}
        coord = (f.get("geometry") or {}).get("coordinates") or [None, None]
        return {
            "id": props.get("id"),
            "name": props.get("label"),
            "category": props.get("category") or [],
            "lat": coord[1],
            "lon": coord[0],
        }

    preferred = [f for f in feats if any(c in rail_like for c in (f.get("properties") or {}).get("category") or [])]
    rest = [f for f in feats if f not in preferred]
    return [shape(f) for f in (preferred + rest)[:limit]]


async def resolve_stop(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    """Use Entur geocoder to resolve free text → NSR:StopPlace:NNN id."""
    resp = await client.get(
        ENTUR_GEOCODER,
        params={"text": query, "size": 5, "layers": "venue"},
        headers={"User-Agent": ENTUR_UA, "ET-Client-Name": "nonatech-mcp-travel"},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise NorwayError(f"entur geocoder {resp.status_code}: {resp.text[:300]}")
    feats = resp.json().get("features") or []
    # Prefer railStation / stopPlace category
    for f in feats:
        cats = (f.get("properties") or {}).get("category") or []
        if any(c in ("railStation", "onstreetTram", "metroStation") for c in cats):
            return {
                "id": (f.get("properties") or {}).get("id"),
                "name": (f.get("properties") or {}).get("label"),
                "category": cats,
            }
    if feats:
        f = feats[0]
        return {
            "id": (f.get("properties") or {}).get("id"),
            "name": (f.get("properties") or {}).get("label"),
            "category": (f.get("properties") or {}).get("category"),
        }
    return None


_TRIP_QUERY = """
query Trip($from: Location!, $to: Location!, $dt: DateTime!, $n: Int!, $arriveBy: Boolean!) {
  trip(
    from: $from
    to: $to
    dateTime: $dt
    arriveBy: $arriveBy
    numTripPatterns: $n
    modes: { transportModes: [
      { transportMode: rail },
      { transportMode: bus },
      { transportMode: water }
    ] }
  ) {
    tripPatterns {
      duration
      expectedStartTime
      expectedEndTime
      legs {
        mode
        distance
        duration
        line { name publicCode operator { name } }
        fromPlace { name }
        toPlace { name }
        expectedStartTime
        expectedEndTime
      }
    }
  }
}
""".strip()


def _summarise_pattern(p: dict) -> dict:
    legs = p.get("legs") or []
    pt_legs = [l for l in legs if l.get("mode") and l["mode"] != "foot"]
    return {
        "depart": p.get("expectedStartTime"),
        "arrive": p.get("expectedEndTime"),
        "duration_seconds": p.get("duration") or 0,
        "duration_minutes": (p.get("duration") or 0) // 60,
        "transfers": max(len(pt_legs) - 1, 0),
        "legs": [
            {
                "mode": l.get("mode"),
                "from": (l.get("fromPlace") or {}).get("name"),
                "to": (l.get("toPlace") or {}).get("name"),
                "depart": l.get("expectedStartTime"),
                "arrive": l.get("expectedEndTime"),
                "duration_minutes": (l.get("duration") or 0) // 60,
                "line_name": (l.get("line") or {}).get("name"),
                "line_code": (l.get("line") or {}).get("publicCode"),
                "operator": ((l.get("line") or {}).get("operator") or {}).get("name"),
                "distance_m": l.get("distance"),
            }
            for l in legs
        ],
    }


async def search_journey(
    client: httpx.AsyncClient,
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> dict[str, Any]:
    o = await resolve_stop(client, origin)
    d = await resolve_stop(client, destination)
    if not o or not d:
        raise NorwayError(f"could not resolve origin={origin!r} or destination={destination!r}")

    body = {
        "query": _TRIP_QUERY,
        "variables": {
            "from": {"place": o["id"]},
            "to": {"place": d["id"]},
            "dt": datetime_iso,
            "arriveBy": is_arrival,
            "n": max_journeys,
        },
    }
    resp = await client.post(
        ENTUR_GRAPHQL,
        json=body,
        headers={
            "User-Agent": ENTUR_UA,
            "ET-Client-Name": "nonatech-mcp-travel",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise NorwayError(f"entur graphql {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if payload.get("errors"):
        raise NorwayError(f"entur graphql errors: {payload['errors']}")

    patterns = ((payload.get("data") or {}).get("trip") or {}).get("tripPatterns") or []
    journeys = [_summarise_pattern(p) for p in patterns[:max_journeys]]

    return {
        "ok": True,
        "mode": "rail",
        "country": "NO",
        "operator_data_source": "Entur (Norwegian national journey-planner)",
        "data_sources": ["entur-live"],
        "from": o["name"],
        "from_id": o["id"],
        "to": d["name"],
        "to_id": d["id"],
        "datetime": datetime_iso,
        "journeys": journeys,
        "booking_deeplink": f"https://www.vy.no/en/journey-planner?from={o['name']}&to={d['name']}",
    }
