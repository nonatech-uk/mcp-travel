"""MCP server for UK rail: RTT next-gen API (data.rtt.io) + Transport API.

Division of labour:
- Transport API  → uk_find_station (name→CRS), uk_journey (change-aware planner)
- RTT next-gen   → uk_stationboard, uk_service (calling pattern), uk_disruptions

RTT auth is a long-life bearer token obtained at https://api-portal.rtt.io.
Transport API auth is an app_id + app_key from https://developer.transportapi.com.
"""

import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime, timedelta

import httpx
from fastmcp import FastMCP

# RTT next-gen — Bearer token. What you get from api-portal.rtt.io is a
# refresh token; we exchange it for a 20-minute access token on demand.
RTT_BASE = os.environ.get("RTT_BASE", "https://data.rtt.io").rstrip("/")
RTT_TOKEN = os.environ.get("RTT_BEARER_TOKEN")  # treat as refresh token
RTT_NAMESPACE = os.environ.get("RTT_NAMESPACE", "gb-nr")

# Access-token cache (one shared across all tool calls in this process).
_access_token: str | None = None
_access_token_exp: float = 0.0  # epoch seconds
_access_token_lock = asyncio.Lock()

# Transport API — app_id / app_key in query params
TAPI_BASE = os.environ.get("TAPI_BASE", "https://transportapi.com/v3").rstrip("/")
TAPI_APP_ID = os.environ.get("TRANSPORT_API_APP_ID")
TAPI_APP_KEY = os.environ.get("TRANSPORT_API_APP_KEY")

USER_AGENT = "mcp-travel/1.0 (+https://github.com/nonatech-uk/mcp-travel)"
HTTP_TIMEOUT = 20.0


def register(mcp: FastMCP) -> None:
    """Register travel_rail_gb_* tools onto the shared FastMCP instance.

    Old `travel_uk_*` names are kept as deprecation aliases (Stage 3a
    rename, 2026-05-05). They will be removed in a future release.
    """
    # Primary (canonical) names — ISO-2 country code.
    mcp.tool(name="travel_rail_gb_find_station")(uk_find_station)
    mcp.tool(name="travel_rail_gb_stationboard")(uk_stationboard)
    mcp.tool(name="travel_rail_gb_journey")(uk_journey)
    mcp.tool(name="travel_rail_gb_service")(uk_service)
    mcp.tool(name="travel_rail_gb_disruptions")(uk_disruptions)

    # Deprecation aliases — old `travel_uk_*` names.
    _dep = "DEPRECATED: renamed to `travel_rail_gb_{}`. Same signature; same behaviour. Will be removed in a future release."
    mcp.tool(name="travel_uk_find_station", description=_dep.format("find_station"))(uk_find_station)
    mcp.tool(name="travel_uk_stationboard", description=_dep.format("stationboard"))(uk_stationboard)
    mcp.tool(name="travel_uk_journey",      description=_dep.format("journey"))(uk_journey)
    mcp.tool(name="travel_uk_service",      description=_dep.format("service"))(uk_service)
    mcp.tool(name="travel_uk_disruptions",  description=_dep.format("disruptions"))(uk_disruptions)


# ---------- formatting helpers ----------

CRS_RE = re.compile(r"^[A-Z0-9]{3,7}$")  # short (3 letters) or long (up to 7)


def _looks_like_code(s: str) -> bool:
    """Heuristic: CRS short code (3 upper) or TIPLOC long code (up to 7 upper/digits)."""
    return bool(s) and bool(CRS_RE.match(s))


def _ns_code(code: str) -> str:
    """Ensure a location code has the namespace prefix (e.g. 'KGX' → 'gb-nr:KGX')."""
    if ":" in code:
        return code
    return f"{RTT_NAMESPACE}:{code}"


