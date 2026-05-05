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


# --- Cross-London + via-composer support -------------------------------
# Used by uk_journey when direct services don't exist (Clandon →
# Cambridge needs a London-terminal interchange). The user supplies
# `via=[...]` for explicit single-change composition or
# `via_london=True` to auto-probe London terminal pairs.

# Main-line terminals probed by via_london=True. Trimmed to the four
# that actually decide most cross-London journeys — RTT's free-tier
# rate limit is tight (~5/sec) and a wider probe burst trips it.
# Callers who need a specific combo can pass via=['XXX','YYY'] explicitly
# (no probing — uses the supplied terminals directly).
LONDON_TERMINALS = ("WAT", "KGX", "STP", "LST")
LONDON_TERMINAL_NAMES = {
    "WAT": "London Waterloo",  "VIC": "London Victoria",
    "LBG": "London Bridge",    "CHX": "London Charing Cross",
    "EUS": "London Euston",    "KGX": "London Kings Cross",
    "STP": "London St Pancras","PAD": "London Paddington",
    "LST": "London Liverpool Street", "FST": "London Fenchurch Street",
    "MYB": "London Marylebone", "WIM": "London Wimbledon",
}

# Tube/walk transfer minutes between London terminals — enough for
# realistic interchange planning. Symmetric (frozenset key). 30-min
# default for any pair not listed.
LONDON_TRANSFER_MIN = {
    frozenset({"KGX", "STP"}): 5,    frozenset({"KGX", "EUS"}): 5,
    frozenset({"STP", "EUS"}): 8,    frozenset({"WAT", "KGX"}): 25,
    frozenset({"WAT", "STP"}): 25,   frozenset({"WAT", "EUS"}): 25,
    frozenset({"WAT", "LST"}): 15,   frozenset({"WAT", "PAD"}): 25,
    frozenset({"WAT", "VIC"}): 15,   frozenset({"WAT", "LBG"}): 10,
    frozenset({"WAT", "CHX"}): 10,
    frozenset({"VIC", "KGX"}): 15,   frozenset({"VIC", "STP"}): 15,
    frozenset({"VIC", "EUS"}): 12,   frozenset({"VIC", "PAD"}): 15,
    frozenset({"LST", "KGX"}): 15,   frozenset({"LST", "STP"}): 15,
    frozenset({"LST", "EUS"}): 20,   frozenset({"LST", "FST"}): 5,
    frozenset({"PAD", "KGX"}): 15,   frozenset({"PAD", "STP"}): 12,
    frozenset({"PAD", "EUS"}): 12,
    frozenset({"CHX", "WAT"}): 10,   frozenset({"CHX", "KGX"}): 15,
    frozenset({"CHX", "LST"}): 12,
    frozenset({"MYB", "PAD"}): 12,   frozenset({"MYB", "EUS"}): 12,
    frozenset({"LBG", "KGX"}): 12,   frozenset({"LBG", "VIC"}): 12,
}
DEFAULT_TRANSFER_MIN = 30

# Walk between main-line platform and Tube ticket hall at each terminal.
# TfL's journey planner gives Tube-entrance-to-Tube-entrance time and
# doesn't include this. KGX is the worst — main-line concourse is well
# above and away from the Northern/Victoria/Piccadilly platforms.
TERMINAL_TUBE_ACCESS_MIN = {
    "KGX": 6, "STP": 5, "EUS": 4, "WAT": 5, "PAD": 5,
    "VIC": 4, "LST": 3, "LBG": 4, "CHX": 3, "MYB": 5,
    "FST": 5, "WIM": 3,
}
DEFAULT_TUBE_ACCESS_MIN = 4


def _tube_access_min(terminal: str) -> int:
    return TERMINAL_TUBE_ACCESS_MIN.get(terminal.upper(), DEFAULT_TUBE_ACCESS_MIN)


def _terminal_transfer_min(a: str, b: str) -> int:
    if a == b:
        return 5
    return LONDON_TRANSFER_MIN.get(frozenset({a, b}), DEFAULT_TRANSFER_MIN)


