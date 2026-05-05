"""Eurostar — live journey timetable + per-class seat availability.

Calls the same GraphQL endpoint Eurostar's website uses
(`site-api.eurostar.com/gateway`, `JourneySearch` operation). Returns
every train running on the date, with departure / arrival times, total
journey duration in minutes, and per-class seat counts (Standard,
Standard Premier, Business Premier).

Endpoint discovered by Stu via the site's XHR traffic; reference script
at /zfs/tank/home/stu/eurostar.py. No auth, no scraping, no Playwright.

Falls back to the static city-pair durations table (kept below) if the
GraphQL endpoint errors. Pricing is not exposed by this endpoint —
booking_url goes to the live booking flow for the chosen train.
"""

from datetime import date as date_type, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

GRAPHQL_URL = "https://site-api.eurostar.com/gateway"
_GQL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.eurostar.com",
    "Referer": "https://www.eurostar.com/uk-en/train-search",
    "x-platform": "web",
    "x-market-code": "uk",
}

_JOURNEY_QUERY = """
query JourneySearch(
  $origin: String!
  $destination: String!
  $outboundDate: String!
  $adults: Int
) {
  journeySearch(
    origin: $origin
    destination: $destination
    outboundDate: $outboundDate
    adults: $adults
  ) {
    outbound {
      journeys {
        timing { departs arrives duration departureDate }
        fares  { class { name code } seats }
      }
    }
  }
}
"""

# UIC station codes — used for booking deeplinks (Eurostar accepts them
# in some flows; mostly here as a stable identifier for our data table).
STATIONS: dict[str, dict[str, Any]] = {
    "london":     {"code": "7015400", "name": "London St Pancras",         "country": "GB"},
    "ashford":    {"code": "7015430", "name": "Ashford International",     "country": "GB"},
    "ebbsfleet":  {"code": "7015415", "name": "Ebbsfleet International",   "country": "GB"},
    "paris":      {"code": "8727100", "name": "Paris Gare du Nord",        "country": "FR"},
    "lille":      {"code": "8722326", "name": "Lille Europe",              "country": "FR"},
    "brussels":   {"code": "8814001", "name": "Brussels Midi",             "country": "BE"},
    "amsterdam":  {"code": "8400058", "name": "Amsterdam Centraal",        "country": "NL"},
    "rotterdam":  {"code": "8400530", "name": "Rotterdam Centraal",        "country": "NL"},
    "disneyland": {"code": "8711184", "name": "Marne-la-Vallée Chessy",    "country": "FR"},
    "avignon":    {"code": "8775620", "name": "Avignon Centre",            "country": "FR"},  # seasonal direct
    "marseille":  {"code": "8775100", "name": "Marseille St Charles",      "country": "FR"},  # seasonal direct
    "bourg-saint-maurice": {"code": "8771000", "name": "Bourg-Saint-Maurice", "country": "FR"},  # winter ski
    "moutiers":   {"code": "8771100", "name": "Moutiers-Salins-Brides-les-Bains", "country": "FR"},  # winter ski
    "aime":       {"code": "8771200", "name": "Aime-La Plagne",            "country": "FR"},  # winter ski
}

# Direct journey durations in minutes. Keys MUST be alphabetically sorted
# tuples so the lookup (which sorts the input pair) hits regardless of
# direction — ("london","paris") and ("paris","london") both lookup
# ("london","paris").
#
# IMPORTANT — only contains routes that actually run in 2026. Removed
# 2026-05-04 (corrected by Stu, verified against seat61):
#   • London ↔ Avignon (suspended 2019, never reinstated)
#   • London ↔ Marseille (suspended 2019, never reinstated)
#   • London ↔ Bourg-Saint-Maurice / Moutiers / Aime ski-train
#       (post-COVID operational status variable year-on-year — don't
#        assume continuity; treat as "no direct" so plan_trip composes
#        via Paris+TGV onward when SNCF data lands)
#   • Ashford International / Ebbsfleet International ↔ Paris
#       (Eurostar permanently dropped these stops in 2020; trains pass
#        through but no longer board passengers)
#
# When a non-listed pair is queried, eurostar_check correctly returns
# `direct: false` with the standard "connect via Lille Europe or Paris
# Gare du Nord; use sncf_journey/db_journey/ns_journey for the onward
# leg" message. That's the honest answer.
_DIRECT_RAW: dict[tuple[str, str], dict[str, Any]] = {
    ("london","paris"):       {"minutes": 136, "frequency": "frequent (hourly+)", "seasonal": False},
    ("lille","london"):       {"minutes":  82, "frequency": "frequent",           "seasonal": False},
    ("brussels","london"):    {"minutes": 120, "frequency": "frequent",           "seasonal": False},
    ("amsterdam","london"):   {"minutes": 232, "frequency": "several daily",      "seasonal": False},
    ("london","rotterdam"):   {"minutes": 206, "frequency": "several daily",      "seasonal": False},
    ("disneyland","london"):  {"minutes": 167, "frequency": "1–2 daily",          "seasonal": False},
}
# Normalise keys to alphabetically-sorted tuples at module load (defensive).
DIRECT_MINUTES: dict[tuple[str, str], dict[str, Any]] = {
    tuple(sorted(k)): v for k, v in _DIRECT_RAW.items()
}

