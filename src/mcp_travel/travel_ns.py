"""Nederlandse Spoorwegen (NS) — Reisinformatie API journey planner.

Auth: subscription key via `Ocp-Apim-Subscription-Key` header. Free tier
covers personal use; sign up at apiportal.ns.nl, subscribe to the
`NsApp` product.

Endpoints used:
  GET /v2/stations          — full station list (cached)
  GET /v3/trips             — journey planner

Free-text origin/destination is resolved via case-insensitive substring
match on the station list (cached at module level after first call).
"""

import os
from datetime import datetime
from typing import Any

import httpx

NS_BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"
_STATIONS_CACHE: list[dict] = []


class NSError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    k = os.environ.get("NS_API_KEY")
    if not k:
        raise NSError("NS_API_KEY is not set")
    return {"Ocp-Apim-Subscription-Key": k, "Accept": "application/json"}


async def _stations(client: httpx.AsyncClient) -> list[dict]:
    global _STATIONS_CACHE
    if _STATIONS_CACHE:
        return _STATIONS_CACHE
    resp = await client.get(f"{NS_BASE}/v2/stations", headers=_headers(), timeout=30.0)
    if resp.status_code >= 400:
        raise NSError(f"ns /stations {resp.status_code}: {resp.text[:300]}")
    payload = resp.json().get("payload") or []
    _STATIONS_CACHE = payload
    return payload


async def resolve_station(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    """Resolve free text to NS station record. Returns dict with code + name or None."""
    q = query.strip().lower()
    if not q:
        return None
    stations = await _stations(client)
    # Exact code match first (e.g. 'ASD', 'RTD', 'UT')
    if 2 <= len(q) <= 6:
        for s in stations:
            if (s.get("code") or "").lower() == q:
                return {"code": s["code"], "name": s.get("namen", {}).get("lang"), "country": s.get("land")}
    # Long name exact, then short name, then substring
    for s in stations:
        if (s.get("namen", {}).get("lang") or "").lower() == q:
            return {"code": s["code"], "name": s["namen"]["lang"], "country": s.get("land")}
    for s in stations:
        if (s.get("namen", {}).get("lang") or "").lower().startswith(q):
            return {"code": s["code"], "name": s["namen"]["lang"], "country": s.get("land")}
    for s in stations:
        if q in (s.get("namen", {}).get("lang") or "").lower():
            return {"code": s["code"], "name": s["namen"]["lang"], "country": s.get("land")}
    return None


def _summarise_leg(leg: dict) -> dict:
    o = leg.get("origin") or {}
    d = leg.get("destination") or {}
    product = leg.get("product") or {}
    return {
        "from": o.get("name"),
        "from_track": o.get("plannedTrack") or o.get("actualTrack"),
        "to": d.get("name"),
        "to_track": d.get("plannedTrack") or d.get("actualTrack"),
        "depart": o.get("plannedDateTime"),
        "arrive": d.get("plannedDateTime"),
        "duration_minutes": leg.get("plannedDurationInMinutes") or 0,
        "operator": product.get("operatorName"),
        "category": product.get("categoryCode") or product.get("longCategoryName"),
        "train_number": product.get("number"),
        "cancelled": leg.get("cancelled", False),
    }


def _summarise_trip(trip: dict) -> dict:
    legs = [_summarise_leg(l) for l in (trip.get("legs") or [])]
    return {
        "duration_minutes": trip.get("plannedDurationInMinutes"),
        "actual_duration_minutes": trip.get("actualDurationInMinutes"),
        "transfers": max(len(legs) - 1, 0),
        "optimal": trip.get("optimal"),
        "crowd_forecast": trip.get("crowdForecast"),
        "depart": legs[0]["depart"] if legs else None,
        "arrive": legs[-1]["arrive"] if legs else None,
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
        raise NSError(f"could not resolve origin={origin!r} or destination={destination!r}")

    params: dict[str, Any] = {
        "fromStation": o["code"],
        "toStation": d["code"],
        "dateTime": datetime_iso,
        "searchForArrival": "true" if is_arrival else "false",
    }
    resp = await client.get(f"{NS_BASE}/v3/trips", params=params, headers=_headers(), timeout=30.0)
    if resp.status_code >= 400:
        raise NSError(f"ns /trips {resp.status_code}: {resp.text[:300]}")

    trips = resp.json().get("trips") or []
    journeys = [_summarise_trip(t) for t in trips[:max_journeys]]

    return {
        "ok": True,
        "mode": "rail",
        "country": "NL",
        "operator_data_source": "NS Reisinformatie",
        "data_sources": ["ns-live"],
        "from": o["name"],
        "from_code": o["code"],
        "to": d["name"],
        "to_code": d["code"],
        "datetime": datetime_iso,
        "is_arrival_time": is_arrival,
        "journeys": journeys,
        "booking_deeplink": f"https://www.ns.nl/en/journeyplanner/#/?vertrek={o['code']}&aankomst={d['code']}",
    }