# Process-wide cache of TfL-computed transfer times; refreshed once per
# (a, b) pair per process. TfL may return slightly different times by
# time-of-day, but within ~5 min — good enough for journey planning.
_tfl_transfer_cache: dict[tuple[str, str], int] = {}


async def _tfl_transfer_min(client, a: str, b: str) -> int:
    """Total minutes to interchange between two London terminals: TfL's
    platform-to-platform Tube/walk time *plus* the main-line egress at
    A and access at B (TfL doesn't include the walk between main-line
    concourse and Tube ticket hall — KGX is ~6 min, LST is ~3 min).
    Falls back to LONDON_TRANSFER_MIN static table on TfL failure
    (which already bakes in some buffer)."""
    if a == b:
        return 5
    key = tuple(sorted([a, b]))
    if key in _tfl_transfer_cache:
        return _tfl_transfer_cache[key]
    try:
        from mcp_travel.travel_tfl import journey as _tfl_journey
        a_name = LONDON_TERMINAL_NAMES.get(a, a)
        b_name = LONDON_TERMINAL_NAMES.get(b, b)
        js = await _tfl_journey(
            client, a_name, b_name, modes=["tube", "walking", "bus"], max_journeys=1,
        )
        if js and js[0].get("duration_minutes"):
            tfl_min = int(js[0]["duration_minutes"])
            mins = tfl_min + _tube_access_min(a) + _tube_access_min(b)
            _tfl_transfer_cache[key] = mins
            return mins
    except Exception:
        pass
    return _terminal_transfer_min(a, b)


async def _rtt_service_detail(uid: str) -> dict:
    """Fetch full calling pattern for a service. Returns the service
    dict with `locations[]` (each having temporalData + location)."""
    return await _rtt_get("/rtt/service", {"uniqueIdentity": uid})


def _arrival_at(service_detail: dict, crs: str) -> datetime | None:
    """Extract the arrival time at `crs` from a service detail's calling
    pattern. Returns None if the service doesn't call there or the
    timestamp can't be parsed.

    RTT stores CRS in `location.shortCodes[]` (the schema's `crs` is
    actually under shortCodes, not a top-level field).
    """
    target = crs.upper()
    locations = (service_detail.get("service") or {}).get("locations") or []
    for loc in locations:
        short_codes = (loc.get("location") or {}).get("shortCodes") or []
        if not any(c.upper() == target for c in short_codes):
            continue
        td = loc.get("temporalData") or {}
        # Prefer arrival; if pass-through with only departure, use that
        for key in ("arrival", "departure"):
            block = td.get(key)
            if block:
                ts = _effective_time(block)
                if ts:
                    try:
                        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                    except ValueError:
                        continue
    return None