ST_PANCRAS_CHECKIN_MIN = 30   # min recommended check-in for security + UK-side immigration
DEFAULT_DRIVE_TO_ST_PANCRAS = 95   # default home origin → St Pancras International by car (off-peak)


class EurostarError(RuntimeError):
    pass


def _resolve_station(query: str) -> dict[str, Any]:
    key = query.strip().lower()
    if key in STATIONS:
        return {"slug": key, **STATIONS[key]}
    for slug, v in STATIONS.items():
        if slug in key or v["name"].lower() in key:
            return {"slug": slug, **v}
    raise EurostarError(
        f"unknown Eurostar station {query!r}; known: {sorted(STATIONS)}"
    )


def _booking_url(
    o_code: str,
    d_code: str,
    date: str,
    adults: int,
    return_date: str | None = None,
) -> str:
    """Build the Eurostar search URL. Confirmed shape (2026-05-04):
        /search/uk-en?adult=N&origin=UIC&destination=UIC&outbound=YYYY-MM-DD[&inbound=YYYY-MM-DD]
    Param names: `adult` (singular), `origin`, `destination`,
    `outbound`, `inbound`. UIC station codes."""
    params: dict[str, Any] = {
        "adult": adults,
        "origin": o_code,
        "destination": d_code,
        "outbound": date,
    }
    if return_date:
        params["inbound"] = return_date
    qs = urlencode(params)
    return f"https://www.eurostar.com/search/uk-en?{qs}"


def build_booking_url(
    origin_city: str,
    dest_city: str,
    date: str,
    adults: int = 2,
    return_date: str | None = None,
) -> dict[str, Any]:
    """Public helper: resolve city slugs and return the booking URL plus
    station metadata. Used by the Safari-pricecheck workflow tool."""
    o = _resolve_station(origin_city)
    d = _resolve_station(dest_city)
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise EurostarError(f"invalid date {date!r}: {e}") from e
    if return_date:
        try:
            date_type.fromisoformat(return_date)
        except ValueError as e:
            raise EurostarError(f"invalid return_date {return_date!r}: {e}") from e
    return {
        "url": _booking_url(o["code"], d["code"], date, adults, return_date),
        "from": o["name"], "from_code": o["code"],
        "to": d["name"], "to_code": d["code"],
    }


