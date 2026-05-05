"""Swiss public transport tools via transport.opendata.ch (HAFAS).

Registered onto the shared FastMCP("travel") instance via `register(mcp)`.
"""

import os
import re
from datetime import datetime, timedelta

import httpx
from fastmcp import FastMCP

API_URL = os.environ.get("SBB_API_URL", "https://transport.opendata.ch/v1").rstrip("/")
USER_AGENT = "mcp-travel/1.0 (+https://github.com/nonatech-uk/mcp-travel)"
HTTP_TIMEOUT = 15.0


# ---------- helpers ----------

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def _fmt_time(ts: str | None) -> str:
    dt = _parse_iso(ts)
    return dt.strftime("%H:%M") if dt else "—"


def _fmt_date_header(ts: str | None) -> str:
    dt = _parse_iso(ts)
    return dt.strftime("%a %d %b %H:%M") if dt else ""


def _minutes(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


def _parse_duration(s: str | None) -> str:
    """HAFAS returns '00d00:56:00'; format as '56m' or '1h 4m'."""
    if not s:
        return "—"
    try:
        days_part, time_part = s.split("d")
        h, m, _sec = time_part.split(":")
        total_min = int(days_part) * 24 * 60 + int(h) * 60 + int(m)
        if total_min < 60:
            return f"{total_min}m"
        return f"{total_min // 60}h {total_min % 60}m"
    except (ValueError, AttributeError):
        return s


def _delay_marker(endpoint: dict) -> str:
    """Extract delay / cancellation marker from a journey endpoint."""
    if endpoint.get("prognosis", {}).get("capacity") == "CANCELLED":
        return " [CANCELLED]"
    # HAFAS uses 'delay' on the endpoint or prognosis.departure/arrival
    delay = endpoint.get("delay")
    if delay is None:
        prog = endpoint.get("prognosis") or {}
        delay = prog.get("delay")
    if delay and isinstance(delay, (int, float)) and delay != 0:
        sign = "+" if delay > 0 else ""
        return f" [{sign}{int(delay)}']"
    return ""


def _platform(endpoint: dict) -> str:
    p = endpoint.get("platform") or (endpoint.get("prognosis") or {}).get("platform")
    return f"plat {p}" if p else ""


async def _get(path: str, params: dict) -> dict:
    """Send a GET request to the Transport API, dropping None values."""
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.get(f"{API_URL}{path}", params=clean)
        r.raise_for_status()
        return r.json()


def _transportations_list(transportations: list[str] | str | None) -> list[str] | None:
    """Accept either a list or comma-separated string."""
    if transportations is None:
        return None
    if isinstance(transportations, str):
        return [t.strip() for t in transportations.split(",") if t.strip()]
    return transportations


# ---------- tools ----------

def register(mcp: FastMCP) -> None:
    """Register travel_rail_ch_* tools onto the shared FastMCP instance.

    Old `travel_sbb_*` names are kept as deprecation aliases (Stage 3a
    rename, 2026-05-05). They will be removed in a future release.
    """
    # Primary (canonical) names — ISO-2 country code.
    mcp.tool(travel_rail_ch_find_station)
    mcp.tool(travel_rail_ch_journey)
    mcp.tool(travel_rail_ch_stationboard)
    mcp.tool(travel_rail_ch_disruptions)

    # Deprecation aliases — old `travel_sbb_*` names.
    _dep = "DEPRECATED: renamed to `travel_rail_ch_{}`. Same signature; same behaviour. Will be removed in a future release."
    mcp.tool(name="travel_sbb_find_station", description=_dep.format("find_station"))(travel_rail_ch_find_station)
    mcp.tool(name="travel_sbb_journey",      description=_dep.format("journey"))(travel_rail_ch_journey)
    mcp.tool(name="travel_sbb_stationboard", description=_dep.format("stationboard"))(travel_rail_ch_stationboard)
    mcp.tool(name="travel_sbb_disruptions",  description=_dep.format("disruptions"))(travel_rail_ch_disruptions)


async def travel_rail_ch_find_station(
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    type_: str = "station",
    limit: int = 5,
) -> str:
    """Find Swiss public transport stations by name or coordinates.

    Provide either `query` (e.g. "Zurich HB", "Bern", "Geneve") or a
    `lat`/`lon` pair (WGS84). Use `type_` to filter: "station", "poi",
    "address", or "all". Useful as a lookup step before calling other
    tools that need an unambiguous station name or ID.
    """
    if not query and (lat is None or lon is None):
        return "sbb_find_station: provide either query= or both lat= and lon="

    # Transport API uses x=latitude, y=longitude (per its docs)
    async def _fetch(q: str | None, t: str) -> list[dict]:
        d = await _get("/locations", {"query": q, "x": lat, "y": lon, "type": t})
        return d.get("stations", [])

    results = await _fetch(query, type_)
    # API's type filter is unreliable (it returns POIs in the stations array
    # even when type=station). Stations have a numeric HAFAS id; POIs/addresses
    # don't — use that as the real filter.
    if type_ == "station":
        filtered = [s for s in results if s.get("id")]
        # Queries like "Zurich HB" match many POIs with "HB" in the name and
        # crowd the actual station out of the results entirely. If the
        # filtered list is empty, retry with common station suffixes stripped.
        if not filtered and query:
            simplified = re.sub(
                r"\b(HB|Hbf|Hauptbahnhof|Bahnhof|Station|Gare|Stazione)\b",
                "",
                query,
                flags=re.IGNORECASE,
            ).strip()
            if simplified and simplified != query:
                filtered = [s for s in await _fetch(simplified, "all") if s.get("id")]
        results = filtered
    stations = results[:limit]
    if not stations:
        label = f"'{query}'" if query else f"{lat},{lon}"
        return f"No stations found for {label}."

    label = f"'{query}'" if query else f"near {lat},{lon}"
    lines = [f"{label} — {len(stations)} matches"]
    for s in stations:
        coord = s.get("coordinate") or {}
        x, y = coord.get("x"), coord.get("y")
        coord_str = f"({x:.5f}, {y:.5f})" if isinstance(x, (int, float)) and isinstance(y, (int, float)) else ""
        extra = []
        if s.get("score") is not None:
            extra.append(f"score {s['score']}")
        if s.get("distance") is not None:
            extra.append(f"{int(s['distance'])}m away")
        extras = f"  {', '.join(extra)}" if extra else ""
        sid = s.get("id") or "—"
        name = s.get("name") or "—"
        lines.append(f"  {sid:>8}  {name:<30}  {coord_str}{extras}")
    return "\n".join(lines)


async def travel_rail_ch_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
    via: list[str] | str | None = None,
    transportations: list[str] | str | None = None,
    direct: bool = False,
) -> str:
    """Plan a journey via the Swiss SBB / transport.opendata.ch planner.

    **Scope** — this is a Switzerland-adjacent planner, not pan-European.
    Use this tool whenever ONE end of the journey is in Switzerland or
    the route transits Switzerland. SBB's HAFAS engine handles the
    cross-border lookup correctly and is the ONLY tool that does:

    What works (preferred for these):
      - Italy ↔ Switzerland cross-border — Milano ↔ Zermatt / Zürich /
        Brig / Lugano / Bern. Trenitalia + Italo can't plan past the
        border, and Italy-static-table doesn't carry these pairs.
      - Anywhere ↔ anywhere via Switzerland (e.g. Lyon ↔ Wien via Zürich)
      - Paris ↔ Italy via Lyria / Gotthard / Simplon (Paris ↔ Milano live)
      - Milano ↔ Germany via Zürich
      - Pure Switzerland-internal (Zürich ↔ Bern ↔ Genève)

    What doesn't (use the right tool instead):
      - Italy-internal (Milano ↔ Roma — both ends Italian)
                                                    → travel_rail_it_journey
      - DB-internal or ÖBB-internal                 → travel_rail_de_journey / travel_rail_at_journey
      - Pure BE↔DE (Brussels ↔ Berlin)              → travel_rail_de_journey
      - Cross-Channel (London ↔ anywhere)           → travel_eurostar_check
      - ⚠ Both ends in Italy: SBB silently mis-routes — its station
        resolver picks plausible Swiss/border alternatives and the
        returned trip looks valid but isn't. Cross-border (one Swiss
        end, one Italian) is fine; both Italian is broken.

    `origin` and `destination` can be station names ("Zurich HB") or IDs.
    `datetime_iso` is ISO 8601 ('2026-06-15T09:00' or with timezone) —
    schema matches the rest of the travel_*_journey tools.
    `is_arrival` treats the time as a required arrival.
    Optional: `via` (1-5 intermediate stops), `transportations` (filter —
    any of: ice_tgv_rj, ec_ic, ir, re_d, ship, bus, cableway, arz_ext,
    tramway_underground), `direct` (no changes), `max_journeys` (1-16).
    """
    via_list = _transportations_list(via)

    # Adapter: split unified ISO datetime into transport.opendata.ch's
    # native date + time strings.
    if "T" in datetime_iso:
        date_part, time_part = datetime_iso.split("T", 1)
        time_part = time_part[:5]   # keep just HH:MM
    else:
        date_part = datetime_iso
        time_part = None

    params: dict = {
        "from": origin,
        "to": destination,
        "date": date_part,
        "time": time_part,
        "isArrivalTime": 1 if is_arrival else 0,
        "direct": 1 if direct else 0,
        "limit": max(1, min(16, max_journeys)),
    }
    if via_list:
        params["via[]"] = via_list
    tr = _transportations_list(transportations)
    if tr:
        params["transportations[]"] = tr

    data = await _get("/connections", params)
    connections = data.get("connections", [])
    if not connections:
        return f"No connections found from {origin} to {destination}."

    first = connections[0]
    header = f"{first['from']['station']['name']} → {first['to']['station']['name']}"
    header += f"   {_fmt_date_header(first['from'].get('departure'))}"
    lines = [header, ""]

    for i, c in enumerate(connections, 1):
        dep = c["from"]
        arr = c["to"]
        dep_time = _fmt_time(dep.get("departure"))
        arr_time = _fmt_time(arr.get("arrival"))
        dep_plat = _platform(dep)
        arr_plat = _platform(arr)
        duration = _parse_duration(c.get("duration"))
        transfers = c.get("transfers", 0)
        change_str = "direct" if transfers == 0 else f"{transfers} change{'s' if transfers != 1 else ''}"

        products = c.get("products") or []
        products_str = " + ".join(products) if products else ""

        markers = _delay_marker(dep) + _delay_marker(arr)

        lines.append(
            f"{i}. {dep_time} {dep['station']['name']} {dep_plat}".rstrip()
            + f"  →  {arr_time} {arr['station']['name']} {arr_plat}".rstrip()
            + f"   ({duration}, {change_str}){markers}"
        )
        if products_str:
            lines.append(f"   {products_str}")

        # Per-leg breakdown — each HAFAS section is either a journey
        # (train/bus/tram/ship) or a walk. Surfacing them lets the LLM
        # show the user exactly which trains they're catching, the
        # interchange platforms, and where to wait.
        for sec in c.get("sections") or []:
            sec_dep = sec.get("departure") or {}
            sec_arr = sec.get("arrival") or {}
            sec_dep_t = _fmt_time(sec_dep.get("departure"))
            sec_arr_t = _fmt_time(sec_arr.get("arrival"))
            sec_dep_st = ((sec_dep.get("station") or {}).get("name") or "?")
            sec_arr_st = ((sec_arr.get("station") or {}).get("name") or "?")
            sec_dep_p = _platform(sec_dep)
            sec_arr_p = _platform(sec_arr)
            walk = sec.get("walk") or {}
            journey = sec.get("journey") or {}
            if walk and not journey:
                walk_min = walk.get("duration")
                walk_label = f"walk {walk_min // 60}m" if isinstance(walk_min, int) else "walk"
                leg = (
                    f"     • {sec_dep_t or '    '} {sec_dep_st}"
                    f"  →  {sec_arr_t or '    '} {sec_arr_st}"
                    f"   ({walk_label})"
                )
            else:
                cat = journey.get("category") or ""
                num = journey.get("number") or journey.get("name") or ""
                operator = journey.get("operator") or ""
                heading = (journey.get("to") or "").strip()
                svc = f"{cat} {num}".strip() or "?"
                op_str = f" [{operator}]" if operator and operator != cat else ""
                head_str = f" → {heading}" if heading else ""
                marker = _delay_marker(sec_dep) + _delay_marker(sec_arr)
                leg = (
                    f"     • {sec_dep_t} {sec_dep_st} {sec_dep_p}".rstrip()
                    + f"  →  {sec_arr_t} {sec_arr_st} {sec_arr_p}".rstrip()
                    + f"   {svc}{op_str}{head_str}{marker}"
                )
            lines.append(leg)
        lines.append("")

    return "\n".join(lines).rstrip()


