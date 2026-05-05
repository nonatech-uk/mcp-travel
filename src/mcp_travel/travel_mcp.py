"""MCP server: door-to-door trip planning across flight, rail, and Eurotunnel.

Phases 1–4 implemented (2026-05-03):
  - flight_check        — Duffel offers (test mode unless DUFFEL_MODE=live)
  - sncf_journey        — SNCF Navitia journeys (needs SNCF_API_KEY)
  - eurostar_check      — Playwright scraper, fail-soft (selectors WIP)
  - eurotunnel_check    — Playwright scraper, fail-soft (selectors WIP)
  - compare_modes       — parallel fan-out across the above

Phase 5 (plan_trip + ranking + sbb/uk_trains composition) and beyond:
see /root/.claude/plans/brief-mcp-travel-steady-fountain.md.
"""

import asyncio
import json
import os
from datetime import date as date_type, datetime
from typing import Any

import asyncpg
import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

from mcp_travel.travel_affiliations import VALID_TAGS, filter_by as affiliations_filter_by
from mcp_travel.travel_cache import cache_get, cache_set
from mcp_travel.travel_drive import DriveError, drive_time as drive_route
from mcp_travel.travel_duffel import DuffelError, search_offers
from mcp_travel.travel_eurostar import (
    EurostarError,
    check as eurostar_scrape,
    build_booking_url as eurostar_build_url,
)
from mcp_travel.travel_eurotunnel import EurotunnelError, check as eurotunnel_scrape
from mcp_travel.travel_ferries import (
    FerryError, check as ferry_check, routes_to as ferry_routes_to,
    ROUTES as FERRY_ROUTES,
)
from mcp_travel.travel_hotels import search as hotels_search
from mcp_travel.travel_multi_leg import plan_multi_leg_impl
from mcp_travel.travel_plan import plan_trip_impl
from mcp_travel.travel_sncf import SncfError, search_journey as sncf_search
from mcp_travel.travel_ns import NSError, search_journey as ns_search
from mcp_travel.travel_sncb import SNCBError, search_journey as sncb_search
from mcp_travel.travel_db import DBError, search_journey as db_search
from mcp_travel.travel_trenitalia import (
    TrenitaliaError,
    search_journey as trenitalia_search,
    build_booking_urls as italy_build_urls,
)
from mcp_travel.travel_renfe import RenfeError, search_journey as renfe_search
from mcp_travel.travel_austria import AustriaError, search_journey as austria_search
from mcp_travel.travel_norway import NorwayError, search_journey as norway_search
from mcp_travel.travel_sweden import SwedenError, search_journey as sweden_search
from mcp_travel.travel_uber import UberError, price_estimates as uber_prices, time_estimates as uber_times, _deeplink as uber_deeplink
from mcp_travel.travel_tfl import (
    TflError,
    journey as tfl_journey,
    find_stop as tfl_find_stop,
    line_status as tfl_line_status,
)
from mcp_travel.travel_geocode import forward_geocode
from mcp_travel.travel_italy_status import ItalyStatusError, departures as italy_departures

_TTL_FLIGHTS = 6 * 3600
_TTL_RAIL = 12 * 3600
_TTL_SCRAPER = 24 * 3600
_TTL_DRIVE = 15 * 60      # traffic conditions stale after ~15 min
_TTL_HOTELS = 30 * 60     # offers slightly volatile but session-stable


@lifespan
async def travel_lifespan(server):
    dsn = os.environ["TRAVEL_DB_DSN"]
    sslmode = os.environ.get("TRAVEL_DB_SSLMODE", "prefer")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, ssl=sslmode)

    # Second pool for read-only access to Stu's mylocation.place — used
    # by travel_geocode.forward_geocode as a first-look named-place
    # lookup before hitting Nominatim. Optional: fails-soft if the
    # mcp_readonly password isn't injected.
    pool_loc = None
    ro_pw = os.environ.get("MCP_READONLY_PASSWORD")
    if ro_pw:
        try:
            pool_loc = await asyncpg.create_pool(
                host=os.environ.get("POSTGRES_HOST", "postgres"),
                port=int(os.environ.get("POSTGRES_PORT", "5432")),
                user="mcp_readonly",
                password=ro_pw,
                database="mylocation",
                min_size=1, max_size=2,
                ssl=sslmode,
            )
        except Exception:
            pool_loc = None

    client = httpx.AsyncClient(timeout=30.0)
    # Playwright was here (Phase 3) for Eurostar / LeShuttle scraping;
    # removed 2026-05-03 in favour of static durations. The eurostar /
    # eurotunnel modules accept a `browser` arg they ignore so the
    # signatures stay stable if scrapers come back.
    try:
        yield {"pool": pool, "client": client, "browser": None, "pool_locations": pool_loc}
    finally:
        await client.aclose()
        if pool_loc is not None:
            await pool_loc.close()
        await pool.close()


mcp = FastMCP("travel", lifespan=travel_lifespan)


def _ctx():
    return get_context().lifespan_context


# --- Per-tool impl helpers (return dicts; tool wrappers json.dumps) ---


async def _flight_impl(
    ctx: dict, origin_iata: str, dest_iata: str, date: str, cabin: str, adults: int,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
) -> dict[str, Any]:
    origin = origin_iata.upper().strip()
    dest = dest_iata.upper().strip()
    args = {
        "origin": origin, "destination": dest, "date": date,
        "cabin": cabin, "adults": adults,
        "prefer_carriers": sorted(prefer_carriers) if prefer_carriers else None,
        "exclude_carriers": sorted(exclude_carriers) if exclude_carriers else None,
    }
    bucket = date_type.fromisoformat(date)

    cached = await cache_get(ctx["pool"], "flight_check", args, bucket)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        result = await search_offers(
            ctx["client"], origin, dest, date,
            adults=adults, cabin=cabin,
            prefer_carriers=prefer_carriers,
            exclude_carriers=exclude_carriers,
        )
    except DuffelError as e:
        return {
            "ok": False,
            "mode": "flight",
            "error": str(e),
            "origin": origin,
            "destination": dest,
            "date": date,
        }

    await cache_set(ctx["pool"], "flight_check", args, bucket, result, _TTL_FLIGHTS)
    result["cached"] = False
    return result


async def _sncf_impl(
    ctx: dict, origin: str, destination: str, datetime_iso: str,
    is_arrival: bool, max_journeys: int,
) -> dict[str, Any]:
    args = {
        "origin": origin,
        "destination": destination,
        "datetime": datetime_iso,
        "is_arrival": is_arrival,
        "max_journeys": max_journeys,
    }
    bucket = date_type.fromisoformat(datetime_iso.split("T", 1)[0])

    cached = await cache_get(ctx["pool"], "sncf_journey", args, bucket)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        result = await sncf_search(
            ctx["client"], origin, destination, datetime_iso,
            is_arrival=is_arrival, max_journeys=max_journeys,
        )
    except SncfError as e:
        return {
            "ok": False,
            "mode": "rail",
            "error": str(e),
            "origin": origin,
            "destination": destination,
            "datetime": datetime_iso,
        }

    await cache_set(ctx["pool"], "sncf_journey", args, bucket, result, _TTL_RAIL)
    result["cached"] = False
    return result


async def _eurostar_impl(
    ctx: dict, origin_city: str, dest_city: str, date: str, adults: int,
    time: str = "10:00",
) -> dict[str, Any]:
    args = {"origin": origin_city, "dest": dest_city, "date": date, "adults": adults, "time": time}
    bucket = date_type.fromisoformat(date)

    cached = await cache_get(ctx["pool"], "eurostar_check", args, bucket)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        result = await eurostar_scrape(
            ctx.get("client"), origin_city, dest_city, date, adults=adults, time=time,
        )
    except EurostarError as e:
        return {
            "ok": False,
            "mode": "eurostar",
            "error": str(e),
            "origin": origin_city,
            "destination": dest_city,
            "date": date,
        }

    # Live timetables can shift seat availability hour-to-hour; cache
    # those for 6h. Static-table fallback is stable, cache 24h.
    ttl = 6 * 3600 if "eurostar-live" in (result.get("data_sources") or []) else _TTL_SCRAPER
    await cache_set(ctx["pool"], "eurostar_check", args, bucket, result, ttl)
    result["cached"] = False
    return result


async def _drive_impl(
    ctx: dict, origin: str, destination: str, depart_at: str | None,
    traffic_model: str, avoid_tolls: bool
) -> dict[str, Any]:
    args = {
        "origin": origin,
        "destination": destination,
        "depart_at": depart_at or "",
        "traffic_model": traffic_model,
        "avoid_tolls": avoid_tolls,
    }
    # Date bucket = the depart date (or today if not specified) — keeps
    # 'tomorrow at 8am' separate from 'next week at 8am' in the cache.
    if depart_at:
        bucket = date_type.fromisoformat(depart_at[:10])
    else:
        bucket = date_type.today()

    cached = await cache_get(ctx["pool"], "drive_time", args, bucket)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        result = await drive_route(
            ctx["client"], origin, destination,
            depart_at=depart_at, traffic_model=traffic_model, avoid_tolls=avoid_tolls,
        )
    except DriveError as e:
        return {
            "ok": False,
            "mode": "drive",
            "error": str(e),
            "origin": origin,
            "destination": destination,
            "depart_at": depart_at,
        }

    await cache_set(ctx["pool"], "drive_time", args, bucket, result, _TTL_DRIVE)
    result["cached"] = False
    return result