async def _compose_via_one(
    from_crs: str, from_name: str,
    to_crs: str, to_name: str,
    depart_dt: datetime, via_crs: str,
    max_pairs: int = 3,
) -> list[dict]:
    """Compose origin → via → destination journeys for one via station.

    Tries the next `max_pairs` services origin → via and for each finds
    the next via → destination service after a sensible change buffer.
    Returns 0–`max_pairs` composed journey dicts ranked by total time.
    """
    journeys: list[dict] = []
    inbound, _ = await _location_lineup(
        from_crs, kind="departure", limit=max_pairs * 2,
        time_from=depart_dt, time_window_min=240, filter_to=via_crs,
    )
    if not inbound:
        return journeys

    for in_svc in inbound[:max_pairs]:
        # Need the inbound service's arrival time AT the via station
        sm = in_svc.get("scheduleMetadata") or {}
        uid = sm.get("uniqueIdentity")
        if not uid:
            continue
        try:
            detail = await _rtt_service_detail(uid)
        except Exception:
            continue
        via_arrival = _arrival_at(detail, via_crs)
        if not via_arrival:
            continue

        # Same-station change uses 10 min; different stations get the
        # London-terminal transfer matrix (covers Tube/walk).
        change_min = _terminal_transfer_min(via_crs.upper(), via_crs.upper())
        target_dep = via_arrival + timedelta(minutes=change_min)

        outbound, _ = await _location_lineup(
            via_crs, kind="departure", limit=2,
            time_from=target_dep, time_window_min=180, filter_to=to_crs,
        )
        if not outbound:
            continue
        out_svc = outbound[0]
        out_dt_block = (out_svc.get("temporalData") or {}).get("departure") or {}
        out_depart_str = _effective_time(out_dt_block)
        try:
            out_depart_dt = datetime.fromisoformat(out_depart_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, AttributeError):
            continue

        # Get out_arrival at dest
        out_uid = (out_svc.get("scheduleMetadata") or {}).get("uniqueIdentity")
        out_arrival = None
        if out_uid:
            try:
                out_detail = await _rtt_service_detail(out_uid)
                out_arrival = _arrival_at(out_detail, to_crs)
            except Exception:
                pass

        in_dep_block = (in_svc.get("temporalData") or {}).get("departure") or {}
        in_depart_str = _effective_time(in_dep_block)
        try:
            in_depart_dt = datetime.fromisoformat(in_depart_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, AttributeError):
            continue

        end = out_arrival or out_depart_dt
        total_min = int((end - in_depart_dt).total_seconds() // 60)
        wait_min = int((out_depart_dt - via_arrival).total_seconds() // 60)
        journeys.append({
            "from": from_name, "to": to_name,
            "via": via_crs.upper(),
            "depart": in_depart_dt, "arrive": end,
            "total_min": total_min, "wait_min": wait_min,
            "leg1": {
                "depart": in_depart_dt, "arrive": via_arrival,
                "from": from_name, "to": via_crs.upper(),
                "operator": (sm.get("operator") or {}).get("name") or "",
                "headcode": sm.get("trainReportingIdentity") or "",
            },
            "leg2": {
                "depart": out_depart_dt, "arrive": out_arrival,
                "from": via_crs.upper(), "to": to_name,
                "operator": ((out_svc.get("scheduleMetadata") or {}).get("operator") or {}).get("name") or "",
                "headcode": (out_svc.get("scheduleMetadata") or {}).get("trainReportingIdentity") or "",
            },
        })
    return journeys


async def _compose_cross_london(
    from_crs: str, from_name: str,
    to_crs: str, to_name: str,
    depart_dt: datetime, max_journeys: int = 5,
    client = None,
) -> list[dict]:
    """Probe all London terminals as candidate entry+exit points and
    compose 3-train journeys (origin train → Tube transfer → onward
    train). Returns ranked composed journeys."""
    # Bounded concurrency — RTT rate-limits aggressively (~5 calls/sec).
    # 2 in flight is plenty fast and well inside the budget.
    sem = asyncio.Semaphore(2)

    async def _probe(crs: str, filter_to: str, window: int):
        async with sem:
            try:
                return await _location_lineup(crs, kind="departure", limit=2,
                                              time_from=depart_dt,
                                              time_window_min=window, filter_to=filter_to)
            except Exception as e:
                return e

    inbound_probes = await asyncio.gather(*[
        _probe(from_crs, t, 240) for t in LONDON_TERMINALS
    ])
    entries = {
        t: (svcs[0] if isinstance(svcs, tuple) and svcs[0] else None)
        for t, svcs in zip(LONDON_TERMINALS, inbound_probes)
    }
    entry_terminals = {t: s for t, s in entries.items() if s}
    if not entry_terminals:
        return []

    # Probe each terminal → dest in parallel — wide time window since we
    # don't yet know the entry-arrival time. Uses the same bounded
    # semaphore so total in-flight RTT calls stay <= 2.
    outbound_probes = await asyncio.gather(*[
        _probe(t, to_crs, 480) for t in LONDON_TERMINALS
    ])
    exit_terminals = {
        t: (svcs[0] if isinstance(svcs, tuple) and svcs[0] else None)
        for t, svcs in zip(LONDON_TERMINALS, outbound_probes)
    }
    exit_terminals = {t: s for t, s in exit_terminals.items() if s}
    if not exit_terminals:
        return []

    candidates: list[dict] = []
    for entry, in_svcs in entry_terminals.items():
        in_svc = in_svcs[0]
        sm = in_svc.get("scheduleMetadata") or {}
        uid = sm.get("uniqueIdentity")
        if not uid:
            continue
        try:
            detail = await _rtt_service_detail(uid)
        except Exception:
            continue
        entry_arrival = _arrival_at(detail, entry)
        if not entry_arrival:
            continue
        in_dep_block = (in_svc.get("temporalData") or {}).get("departure") or {}
        try:
            in_depart_dt = datetime.fromisoformat(_effective_time(in_dep_block).replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, AttributeError):
            continue

        for exit_t in exit_terminals:
            if exit_t == entry:
                continue
            # Live Tube transfer time from TfL where we have a client;
            # fall back to static table otherwise.
            if client is not None:
                tube_min = await _tfl_transfer_min(client, entry, exit_t)
            else:
                tube_min = _terminal_transfer_min(entry, exit_t)
            target_dep = entry_arrival + timedelta(minutes=tube_min)
            # Find the next exit→dest service after target_dep
            outbound_search, _ = await _location_lineup(
                exit_t, kind="departure", limit=1,
                time_from=target_dep, time_window_min=180, filter_to=to_crs,
            )
            if not outbound_search:
                continue
            out_svc = outbound_search[0]
            out_sm = out_svc.get("scheduleMetadata") or {}
            try:
                out_depart_dt = datetime.fromisoformat(
                    _effective_time((out_svc.get("temporalData") or {}).get("departure") or {}).replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (ValueError, AttributeError):
                continue
            out_uid = out_sm.get("uniqueIdentity")
            out_arrival = None
            if out_uid:
                try:
                    out_detail = await _rtt_service_detail(out_uid)
                    out_arrival = _arrival_at(out_detail, to_crs)
                except Exception:
                    pass
            end = out_arrival or out_depart_dt
            total_min = int((end - in_depart_dt).total_seconds() // 60)
            candidates.append({
                "from": from_name, "to": to_name,
                "entry": entry, "exit": exit_t,
                "depart": in_depart_dt, "arrive": end,
                "total_min": total_min,
                "tube_min": tube_min,
                "leg1": {
                    "depart": in_depart_dt, "arrive": entry_arrival,
                    "from": from_name, "to": LONDON_TERMINAL_NAMES.get(entry, entry),
                    "operator": (sm.get("operator") or {}).get("name") or "",
                    "headcode": sm.get("trainReportingIdentity") or "",
                },
                "tube": {
                    "from": LONDON_TERMINAL_NAMES.get(entry, entry),
                    "to": LONDON_TERMINAL_NAMES.get(exit_t, exit_t),
                    "minutes": tube_min,
                },
                "leg2": {
                    "depart": out_depart_dt, "arrive": out_arrival,
                    "from": LONDON_TERMINAL_NAMES.get(exit_t, exit_t),
                    "to": to_name,
                    "operator": (out_sm.get("operator") or {}).get("name") or "",
                    "headcode": out_sm.get("trainReportingIdentity") or "",
                },
            })
    candidates.sort(key=lambda j: j["total_min"])
    return candidates[:max_journeys]


def _fmt_composed(j: dict, idx: int) -> list[str]:
    """Render one composed journey as text lines for the uk_journey output."""
    lines = []
    lead_arr = j['arrive'].strftime('%H:%M') if j['arrive'] else '?'
    lead_dep = j['depart'].strftime('%H:%M')
    if 'tube' in j:
        # Cross-London 3-leg
        head = (f"{idx}. {lead_dep} {j['from']} → {lead_arr} {j['to']}   "
                f"({j['total_min']} min via {j['entry']}→{j['exit']})")
    else:
        head = (f"{idx}. {lead_dep} {j['from']} → {lead_arr} {j['to']}   "
                f"({j['total_min']} min via {j['via']}, change wait {j['wait_min']}m)")
    lines.append(head)
    leg1 = j['leg1']
    a1 = leg1['arrive'].strftime('%H:%M') if leg1['arrive'] else '?'
    op1 = f" [{leg1['operator']}]" if leg1.get('operator') else ""
    hc1 = f" {leg1['headcode']}" if leg1.get('headcode') else ""
    lines.append(f"     • {leg1['depart'].strftime('%H:%M')} {leg1['from']} → {a1} {leg1['to']}{op1}{hc1}")
    if 'tube' in j:
        t = j['tube']
        lines.append(f"     • Tube/walk {t['from']} → {t['to']} (~{t['minutes']} min)")
    leg2 = j['leg2']
    a2 = leg2['arrive'].strftime('%H:%M') if leg2['arrive'] else '?'
    op2 = f" [{leg2['operator']}]" if leg2.get('operator') else ""
    hc2 = f" {leg2['headcode']}" if leg2.get('headcode') else ""
    lines.append(f"     • {leg2['depart'].strftime('%H:%M')} {leg2['from']} → {a2} {leg2['to']}{op2}{hc2}")
    lines.append("")
    return lines


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
    via: list[str] | None = None,
    via_london: bool = False,
) -> str:
    """Plan a train journey between two UK stations.

    `origin` and `destination` accept CRS codes or station names.
    `datetime_iso` is ISO 8601 ('2026-06-15T09:00' or with timezone).
    `is_arrival` treats the time as a required arrival time instead of
    departure. Schema matches the rest of the travel_*_journey tools.

    `via` is a list of CRS codes or station names to try as single-change
    interchange points. `via_london` enables auto cross-London routing
    (probes the major London terminals as entry/exit pairs and uses TfL
    journey planner for live Tube transfer times). Both flags are
    additive — passing them returns composed journeys merged with any
    direct services.

    If the route has no direct services AND neither `via` nor
    `via_london` is supplied, the response prompts the caller to suggest
    a change point.

    Prefers Transport API's `/uk/public/journey` (change-aware routing)
    when the account's plan includes it; the Home tier currently does
    not, so falls through to RTT direct-only + composed.
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

    # No direct services — try composer if user asked for via/via_london
    if not services:
        composed: list[dict] = []
        if via:
            for via_label in via:
                via_crs, _ = await _resolve_crs(via_label)
                composed.extend(await _compose_via_one(
                    from_crs, from_name, to_crs, to_name, time_from, via_crs,
                ))
        if via_london:
            async with httpx.AsyncClient(timeout=20.0) as tfl_client:
                composed.extend(await _compose_cross_london(
                    from_crs, from_name, to_crs, to_name, time_from,
                    max_journeys=max_journeys, client=tfl_client,
                ))
        composed.sort(key=lambda j: j["total_min"])
        composed = composed[:max_journeys]

        if composed:
            header = (
                f"{from_name} ({from_crs}) → {to_name} ({to_crs}) — "
                f"{len(composed)} composed journey"
                f"{'s' if len(composed) != 1 else ''} (no direct services)"
            )
            lines = [header, ""]
            for i, j in enumerate(composed, 1):
                lines.extend(_fmt_composed(j, i))
            return "\n".join(lines).rstrip()

        # Nothing direct, no via supplied — prompt the caller
        if not via and not via_london:
            return (
                f"NO Direct Trains available {from_name} → {to_name} — "
                f"please suggest a change.\n"
                f"  • For cross-London routing, retry with via_london=True "
                f"(uses TfL transfer times via the major London terminals)\n"
                f"  • For a known interchange, retry with via=['CRS'] "
                f"(e.g. via=['RDG'] for Reading, via=['BHM'] for Birmingham)"
            )
        # via supplied but composer found nothing
        return (
            f"No journeys found {from_name} → {to_name}, even via "
            f"{via or 'London terminals'}. Try a different change point "
            f"or a wider time window."
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