def _fmt_time(iso: str | None) -> str:
    """Format an ISO 8601 datetime as HH:MM local; accepts None."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return str(iso)[:16]


def _fmt_date_header(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a %d %b %H:%M")
    except (ValueError, TypeError):
        return ""


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _delay_marker(temporal: dict | None) -> str:
    """Extract delay/cancellation marker from an IndividualTemporalData block."""
    if not temporal:
        return ""
    if temporal.get("isCancelled"):
        return " [CANCELLED]"
    late = temporal.get("realtimeAdvertisedLateness")
    if late is None:
        late = temporal.get("realtimeInternalLateness")
    if late and late != 0:
        sign = "+" if late > 0 else ""
        return f" [{sign}{int(late)}']"
    return ""


def _effective_time(temporal: dict | None) -> str | None:
    """Pick the best time to display: actual > forecast > estimate > advertised."""
    if not temporal:
        return None
    for k in ("realtimeActual", "realtimeForecast", "realtimeEstimate", "scheduleAdvertised", "scheduleInternal"):
        v = temporal.get(k)
        if v:
            return v
    return None


def _display_code(location: dict | None) -> str:
    if not location:
        return "—"
    shorts = location.get("shortCodes") or []
    return shorts[0] if shorts else (location.get("longCodes") or ["—"])[0]


def _display_name(location: dict | None) -> str:
    if not location:
        return "—"
    return location.get("description") or _display_code(location)


# ---------- Transport API (unchanged from previous) ----------

async def _tapi_get(path: str, params: dict) -> dict:
    if not TAPI_APP_ID or not TAPI_APP_KEY:
        raise RuntimeError(
            "Transport API credentials not configured "
            "(set TRANSPORT_API_APP_ID and TRANSPORT_API_APP_KEY in .env.uk_trains)."
        )
    params = {**params, "app_id": TAPI_APP_ID, "app_key": TAPI_APP_KEY}
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        r = await client.get(f"{TAPI_BASE}{path}", params=clean)
        if r.status_code == 401:
            raise RuntimeError("Transport API auth rejected (401). Check app_id/app_key.")
        if r.status_code == 403:
            # Distinguish "bad creds" from "endpoint not in your plan"
            body = (r.text or "")[:400]
            if "not part of your plan" in body:
                raise RuntimeError(
                    "Transport API: this endpoint is not included in your current "
                    "plan. Upgrade at developer.transportapi.com."
                )
            raise RuntimeError(f"Transport API auth rejected (403): {body}")
        if r.status_code == 429:
            raise RuntimeError("Transport API rate limit hit. Try again later.")
        r.raise_for_status()
        return r.json()


async def _resolve_crs(station: str) -> tuple[str, str]:
    """Return (short_code, display_name) for a given station string (CRS or name)."""
    if _looks_like_code(station):
        return station.upper(), station.upper()
    data = await _tapi_get("/uk/places.json", {"query": station, "type": "train_station"})
    places = data.get("member", [])
    if not places:
        raise RuntimeError(f"No UK station found matching '{station}'.")
    top = places[0]
    crs = top.get("station_code") or top.get("atcocode") or ""
    name = top.get("name") or station
    if not crs:
        raise RuntimeError(f"Station '{station}' has no CRS code in the search result.")
    return crs, name


# ---------- RTT next-gen ----------

def _jwt_exp(token: str) -> float:
    """Extract the `exp` claim from a JWT without verifying the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return float(claims.get("exp", 0))
    except (ValueError, IndexError, json.JSONDecodeError):
        return 0.0


async def _refresh_access_token() -> str:
    """Exchange the configured refresh token for a fresh access token."""
    global _access_token, _access_token_exp
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {RTT_TOKEN}",
            "Accept": "application/json",
        },
    ) as client:
        r = await client.get(f"{RTT_BASE}/api/get_access_token")
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"RTT refresh rejected ({r.status_code}). Check RTT_BEARER_TOKEN "
                "(get one from https://api-portal.rtt.io)."
            )
        r.raise_for_status()
        data = r.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        raise RuntimeError(f"RTT refresh returned no token; got keys: {list(data)}")
    _access_token = token
    _access_token_exp = _jwt_exp(token)
    return token