async def travel_rail_ch_stationboard(
    station: str,
    limit: int = 10,
    kind: str = "departure",
    datetime_: str | None = None,
    transportations: list[str] | str | None = None,
) -> str:
    """Get the next departures or arrivals at a Swiss station.

    `station` is a name ("Zurich HB") or station ID. `kind` is
    "departure" (default) or "arrival". `limit` up to ~300.
    `datetime_` (YYYY-MM-DD HH:MM) to query a specific time.
    `transportations` filter as in sbb_journey.
    """
    params: dict = {
        "station": station,
        "limit": limit,
        "type": kind,
        "datetime": datetime_,
    }
    tr = _transportations_list(transportations)
    if tr:
        params["transportations[]"] = tr

    data = await _get("/stationboard", params)
    board = data.get("stationboard", [])
    st_name = (data.get("station") or {}).get("name") or station
    if not board:
        return f"{st_name}: no {kind}s found."

    verb = "departures" if kind == "departure" else "arrivals"
    lines = [f"{st_name} — next {len(board)} {verb}"]

    for entry in board:
        stop = entry.get("stop") or {}
        ts = stop.get(kind) or stop.get("departure") or stop.get("arrival")
        time_str = _fmt_time(ts)
        category = entry.get("category") or ""
        number = entry.get("number") or ""
        name = f"{category}{number}".strip() or entry.get("name") or ""
        to = entry.get("to") or ""
        plat = _platform(stop)
        markers = _delay_marker(stop)
        lines.append(
            f"  {time_str}  {name:<8} → {to:<30} {plat}{markers}".rstrip()
        )
    return "\n".join(lines)