async def _fetch_journeys(
    client: httpx.AsyncClient,
    origin_uic: str,
    dest_uic: str,
    date: str,
    adults: int,
) -> list[dict[str, Any]]:
    payload = {
        "operationName": "JourneySearch",
        "variables": {
            "origin": origin_uic,
            "destination": dest_uic,
            "outboundDate": date,
            "adults": adults,
        },
        "query": _JOURNEY_QUERY,
    }
    resp = await client.post(
        GRAPHQL_URL, json=payload, headers=_GQL_HEADERS, timeout=20.0,
    )
    if resp.status_code >= 400:
        raise EurostarError(f"eurostar graphql {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("errors"):
        raise EurostarError(f"eurostar graphql errors: {data['errors']}")
    raw = (
        (data.get("data") or {})
        .get("journeySearch", {})
        .get("outbound", {})
        .get("journeys", []) or []
    )
    out: list[dict[str, Any]] = []
    for j in raw:
        t = j.get("timing") or {}
        fares = []
        any_seats = 0
        for f in j.get("fares") or []:
            seats = f.get("seats")
            if seats is None:
                continue
            cls = f.get("class") or {}
            fares.append({
                "class_name": cls.get("name"),
                "class_code": cls.get("code"),
                "seats": seats,
                "available": seats > 0,
            })
            any_seats = max(any_seats, seats)
        out.append({
            "departure": t.get("departs"),
            "arrival": t.get("arrives"),
            "duration_minutes": t.get("duration"),
            "date": t.get("departureDate") or date,
            "fares": fares,
            "available": any_seats > 0,
        })
    out.sort(key=lambda x: (x.get("departure") or ""))
    return out


def _hhmm_to_minutes(s: str | None) -> int | None:
    if not s or len(s) < 4:
        return None
    try:
        h, m = s.split(":", 1)
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _pick_nearest_journey(
    journeys: list[dict[str, Any]], target_time: str,
) -> dict[str, Any] | None:
    if not journeys:
        return None
    target = _hhmm_to_minutes(target_time) or _hhmm_to_minutes("10:00") or 600
    available = [j for j in journeys if j.get("available")]
    pool = available or journeys
    after = [j for j in pool if (m := _hhmm_to_minutes(j.get("departure"))) is not None and m >= target]
    if after:
        return after[0]
    return min(
        pool,
        key=lambda j: abs((_hhmm_to_minutes(j.get("departure")) or 0) - target),
    )


async def check(
    client: httpx.AsyncClient | None,
    origin_city: str,
    dest_city: str,
    date: str,
    adults: int = 2,
    time: str = "10:00",
) -> dict[str, Any]:
    o = _resolve_station(origin_city)
    d = _resolve_station(dest_city)
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise EurostarError(f"invalid date {date!r}: {e}") from e

    pair = tuple(sorted([o["slug"], d["slug"]]))
    direct_static = DIRECT_MINUTES.get(pair)

    base: dict[str, Any] = {
        "ok": True,
        "mode": "eurostar",
        "from": o["name"],
        "from_code": o["code"],
        "to": d["name"],
        "to_code": d["code"],
        "date": date,
        "time": time,
        "adults": adults,
        "checkin_minutes": ST_PANCRAS_CHECKIN_MIN if o["country"] == "GB" else 30,
        "default_drive_to_st_pancras_min": DEFAULT_DRIVE_TO_ST_PANCRAS,
        "booking_url": _booking_url(o["code"], d["code"], date, adults),
        "as_of": datetime.utcnow().isoformat() + "Z",
    }

    # Try live first
    if client is not None:
        try:
            journeys = await _fetch_journeys(client, o["code"], d["code"], date, adults)
        except (EurostarError, httpx.HTTPError) as e:
            journeys = None
            base["live_error"] = str(e)
    else:
        journeys = None

    if journeys is not None:
        nearest = _pick_nearest_journey(journeys, time)
        return {
            **base,
            "source": "eurostar-live",
            "data_sources": ["eurostar-live"],
            "direct": len(journeys) > 0,
            "minutes": (nearest or {}).get("duration_minutes")
                       or (direct_static and direct_static["minutes"]),
            "selected_journey": nearest,
            "journeys": journeys,
            "journey_count": len(journeys),
            "available_count": sum(1 for j in journeys if j.get("available")),
            "note": (
                "Live timetable + seat availability per class. Prices "
                "are not exposed on this endpoint — booking_url for fares."
            ),
        }

    # Static-table fallback
    result = {
        **base,
        "source": "static-timetable",
        "data_sources": ["static-table"],
        "note": (
            "Time-only data (live timetable unavailable). "
            "Use booking_url for live availability and prices."
        ),
    }
    if direct_static:
        result["direct"] = True
        result["minutes"] = direct_static["minutes"]
        result["frequency"] = direct_static["frequency"]
        result["seasonal"] = direct_static["seasonal"]
    else:
        result["direct"] = False
        result["minutes"] = None
        result["note"] = (
            f"No direct Eurostar between {o['name']} and {d['name']}. "
            "Connect via Lille Europe or Paris Gare du Nord; consult "
            "travel_sncf_journey for the onward TGV leg."
        )
    return result