async def _get_access_token() -> str:
    """Return a valid access token, refreshing if expired or near expiry."""
    if not RTT_TOKEN:
        raise RuntimeError(
            "RTT bearer token not configured "
            "(set RTT_BEARER_TOKEN in .env.uk_trains — get one from https://api-portal.rtt.io)."
        )
    async with _access_token_lock:
        # Refresh if we have no token or it's within 60s of expiry
        if not _access_token or time.time() >= (_access_token_exp - 60):
            return await _refresh_access_token()
        return _access_token


async def _rtt_get(path: str, params: dict | None = None) -> dict:
    clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}

    for attempt in (1, 2):
        token = await _get_access_token()
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        ) as client:
            r = await client.get(f"{RTT_BASE}{path}", params=clean)
        if r.status_code in (401, 403) and attempt == 1:
            # Possibly expired between refresh and use; force a new one and retry once.
            global _access_token
            _access_token = None
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"RTT auth rejected ({r.status_code}) after refresh.")
        if r.status_code == 429:
            retry = r.headers.get("Retry-After", "?")
            raise RuntimeError(f"RTT rate limit hit. Retry after {retry}s.")
        if r.status_code == 204:
            return {}
        r.raise_for_status()
        return r.json()
    raise RuntimeError("RTT request failed after retries.")  # unreachable


async def _location_lineup(
    code: str,
    kind: str = "departure",
    limit: int = 10,
    time_from: datetime | None = None,
    time_window_min: int = 120,
    filter_to: str | None = None,
) -> tuple[list[dict], str]:
    """Query /rtt/location and split services into departures or arrivals.

    Returns (selected_services, display_name).
    """
    params = {
        "code": _ns_code(code),
        "timeWindow": time_window_min,
    }
    if time_from:
        params["timeFrom"] = time_from.isoformat()
    if filter_to:
        params["filterTo"] = _ns_code(filter_to)

    data = await _rtt_get("/rtt/location", params)
    services = data.get("services") or []
    query_loc = (data.get("query") or {}).get("location") or {}
    display = _display_name(query_loc)

    # Split by temporal field. A service with `departure` populated is leaving
    # here; with `arrival` only, it's terminating here. For a sensible board
    # we show calls + starts for departures, calls + terminates for arrivals.
    selected = []
    for s in services:
        td = s.get("temporalData") or {}
        has_dep = bool(td.get("departure"))
        has_arr = bool(td.get("arrival"))
        display_as = td.get("displayAs")
        # Skip pure pass-throughs on the public board
        if display_as == "PASS":
            continue
        if kind == "departure" and has_dep:
            selected.append(s)
        elif kind == "arrival" and has_arr and not has_dep:
            # "arrival only" = the service terminates here
            selected.append(s)
        elif kind == "arrival" and has_arr and has_dep:
            # a call shows on both boards; include for arrivals too
            selected.append(s)
    return selected[:limit], display


def _fmt_lineup_row(service: dict, kind: str) -> str:
    td = service.get("temporalData") or {}
    activity = td.get(kind) or {}
    time_str = _fmt_time(_effective_time(activity))
    marker = _delay_marker(activity)
    plat_block = (service.get("locationMetadata") or {}).get("platform") or {}
    plat = plat_block.get("actual") or plat_block.get("planned")
    plat_str = f"plat {plat}" if plat else ""

    sm = service.get("scheduleMetadata") or {}
    op = (sm.get("operator") or {}).get("code") or ""
    op_name = (sm.get("operator") or {}).get("name") or op

    if kind == "departure":
        head_list = service.get("destination") or []
    else:
        head_list = service.get("origin") or []
    head = ", ".join(_display_name(p.get("location")) for p in head_list) or "—"

    return (
        f"  {time_str}  {_truncate(op_name, 14):<14} → {_truncate(head, 30):<30} "
        f"{plat_str}{marker}"
    ).rstrip()


# ---------- tools ----------

