"""SNCF Navitia API client.

Free tier: 5,000 req/month at api.sncf.com (Navitia, coverage='sncf').
Auth is HTTP Basic — API key as username, blank password.

Inputs accept three forms for origin/destination:
  - 'stop_area:SNCF:87686006' (Navitia ID, fastest)
  - '48.8443;2.3735'          (lat;lon, in Navitia order)
  - 'Paris Gare de Lyon'      (free text, resolved via /places)

Live pricing/booking is **not** in the public API — sncf-connect.com is the
canonical price source. We return a search deeplink instead.
"""

import os
from typing import Any
from urllib.parse import quote

import httpx

NAV_BASE = "https://api.sncf.com/v1/coverage/sncf"


class SncfError(RuntimeError):
    pass


def _auth() -> tuple[str, str]:
    key = os.environ.get("SNCF_API_KEY")
    if not key:
        raise SncfError("SNCF_API_KEY is not set")
    return (key, "")


def _looks_like_id(s: str) -> bool:
    return s.startswith(("stop_area:", "stop_point:", "admin:", "address:", "poi:"))


def _looks_like_coord(s: str) -> bool:
    if ";" not in s:
        return False
    a, b = s.split(";", 1)
    try:
        float(a)
        float(b)
        return True
    except ValueError:
        return False


def _fmt_dt(iso: str) -> str:
    """ISO datetime → YYYYMMDDTHHMMSS (Navitia format)."""
    cleaned = iso.replace("-", "").replace(":", "")
    if "T" not in cleaned:
        cleaned += "T000000"
    date, time = cleaned.split("T", 1)
    time = (time + "000000")[:6]
    return f"{date}T{time}"