async def _eurotunnel_impl(
    ctx: dict,
    date: str,
    time: str,
    vehicle: str,
    passengers: int,
    direction: str = "FOCA",
    country_of_residence: str = "GB",
) -> dict[str, Any]:
    args = {
        "date": date, "time": time, "vehicle": vehicle, "passengers": passengers,
        "direction": direction, "cor": country_of_residence,
    }
    bucket = date_type.fromisoformat(date)

    cached = await cache_get(ctx["pool"], "eurotunnel_check", args, bucket)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        result = await eurotunnel_scrape(
            ctx.get("client"),
            date=date,
            time=time,
            vehicle=vehicle,
            passengers=passengers,
            direction=direction,
            country_of_residence=country_of_residence,
        )
    except EurotunnelError as e:
        return {
            "ok": False,
            "mode": "eurotunnel",
            "error": str(e),
            "date": date,
            "time": time,
            "vehicle": vehicle,
        }

    # Cache live results for 6h (prices can move); fallback for 24h.
    ttl = 6 * 3600 if "leshuttle-live" in (result.get("data_sources") or []) else _TTL_SCRAPER
    await cache_set(ctx["pool"], "eurotunnel_check", args, bucket, result, ttl)
    result["cached"] = False
    return result


# --- MCP tools ---


@mcp.tool()
async def travel_flight_check(
    origin_iata: str,
    dest_iata: str,
    date: str,
    cabin: str = "economy",
    adults: int = 2,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
) -> str:
    """Find flight offers between two airports on a date.

    Args:
        origin_iata: IATA code of origin airport (e.g. 'LGW', 'LHR').
        dest_iata: IATA code of destination airport (e.g. 'NCE', 'GVA').
        date: Departure date in ISO format (YYYY-MM-DD).
        cabin: Cabin class — 'economy', 'premium_economy', 'business', 'first'.
        adults: Number of adult passengers (default 2).
        prefer_carriers: Soft preference — matching offers move to top of
            results, non-matching kept as fallback below. Match is by IATA
            code OR case-insensitive substring on carrier name; checks
            both `owner` and per-segment `marketing_carrier`. E.g.
            ['BA','AY'] for British Airways or Finnair, ['easyJet'] for
            substring match. Affects ranking order, not which offers are
            returned.
        exclude_carriers: Hard exclusion — drops matching offers entirely.
            Same matching shape. E.g. ['Ryanair','Wizz'] to avoid budget
            carriers, ['U2','FR'] by IATA code.

    Returns up to 5 offers ranked by price (with preferred carriers
    bubbled to the top) plus a Skyscanner deeplink as a booking fallback.
    Uses Duffel test mode unless DUFFEL_MODE=live.
    """
    return json.dumps(
        await _flight_impl(
            _ctx(), origin_iata, dest_iata, date, cabin, adults,
            prefer_carriers=prefer_carriers,
            exclude_carriers=exclude_carriers,
        ),
        indent=2,
    )


@mcp.tool()
async def travel_sncf_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> str:
    """Plan a French rail journey via the SNCF Navitia API.

    Inputs accept free-text place names ('Paris Gare de Lyon'), Navitia IDs
    ('stop_area:SNCF:87686006'), or 'lat;lon' coords. Live pricing is **not**
    in the SNCF API — `booking_deeplink` jumps to sncf-connect.com.
    """
    return json.dumps(
        await _sncf_impl(_ctx(), origin, destination, datetime_iso, is_arrival, max_journeys),
        indent=2,
    )


@mcp.tool()
async def travel_ns_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> str:
    """Plan a Dutch rail journey via NS Reisinformatie API (NL).

    Free-text origin/destination resolves against NS station list (cached
    after first call). Accepts station codes ('ASD','RTD','UT'), exact
    names ('Amsterdam Centraal','Utrecht Centraal'), or substrings.

    Args:
        origin: Free-text Dutch station name or code.
        destination: Same.
        datetime_iso: ISO datetime with timezone offset
            ('2026-06-15T09:00:00+02:00') or naive ISO.
        is_arrival: If True, datetime_iso is the arrive-by target.
        max_journeys: Cap on returned options.

    Each journey carries planned + actual durations, transfers, crowd
    forecast, and per-leg details (operator, train number, platforms).
    """
    try:
        result = await ns_search(
            _ctx()["client"], origin, destination, datetime_iso,
            is_arrival=is_arrival, max_journeys=max_journeys,
        )
    except NSError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "NL",
                           "error": str(e), "origin": origin,
                           "destination": destination, "datetime": datetime_iso}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_sncb_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> str:
    """Plan a Belgian rail journey via the iRail community API (BE).

    Free-text origin/destination resolves against the iRail station list.
    iRail uses English-dash names ('Brussels-South','Antwerp-Central',
    'Liège-Guillemins') but accepts substrings of either standard or
    local names.

    Args:
        origin: Free-text Belgian station name.
        destination: Same.
        datetime_iso: ISO datetime; date+time used (timezone ignored —
            iRail assumes Belgium local).
        is_arrival: If True, datetime_iso is the arrive-by target
            (iRail timeSel=arrival).
        max_journeys: Cap on returned options.

    No auth needed. Live data from SNCB / NMBS scraped by iRail.
    """
    try:
        result = await sncb_search(
            _ctx()["client"], origin, destination, datetime_iso,
            is_arrival=is_arrival, max_journeys=max_journeys,
        )
    except SNCBError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "BE",
                           "error": str(e), "origin": origin,
                           "destination": destination, "datetime": datetime_iso}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_db_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> str:
    """Plan a German rail journey via db-rest (community DB HAFAS wrapper).

    Free-text origin/destination resolves via db-rest /locations endpoint
    (handles 'Köln Hbf', 'München Hauptbahnhof', 'Frankfurt(Main)Hbf'
    transparently — preferring 'stop' type results).

    Args:
        origin: Free-text German (or cross-border) station name.
        destination: Same.
        datetime_iso: ISO datetime with timezone offset preferred.
        is_arrival: If True, datetime_iso is the arrive-by target
            (db-rest `arrival` param).
        max_journeys: Cap on returned options.

    No auth. db-rest (v6.db.transport.rest) is a community service;
    occasionally has downtime. We fail-soft. To self-host, set
    DB_REST_BASE env var to your own instance.
    """
    try:
        result = await db_search(
            _ctx()["client"], origin, destination, datetime_iso,
            is_arrival=is_arrival, max_journeys=max_journeys,
        )
    except DBError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "DE",
                           "error": str(e), "origin": origin,
                           "destination": destination, "datetime": datetime_iso}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_italy_journey(
    origin: str,
    destination: str,
    date: str,
    adults: int = 2,
) -> str:
    """Italian high-speed rail (curated city-pair durations + booking deeplinks).

    Trenitalia (Frecciarossa) and Italo (NTV) have no public journey-
    planning API; their booking sites need SPA session auth that's not
    cleanly scrapeable. This tool returns curated direct-route durations
    for major Italian HSR pairs (Milano/Roma/Napoli/Firenze/Venezia/
    Torino/Bologna/etc.) plus deeplinks for booking on
    lefrecce.it (Trenitalia) and italotreno.com (Italo).

    **Scope: Italy-INTERNAL only.** For Italy ↔ Switzerland cross-border
    journeys (Milano ↔ Zermatt / Brig / Lugano / Zürich / Bern), use
    `travel_sbb_journey` instead — SBB's HAFAS planner handles the
    cross-border lookup correctly with live data, while this tool's
    static table only carries domestic pairs. For live Italo prices on
    domestic routes, use `travel_italy_prices_via_safari`.

    Args:
        origin: Italian city — 'milano', 'roma', 'firenze' (case-
                insensitive substring on station name accepted too).
        destination: Same.
        date: ISO date YYYY-MM-DD.
        adults: Headcount (default 2).

    For unknown city pairs returns ok=true direct=false with the booking
    URLs — user can plan via Trenitalia or Italo's site directly.
    """
    try:
        result = await trenitalia_search(_ctx()["client"], origin, destination, date, adults=adults)
    except TrenitaliaError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "IT",
                           "error": str(e), "origin": origin,
                           "destination": destination, "date": date}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_italy_prices_via_safari(
    origin_city: str,
    destination_city: str,
    date: str,
    adults: int = 2,
) -> str:
    """Italo train prices via the user's desktop Safari (apple_browser_*).

    Italo (italotreno.com / biglietti.italotreno.com) has no public
    pricing API but DOES carry search state in the URL — its SPA reads
    `osc=` (origin code) / `dsc=` (destination code) / `od=DD/MM/YYYY` /
    `adt=N` and auto-runs the search via `startSearch=true`. The page
    settles on /booking/selezione-treno-andata with prices + seat
    availability rendered as plain text.

    Trenitalia (lefrecce.it) is NOT supported by this tool — its SPA
    strips search state from the URL, so auto-fill isn't possible. To
    compare Trenitalia, the user must run the search by hand on the
    homepage and then call apple_browser_get_page_text manually.

    Coverage: only city pairs whose Italo booking codes are known. So
    far that's MC_ (Milano Centrale) and RT_ (Roma Termini) — verified
    2026-05-04. Other Italian cities need their codes probed via a
    one-off manual Italo search before this tool can build URLs for
    them. Tool returns `italo_codes_missing` with the offending slugs
    when codes haven't been verified yet.

    Args:
        origin_city: Italian city slug ('milano', 'roma', etc.) or
                     case-insensitive substring of the station name.
        destination_city: Same.
        date: YYYY-MM-DD outbound date (auto-converted to DD/MM/YYYY
              for the Italo URL).
        adults: Pax count (default 2).

    Workflow the calling LLM should run after this tool:
      1. mcp__claude_ai_Mees_Only__apple_browser_open_url(url=<italo_booking_url>)
      2. wait ~6 seconds for Italo's SPA to auto-run the search and
         redirect to /booking/selezione-treno-andata
      3. mcp__claude_ai_Mees_Only__apple_browser_get_page_text()
      4. parse train rows: each block is 'HH:MM - HH:MM' / station /
         'N h N min' / 'No available seats left' OR 'from £NN.NN in <fare>'
    """
    try:
        urls = italy_build_urls(origin_city, destination_city, date, adults)
    except TrenitaliaError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "IT",
                           "error": str(e)}, indent=2)

    # Also pull the static-table journey for duration context
    try:
        timetable = await trenitalia_search(
            _ctx()["client"], origin_city, destination_city, date, adults=adults,
        )
    except TrenitaliaError:
        timetable = None

    if urls.get("italo_codes_missing"):
        workflow_note = (
            f"Italo booking codes are not yet verified for: "
            f"{', '.join(urls['italo_codes_missing'])}. The auto-fill URL "
            "is empty — user must run the Italo search by hand at "
            "https://www.italotreno.com/en, then provide the destination "
            "code so the tool can build URLs for this pair next time."
        )
        workflow = [
            {"step": 1, "tool": "mcp__claude_ai_Mees_Only__apple_browser_open_url",
             "args": {"url": "https://www.italotreno.com/en"},
             "purpose": "Open Italo homepage; user fills the form by hand"},
            {"step": 2, "tool": None, "wait_seconds": 0,
             "purpose": "Wait for the user to complete the search"},
            {"step": 3, "tool": "mcp__claude_ai_Mees_Only__apple_browser_get_page_text",
             "args": {}, "purpose": "Read prices off the rendered results"},
        ]
    else:
        workflow_note = None
        workflow = [
            {"step": 1, "tool": "mcp__claude_ai_Mees_Only__apple_browser_open_url",
             "args": {"url": urls["italo_booking_url"]},
             "purpose": "Italo's SPA auto-runs the search via startSearch=true"},
            {"step": 2, "tool": None, "wait_seconds": 6,
             "purpose": "Wait for redirect to /selezione-treno-andata + render"},
            {"step": 3, "tool": "mcp__claude_ai_Mees_Only__apple_browser_get_page_text",
             "args": {}, "purpose": "Read trains + prices off the rendered page"},
            {"step": 4, "tool": None,
             "purpose": (
                 "Parse train rows: each block is 'HH:MM - HH:MM' + "
                 "station names + 'N h N min' + either 'No available "
                 "seats left' OR 'from £NN.NN in <fare class>'."
             )},
        ]

    parsing_hints = {
        "price_pattern": r"from\s+(\d+(?:\.\d{2})?)\s*£",
        "time_pattern": r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}",
        "duration_pattern": r"\d+\s*h\s*\d+\s*min",
        "fare_classes": ["Smart", "Comfort", "Prima", "Club Executive"],
        "sold_out_marker": "No available seats left",
        "next_day_marker": "+ 1 day",
        "currency_note": (
            "Italo localises currency to your IP / language setting. "
            "lang=en + a UK source IP shows £ GBP; from an Italian IP "
            "it'll show €. The price-pattern regex above assumes £; "
            "swap the suffix character if the page text shows €."
        ),
    }

    return json.dumps({
        "ok": True,
        "mode": "rail",
        "country": "IT",
        "service": "italy-safari-pricecheck",
        "operator": "Italo",
        "from": urls["from"], "from_slug": urls["from_slug"],
        "to": urls["to"], "to_slug": urls["to_slug"],
        "date": date,
        "adults": adults,
        "italo_booking_url": urls["italo_booking_url"],
        "italo_codes_missing": urls.get("italo_codes_missing"),
        "trenitalia_homepage": urls["trenitalia_homepage"],
        "trenitalia_note": urls["trenitalia_note"],
        "static_timetable": (
            {"direct": timetable.get("direct"),
             "minutes": timetable.get("minutes"),
             "operators": timetable.get("operators"),
             "frequency": timetable.get("frequency")}
            if isinstance(timetable, dict) else None
        ),
        "workflow": workflow,
        "workflow_note": workflow_note,
        "parsing_hints": parsing_hints,
        "data_sources": ["italo-via-safari", "static-table"],
        "as_of": datetime.utcnow().isoformat() + "Z",
    }, indent=2)