async def uk_find_station(query: str, limit: int = 5) -> str:
    """Search for a UK train station by name. Returns CRS code + name + coords.

    Use this to find the 3-letter CRS code needed by other tools
    (e.g. "Kings Cross" → KGX, "Bath Spa" → BTH). Backed by Transport API.
    """
    data = await _tapi_get("/uk/places.json", {"query": query, "type": "train_station"})
    places = data.get("member", [])[:limit]
    if not places:
        return f"No UK stations found for '{query}'."
    lines = [f"'{query}' — {len(places)} matches"]
    for p in places:
        crs = p.get("station_code") or "—"
        name = p.get("name") or "—"
        lat = p.get("latitude")
        lon = p.get("longitude")
        coord = f"({lat:.4f}, {lon:.4f})" if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else ""
        lines.append(f"  {crs:>4}  {_truncate(name, 32):<32}  {coord}")
    return "\n".join(lines)


async def uk_stationboard(
    station: str,
    kind: str = "departure",
    limit: int = 10,
    datetime_: str | None = None,
    to_station: str | None = None,
) -> str:
    """Live departures or arrivals at a UK station.

    `station` accepts a 3-letter CRS code ("KGX") or a name ("Kings
    Cross") — names are resolved via Transport API first. `kind` is
    "departure" (default) or "arrival". `datetime_` is optional
    ISO 8601 (e.g. "2026-04-19T09:00") to query a specific start time.
    `to_station` optionally filters to services heading for that
    destination (CRS or name) — handy for "next trains from A to B".
    """
    crs, display = await _resolve_crs(station)
    time_from = None
    if datetime_:
        try:
            time_from = datetime.fromisoformat(datetime_.replace("Z", "+00:00"))
        except ValueError:
            raise RuntimeError(f"Could not parse datetime_={datetime_!r}; use ISO 8601.")

    filter_to = None
    if to_station:
        to_crs, _ = await _resolve_crs(to_station)
        filter_to = to_crs

    services, name = await _location_lineup(
        crs, kind=kind, limit=limit, time_from=time_from, filter_to=filter_to,
    )
    if not services:
        return f"{name or display}: no {kind}s found."

    verb = "departures" if kind == "departure" else "arrivals"
    header = f"{name or display} ({crs}) — {len(services)} {verb}"
    if filter_to:
        header += f" → {to_station}"
    lines = [header]
    for s in services:
        lines.append(_fmt_lineup_row(s, kind))
    return "\n".join(lines)


async def _tapi_journey(
    from_crs: str,
    to_crs: str,
    date: str,
    time: str,
    is_arrival: bool,
    limit: int,
) -> str | None:
    """Try Transport API's journey planner. Returns formatted text on
    success, None on 403 (plan doesn't include journey endpoint) so the
    caller can fall back to RTT direct-only search."""
    mode = "to" if is_arrival else "at"
    path = f"/uk/public/journey/from/crs:{from_crs}/to/crs:{to_crs}/{mode}/{date}/{time}.json"
    try:
        data = await _tapi_get(path, {"service": "train", "modes": "train"})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return None
        raise
    except RuntimeError as e:
        # _tapi_get raises RuntimeError on 401/403 with a friendly message;
        # fall back for the "not part of your plan" case.
        msg = str(e)
        if "not part of your plan" in msg or "auth rejected (403" in msg:
            return None
        raise

    routes = (data.get("routes") or [])[:limit]
    if not routes:
        return f"Transport API: no journeys found for {from_crs} → {to_crs}."

    header = f"{from_crs} → {to_crs}   {date} {time} ({'arrive by' if is_arrival else 'depart'})"
    lines = [header, ""]
    for i, r in enumerate(routes, 1):
        dep_t = r.get("departure_time") or r.get("departure_datetime", "")
        arr_t = r.get("arrival_time") or r.get("arrival_datetime", "")
        duration = r.get("duration") or ""
        parts = r.get("route_parts") or []
        changes = max(0, len(parts) - 1)
        change_str = "direct" if changes == 0 else f"{changes} change{'s' if changes != 1 else ''}"
        modes = " + ".join(
            p.get("mode", "").upper() or p.get("service", "")
            for p in parts
        )
        lines.append(f"{i}. {dep_t} {from_crs}  →  {arr_t} {to_crs}   ({duration}, {change_str})")
        if modes:
            lines.append(f"   {modes}")
        # Per-leg breakdown — each route_part is a single train (or walk)
        # segment. Surfacing the full breakdown lets the LLM tell the
        # user which trains to catch and where to change. (Same pattern
        # as travel_rail_ch_journey.)
        for p in parts:
            leg_dep = p.get("departure_time") or ""
            leg_arr = p.get("arrival_time") or ""
            leg_from = p.get("from_point_name") or p.get("from_point", "")
            leg_to = p.get("to_point_name") or p.get("to_point", "")
            leg_mode = (p.get("mode") or "").upper()
            leg_svc = p.get("service") or ""
            leg_op = p.get("operator_name") or p.get("operator") or ""
            svc_str = f"{leg_mode} {leg_svc}".strip() if leg_svc else (leg_mode or "?")
            op_str = f" [{leg_op}]" if leg_op else ""
            lines.append(
                f"     • {leg_dep} {leg_from}  →  {leg_arr} {leg_to}   "
                f"{svc_str}{op_str}".rstrip()
            )
        lines.append("")
    return "\n".join(lines).rstrip()