async def find_place(
    client: httpx.AsyncClient, query: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to `limit` Navitia /places candidates. stop_area first, then
    administrative_region / address / poi. Already-resolved IDs/coords short-
    circuit to a single self-describing entry."""
    if _looks_like_id(query) or _looks_like_coord(query):
        return [{"id": query, "name": query, "embedded_type": "raw"}]

    resp = await client.get(
        f"{NAV_BASE}/places",
        params=[
            ("q", query),
            ("count", str(max(limit, 5))),
            ("type[]", "stop_area"),
            ("type[]", "address"),
            ("type[]", "administrative_region"),
        ],
        auth=_auth(),
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise SncfError(f"sncf /places {resp.status_code}: {resp.text[:300]}")
    places = resp.json().get("places", []) or []

    rank = {"stop_area": 0, "administrative_region": 1, "address": 2, "poi": 3}
    places_sorted = sorted(places, key=lambda p: rank.get(p.get("embedded_type"), 99))

    out: list[dict[str, Any]] = []
    for p in places_sorted[:limit]:
        coord = p.get("coord") or {}
        out.append({
            "id": p.get("id") or p.get("uri"),
            "name": p.get("name"),
            "embedded_type": p.get("embedded_type"),
            "lat": coord.get("lat"),
            "lon": coord.get("lon"),
        })
    return out


async def resolve_place(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    """Resolve free text via Navitia /places. Returns {id, name, embedded_type}."""
    if _looks_like_id(query) or _looks_like_coord(query):
        return {"id": query, "name": query, "embedded_type": "raw"}

    resp = await client.get(
        f"{NAV_BASE}/places",
        params=[
            ("q", query),
            ("type[]", "stop_area"),
            ("type[]", "address"),
            ("type[]", "administrative_region"),
        ],
        auth=_auth(),
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise SncfError(f"sncf /places {resp.status_code}: {resp.text[:300]}")

    places = resp.json().get("places", [])
    by_kind: dict[str, dict] = {}
    for p in places:
        by_kind.setdefault(p.get("embedded_type"), p)
    for kind in ("stop_area", "administrative_region", "address", "poi"):
        if kind in by_kind:
            p = by_kind[kind]
            return {"id": p.get("id") or p.get("uri"), "name": p.get("name"), "embedded_type": kind}
    return None


async def stationboard(
    client: httpx.AsyncClient,
    station: str,
    kind: str = "departure",
    limit: int = 10,
) -> dict[str, Any]:
    """Live departures or arrivals at an SNCF stop_area via Navitia.

    `station` is a name, Navitia stop_area id, or 'lat;lon' coords.
    Resolves via /places (stop_area first), then queries the
    /coverage/sncf/stop_areas/{id}/{departures|arrivals} endpoint.
    """
    if kind not in ("departure", "arrival"):
        raise SncfError(f"kind must be 'departure' or 'arrival', got {kind!r}")
    resolved = await resolve_place(client, station)
    if not resolved or not resolved.get("id"):
        raise SncfError(f"unknown SNCF place {station!r}")
    sid = resolved["id"]

    endpoint = "departures" if kind == "departure" else "arrivals"
    resp = await client.get(
        f"{NAV_BASE}/stop_areas/{sid}/{endpoint}",
        params={"count": limit},
        auth=_auth(),
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise SncfError(f"sncf /{endpoint} {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    rows_raw = payload.get(endpoint) or []

    rows = []
    for r in rows_raw[:limit]:
        sdt = r.get("stop_date_time") or {}
        di = (r.get("display_informations") or {})
        route = (r.get("route") or {})
        time_field = sdt.get("departure_date_time") if kind == "departure" else sdt.get("arrival_date_time")
        base_field = sdt.get("base_departure_date_time") if kind == "departure" else sdt.get("base_arrival_date_time")
        rows.append({
            "time": time_field,
            "planned_time": base_field,
            "destination": di.get("direction") or route.get("name"),
            "platform": (sdt.get("data_freshness") and r.get("stop_point", {}).get("name")) or None,
            "line_name": di.get("label") or di.get("commercial_mode"),
            "headsign": di.get("headsign"),
            "operator": di.get("network"),
            "physical_mode": di.get("physical_mode"),
        })
    return {
        "station": resolved.get("name"),
        "id": sid,
        "kind": kind,
        "row_count": len(rows),
        "rows": rows,
    }


def _summarise_section(s: dict) -> dict:
    """Canonical leg shape — see README §Leg shape (canonical).

    SNCF Navitia uses 'sections' instead of 'legs' and labels walks/
    transfers as type='street_network'/'transfer'/'waiting'. The
    is_walking flag captures all non-public-transport sections so
    callers can ignore them in change-counting.
    """
    kind = s.get("type")
    di = s.get("display_informations") or {}
    out: dict[str, Any] = {
        "from": (s.get("from") or {}).get("name") if s.get("from") else None,
        "to": (s.get("to") or {}).get("name") if s.get("to") else None,
        "from_platform": None,    # Navitia /journeys doesn't expose platforms
        "to_platform": None,
        "depart": s.get("departure_date_time"),
        "arrive": s.get("arrival_date_time"),
        "duration_minutes": (s.get("duration") or 0) // 60,
        "type": kind,             # public_transport / street_network / transfer / waiting
        "is_walking": kind != "public_transport",
    }
    if kind == "public_transport":
        out["operator"] = di.get("network")
        out["category"] = di.get("commercial_mode") or di.get("network")
        out["train_number"] = di.get("trip_short_name") or di.get("headsign")
        out["line_name"] = di.get("label") or di.get("commercial_mode")
        out["headsign"] = di.get("headsign")
        sdt = s.get("stop_date_times") or []
        out["stops"] = max(len(sdt) - 2, 0)
    return out


def _summarise_journey(j: dict) -> dict:
    sections = [_summarise_section(s) for s in j.get("sections", [])]
    pt_only = [s for s in sections if s.get("type") == "public_transport"]
    return {
        "departure": j.get("departure_date_time"),
        "arrival": j.get("arrival_date_time"),
        "duration_minutes": (j.get("duration") or 0) // 60,
        "transfers": max(len(pt_only) - 1, 0),
        "co2_grams": (j.get("co2_emission") or {}).get("value"),
        "sections": sections,
    }


def _deeplink(from_name: str, to_name: str, datetime_iso: str) -> str:
    date_part = datetime_iso.split("T", 1)[0] if "T" in datetime_iso else datetime_iso
    return (
        "https://www.sncf-connect.com/app/home/search"
        f"?origin={quote(from_name)}&destination={quote(to_name)}&outward={quote(date_part)}"
    )


async def search_journey(
    client: httpx.AsyncClient,
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> dict[str, Any]:
    o = await resolve_place(client, origin)
    d = await resolve_place(client, destination)
    if not o or not d:
        raise SncfError(
            f"could not resolve origin={origin!r} or destination={destination!r}"
        )

    resp = await client.get(
        f"{NAV_BASE}/journeys",
        params={
            "from": o["id"],
            "to": d["id"],
            "datetime": _fmt_dt(datetime_iso),
            "datetime_represents": "arrival" if is_arrival else "departure",
            "count": max_journeys,
        },
        auth=_auth(),
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise SncfError(f"sncf /journeys {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    journeys = [_summarise_journey(j) for j in payload.get("journeys", [])]

    return {
        "ok": True,
        "mode": "rail",
        "from": o["name"],
        "from_id": o["id"],
        "to": d["name"],
        "to_id": d["id"],
        "datetime": datetime_iso,
        "journeys": journeys,
        "booking_deeplink": _deeplink(o["name"], d["name"], datetime_iso),
    }
