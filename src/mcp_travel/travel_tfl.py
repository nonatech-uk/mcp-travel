"""Transport for London API — journey planning + stop search + line status.

TfL exposes a genuinely first-class public API:
  - api.tfl.gov.uk
  - Free tier: 50 req/min for journey, 500 req/min for status (no key)
  - App key tier (500/min everywhere) requires registration; we don't
    need it at our volume.

What it covers:
  - London Underground (Tube, all lines)
  - DLR (Docklands Light Railway)
  - London Overground
  - Elizabeth Line (Crossrail)
  - Trams (Croydon)
  - Buses (>700 routes)
  - River (Thames Clippers)
  - Cable Car (Emirates Air Line)
  - National Rail trains into London zones (TfL surfaces these in
    Journey Planner via Open Rail Data feeds)

What it doesn't cover:
  - Long-distance National Rail (use travel_uk_journey for that)
  - Anywhere outside Greater London commuter area

Three useful endpoints exposed:
  /Journey/JourneyResults/{from}/to/{to}
  /StopPoint/Search/{query}
  /Line/{mode-or-id}/Status
"""

from datetime import date as _date, datetime
from typing import Any
from urllib.parse import quote

import httpx

API_BASE = "https://api.tfl.gov.uk"

_HEADERS = {
    "User-Agent": "mcp-travel/1.0 (personal-use trip planner)",
    "Accept": "application/json",
}


class TflError(RuntimeError):
    pass


def _parse_iso_minutes(d: str | None) -> int | None:
    """Parse 'PT15M' / 'PT1H30M' to minutes."""
    if not d:
        return None
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", d)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    return h * 60 + mins or None


async def find_stop(
    client: httpx.AsyncClient, query: str, max_results: int = 8,
) -> list[dict[str, Any]]:
    """Search TfL stops by name. Returns id / name / mode.

    Modes are TfL's own classification: 'tube', 'bus', 'national-rail',
    'overground', 'elizabeth-line', 'dlr', 'tram', 'river-bus', 'pier',
    'cable-car', 'coach'.
    """
    resp = await client.get(
        f"{API_BASE}/StopPoint/Search/{quote(query)}",
        params={"maxResults": max_results},
        headers=_HEADERS, timeout=15.0,
    )
    if resp.status_code >= 400:
        raise TflError(f"TfL StopPoint/Search {resp.status_code}: {resp.text[:200]}")
    data = resp.json()

    out: list[dict[str, Any]] = []
    for m in data.get("matches", []) or []:
        out.append({
            "id":     m.get("id"),
            "name":   m.get("name"),
            "modes":  m.get("modes") or [],
            "lat":    m.get("lat"),
            "lon":    m.get("lon"),
            "zone":   m.get("zone"),
        })
    return out


def _disambig_pick(disamb: dict | None) -> str | None:
    """TfL returns a disambiguation list when a free-text stop matches
    multiple places. Pick the highest-quality option that's actually a
    StopPoint (not an Area / Postcode / Borough), preferring tube/rail
    interchange hubs over individual platforms."""
    if not disamb:
        return None
    options = disamb.get("disambiguationOptions") or []
    if not options:
        return None

    def _score(opt: dict) -> tuple:
        place = opt.get("place") or {}
        ptype = place.get("placeType") or ""
        modes = place.get("modes") or []
        ics = place.get("icsCode")
        naptan = place.get("naptanId") or place.get("id")
        # Multi-mode HUB places (icsCode like 'HUBKGX') are best:
        # they cover Tube + National Rail + Elizabeth Line at once.
        is_hub = bool(ics) and str(ics).startswith("HUB")
        is_stop = ptype == "StopPoint" or bool(naptan)
        is_transit = any(m in modes for m in
                         ("tube", "national-rail", "elizabeth-line",
                          "overground", "dlr"))
        return (
            is_hub,            # prefer hubs
            is_stop,           # prefer real stops
            is_transit,        # prefer transit-served
            opt.get("matchQuality", 0),
        )

    options.sort(key=_score, reverse=True)
    top = options[0]
    place = top.get("place") or {}
    return place.get("icsCode") or place.get("naptanId") or place.get("id")