async def uk_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
    window_hours: int = 6,
) -> str:
    """Plan a train journey between two UK stations.

    `origin` and `destination` accept CRS codes or station names.
    `datetime_iso` is ISO 8601 ('2026-06-15T09:00' or with timezone).
    `is_arrival` treats the time as a required arrival time instead of
    departure. Schema matches the rest of the travel_*_journey tools.

    Prefers Transport API's `/uk/public/journey` (change-aware routing)
    when the account's plan includes it. Falls back to RTT `filterTo`
    which finds only direct services — in that mode, `window_hours`
    controls how far ahead to look and `is_arrival` is ignored.
    """
    from_crs, from_name = await _resolve_crs(origin)
    to_crs, to_name = await _resolve_crs(destination)

    # Adapter: split unified ISO datetime into the upstream's native
    # date + time strings (Transport API + RTT both want them separate).
    if "T" in datetime_iso:
        d, t = datetime_iso.split("T", 1)
        # strip any trailing timezone / seconds — keep just HH:MM
        t = t[:5]
    else:
        d = datetime_iso
        t = datetime.now().strftime("%H:%M")

    # Try Transport API first (change-aware)
    if TAPI_APP_ID and TAPI_APP_KEY:
        tapi_result = await _tapi_journey(from_crs, to_crs, d, t, is_arrival, max_journeys)
        if tapi_result is not None:
            return tapi_result
        # Fall through to RTT direct-only with a note

    # RTT direct-only fallback
    try:
        time_from = datetime.fromisoformat(f"{d}T{t}")
    except ValueError:
        raise RuntimeError(f"Could not parse datetime_iso={datetime_iso!r}")

    window_min = max(60, min(60 * window_hours, 23 * 60 + 59))
    services, _ = await _location_lineup(
        from_crs,
        kind="departure",
        limit=max_journeys,
        time_from=time_from,
        time_window_min=window_min,
        filter_to=to_crs,
    )
    hint = (
        "(Transport API plan does not include journey planning — showing "
        "direct services only. Upgrade to Home Use at developer.transportapi.com "
        "to enable change-aware routing.)"
    )
    if not services:
        return (
            f"No direct trains found {from_name} → {to_name}.\n{hint}"
        )

    header = (
        f"{from_name} ({from_crs}) → {to_name} ({to_crs}) — "
        f"{len(services)} direct service{'s' if len(services) != 1 else ''}"
    )
    lines = [header, ""]
    for i, s in enumerate(services, 1):
        td = s.get("temporalData") or {}
        dep = td.get("departure") or {}
        dep_time = _fmt_time(_effective_time(dep))
        dep_marker = _delay_marker(dep)
        plat_block = (s.get("locationMetadata") or {}).get("platform") or {}
        plat = plat_block.get("actual") or plat_block.get("planned")
        plat_str = f"plat {plat}" if plat else ""
        sm = s.get("scheduleMetadata") or {}
        op_name = (sm.get("operator") or {}).get("name") or (sm.get("operator") or {}).get("code") or ""
        uid = sm.get("uniqueIdentity") or ""
        dest_list = s.get("destination") or []
        final_dest = ", ".join(_display_name(p.get("location")) for p in dest_list) or "—"
        lines.append(
            f"{i}. {dep_time} {from_crs} {plat_str}  {_truncate(op_name, 20):<20} "
            f"→ {_truncate(final_dest, 30)}{dep_marker}".rstrip()
        )
        if uid:
            lines.append(f"   service: {uid}")
    lines.append("")
    lines.append(hint)
    return "\n".join(lines).rstrip()