@mcp.tool()
async def travel_spain_journey(
    origin: str,
    destination: str,
    date: str,
    adults: int = 2,
) -> str:
    """Spanish high-speed rail (curated city-pair durations + booking deeplinks).

    Renfe (AVE/AVLO), Iryo, and Ouigo Spain have no public journey-
    planning API. Renfe's data.renfe.com publishes static GTFS but
    not in a journey-planner shape. This tool returns curated direct-
    route durations for the major AVE corridors (Madrid-Barcelona,
    Madrid-Sevilla/Málaga/Valencia/Alicante/Zaragoza, plus the
    Mediterranean and cross-border to France) with all three operators'
    booking deeplinks.

    Args:
        origin: Spanish city — 'madrid', 'barcelona', 'sevilla', etc.
                (case-insensitive substring on station name accepted).
        destination: Same.
        date: ISO date YYYY-MM-DD.
        adults: Headcount (default 2).

    For unknown city pairs returns ok=true direct=false with all three
    booking URLs — Spain's three-operator HSR market means you'll
    typically check renfe.com / iryo.eu / ouigo.com to compare.
    """
    try:
        result = await renfe_search(_ctx()["client"], origin, destination, date, adults=adults)
    except RenfeError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "ES",
                           "error": str(e), "origin": origin,
                           "destination": destination, "date": date}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_austria_journey(
    origin: str,
    destination: str,
    date: str,
    adults: int = 2,
) -> str:
    """Austrian rail (curated city-pair durations + ÖBB booking deeplink).

    Covers ÖBB Railjet (Wien-Salzburg-Innsbruck-Bregenz, Wien-Graz),
    Eurocity cross-border (Innsbruck-Italy, Salzburg-München, Wien-Praha
    /Budapest/Bratislava), and Nightjet sleepers (Wien overnight to
    Roma/Milano/Hamburg/Amsterdam/Brussels/Paris).

    Static-timetable data — no live availability or pricing. Use the
    booking_url for actual prices on shop.oebbtickets.at.
    """
    try:
        result = await austria_search(_ctx()["client"], origin, destination, date, adults=adults)
    except AustriaError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "AT",
                           "error": str(e), "origin": origin,
                           "destination": destination, "date": date}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_norway_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> str:
    """Norwegian rail journey planner via Entur (NO).

    Entur is Norway's national journey-planner data hub — fully public
    GraphQL API, no auth, gold-standard documentation. Covers Vy and
    other Norwegian operators. Free-text origin/destination resolved via
    Entur's geocoder.

    Args:
        origin: Free-text Norwegian station ('Oslo S', 'Bergen', 'Trondheim').
        destination: Same.
        datetime_iso: ISO datetime ('2026-06-15T08:00:00').
        is_arrival: If True, datetime_iso is the arrive-by target
            (Entur GraphQL `arriveBy: true`).
        max_journeys: Cap on returned options (default 5).

    Live timetable + line/operator info per leg. The best European rail
    API we have access to — wish all countries did it like this.

    **Note on fjord car-ferries**: Most Norwegian car ferries are
    turn-up-and-go (queue, drive on, pay onboard or via licence-plate
    camera billing) — no reservation. This tool returns schedule + live
    status, which is what you actually need to plan around them. The
    GraphQL query includes water transport so ferry legs surface
    naturally in mixed rail+ferry trips (Bergen→Stavanger via Mortavika
    crossing, Bodø→Moskenes Lofoten, etc.).
    """
    try:
        result = await norway_search(
            _ctx()["client"], origin, destination, datetime_iso,
            is_arrival=is_arrival, max_journeys=max_journeys,
        )
    except NorwayError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "NO",
                           "error": str(e), "origin": origin,
                           "destination": destination, "datetime": datetime_iso}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_sweden_journey(
    origin: str,
    destination: str,
    datetime_iso: str,
    is_arrival: bool = False,
    max_journeys: int = 5,
) -> str:
    """Swedish national journey planner via Trafiklab ResRobot v2.1 (SE).

    Pan-Sweden multi-modal — SJ national rail, regional operators, bus,
    tram, ferry, Stockholm Tunnelbana. HAFAS-based; ResRobot is the
    consolidated successor to the older Reseplanerare / Stolptidstabeller
    APIs. Free-text origin/destination resolved via location.name first.

    Args:
        origin: Free-text Swedish station ('Stockholm Centralstation',
                'Göteborg', 'Malmö C', 'Kiruna').
        destination: Same.
        datetime_iso: ISO datetime ('2026-06-15T08:00:00').
        is_arrival: If True, datetime_iso is the arrive-by target
            (ResRobot `searchForArrival=1`).
        max_journeys: Cap on returned options.

    Live HAFAS data — includes train number (e.g. Snabbtåg 429),
    operator, per-leg from/to + tracks. Snabbtåg = SJ's high-speed
    service.
    """
    try:
        result = await sweden_search(
            _ctx()["client"], origin, destination, datetime_iso,
            is_arrival=is_arrival, max_journeys=max_journeys,
        )
    except SwedenError as e:
        return json.dumps({"ok": False, "mode": "rail", "country": "SE",
                           "error": str(e), "origin": origin,
                           "destination": destination, "datetime": datetime_iso}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_uber_estimate(
    origin: str,
    destination: str,
) -> str:
    """Uber Rides estimate — price ranges + pickup ETA (UK + global cities).

    Geocodes free-text origin / destination via the same resolver
    plan_trip uses (mylocation.place named-places → travel.geocode_cache
    → Nominatim), then hits Uber's `/v1.2/estimates/price` and
    `/v1.2/estimates/time` in parallel.

    Returns per-product price ranges (UberX, Comfort, UberXL, Black,
    etc., depending on availability at the pickup point) plus pickup
    ETA per product. Plus a one-tap deeplink that opens the Uber
    app/web with pickup+dropoff prefilled — works regardless of whether
    UBER_CLIENT_ID/UBER_CLIENT_SECRET are set or whether the app has
    been granted Rides API scopes by Uber business development.

    Geographic note: Uber coverage is patchy outside major cities. UK
    airports + cities = full coverage. Most European capitals = partial.
    Rural France / Italy / smaller German cities = often no Uber at the
    pickup point — the API will return an empty product list. Bolt is
    often a better fit in eastern + southern Europe but isn't wired here.

    Args:
        origin: Free-text pickup ('LGW airport', 'Roma Termini', 'EC1A 1BB',
                or 'lat,lon').
        destination: Free-text dropoff (same forms).
    """
    ctx = _ctx()
    pool = ctx["pool"]
    client = ctx["client"]
    pool_loc = ctx.get("pool_locations")

    o = await forward_geocode(client, pool, origin, pool_locations=pool_loc)
    d = await forward_geocode(client, pool, destination, pool_locations=pool_loc)
    if not o or not d:
        return json.dumps({
            "ok": False, "mode": "rideshare", "service": "uber",
            "error": f"could not geocode origin={origin!r} or destination={destination!r}",
        }, indent=2)

    deeplink = uber_deeplink(o["lat"], o["lon"], d["lat"], d["lon"])

    # Try the API; if no token / API fails, return deeplink-only
    try:
        prices_task = uber_prices(client, o["lat"], o["lon"], d["lat"], d["lon"])
        times_task = uber_times(client, o["lat"], o["lon"])
        prices, times = await asyncio.gather(prices_task, times_task)
    except UberError as e:
        return json.dumps({
            "ok": True,
            "mode": "rideshare",
            "service": "uber",
            "from": o["display_name"],
            "to": d["display_name"],
            "from_lat": o["lat"], "from_lon": o["lon"],
            "to_lat": d["lat"], "to_lon": d["lon"],
            "deeplink": deeplink,
            "live_data": False,
            "note": f"Live estimates unavailable: {e}. Deeplink still works.",
            "products": [],
        }, indent=2)

    # Merge price + time per product
    times_by_product = {t.get("product_id"): t for t in times}
    products = []
    for p in prices:
        pid = p.get("product_id")
        t = times_by_product.get(pid) or {}
        products.append({
            "product_id": pid,
            "product": p.get("display_name") or p.get("localized_display_name"),
            "price_low": p.get("low_estimate"),
            "price_high": p.get("high_estimate"),
            "currency": p.get("currency_code"),
            "price_estimate_str": p.get("estimate"),
            "surge_multiplier": p.get("surge_multiplier"),
            "duration_seconds": p.get("duration"),
            "duration_minutes": (p.get("duration") or 0) // 60,
            "distance_miles": p.get("distance"),
            "pickup_eta_seconds": t.get("estimate"),
            "pickup_eta_minutes": (t.get("estimate") or 0) // 60,
        })

    # Cheapest non-surge first if available
    products.sort(key=lambda x: (x.get("price_low") or 99999))

    return json.dumps({
        "ok": True,
        "mode": "rideshare",
        "service": "uber",
        "data_sources": ["uber-live"],
        "from": o["display_name"],
        "to": d["display_name"],
        "from_lat": o["lat"], "from_lon": o["lon"],
        "to_lat": d["lat"], "to_lon": d["lon"],
        "deeplink": deeplink,
        "live_data": True,
        "products": products,
    }, indent=2)


@mcp.tool()
async def travel_italy_status(
    station: str,
    datetime_iso: str | None = None,
    max_results: int = 20,
) -> str:
    """Italian rail live departures via ViaggiaTreno (Trenitalia infomobilità).

    Live train status from Trenitalia's still-public infomobilità REST
    endpoints. Useful for "what's leaving Roma Termini in the next hour"
    and "is FR9523 to Milano running on time" — the kind of last-minute
    check that static tables can't answer.

    Args:
        station: Free-text station name; ViaggiaTreno autocomplete
            resolves it to a station ID (e.g. 'Roma Termini' → S08409).
        datetime_iso: ISO datetime; defaults to 'now'.
        max_results: Cap on returned trains (default 20).

    Each train carries: number, category (FR/IC/RV/etc.), destination,
    track, scheduled time, **delay in minutes**, departed/in-station
    flags. Italy's regional/branch-line trains DO appear here, unlike
    in travel_italy_journey which is HSR-only.
    """
    try:
        result = await italy_departures(
            _ctx()["client"], station, datetime_iso=datetime_iso, max_results=max_results,
        )
    except ItalyStatusError as e:
        return json.dumps({"ok": False, "country": "IT",
                           "error": str(e), "station": station,
                           "datetime": datetime_iso}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_eurostar_check(
    origin_city: str,
    dest_city: str,
    date: str,
    adults: int = 2,
    time: str = "10:00",
) -> str:
    """Live Eurostar timetable + per-class seat availability for a date.

    Calls the GraphQL endpoint Eurostar's site uses (`site-api.eurostar.com`).
    Returns every train running on `date` plus per-class seat counts
    (Standard, Standard Premier, Business Premier) and selects the train
    nearest the requested `time`.

    Stations: 'london' → 'paris' / 'brussels' / 'amsterdam' / 'rotterdam' /
    'disneyland' / 'lille'. Pricing is not exposed on this endpoint;
    booking_url goes to the live booking flow with the correct date.

    Args:
        origin_city: 'london' or another known slug.
        dest_city: target station slug.
        date: YYYY-MM-DD departure date.
        adults: Pax count (affects fare-class availability filtering).
        time: HH:MM target — selected_journey is the train at-or-after.

    Live cache TTL 6h, static-table fallback 24h.
    """
    return json.dumps(
        await _eurostar_impl(_ctx(), origin_city, dest_city, date, adults, time=time),
        indent=2,
    )


@mcp.tool()
async def travel_eurostar_prices_via_safari(
    origin_city: str,
    dest_city: str,
    date: str,
    adults: int = 2,
    return_date: str | None = None,
) -> str:
    """Build a Eurostar booking URL + workflow for fetching live prices
    through the user's desktop Safari (apple_browser_* MCP tools).

    Eurostar's GraphQL `JourneySearch` endpoint exposes seat availability
    but NOT prices — fares live behind the React booking SPA, which is
    actively hostile to headless scraping. The reliable workaround is to
    open the booking URL in the user's actual Safari (already
    authenticated, real fingerprint) and read prices off the rendered
    page via `apple_browser_get_page_text`.

    This tool returns:
      - `booking_url`: the precise Eurostar URL (origin/dest/date/adults)
      - `journeys`: live timetable + per-class seat counts (from the
        same JourneySearch endpoint travel_eurostar_check uses) —
        useful for matching prices in the page text back to specific
        train times
      - `workflow`: ordered apple_browser_* tool calls the calling LLM
        should chain to fetch + read prices
      - `parsing_hints`: text patterns to look for when extracting prices

    Args:
        origin_city: 'london' or another known slug.
        dest_city: target station slug.
        date: YYYY-MM-DD outbound date.
        adults: Pax count (affects displayed totals).
        return_date: Optional YYYY-MM-DD for return-trip pricing.

    Workflow the calling LLM should run after this tool:
      1. mcp__claude_ai_Mees_Only__apple_browser_open_url(url=<booking_url>)
      2. wait ~6 seconds for the React SPA to render the timetable
      3. mcp__claude_ai_Mees_Only__apple_browser_get_page_text()
      4. parse train rows: lines pairing 'HH:MM → HH:MM' with '£NN'
    """
    try:
        url_info = eurostar_build_url(
            origin_city=origin_city,
            dest_city=dest_city,
            date=date,
            adults=adults,
            return_date=return_date,
        )
    except EurostarError as e:
        return json.dumps({"ok": False, "mode": "eurostar", "error": str(e)}, indent=2)

    # Reuse the live JourneySearch result so the LLM has the timetable
    # to correlate against the page text.
    timetable = await _eurostar_impl(
        _ctx(), origin_city, dest_city, date, adults, time="10:00",
    )

    workflow = [
        {
            "step": 1,
            "tool": "mcp__claude_ai_Mees_Only__apple_browser_open_url",
            "args": {"url": url_info["url"]},
            "purpose": "Open the Eurostar booking page in the user's desktop Safari",
        },
        {
            "step": 2,
            "tool": None,
            "wait_seconds": 6,
            "purpose": "Let the React SPA render the timetable + prices",
        },
        {
            "step": 3,
            "tool": "mcp__claude_ai_Mees_Only__apple_browser_get_page_text",
            "args": {},
            "purpose": "Extract visible page text — includes per-train prices",
        },
        {
            "step": 4,
            "tool": None,
            "purpose": (
                "Parse prices from text: each train row pairs a "
                "'HH:MM → HH:MM' time with a '£NN' price; correlate "
                "with `journeys` from this response to attach prices "
                "to specific trains."
            ),
        },
    ]

    parsing_hints = {
        "price_pattern": r"£\s?\d+(?:\.\d{2})?",
        "time_pattern": r"\d{2}:\d{2}\s*[→\-–]\s*\d{2}:\d{2}",
        # UI fare class names (verified 2026-05-04). These differ from
        # the GraphQL backend codes (STD / SP / BP) which the journeys
        # array uses — UI displays the marketing names below.
        "fare_classes_ui": ["Eurostar Standard", "Eurostar Plus", "Eurostar Premier"],
        "fare_classes_backend_map": {
            "STD": "Eurostar Standard",
            "SP":  "Eurostar Plus",       # was "Standard Premier" pre-2024
            "BP":  "Eurostar Premier",    # was "Business Premier" pre-2024
        },
        "page_layout": (
            "Each train block has 'Departs:HH:MM' / 'Arrives:HH:MM' / "
            "'Journey duration: N hr N min' / 'Direct' or 'N change' "
            "followed by three fare-class rows. Each row is either "
            "'£NN' (often with 'NN seats left' / 'Only N seats left') "
            "or 'Not available'. Lowest fare is tagged 'Lowest fare'."
        ),
        "seats_pattern": r"(\d+ seats left|Only \d+ seats? left|Not available)",
        "note": (
            "Direct trains list as 'Direct route'; non-direct list "
            "'1 change' (typically via Brussels for Amsterdam, via Lille "
            "for Paris). For round-trips the prices shown are the "
            "outbound leg only — the inbound is on a separate page."
        ),
    }

    return json.dumps({
        "ok": True,
        "mode": "eurostar",
        "service": "eurostar-safari-pricecheck",
        "from": url_info["from"], "from_code": url_info["from_code"],
        "to": url_info["to"], "to_code": url_info["to_code"],
        "date": date,
        "return_date": return_date,
        "adults": adults,
        "booking_url": url_info["url"],
        "journeys": timetable.get("journeys") if isinstance(timetable, dict) else None,
        "journey_count": timetable.get("journey_count") if isinstance(timetable, dict) else None,
        "workflow": workflow,
        "parsing_hints": parsing_hints,
        "data_sources": ["eurostar-live", "manual-via-safari"],
        "as_of": datetime.utcnow().isoformat() + "Z",
    }, indent=2)


@mcp.tool()
async def travel_eurotunnel_check(
    date: str,
    time: str = "10:00",
    vehicle: str = "car",
    passengers: int = 2,
    direction: str = "FOCA",
    country_of_residence: str = "GB",
) -> str:
    """Live LeShuttle (Eurotunnel) crossings + GBP prices for a date.

    Calls the public `nextus-api-prod.leshuttle.com/ExactViewQuote`
    endpoint — same one the website uses — and returns:
      - `selected_crossing`: the crossing nearest `time` (with prices)
      - `crossings`: every available slot for the day with per-ticket-type
        prices (Standard, FlexiLongstay, etc.) and best_price

    Args:
        date: Departure date (YYYY-MM-DD).
        time: Preferred departure time HH:MM (used to pick selected_crossing).
        vehicle: 'car' / 'high-vehicle' / 'caravan-trailer' / 'motorhome' /
                 'motorcycle' — aliases accepted.
        passengers: For deeplink only (LeShuttle prices by vehicle, not pax).
        direction: 'FOCA' = Folkestone→Calais (default), 'CAFO' = reverse.
        country_of_residence: ISO-2 code; affects displayed currency
                              (GB → GBP, FR → EUR).

    Falls back to static-table durations if the live API errors. Live
    cache TTL 6h, fallback 24h.
    """
    return json.dumps(
        await _eurotunnel_impl(
            _ctx(), date, time, vehicle, passengers,
            direction=direction, country_of_residence=country_of_residence,
        ),
        indent=2,
    )


@mcp.tool()
async def travel_drive_time(
    origin: str,
    destination: str,
    depart_at: str | None = None,
    traffic_model: str = "aware",
    avoid_tolls: bool = False,
) -> str:
    """Traffic-aware drive time via Google Maps Routes API.

    Args:
        origin: Address, postcode, or 'lat,lon' coords (e.g. 'EC1A 1BB',
                'London, UK', '51.5074,-0.1278').
        destination: Same forms.
        depart_at: ISO datetime (e.g. '2026-05-04T05:45:00Z'). Defaults to
                   ~now (5 min in the future). Future timestamps are
                   required for traffic-aware ETAs to apply.
        traffic_model: 'aware' (default, fast, cheap) or 'optimal' (slower,
                       more accurate, more expensive) or 'static' (no
                       traffic, road network only).
        avoid_tolls: If True, route avoids toll roads.

    Returns duration_minutes (traffic-aware), static_duration_minutes (no
    traffic), traffic_delay_minutes (the difference), and distance.
    Cache TTL is 15 min — traffic is volatile but stable within a planning
    session. The cache key includes the depart_at date so tomorrow's 8am
    drive is cached separately from next week's 8am drive.
    """
    return json.dumps(
        await _drive_impl(
            _ctx(), origin, destination, depart_at, traffic_model, avoid_tolls
        ),
        indent=2,
    )


@mcp.tool()
async def travel_compare_modes(
    origin: str | None = None,
    destination: str | None = None,
    datetime_iso: str | None = None,
    flight: dict | None = None,
    eurostar: dict | None = None,
    eurotunnel: dict | None = None,
    sncf: dict | None = None,
    ns: dict | None = None,
    sncb: dict | None = None,
    db: dict | None = None,
    norway: dict | None = None,
    sweden: dict | None = None,
    italy: dict | None = None,
    spain: dict | None = None,
    austria: dict | None = None,
) -> str:
    """Run multiple modes in parallel and return a side-by-side comparison.

    The top-level `origin`, `destination`, `datetime_iso` are a
    **universal core** — any rail mode whose dict doesn't override them
    inherits them. Per-mode dicts can:
      - Pass `{}` to engage the mode with full inheritance from the core
      - Override any field (e.g. `{'origin':'Paris Gare de Lyon'}` to
        change just the origin for SNCF, leaving destination + datetime
        inherited)
      - Add mode-specific extras (cabin, prefer_carriers, vehicle, etc.)

    Args:
        origin: Universal origin (free text — used by rail modes that
            don't override).
        destination: Universal destination (same).
        datetime_iso: Universal ISO datetime — rail modes use this; static
            tables and eurotunnel derive a date from it.
        flight: {"origin_iata":"LGW","dest_iata":"NCE","cabin":"economy",
                 "adults":2,"prefer_carriers":["BA"]}
                — flight needs IATA codes so it doesn't share the rail
                free-text core.
        eurostar: {"origin_city":"london","dest_city":"paris","adults":2}
        eurotunnel: {"time":"10:00","vehicle":"car","passengers":2}
        sncf / ns / sncb / db / norway / sweden:
            {} (full inherit) or {"origin":..., "destination":...,
             "datetime_iso":..., "is_arrival":false, "max_journeys":5}
        italy / spain / austria:
            {} or {"origin":..., "destination":..., "adults":2}
            — static HSR tables, derive `date` from datetime_iso/date.

    Returns:
        JSON dict {origin, destination, datetime_iso, requested: [...],
        results: {mode: <result_dict>}}. Each mode's failure is reported
        in its own row; one dead mode never kills the others.
    """
    ctx = _ctx()
    tasks: dict[str, Any] = {}

    # Derive a date from datetime_iso if needed for static-table modes
    derived_date = datetime_iso.split("T", 1)[0] if datetime_iso else None

    def _pull(d: dict, key: str, fallback):
        """Mode-dict override or universal fallback."""
        return d.get(key, fallback)

    if flight is not None:
        tasks["flight"] = _flight_impl(
            ctx,
            origin_iata=flight["origin_iata"],
            dest_iata=flight["dest_iata"],
            date=_pull(flight, "date", derived_date),
            cabin=flight.get("cabin", "economy"),
            adults=flight.get("adults", 2),
            prefer_carriers=flight.get("prefer_carriers"),
            exclude_carriers=flight.get("exclude_carriers"),
        )

    if eurostar is not None:
        tasks["eurostar"] = _eurostar_impl(
            ctx,
            origin_city=_pull(eurostar, "origin_city", origin or "london"),
            dest_city=_pull(eurostar, "dest_city", destination),
            date=_pull(eurostar, "date", derived_date),
            adults=eurostar.get("adults", 2),
        )

    if eurotunnel is not None:
        tasks["eurotunnel"] = _eurotunnel_impl(
            ctx,
            date=_pull(eurotunnel, "date", derived_date),
            time=eurotunnel.get("time", "10:00"),
            vehicle=eurotunnel.get("vehicle", "car"),
            passengers=eurotunnel.get("passengers", 2),
        )

    if sncf is not None:
        tasks["sncf"] = _sncf_impl(
            ctx,
            origin=_pull(sncf, "origin", origin),
            destination=_pull(sncf, "destination", destination),
            datetime_iso=_pull(sncf, "datetime_iso", datetime_iso),
            is_arrival=sncf.get("is_arrival", False),
            max_journeys=sncf.get("max_journeys", 5),
        )

    if ns is not None:
        tasks["ns"] = ns_search(
            ctx["client"],
            _pull(ns, "origin", origin),
            _pull(ns, "destination", destination),
            _pull(ns, "datetime_iso", datetime_iso),
            is_arrival=ns.get("is_arrival", False),
            max_journeys=ns.get("max_journeys", 5),
        )

    if sncb is not None:
        tasks["sncb"] = sncb_search(
            ctx["client"],
            _pull(sncb, "origin", origin),
            _pull(sncb, "destination", destination),
            _pull(sncb, "datetime_iso", datetime_iso),
            is_arrival=sncb.get("is_arrival", False),
            max_journeys=sncb.get("max_journeys", 5),
        )

    if db is not None:
        tasks["db"] = db_search(
            ctx["client"],
            _pull(db, "origin", origin),
            _pull(db, "destination", destination),
            _pull(db, "datetime_iso", datetime_iso),
            is_arrival=db.get("is_arrival", False),
            max_journeys=db.get("max_journeys", 5),
        )

    if norway is not None:
        tasks["norway"] = norway_search(
            ctx["client"],
            _pull(norway, "origin", origin),
            _pull(norway, "destination", destination),
            _pull(norway, "datetime_iso", datetime_iso),
            is_arrival=norway.get("is_arrival", False),
            max_journeys=norway.get("max_journeys", 5),
        )

    if sweden is not None:
        tasks["sweden"] = sweden_search(
            ctx["client"],
            _pull(sweden, "origin", origin),
            _pull(sweden, "destination", destination),
            _pull(sweden, "datetime_iso", datetime_iso),
            is_arrival=sweden.get("is_arrival", False),
            max_journeys=sweden.get("max_journeys", 5),
        )

    if italy is not None:
        tasks["italy"] = trenitalia_search(
            ctx["client"],
            _pull(italy, "origin", origin),
            _pull(italy, "destination", destination),
            _pull(italy, "date", derived_date),
            adults=italy.get("adults", 2),
        )

    if spain is not None:
        tasks["spain"] = renfe_search(
            ctx["client"],
            _pull(spain, "origin", origin),
            _pull(spain, "destination", destination),
            _pull(spain, "date", derived_date),
            adults=spain.get("adults", 2),
        )

    if austria is not None:
        tasks["austria"] = austria_search(
            ctx["client"],
            _pull(austria, "origin", origin),
            _pull(austria, "destination", destination),
            _pull(austria, "date", derived_date),
            adults=austria.get("adults", 2),
        )

    if not tasks:
        return json.dumps(
            {"ok": False, "error": "no modes requested; pass at least one of "
             "flight/eurostar/eurotunnel/sncf/ns/sncb/db/norway/sweden/italy/spain/austria"},
            indent=2,
        )

    keys = list(tasks.keys())
    raw = await asyncio.gather(*tasks.values(), return_exceptions=True)
    results: dict[str, Any] = {}
    for k, r in zip(keys, raw):
        if isinstance(r, BaseException):
            results[k] = {
                "ok": False,
                "mode": k,
                "error": f"{type(r).__name__}: {r}",
            }
        else:
            results[k] = r

    return json.dumps(
        {
            "origin": origin,
            "destination": destination,
            "datetime_iso": datetime_iso,
            "date": derived_date,
            "requested": keys,
            "results": results,
        },
        indent=2,
    )


@mcp.tool()
async def travel_affiliation_search(
    affiliation: str = "RC",
    max_drive_min_from: list | None = None,
    max_drive_min_to: list | None = None,
    countries: list[str] | None = None,
    max_results: int = 12,
) -> str:
    """Find hotels in a curated luxury-affiliation list, drive-time validated.

    LiteAPI's hotel inventory doesn't carry boutique-affiliation properties
    (Relais & Châteaux, Leading Hotels of the World, etc.) reliably — they
    book through their own channels. This tool consults a curated module
    list (~40 well-known properties on western-European routes) and adds
    drive-time validation via Google Maps so you can ask "R&C on the
    Calais → Tasch route" without LiteAPI inventory gaps biting.

    Args:
        affiliation: One of 'RC' (Relais & Châteaux), 'LHW' (Leading Hotels),
                     'SLH' (Small Luxury Hotels), 'CHC' (Châteaux & Hôtels
                     Collection). Default 'RC'.
        max_drive_min_from: ['origin', minutes] — drop entries beyond budget
                            from this point (e.g. ['Calais, France', 480]).
        max_drive_min_to:   ['destination', minutes] — drop entries that
                            can't reach this point in budget (e.g. for a
                            second-day continuation: ['Tasch, Switzerland', 480]).
        countries: ISO-2 country codes filter (e.g. ['FR','CH']).
        max_results: cap on returned rows (default 12).

    Each result has name + city + lat/lon + URL (R&C search if no
    canonical), drive minutes from/to where requested, and the affiliation
    tag set (a property may carry multiple — e.g. RC+LHW). Bookings happen
    on the affiliation's own site — there's no live availability or price
    here, just route validation.
    """
    aff = (affiliation or "RC").upper()
    if aff not in VALID_TAGS:
        return json.dumps({
            "ok": False, "error": f"Unknown affiliation {aff!r}; valid: {sorted(VALID_TAGS)}",
        }, indent=2)

    candidates = affiliations_filter_by(affiliation=aff, countries=countries)
    if not candidates:
        return json.dumps({"ok": True, "affiliation": aff, "results": [], "note": "no entries match"}, indent=2)

    client = _ctx()["client"]
    drive_from_origin = max_drive_min_from[0] if max_drive_min_from else None
    drive_from_budget = max_drive_min_from[1] if max_drive_min_from else None
    drive_to_dest = max_drive_min_to[0] if max_drive_min_to else None
    drive_to_budget = max_drive_min_to[1] if max_drive_min_to else None

    async def _validate(h: dict) -> dict:
        coord = f"{h['lat']},{h['lon']}"
        from_min = None
        to_min = None
        if drive_from_origin:
            try:
                d = await drive_route(client, drive_from_origin, coord, traffic_model="static")
                from_min = d["duration_minutes"]
            except DriveError:
                pass
        if drive_to_dest:
            try:
                d = await drive_route(client, coord, drive_to_dest, traffic_model="static")
                to_min = d["duration_minutes"]
            except DriveError:
                pass
        return {**h, "drive_from_min": from_min, "drive_to_min": to_min}

    enriched = await asyncio.gather(*[_validate(h) for h in candidates])

    if drive_from_budget is not None:
        enriched = [h for h in enriched if h["drive_from_min"] is not None
                                          and h["drive_from_min"] <= drive_from_budget]
    if drive_to_budget is not None:
        enriched = [h for h in enriched if h["drive_to_min"] is not None
                                          and h["drive_to_min"] <= drive_to_budget]

    # Rank: lower max(from, to) is better — keeps days balanced
    def _score(h):
        f = h.get("drive_from_min") or 0
        t = h.get("drive_to_min") or 0
        return max(f, t) if (drive_from_budget or drive_to_budget) else f or t or 0

    enriched.sort(key=_score)

    return json.dumps({
        "ok": True,
        "affiliation": aff,
        "drive_filter_from": {"origin": drive_from_origin, "max_minutes": drive_from_budget} if drive_from_origin else None,
        "drive_filter_to":   {"destination": drive_to_dest, "max_minutes": drive_to_budget} if drive_to_dest else None,
        "candidate_count": len(candidates),
        "results": enriched[:max_results],
    }, indent=2)


@mcp.tool()
async def travel_hotel_search(
    near: str,
    check_in: str,
    check_out: str,
    min_stars: int = 4,
    pet_friendly: bool = False,
    max_drive_min_from: list | None = None,
    max_drive_min_to: list | None = None,
    chain_contains: str | None = None,
    radius_km: int = 25,
    guests: int = 2,
    max_results: int = 10,
) -> str:
    """Search hotels via Amadeus, optionally filtered by drive-time from another point.

    Args:
        near: City name, place, or 'lat,lon' string. Geocoded via Nominatim.
        check_in: ISO date (YYYY-MM-DD).
        check_out: ISO date.
        min_stars: Minimum star rating (default 4).
        pet_friendly: If True, filter for the PETS_ALLOWED amenity tag.
        max_drive_min_from: ['origin', minutes] e.g. ['Calais, France', 180].
                            Each candidate is checked via drive_time and
                            dropped if over budget.
        radius_km: Geocode-search radius around `near` (default 25 km).
        guests: Adult headcount (default 2).
        max_results: Cap on returned offers (default 10).

    Returns ranked list of hotels with live Amadeus offers (price, room
    description, amenities) — sorted by stars desc then price asc.
    Cache TTL is 30 min — re-running the same query within the session
    won't re-bill Amadeus.
    """
    args = {
        "near": near, "check_in": check_in, "check_out": check_out,
        "min_stars": min_stars, "pet_friendly": pet_friendly,
        "max_drive_min_from": list(max_drive_min_from) if max_drive_min_from else None,
        "max_drive_min_to": list(max_drive_min_to) if max_drive_min_to else None,
        "chain_contains": chain_contains,
        "radius_km": radius_km, "guests": guests, "max_results": max_results,
    }
    bucket = date_type.fromisoformat(check_in)
    cached = await cache_get(_ctx()["pool"], "hotel_search", args, bucket)
    if cached is not None:
        cached["cached"] = True
        return json.dumps(cached, indent=2)

    drive_budget = tuple(max_drive_min_from) if max_drive_min_from else None
    drive_budget_to = tuple(max_drive_min_to) if max_drive_min_to else None
    result = await hotels_search(
        _ctx()["pool"], _ctx()["client"],
        near=near, check_in=check_in, check_out=check_out,
        min_stars=min_stars, pet_friendly=pet_friendly,
        max_drive_min_from=drive_budget, max_drive_min_to=drive_budget_to,
        chain_contains=chain_contains, radius_km=radius_km,
        guests=guests, max_results=max_results,
        pool_locations=_ctx().get("pool_locations"),
    )
    if result.get("ok"):
        await cache_set(_ctx()["pool"], "hotel_search", args, bucket, result, _TTL_HOTELS)
    result["cached"] = False
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_list_named_places(
    place_type: str | None = None,
    name_contains: str | None = None,
    limit: int = 50,
) -> str:
    """List Stu's curated named places from mylocation.place.

    These are the places that resolve instantly in `forward_geocode` (and
    therefore in `plan_trip` / `hotel_search` / `affiliation_search` too)
    before we ever hit Nominatim. Useful for the LLM to see what
    short-form names are recognised — e.g. 'Mum's', 'Diddy's vet', a
    favoured hotel by nickname.

    Args:
        place_type: optional filter on place_type.name (Hotel, Restaurant,
                    Home, Family, Venue, Pub, City, Accommodation, Jazz Club,
                    Airport, …).
        name_contains: case-insensitive substring filter on place.name.
        limit: max rows (default 50).
    """
    pool_loc = _ctx().get("pool_locations")
    if pool_loc is None:
        return json.dumps({
            "ok": False,
            "error": "mylocation pool not initialised — MCP_READONLY_PASSWORD secret not mounted",
        }, indent=2)

    sql = (
        "SELECT p.name, pt.name AS place_type, p.lat, p.lon, p.notes, "
        "       p.date_from, p.date_to "
        "FROM place p JOIN place_type pt ON p.place_type_id = pt.id WHERE 1=1 "
    )
    args: list = []
    if place_type:
        args.append(place_type)
        sql += f"AND pt.name ILIKE ${len(args)} "
    if name_contains:
        args.append(f"%{name_contains}%")
        sql += f"AND (p.name ILIKE ${len(args)} OR p.notes ILIKE ${len(args)}) "
    args.append(limit)
    sql += f"ORDER BY pt.name, p.name LIMIT ${len(args)}"
    async with pool_loc.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    out = [
        {
            "name": r["name"],
            "type": r["place_type"],
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "notes": r["notes"],
            "date_from": r["date_from"].isoformat() if r["date_from"] else None,
            "date_to": r["date_to"].isoformat() if r["date_to"] else None,
        }
        for r in rows
    ]
    return json.dumps({"ok": True, "count": len(out), "places": out}, indent=2)


@mcp.tool()
async def travel_recent_trips(
    limit: int = 10,
    destination_contains: str | None = None,
) -> str:
    """Recent plan_trip queries from the journey_log audit table.

    Useful for retrospectives ("what did we look at for the May trip?")
    and for ranking-weight tuning. Each row is one plan_trip call with
    the destination, dates, party, and the chosen 'best' option summary.

    Args:
        limit: max rows (default 10).
        destination_contains: case-insensitive substring filter on
            destination (e.g. 'avignon', 'zermatt').
    """
    pool = _ctx()["pool"]
    sql = (
        "SELECT id, asked_at, destination, depart_date, return_date, party, "
        "       result->>'best' AS best, result->>'region' AS region "
        "FROM journey_log "
    )
    args: list = []
    if destination_contains:
        sql += "WHERE destination ILIKE $1 "
        args.append(f"%{destination_contains}%")
    sql += "ORDER BY asked_at DESC LIMIT $%d" % (len(args) + 1)
    args.append(limit)
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    out = []
    for r in rows:
        party_raw = r["party"]
        # asyncpg returns jsonb as the parsed Python object
        out.append({
            "id": r["id"],
            "asked_at": r["asked_at"].isoformat(),
            "destination": r["destination"],
            "region": r["region"],
            "depart_date": r["depart_date"].isoformat() if r["depart_date"] else None,
            "return_date": r["return_date"].isoformat() if r["return_date"] else None,
            "party": party_raw if isinstance(party_raw, list) else json.loads(party_raw or "[]"),
            "best": r["best"],
        })
    return json.dumps({"ok": True, "count": len(out), "trips": out}, indent=2)


@mcp.tool()
async def travel_plan_trip(
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    party: list[str] | None = None,
    depart_time: str = "08:00",
    max_options: int = 6,
    origin: str | None = None,
    origin_label: str | None = None,
    fly_only: bool = False,
    dest_airports: list[str] | None = None,
    overnight_near: str | None = None,
    prefer_affiliation: str | None = None,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
) -> str:
    """Plan a trip from the configured home origin to a destination — door-to-door, mode-aware.

    Args:
        destination: Free-text place — town, postcode, or 'lat,lon'.
                     ('Avignon', 'Saint-Malo', 'Verbier', 'Nice, France'
                     or '43.95,4.81' all work.)
        depart_date: ISO date (YYYY-MM-DD).
        return_date: Optional ISO date (return trip not yet folded into ranking).
        party: Names of travellers; defaults to is_default=true rows from
               party_member table (currently 'Stu' + 'Fran').
        depart_time: Hour to leave home (HH:MM, default 08:00). Drives the
                     traffic-aware leg estimates from Google Maps.
        max_options: Cap on returned options (default 6).

    Returns ranked list of door-to-door options across realistic modes
    (Eurostar, flight, drive+Eurotunnel, fly Geneva+drive for Alps), each
    with leg-by-leg breakdown, total minutes, transfers, booking URLs, and
    a confidence tag. Region heuristics drive which modes are tried —
    Côte d'Azur leads with flight, Brittany leads with Eurotunnel, etc.
    Persisted to journey_log table for retrospective ('what did we look
    at last summer?') and ranking-weight tuning.
    """
    return json.dumps(
        await plan_trip_impl(
            _ctx(), destination, depart_date, return_date, party,
            depart_time, max_options,
            origin=origin, origin_label=origin_label,
            fly_only=fly_only, dest_airports=dest_airports,
            overnight_near=overnight_near,
            prefer_affiliation=prefer_affiliation,
            prefer_carriers=prefer_carriers,
            exclude_carriers=exclude_carriers,
        ),
        indent=2,
    )


@mcp.tool()
async def travel_ferry_check(
    origin_port: str,
    dest_port: str,
    date: str,
    vehicle: str = "car",
    passengers: int = 2,
) -> str:
    """Find ferry crossings between two named ports on a date.

    DFDS, Brittany Ferries, Stena Line, and P&O Ferries rows carry LIVE
    data — every sailing for the day with real prices and availability:
      - DFDS: Channel (Dover↔Calais/Dunkirk, Newhaven↔Dieppe), Channel
        Islands, Newcastle↔Amsterdam cabin, Rosslare↔Dunkirk, Baltic.
      - Brittany Ferries: Plymouth↔Roscoff, Portsmouth↔StMalo/Caen/
        LeHavre/Cherbourg, Poole↔Cherbourg, Portsmouth↔Bilbao/Santander,
        Cork↔Roscoff, Rosslare↔Bilbao. Per-tier (economy/standard/flexi).
      - Stena Line (GraphQL): Holyhead↔Dublin, Belfast↔Cairnryan/Liverpool,
        Fishguard↔Rosslare, Harwich↔Hook of Holland, plus 7 Scandinavian
        routes. Multi-currency (GBP/EUR/SEK/DKK/NOK).
      - P&O Ferries (Expian REST): Dover↔Calais (Channel — competes with
        DFDS), Larne↔Cairnryan, Hull↔Rotterdam (sole operator). 3-tier
        fares (Standard / Flexi / Fully Flexi).

    Other operators (Irish Ferries, Steam Packet) return static crossing-
    time + frequency data only — their booking sites don't expose public
    APIs; click their booking_url for live data.

    Returns one entry per operator/route combo (e.g. Dover→Calais comes
    back as DFDS [live], P&O [static], and Irish Ferries [static] — three
    rows). Live rows additionally include `sailings[]` with the day's
    timetable, per-sailing prices, and a `best_price`.

    Args:
        origin_port: Substring of port name — 'Dover', 'Portsmouth',
                     'Holyhead', 'Cairnryan' etc. Case-insensitive.
        dest_port: Substring of destination port. 'Calais', 'Dublin',
                   'Belfast', 'Caen', etc.
        date: ISO date (YYYY-MM-DD).
        vehicle: 'car' (default), 'motorcycle', or 'foot' (DFDS live);
                 'high-vehicle' / 'caravan-trailer' / 'foot-passenger'
                 accepted but treated as 'car' for DFDS price lookup.
        passengers: Headcount (default 2).
    """
    try:
        result = await ferry_check(
            _ctx()["client"], origin_port=origin_port, dest_port=dest_port,
            date=date, vehicle=vehicle, passengers=passengers,
        )
    except FerryError as e:
        return json.dumps({"ok": False, "mode": "ferry", "error": str(e),
                           "origin_port": origin_port, "dest_port": dest_port,
                           "date": date}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def travel_ferry_routes_to(
    country_or_region: str,
) -> str:
    """List ferry routes whose destination matches a country / region.

    Useful for discovery before `travel_ferry_check`: 'where can I sail
    from the UK to France / Ireland / Spain / Netherlands?' Accepts
    country names ('Ireland', 'France'), ISO codes ('IE', 'FR'), or
    region groupings ('Channel', 'North Sea', 'Irish Sea',
    'British Isles', 'Northern Ireland').

    Returns a list of routes (origin_port, dest_port, operator,
    crossing_minutes, frequency, seasonal flag) sorted by crossing time.
    """
    routes = ferry_routes_to(country_or_region)
    if not routes:
        return json.dumps({
            "ok": True, "country_or_region": country_or_region, "routes": [],
            "note": f"No curated routes for {country_or_region!r}. Known regions: "
                    "France, Spain, Netherlands, Belgium, Ireland, Northern Ireland, "
                    "Isle of Man, Channel, North Sea, Irish Sea, British Isles.",
        }, indent=2)
    routes_sorted = sorted(routes, key=lambda r: r["crossing_minutes"])
    return json.dumps({
        "ok": True,
        "country_or_region": country_or_region,
        "count": len(routes_sorted),
        "routes": routes_sorted,
    }, indent=2)


@mcp.tool()
async def travel_plan_multi_leg(
    name: str,
    legs: list[dict],
    stops: list[dict] | None = None,
    party: list[str] | None = None,
    pacing_seconds: float = 6.0,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
) -> str:
    """Plan a multi-leg trip — N flights + M hotel stays — in one call.

    Each leg gets a Duffel `flight_check`; legs are run sequentially with
    `pacing_seconds` (default 6 s) between calls to stay under Duffel's
    rate limit. Each stop gets a LiteAPI `hotel_search` (run in parallel
    — LiteAPI tolerates fan-out). Aggregates total flight cost +
    hours, total hotel cost (cheapest 4★+ × nights), and persists the
    whole itinerary as one journey_log row keyed by `name`.

    Args:
        name: Human-readable itinerary name (logged + returned).
        legs: List of flight specs:
              [{"orig":"LHR","dest":"EZE","date":"2026-09-01",
                "cabin":"business","adults":2}, ...]
              `cabin` defaults to 'economy', `adults` defaults to party size.
        stops: Optional list of hotel-stay specs:
               [{"city":"Buenos Aires","check_in":"2026-09-01",
                 "check_out":"2026-09-06","min_stars":4,
                 "pet_friendly":false,"radius_km":25,"max_results":5,
                 "chain_contains":null}, ...]
        party: Default = is_default=true rows from party_member.
        pacing_seconds: Delay between Duffel calls. Lower → faster but
                        risks 429. Default 6 s is well within tolerance.

    Returns one structured object with `flights[]`, `hotels[]`,
    `total_flight_cost`, `total_hotel_cost`, `total_estimated_cost`,
    `total_flight_hours`. Use `travel_recent_trips` to retrieve later
    (the `[multi-leg] <name>` row in journey_log).
    """
    return json.dumps(
        await plan_multi_leg_impl(
            _ctx(), name=name, legs=legs, stops=stops,
            party=party, pacing_seconds=pacing_seconds,
            prefer_carriers=prefer_carriers,
            exclude_carriers=exclude_carriers,
        ),
        indent=2,
    )


@mcp.tool()
async def travel_tfl_journey(
    origin: str,
    destination: str,
    datetime_iso: str | None = None,
    is_arrival: bool = False,
    modes: list[str] | None = None,
    max_journeys: int = 4,
) -> str:
    """Plan a journey via Transport for London's Journey Planner.

    TfL's planner covers all London modes: Tube, Bus, Overground, DLR,
    Elizabeth Line, Trams, Cable Car, Thames Clippers — plus National
    Rail trains into London zones. Free public API, no auth.

    Args:
        origin: Stop name ('Waterloo'), specific TfL StopPoint ID
                ('940GZZLUKSX'), or 'lat,lon'. Free-text names get
                disambiguated automatically (prefers transit hubs).
        destination: Same.
        datetime_iso: ISO 8601 datetime. None = now.
        is_arrival: Treat datetime as required arrival time.
        modes: Filter to ['tube', 'bus', 'walking', 'national-rail',
               'overground', 'elizabeth-line', 'dlr', 'tram'] etc.
        max_journeys: Cap on returned options.

    Returns: list of journeys with depart/arrive times, duration_minutes,
    legs (mode + from/to + minutes + instruction), and Tube fare in
    pence/GBP where TfL prices it.
    """
    try:
        result = await tfl_journey(
            _ctx()["client"], origin, destination,
            datetime_iso=datetime_iso, is_arrival=is_arrival,
            modes=modes, max_journeys=max_journeys,
        )
    except TflError as e:
        return json.dumps({"ok": False, "mode": "tfl", "error": str(e),
                           "origin": origin, "destination": destination}, indent=2)
    return json.dumps({"ok": True, "mode": "tfl",
                       "origin": origin, "destination": destination,
                       "journeys": result, "journey_count": len(result)},
                      indent=2)


@mcp.tool()
async def travel_tfl_find_stop(query: str, max_results: int = 8) -> str:
    """Search TfL stops by name.

    Returns id / name / modes / lat-lon / zone. Use the `id` directly
    in travel_tfl_journey to skip ambiguity (e.g. '940GZZLUKSX' for
    King's Cross St Pancras Tube).
    """
    try:
        result = await tfl_find_stop(_ctx()["client"], query, max_results=max_results)
    except TflError as e:
        return json.dumps({"ok": False, "error": str(e), "query": query}, indent=2)
    return json.dumps({"ok": True, "query": query, "stops": result,
                       "count": len(result)}, indent=2)


@mcp.tool()
async def travel_tfl_line_status(target: str = "tube") -> str:
    """Get live TfL line status — Good Service / Minor Delays / Severe
    Delays / Part Suspended etc., with reason text.

    Args:
        target: A mode ('tube', 'bus', 'overground', 'dlr', 'tram',
                'elizabeth-line', 'national-rail', 'river-bus',
                'cable-car') for ALL lines in that mode, or a specific
                line id ('victoria', 'central', 'piccadilly', etc.) for
                just that line.
    """
    try:
        result = await tfl_line_status(_ctx()["client"], target)
    except TflError as e:
        return json.dumps({"ok": False, "error": str(e), "target": target}, indent=2)
    return json.dumps({"ok": True, "target": target, "lines": result,
                       "count": len(result)}, indent=2)


# Fold rail submodules into the same FastMCP("travel") server so the gateway
# routes a single backend (no separate mcp-sbb / mcp-uk-trains containers).
from . import travel_sbb, travel_uk  # noqa: E402

travel_sbb.register(mcp)
travel_uk.register(mcp)


if __name__ == "__main__":
    from mcp_travel.run import serve

    serve(mcp)