async def journey(
    client: httpx.AsyncClient,
    origin: str,
    destination: str,
    datetime_iso: str | None = None,
    is_arrival: bool = False,
    modes: list[str] | None = None,
    max_journeys: int = 4,
) -> list[dict[str, Any]]:
    """Plan a journey via TfL Journey Planner.

    Args:
        origin: Stop name ('Waterloo'), TfL StopPoint ID
                ('940GZZLUKSX' for King's Cross St Pancras), or
                'lat,lon' coordinate pair.
        destination: Same.
        datetime_iso: ISO 8601 datetime ('2026-05-11T09:00'). If None,
                      uses 'now'.
        is_arrival: If True, treat datetime as required arrival time.
        modes: Filter to specific modes — e.g. ['tube', 'bus', 'walking'].
               None = all modes.
        max_journeys: Limit on returned options.

    Returns:
        List of journey dicts: depart / arrive / duration_minutes /
        legs[] / fare (if priced).
    """
    params: dict[str, Any] = {}
    if datetime_iso:
        try:
            dt = datetime.fromisoformat(datetime_iso.replace("Z", ""))
        except ValueError as e:
            raise TflError(f"invalid datetime_iso {datetime_iso!r}: {e}") from e
        params["date"] = dt.strftime("%Y%m%d")
        params["time"] = dt.strftime("%H%M")
        params["timeIs"] = "Arriving" if is_arrival else "Departing"
    if modes:
        params["mode"] = ",".join(modes)

    async def _query(o: str, d: str) -> dict:
        url = f"{API_BASE}/Journey/JourneyResults/{quote(o)}/to/{quote(d)}"
        r = await client.get(url, params=params, headers=_HEADERS, timeout=20.0)
        if r.status_code >= 400:
            raise TflError(f"TfL Journey {r.status_code}: {r.text[:300]}")
        return r.json()

    data = await _query(origin, destination)

    # TfL returns a disambiguation page if stop names are ambiguous —
    # pick the top match and retry once.
    if not data.get("journeys"):
        from_disambig = _disambig_pick(data.get("fromLocationDisambiguation"))
        to_disambig = _disambig_pick(data.get("toLocationDisambiguation"))
        if from_disambig or to_disambig:
            data = await _query(from_disambig or origin, to_disambig or destination)

    journeys = data.get("journeys") or []
    out: list[dict[str, Any]] = []
    for j in journeys[:max_journeys]:
        legs_out = []
        for leg in j.get("legs", []) or []:
            mode = (leg.get("mode") or {}).get("name") or ""
            instr = (leg.get("instruction") or {}).get("summary") or ""
            from_pt = (leg.get("departurePoint") or {}).get("commonName") or ""
            to_pt = (leg.get("arrivalPoint") or {}).get("commonName") or ""
            legs_out.append({
                "mode": mode,
                "minutes": leg.get("duration"),
                "from": from_pt,
                "to": to_pt,
                "instruction": instr,
                "depart": leg.get("departureTime"),
                "arrive": leg.get("arrivalTime"),
            })

        fare = (j.get("fare") or {}).get("totalCost")
        out.append({
            "depart": j.get("startDateTime"),
            "arrive": j.get("arrivalDateTime"),
            "duration_minutes": j.get("duration"),
            "legs": legs_out,
            "fare_pence": fare,
            "fare_gbp": (fare / 100.0) if fare else None,
        })
    return out


async def line_status(
    client: httpx.AsyncClient,
    target: str = "tube",
) -> list[dict[str, Any]]:
    """Get live status for a TfL line or mode.

    Args:
        target: Either a mode ('tube', 'bus', 'overground', 'dlr',
                'tram', 'elizabeth-line', 'national-rail') for all
                lines in that mode, or a specific line id ('victoria',
                'central', 'piccadilly', 'jubilee', etc.).

    Returns:
        List of {id, name, modeName, statusSeverity, reason}.
    """
    # /Line/Mode/{modes}/Status   or   /Line/{ids}/Status
    if target in ("tube", "bus", "overground", "dlr", "tram",
                  "elizabeth-line", "national-rail", "river-bus",
                  "cable-car"):
        url = f"{API_BASE}/Line/Mode/{target}/Status"
    else:
        url = f"{API_BASE}/Line/{quote(target)}/Status"

    resp = await client.get(url, headers=_HEADERS, timeout=15.0)
    if resp.status_code >= 400:
        raise TflError(f"TfL Line Status {resp.status_code}: {resp.text[:200]}")
    data = resp.json()

    out: list[dict[str, Any]] = []
    for line in data:
        statuses = line.get("lineStatuses") or []
        primary = statuses[0] if statuses else {}
        out.append({
            "id":              line.get("id"),
            "name":            line.get("name"),
            "modeName":        line.get("modeName"),
            "statusSeverity":  primary.get("statusSeverityDescription"),
            "reason":          primary.get("reason") or "",
        })
    return out