async def uk_service(unique_identity: str) -> str:
    """Show the calling pattern (stops) for a specific RTT service.

    `unique_identity` is the RTT service ID shown in stationboard
    results — format is `namespace:identity:YYYY-MM-DD`, e.g.
    `gb-nr:L01525:2026-04-19`. You can also pass just the identity
    plus a date (e.g. `L01525 2026-04-19`) and it will be normalised.
    """
    uid = unique_identity.strip()
    if " " in uid and ":" not in uid:
        identity, date_part = uid.split()
        uid = f"{RTT_NAMESPACE}:{identity}:{date_part}"
    elif ":" not in uid:
        raise RuntimeError("unique_identity must be 'namespace:identity:YYYY-MM-DD'")

    data = await _rtt_get("/rtt/service", {"uniqueIdentity": uid})
    service = data.get("service") or {}
    locations = service.get("locations") or []
    if not locations:
        return f"Service {uid}: no calling-pattern data."

    sm = service.get("scheduleMetadata") or {}
    op_name = (sm.get("operator") or {}).get("name") or (sm.get("operator") or {}).get("code") or ""
    headcode = sm.get("trainReportingIdentity") or sm.get("identity") or ""
    origin_name = _display_name((service.get("origin") or [{}])[0].get("location"))
    dest_name = _display_name((service.get("destination") or [{}])[0].get("location"))
    dep_date = sm.get("departureDate") or ""

    lines = [
        f"{uid}  {headcode}  {op_name}".rstrip(),
        f"{origin_name} → {dest_name}  ({dep_date})",
        "",
    ]
    for loc in locations:
        td = loc.get("temporalData") or {}
        arrive = td.get("arrival")
        depart = td.get("departure")
        arr_time = _fmt_time(_effective_time(arrive)) if arrive else "    "
        dep_time = _fmt_time(_effective_time(depart)) if depart else "    "
        marker_a = _delay_marker(arrive)
        marker_d = _delay_marker(depart)
        plat_block = (loc.get("locationMetadata") or {}).get("platform") or {}
        plat = plat_block.get("actual") or plat_block.get("planned")
        plat_str = f"plat {plat}" if plat else ""
        name = _display_name(loc.get("location"))
        lines.append(
            f"  {arr_time}{marker_a:<8} {dep_time}{marker_d:<8} {_truncate(name, 32):<32} {plat_str}".rstrip()
        )
    return "\n".join(lines)


async def uk_disruptions(
    station: str,
    window_minutes: int = 60,
    kind: str = "departure",
) -> str:
    """Report delays and cancellations at a UK station in the next N minutes.

    Filters the live RTT board to entries with non-zero delay or a
    cancellation flag. `kind` is "departure" (default) or "arrival".
    """
    crs, display = await _resolve_crs(station)
    services, name = await _location_lineup(
        crs, kind=kind, limit=50, time_window_min=max(60, window_minutes),
    )
    if not services:
        return f"{name or display}: no {kind}s found."

    disrupted = []
    for s in services:
        td = s.get("temporalData") or {}
        activity = td.get(kind) or {}
        marker = _delay_marker(activity)
        if marker:
            disrupted.append(s)

    if not disrupted:
        return f"{name or display}: no disruptions in next {window_minutes}min."

    lines = [
        f"{name or display} ({crs}) — {len(disrupted)} disruption"
        f"{'s' if len(disrupted) != 1 else ''} in next {window_minutes}min"
    ]
    for s in disrupted:
        lines.append(_fmt_lineup_row(s, kind))
    return "\n".join(lines)