async def travel_rail_ch_disruptions(
    station: str,
    window_minutes: int = 60,
    kind: str = "departure",
) -> str:
    """Report delays and cancellations at a station in the next N minutes.

    Convenience wrapper over the station board that filters to entries
    with a non-zero delay or a cancellation flag. `kind` is "departure"
    or "arrival". Returns a short summary; empty when everything is
    running on time.
    """
    # Pull a generous board so we can filter to the time window
    data = await _get("/stationboard", {
        "station": station,
        "limit": 40,
        "type": kind,
    })
    board = data.get("stationboard", [])
    st_name = (data.get("station") or {}).get("name") or station
    if not board:
        return f"{st_name}: no {kind}s found."

    cutoff = datetime.now().astimezone() + timedelta(minutes=window_minutes)
    disrupted = []
    for entry in board:
        stop = entry.get("stop") or {}
        ts = _parse_iso(stop.get(kind) or stop.get("departure") or stop.get("arrival"))
        if ts and ts > cutoff:
            continue
        marker = _delay_marker(stop)
        if marker:
            disrupted.append((stop, entry, marker, ts))

    if not disrupted:
        return f"{st_name}: no disruptions in next {window_minutes}min."

    lines = [f"{st_name} — {len(disrupted)} disruption{'s' if len(disrupted) != 1 else ''} in next {window_minutes}min"]
    for stop, entry, marker, _ts in disrupted:
        ts = stop.get(kind) or stop.get("departure") or stop.get("arrival")
        time_str = _fmt_time(ts)
        category = entry.get("category") or ""
        number = entry.get("number") or ""
        name = f"{category}{number}".strip() or entry.get("name") or ""
        to = entry.get("to") or ""
        plat = _platform(stop)
        lines.append(f"  {time_str}  {name:<8} → {to:<30} {plat}{marker}".rstrip())
    return "\n".join(lines)


