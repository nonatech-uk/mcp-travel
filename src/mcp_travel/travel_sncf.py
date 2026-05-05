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


def _summarise_section(s: dict) -> dict:
    kind = s.get("type")
    out: dict[str, Any] = {
        "type": kind,
        "depart": s.get("departure_date_time"),
        "arrive": s.get("arrival_date_time"),
        "duration_minutes": (s.get("duration") or 0) // 60,
    }
    di = s.get("display_informations") or {}
    if kind == "public_transport":
        out["mode"] = di.get("commercial_mode") or di.get("network")
        out["operator"] = di.get("network")
        out["headsign"] = di.get("headsign")
        out["train_id"] = di.get("trip_short_name") or di.get("headsign")
        out["from"] = (s.get("from") or {}).get("name")
        out["to"] = (s.get("to") or {}).get("name")
        sdt = s.get("stop_date_times") or []
        out["stops"] = max(len(sdt) - 2, 0)
    else:
        if s.get("from"):
            out["from"] = s["from"].get("name")
        if s.get("to"):
            out["to"] = s["to"].get("name")
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
