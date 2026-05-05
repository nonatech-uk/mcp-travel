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


async def find_station(
    client: httpx.AsyncClient, query: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to `limit` iRail stations matching `query`. Match order:
    exact name (standardname/name), prefix, substring."""
    q = query.strip().lower()
    if not q:
        return []
    stations = await _stations(client)
    matchers = []
    for key in ("standardname", "name"):
        matchers.append(lambda s, k=key: (s.get(k) or "").lower() == q)
    for key in ("standardname", "name"):
        matchers.append(lambda s, k=key: (s.get(k) or "").lower().startswith(q))
    for key in ("standardname", "name"):
        matchers.append(lambda s, k=key: q in (s.get(k) or "").lower())
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in matchers:
        for s in stations:
            name = s.get("standardname") or s.get("name") or ""
            if not name or name in seen or not m(s):
                continue
            seen.add(name)
            matches.append({
                "name": name,
                "id": s.get("@id") or s.get("id"),
                "lat": s.get("locationY"),
                "lon": s.get("locationX"),
            })
            if len(matches) >= limit:
                return matches
    return matches


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


async def stationboard(
    client: httpx.AsyncClient,
    station: str,
    kind: str = "departure",
    limit: int = 10,
) -> dict[str, Any]:
    """Live departures or arrivals at an SNCB / NMBS station via iRail
    /liveboard. `station` is a name or iRail standardname; resolved
    via the cached station list."""
    if kind not in ("departure", "arrival"):
        raise SNCBError(f"kind must be 'departure' or 'arrival', got {kind!r}")
    name = await resolve_station(client, station)
    if not name:
        raise SNCBError(f"unknown SNCB station {station!r}")

    resp = await client.get(
        f"{IRAIL_BASE}/liveboard/",
        params={"station": name, "arrdep": kind, "format": "json"},
        headers={"User-Agent": IRAIL_UA},
        follow_redirects=True,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise SNCBError(f"irail /liveboard {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    container = payload.get("departures") or payload.get("arrivals") or {}
    rows_raw = container.get("departure") or container.get("arrival") or []

    rows = []
    for r in rows_raw[:limit]:
        platform = (r.get("platform") or {})
        platform_str = platform.get("name") if isinstance(platform, dict) else str(platform)
        rows.append({
            "time": _epoch_to_iso(r.get("time")),
            "delay_seconds": int(r.get("delay") or 0),
            "destination" if kind == "departure" else "origin": (
                r.get("station") or r.get("stationinfo", {}).get("standardname")
            ),
            "platform": platform_str,
            "vehicle": r.get("vehicle"),
            "canceled": bool(int(r.get("canceled") or 0)),
        })
    return {
        "station": name,
        "kind": kind,
        "row_count": len(rows),
        "rows": rows,
    }


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


def _vehicle_summary(vehicle: dict) -> tuple[str | None, str | None, str | None]:
    """Pull (category, train_number, line_name) out of an iRail
    vehicleinfo. `shortname` is the user-facing form ('IC 1832');
    `name` is the wire-format ID ('BE.NMBS.IC1832')."""
    short = (vehicle or {}).get("shortname") or ""
    name = (vehicle or {}).get("name") or ""
    if short:
        parts = short.split(None, 1)
        cat = parts[0] if parts else None
        num = parts[1] if len(parts) > 1 else None
        return cat, num, short
    if name:
        return None, name, name
    return None, None, None


def _build_legs(conn: dict) -> list[dict]:
    """Synthesise per-leg breakdown from iRail's connection-level data.

    iRail returns top-level departure/arrival + a list of `vias`
    (interchange points). Per-leg detail is implicit between the
    departure → first via, via → via, and last via → arrival. Each
    via's `vehicleinfo` is the train you board AT that via (i.e. the
    next leg's train). Output matches the canonical leg shape — see
    README §Leg shape (canonical).
    """
    dep = conn.get("departure") or {}
    arr = conn.get("arrival") or {}
    vias_block = conn.get("vias") or {}
    vias = vias_block.get("via", []) if isinstance(vias_block, dict) else []
    if vias is None:
        vias = []
    if not isinstance(vias, list):
        vias = [vias]

    legs: list[dict] = []

    # Leg 0: from departure to first via (or to arrival if direct).
    if vias:
        next_arr_block = vias[0].get("arrival") or {}
        next_station = vias[0].get("station")
    else:
        next_arr_block = arr
        next_station = arr.get("station")
    cat, num, line = _vehicle_summary(dep.get("vehicleinfo"))
    legs.append({
        "from": dep.get("station"),
        "to": next_station,
        "from_platform": dep.get("platform"),
        "to_platform": next_arr_block.get("platform"),
        "depart": _epoch_to_iso(dep.get("time")),
        "arrive": _epoch_to_iso(next_arr_block.get("time")),
        "duration_minutes": None,
        "operator": "SNCB",
        "category": cat,
        "train_number": num,
        "line_name": line,
        "is_walking": False,
    })

    # Subsequent legs — one per via, using the via's outbound vehicleinfo.
    for i, via in enumerate(vias):
        via_dep = via.get("departure") or {}
        if i + 1 < len(vias):
            next_arr_block = vias[i + 1].get("arrival") or {}
            next_station = vias[i + 1].get("station")
        else:
            next_arr_block = arr
            next_station = arr.get("station")
        cat, num, line = _vehicle_summary(via.get("vehicleinfo"))
        legs.append({
            "from": via.get("station"),
            "to": next_station,
            "from_platform": via_dep.get("platform"),
            "to_platform": next_arr_block.get("platform"),
            "depart": _epoch_to_iso(via_dep.get("time")),
            "arrive": _epoch_to_iso(next_arr_block.get("time")),
            "duration_minutes": None,
            "operator": "SNCB",
            "category": cat,
            "train_number": num,
            "line_name": line,
            "is_walking": False,
        })
    return legs


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
        "legs": _build_legs(conn),
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
