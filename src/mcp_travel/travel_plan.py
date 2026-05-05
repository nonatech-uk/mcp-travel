"""plan_trip orchestrator: per-mode door-to-door builders.

Each builder takes the lifespan ctx + destination context + a target
departure datetime, calls the appropriate single-mode tool plus
drive_time for the access legs, and returns a normalised option dict:

  {
    "ok": bool,
    "mode": "eurostar|flight|eurotunnel|fly_geneva_drive",
    "door_to_door_minutes": int,
    "transfers": int,           # count of mode-changes (1 = single change)
    "legs": [                   # ordered list of journey segments
      {"kind":"drive|train|flight|crossing|transfer",
       "from": str, "to": str, "minutes": int, "operator": str?,
       "booking_url": str?, "note": str?}
    ],
    "data_sources": [...],
    "booking_urls": [...],
    "trade_offs": [...],
    "summary": str,
  }

The orchestrator (plan_trip) geocodes the destination, classifies its
region, picks the candidate mode set, runs the builders concurrently
with asyncio.gather, ranks them via travel_rank.score, and persists the
ranked comparison to journey_log.
"""

import asyncio
import json
import os
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Any

from mcp_travel.travel_drive import DriveError, drive_time as drive_route
from mcp_travel.travel_eurostar import STATIONS as EUROSTAR_STATIONS, check as eurostar_check
from mcp_travel.travel_eurotunnel import check as eurotunnel_check
from mcp_travel.travel_geocode import forward_geocode
from mcp_travel.travel_duffel import search_offers
from mcp_travel.travel_rank import (
    AIRPORT_OVERHEAD_MIN,
    PREDEPARTURE_BUFFER_MIN,
    REGION_AIRPORTS,
    REGION_EUROSTAR,
    REGION_MODES,
    SKI_RESORTS,
    classify_origin_region,
    classify_region,
    confidence,
    filter_modes_by_origin,
    score,
)

ORIGIN_HOME_DEFAULT = os.environ.get("MCP_TRAVEL_DEFAULT_ORIGIN", "London, UK")
ORIGIN_HOME_LABEL_DEFAULT = os.environ.get("MCP_TRAVEL_DEFAULT_ORIGIN_LABEL", "London (default)")


def _to_iso_z(dt: datetime) -> str:
    return dt.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def _drive_or_fallback(
    client, origin: str, destination: str, depart_at_iso: str, fallback_min: int
) -> dict[str, Any]:
    """drive_time but never raises — fallbacks to a static estimate on error."""
    try:
        return await drive_route(client, origin, destination, depart_at=depart_at_iso)
    except DriveError as e:
        return {
            "ok": False,
            "duration_minutes": fallback_min,
            "error": str(e),
            "fallback": True,
            "origin": origin,
            "destination": destination,
        }


# Car-free destinations where the village itself isn't drivable —
# substitute the parking-station coords for the drive lookup, then
# add a short final transit hop.
_CAR_FREE_PARKING: dict[tuple[float, float], dict[str, Any]] = {
    # (lat, lon) → parking + final-leg metadata. Lat/lon are ROUNDED
    # to 2 dp for matching, so coords within ~1km of a known car-free
    # destination route correctly.
    (46.02, 7.75): {  # Zermatt
        "village": "Zermatt",
        "parking_lat": 46.066, "parking_lon": 7.7787,
        "parking_name": "Täsch (Zermatt parking)",
        "final_minutes": 12,
        "final_operator": "MGB",
        "final_kind": "train",
        "final_note": (
            "MGB shuttle train Täsch → Zermatt (12 min, every 20 min, "
            "~CHF 8 each way; Zermatt itself is car-free)"
        ),
    },
}


def _resolve_drive_target(dest: dict) -> tuple[float, float, dict | None]:
    """If the destination is a known car-free village, return parking
    coords + final-leg metadata. Otherwise return the destination's
    own coords + None."""
    key = (round(dest["lat"], 2), round(dest["lon"], 2))
    info = _CAR_FREE_PARKING.get(key)
    if info:
        return info["parking_lat"], info["parking_lon"], info
    return dest["lat"], dest["lon"], None


def _final_leg_from(info: dict | None) -> list[dict[str, Any]]:
    """Return [final_train_leg] if needed, else []."""
    if not info:
        return []
    return [{
        "kind": info["final_kind"],
        "from": info["parking_name"].split(" (")[0],
        "to": info["village"],
        "minutes": info["final_minutes"],
        "operator": info["final_operator"],
        "note": info["final_note"],
    }]


async def build_eurotunnel(
    ctx, dest: dict, depart_dt: datetime,
    origin: str = ORIGIN_HOME_DEFAULT, origin_label: str = ORIGIN_HOME_LABEL_DEFAULT,
) -> dict[str, Any]:
    """Drive home → Folkestone → crossing → Calais → drive to destination."""
    home_to_folkestone_depart = depart_dt
    drive_to_folkestone = await _drive_or_fallback(
        ctx["client"], origin, "Folkestone Eurotunnel Terminal",
        _to_iso_z(home_to_folkestone_depart), fallback_min=95,
    )
    drive_min_uk = int(round(drive_to_folkestone["duration_minutes"]))

    arrive_folkestone = depart_dt + timedelta(minutes=drive_min_uk)
    crossing_depart_uk = arrive_folkestone + timedelta(minutes=PREDEPARTURE_BUFFER_MIN)
    crossing_arrive_fr = crossing_depart_uk + timedelta(minutes=35 + 5)  # 35 crossing + 5 customs
    # Calais Coquelles → destination — substitute parking-station coords
    # for car-free villages (Zermatt → Täsch). Final leg appended below.
    drive_target_lat, drive_target_lon, carfree = _resolve_drive_target(dest)
    final_legs = _final_leg_from(carfree)
    final_minutes = sum(l["minutes"] for l in final_legs)

    # Static fallback: Calais → most European destinations is 3-10h.
    # We pick a reasonable default; live Google Maps overrides it.
    drive_calais = await _drive_or_fallback(
        ctx["client"],
        "Calais Eurotunnel Terminal, France",
        f"{drive_target_lat},{drive_target_lon}",
        _to_iso_z(crossing_arrive_fr),
        fallback_min=540,   # 9h fallback for distant Alpine destinations
    )
    drive_min_fr = int(round(drive_calais["duration_minutes"]))

    et = await eurotunnel_check(
        ctx["client"], date=depart_dt.strftime("%Y-%m-%d"),
        time=crossing_depart_uk.strftime("%H:%M"), vehicle="car", passengers=2,
    )

    # If we got live data, prefer the actual booked crossing time (the API
    # rounds to its real timetable; our calc was just a target).
    et_live = "leshuttle-live" in (et.get("data_sources") or [])
    selected = et.get("selected_crossing") or {}
    actual_dep_iso = selected.get("departure")
    if et_live and actual_dep_iso:
        try:
            actual_dep = datetime.fromisoformat(actual_dep_iso.replace("Z", ""))
            crossing_depart_uk = actual_dep
            crossing_arrive_fr = actual_dep + timedelta(minutes=35 + 5)
        except (ValueError, TypeError):
            pass

    best_price = selected.get("best_price")
    currency = et.get("currency") or "GBP"

    door_to_door = drive_min_uk + PREDEPARTURE_BUFFER_MIN + 35 + 5 + drive_min_fr + final_minutes
    crossing_note = "35 min crossing + 5 min customs/disembark"
    if et_live and actual_dep_iso:
        crossing_note = f"{crossing_note}; live slot {crossing_depart_uk.strftime('%H:%M')}"
        if best_price is not None:
            crossing_note += f", from {currency} {best_price}"

    legs = [
        {"kind": "drive", "from": origin_label, "to": "Folkestone Terminal",
         "minutes": drive_min_uk, "operator": "self-drive",
         "note": f"{drive_to_folkestone.get('distance_km','?')} km via M25/M20"
                 + (" (live traffic)" if not drive_to_folkestone.get("fallback") else " (static estimate)")},
        {"kind": "wait", "from": "Folkestone Terminal", "to": "Folkestone Terminal",
         "minutes": PREDEPARTURE_BUFFER_MIN, "note": "60-min pre-departure buffer"},
        {"kind": "crossing", "from": "Folkestone", "to": "Calais Coquelles",
         "minutes": 40, "operator": "LeShuttle",
         "depart": crossing_depart_uk.isoformat() if et_live else None,
         "arrive": crossing_arrive_fr.isoformat() if et_live else None,
         "price_gbp": best_price if currency == "GBP" else None,
         "price": best_price, "price_currency": currency if best_price is not None else None,
         "booking_url": et.get("booking_url"),
         "note": crossing_note},
        {"kind": "drive", "from": "Calais Coquelles",
         "to": (carfree["parking_name"] if carfree else dest["display_name"]),
         "minutes": drive_min_fr, "operator": "self-drive",
         "note": f"{drive_calais.get('distance_km','?')} km via French motorway"
                 + (" (live traffic)" if not drive_calais.get("fallback") else " (static estimate)")},
        *final_legs,
    ]
    return {
        "ok": True,
        "mode": "eurotunnel",
        "door_to_door_minutes": door_to_door,
        "transfers": 1,
        "legs": legs,
        "total_cost_gbp": best_price if (currency == "GBP" and best_price is not None) else None,
        "data_sources": [
            "leshuttle-live" if et_live else "static-timetable",
            "google-maps" if not drive_to_folkestone.get("fallback") else "static-fallback",
            "google-maps" if not drive_calais.get("fallback") else "static-fallback",
        ],
        "booking_urls": [et.get("booking_url")],
        "trade_offs": [
            "Take your own car — no transfers at the destination, room for luggage.",
            f"Total drive: {drive_min_uk + drive_min_fr} min ({drive_min_uk} UK + {drive_min_fr} FR).",
        ],
        "summary": f"Drive + LeShuttle: {door_to_door} min door-to-door "
                   f"({drive_min_uk}+{PREDEPARTURE_BUFFER_MIN}+40+{drive_min_fr})",
    }


async def build_eurostar(
    ctx, region: str, dest: dict, depart_dt: datetime,
    origin: str = ORIGIN_HOME_DEFAULT, origin_label: str = ORIGIN_HOME_LABEL_DEFAULT,
) -> dict[str, Any]:
    home_to_stp_depart = depart_dt
    drive_to_stp = await _drive_or_fallback(
        ctx["client"], origin, "St Pancras International, London",
        _to_iso_z(home_to_stp_depart), fallback_min=95,
    )
    drive_min_uk = int(round(drive_to_stp["duration_minutes"]))

    es_dest = REGION_EUROSTAR.get(region, "paris")
    # Target time = arrive at St Pancras + 60-min buffer
    arrive_stp = depart_dt + timedelta(minutes=drive_min_uk)
    target_train_time = (arrive_stp + timedelta(minutes=PREDEPARTURE_BUFFER_MIN)).strftime("%H:%M")
    es = await eurostar_check(
        ctx["client"], "london", es_dest, depart_dt.strftime("%Y-%m-%d"),
        adults=2, time=target_train_time,
    )

    direct = es.get("direct", False)
    if not direct:
        return {
            "ok": False,
            "mode": "eurostar",
            "error": f"No direct Eurostar to {es.get('to','destination')}; via-Paris+TGV path not yet implemented",
            "trade_offs": ["Would need SNCF-API key and TGV interchange logic to compute"],
        }

    # Seasonal filtering — direct routes are sometimes summer-only or
    # winter-only. Don't return an option for a date outside the window.
    seasonal = es.get("seasonal")
    if isinstance(seasonal, str):
        m = depart_dt.month
        in_window = True
        s_lower = seasonal.lower()
        if "summer" in s_lower or ("may" in s_lower and "sep" in s_lower):
            in_window = 5 <= m <= 9
        elif "winter" in s_lower or "dec" in s_lower:
            in_window = m == 12 or m <= 4
        if not in_window:
            return {
                "ok": False,
                "mode": "eurostar",
                "error": f"Direct {es.get('to')} Eurostar is {seasonal}; not running on {depart_dt.date()}",
                "trade_offs": ["Use the Paris/Lille interchange path (not yet implemented), or pick another mode."],
            }

    es_live = "eurostar-live" in (es.get("data_sources") or [])
    selected = es.get("selected_journey") or {}
    train_min = selected.get("duration_minutes") or es.get("minutes") or 180
    es_depart = selected.get("departure")
    es_arrive = selected.get("arrival")

    # Live fares (no prices, but seat counts + class names) bubble up
    fare_summary = None
    if es_live and selected.get("fares"):
        avail = [f for f in selected["fares"] if f.get("available")]
        fare_summary = ", ".join(
            f"{f['class_name']} ({f['seats']})" for f in avail if f.get("class_name")
        ) or "fully booked"

    # Final drive at destination (Eurostar arrival station → user's lat/lon)
    arrive_dest_station = depart_dt + timedelta(minutes=drive_min_uk + PREDEPARTURE_BUFFER_MIN + train_min)
    drive_dest = await _drive_or_fallback(
        ctx["client"],
        f"{es.get('to','Paris')}, France",
        f"{dest['lat']},{dest['lon']}",
        _to_iso_z(arrive_dest_station),
        fallback_min=30,
    )
    drive_min_dest = int(round(drive_dest["duration_minutes"]))

    door_to_door = drive_min_uk + PREDEPARTURE_BUFFER_MIN + train_min + drive_min_dest

    train_note = (
        f"Booked train: {es_depart} → {es_arrive} ({train_min} min)"
        if es_live and es_depart else
        f"{es.get('frequency','?')}{(' — '+es['seasonal']) if es.get('seasonal') else ''}"
    )
    if fare_summary:
        train_note += f". Seats: {fare_summary}"

    legs = [
        {"kind": "drive", "from": origin_label, "to": "St Pancras International",
         "minutes": drive_min_uk, "operator": "self-drive",
         "note": f"{drive_to_stp.get('distance_km','?')} km"
                 + (" (live traffic)" if not drive_to_stp.get("fallback") else " (static estimate)")},
        {"kind": "wait", "from": "St Pancras", "to": "St Pancras",
         "minutes": PREDEPARTURE_BUFFER_MIN, "note": "60-min pre-departure (security + customs)"},
        {"kind": "train", "from": "London St Pancras", "to": es.get("to"),
         "minutes": train_min, "operator": "Eurostar",
         "depart": es_depart, "arrive": es_arrive,
         "booking_url": es.get("booking_url"),
         "fares": selected.get("fares") if es_live else None,
         "note": train_note},
        {"kind": "drive", "from": es.get("to"), "to": dest["display_name"],
         "minutes": drive_min_dest, "operator": "taxi/hire",
         "note": f"{drive_dest.get('distance_km','?')} km"
                 + (" (live traffic)" if not drive_dest.get("fallback") else " (static estimate)")},
    ]
    return {
        "ok": True,
        "mode": "eurostar",
        "door_to_door_minutes": door_to_door,
        "transfers": 1,
        "legs": legs,
        "data_sources": [
            "eurostar-live" if es_live else "static-timetable",
            "google-maps" if not drive_to_stp.get("fallback") else "static-fallback",
            "google-maps" if not drive_dest.get("fallback") else "static-fallback",
        ],
        "booking_urls": [es.get("booking_url")],
        "trade_offs": [
            f"Direct Eurostar — no airport hassle.",
            f"Taxi/hire at destination ({drive_min_dest} min) is the friction.",
            *([f"Booked train: {es_depart} → {es_arrive}"] if es_live and es_depart else []),
            *([f"Available classes: {fare_summary}"] if fare_summary else []),
            *(["Seasonal: " + es["seasonal"]] if es.get("seasonal") else []),
        ],
        "summary": f"Eurostar: {door_to_door} min door-to-door "
                   f"({drive_min_uk}+{PREDEPARTURE_BUFFER_MIN}+{train_min}+{drive_min_dest})",
    }


async def build_flight(
    ctx, region: str, dest: dict, depart_dt: datetime, alps_geneva: bool = False,
    origin: str = ORIGIN_HOME_DEFAULT, origin_label: str = ORIGIN_HOME_LABEL_DEFAULT,
    dest_iata_override: str | None = None,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
    origin_region: str = "uk",
) -> dict[str, Any]:
    airports = REGION_AIRPORTS.get(region) or {"origin": ["LGW"], "destination": ["CDG"]}
    if origin_region != "uk":
        # build_flight currently assumes UK origin airports (LGW/LHR). Picking
        # one for a non-UK origin would route the user via London — wrong
        # answer. Stage 5b will geocode the origin and pick a nearby airport;
        # until then surface a clean error pointing at the per-mode tools.
        return {
            "ok": False,
            "mode": "flight",
            "error": (
                f"plan_trip's flight composer assumes a UK origin. For non-UK "
                f"origins ({origin_region!r}), use travel_flight_check + the "
                f"appropriate travel_rail_<iso>_journey directly until Stage 5b "
                f"cross-EU multi-leg composition lands."
            ),
        }
    origin_iata = airports["origin"][0]
    if dest_iata_override:
        dest_iata = dest_iata_override
    elif alps_geneva:
        dest_iata = "GVA"
    else:
        dest_iata = airports["destination"][0]

    # Drive home → origin airport
    drive_to_apt = await _drive_or_fallback(
        ctx["client"], origin,
        f"{origin_iata} airport",
        _to_iso_z(depart_dt), fallback_min=60,
    )
    drive_min_uk = int(round(drive_to_apt["duration_minutes"]))

    # Flight via Duffel
    try:
        offers = await search_offers(
            ctx["client"], origin_iata, dest_iata,
            depart_dt.strftime("%Y-%m-%d"), adults=2, cabin="economy",
            prefer_carriers=prefer_carriers,
            exclude_carriers=exclude_carriers,
        )
        cheapest = offers["offers"][0] if offers.get("offers") else None
        if not cheapest:
            return {"ok": False, "mode": "flight", "error": f"no Duffel offers {origin_iata}→{dest_iata}"}
        slc = cheapest["slices"][0]
        # Use block time (in-air, sum of segment durations) for door-to-door
        # ranking — more honest than elapsed which double-counts long layovers.
        flight_min = slc.get("block_minutes") or _parse_iso_duration_min(slc.get("duration") or "")
        flight_carrier = cheapest["owner"]
        flight_price = cheapest["total_amount"]
        flight_currency = cheapest["total_currency"]
    except Exception as e:
        return {"ok": False, "mode": "flight" + ("/geneva-drive" if alps_geneva else ""),
                "error": f"flight lookup failed: {type(e).__name__}: {str(e)[:200]}"}

    # Drive at destination — for car-free villages (Zermatt) substitute
    # the parking-station coords and append the final transit hop.
    drive_target_lat, drive_target_lon, carfree = _resolve_drive_target(dest)
    final_legs = _final_leg_from(carfree)
    final_minutes = sum(l["minutes"] for l in final_legs)

    arrive_apt = depart_dt + timedelta(minutes=drive_min_uk + AIRPORT_OVERHEAD_MIN + flight_min + 30)
    drive_dest = await _drive_or_fallback(
        ctx["client"],
        f"{dest_iata} airport",
        f"{drive_target_lat},{drive_target_lon}",
        _to_iso_z(arrive_apt),
        # GVA → Alpine ski resort is typically 90-210 min depending on
        # destination; use a generous floor for the static fallback.
        fallback_min=180 if alps_geneva else 30,
    )
    drive_min_dest = int(round(drive_dest["duration_minutes"]))

    door_to_door = drive_min_uk + AIRPORT_OVERHEAD_MIN + flight_min + 30 + drive_min_dest + final_minutes
    mode = "fly_geneva_drive" if alps_geneva else "flight"
    legs = [
        {"kind": "drive", "from": origin_label, "to": f"{origin_iata} airport",
         "minutes": drive_min_uk, "operator": "self-drive",
         "note": f"{drive_to_apt.get('distance_km','?')} km"
                 + (" (live traffic)" if not drive_to_apt.get("fallback") else " (static estimate)")},
        {"kind": "wait", "from": f"{origin_iata} airport", "to": f"{origin_iata} airport",
         "minutes": AIRPORT_OVERHEAD_MIN, "note": "Check-in + security + walk to gate"},
        {"kind": "flight", "from": origin_iata, "to": dest_iata,
         "minutes": flight_min, "operator": flight_carrier,
         "booking_url": offers.get("booking_deeplink"),
         "note": f"£{flight_price} {flight_currency} (cheapest of {len(offers['offers'])} offers)"},
        {"kind": "wait", "from": f"{dest_iata} airport", "to": f"{dest_iata} airport",
         "minutes": 30, "note": "Disembark + baggage + walk to car"},
        {"kind": "drive",
         "from": f"{dest_iata} airport",
         "to": (carfree["parking_name"] if carfree else dest["display_name"]),
         "minutes": drive_min_dest, "operator": "hire car / taxi",
         "note": f"{drive_dest.get('distance_km','?')} km"
                 + (" (live traffic)" if not drive_dest.get("fallback") else " (static estimate)")},
        *final_legs,
    ]
    return {
        "ok": True,
        "mode": mode,
        "door_to_door_minutes": door_to_door,
        "transfers": 2,
        "legs": legs,
        "data_sources": [
            "duffel-test" if not offers.get("live") else "duffel-live",
            "google-maps" if not drive_to_apt.get("fallback") else "static-fallback",
            "google-maps" if not drive_dest.get("fallback") else "static-fallback",
        ],
        "booking_urls": [offers.get("booking_deeplink")],
        "trade_offs": [
            f"Cheapest flight {flight_carrier} £{flight_price}.",
            f"Airport overhead: {AIRPORT_OVERHEAD_MIN}+30 = {AIRPORT_OVERHEAD_MIN+30} min terminal time.",
            *(["Hire car at destination."] if alps_geneva or drive_min_dest > 30 else []),
        ],
        "summary": f"Fly {origin_iata}→{dest_iata}: {door_to_door} min door-to-door, £{flight_price}.",
        "total_cost_gbp": flight_price,
    }


def _parse_iso_duration_min(s: str) -> int:
    """Parse ISO 8601 duration to minutes. Handles PT3H20M, P1DT11H35M, P2D etc."""
    if not s or not s.startswith("P"):
        return 0
    rest = s[1:]
    if "T" in rest:
        date_part, time_part = rest.split("T", 1)
    else:
        date_part, time_part = rest, ""
    days = 0
    cur = ""
    for ch in date_part:
        if ch.isdigit():
            cur += ch
        elif ch == "D":
            days = int(cur or "0")
            cur = ""
    total = days * 24 * 60
    cur = ""
    for ch in time_part:
        if ch.isdigit():
            cur += ch
        elif ch == "H":
            total += int(cur or "0") * 60
            cur = ""
        elif ch == "M":
            total += int(cur or "0")
            cur = ""
    return total


async def build_irish_sea_ferry(
    ctx, dest: dict, depart_dt: datetime,
    origin: str = ORIGIN_HOME_DEFAULT, origin_label: str = ORIGIN_HOME_LABEL_DEFAULT,
) -> dict[str, Any]:
    """Drive to a UK port → Irish Sea ferry → drive to destination in IE/NI.

    Picks the optimal UK port for the (origin, dest) pair by scoring
    drive-home-to-port + crossing-time + drive-port-to-dest. Live prices
    where available (Stena Line GraphQL); other operators static.

    Common picks from Surrey origin:
      - Holyhead → Dublin (Stena live, £64-220 + 195min crossing)
      - Liverpool → Dublin (P&O overnight cabin, longer)
      - Pembroke → Rosslare (Irish Ferries, useful for SW Ireland)
      - Cairnryan → Belfast (Stena live, only sensible for Scotland origins)
    """
    # UK port candidates. Each row carries a static "Irish-side port → typical
    # major city drive-time" used for ranking — Dublin Port is 5 min to Dublin
    # centre but 150 min from Rosslare; we don't want to pick Rosslare for a
    # Dublin-bound trip just because the crossing's a few minutes shorter.
    UK_PORTS = [
        # uk_geo, uk_port_name, ie_geo, ie_port_name, crossing_min, operator,
        # dest_pref, static_drive_dub_min, static_drive_bel_min
        ("Holyhead, UK",     "Holyhead",     "Dublin Port, IE",   "Dublin",   195, "Stena Line",     "IE",   5, 120),
        ("Holyhead, UK",     "Holyhead",     "Dublin Port, IE",   "Dublin",   210, "Irish Ferries",  "IE",   5, 120),
        ("Liverpool, UK",    "Liverpool",    "Dublin Port, IE",   "Dublin",   480, "P&O Ferries",    "IE",   5, 120),
        ("Pembroke Dock, UK","Pembroke Dock","Rosslare, IE",      "Rosslare", 240, "Irish Ferries",  "IE", 150, 270),
        ("Fishguard, UK",    "Fishguard",    "Rosslare, IE",      "Rosslare", 210, "Stena Line",     "IE", 150, 270),
        ("Cairnryan, UK",    "Cairnryan",    "Belfast, GB",       "Belfast",  135, "Stena Line",     "NI", 120,  10),
        ("Cairnryan, UK",    "Cairnryan",    "Larne, GB",         "Larne",    120, "P&O Ferries",    "NI", 120,  30),
        ("Liverpool, UK",    "Liverpool",    "Belfast, GB",       "Belfast",  480, "Stena Line",     "NI", 120,  10),
    ]

    # Determine if destination is in ROI or NI based on coords. Used both
    # for the final-drive estimate and to prefer same-side ports.
    is_ni_dest = dest["lat"] >= 54.0 and dest["lon"] >= -8.0  # rough NI box

    # Score each candidate = drive home → UK port + crossing + static
    # estimate of port→destination-country-centre drive (the lattermost
    # is what makes Holyhead-Dublin beat Fishguard-Rosslare for a Dublin
    # destination — the actual Port→Lucan drive is computed for the
    # winner only).
    async def _score(uk_geo, uk_name, ie_geo, ie_name, crossing_min, op,
                     dest_pref, static_dub, static_bel):
        d = await _drive_or_fallback(
            ctx["client"], origin, uk_geo,
            _to_iso_z(depart_dt), fallback_min=300,
        )
        static_final = static_bel if is_ni_dest else static_dub
        return {
            "uk_port_geo": uk_geo, "uk_port": uk_name,
            "ie_port_geo": ie_geo, "ie_port": ie_name,
            "crossing_min": crossing_min, "operator": op,
            "dest_pref": dest_pref,
            "drive_home_min": int(round(d["duration_minutes"])),
            "drive_home_km": d.get("distance_km"),
            "drive_home_fallback": d.get("fallback", False),
            "static_final_drive_min": static_final,
            "estimated_total_min": int(round(d["duration_minutes"])) + crossing_min + static_final,
        }
    scored = await asyncio.gather(*[_score(*p) for p in UK_PORTS])

    # Sort by full-trip estimate (drive home + crossing + port→dest-centre).
    # Ports already routing to the right country naturally win since their
    # static_final is small.
    scored.sort(key=lambda x: x["estimated_total_min"])
    pick = scored[0]

    # Live prices: Stena GraphQL or P&O Expian REST. Falls through to
    # static if neither operator handles this route.
    live: dict[str, Any] | None = None
    op_lower = pick["operator"].lower()
    if "stena" in op_lower:
        try:
            from mcp_travel.travel_stena_line import (
                get_sailings as stena_sailings, is_known_route as stena_known,
            )
            if stena_known(pick["uk_port"], pick["ie_port"]):
                sailings = await stena_sailings(
                    ctx["client"], date=depart_dt.strftime("%Y-%m-%d"),
                    origin=pick["uk_port"], destination=pick["ie_port"],
                    adults=2, vehicle="car", currency="GBP",
                )
                avail = [s["best_price"] for s in sailings if s.get("best_price") is not None]
                live = {
                    "sailings": sailings,
                    "best_price": min(avail) if avail else None,
                    "currency": sailings[0]["currency"] if sailings else "GBP",
                    "source": "stena-line-live",
                }
        except (Exception,):
            live = None
    elif "p&o" in op_lower or "p and o" in op_lower:
        try:
            from mcp_travel.travel_po_ferries import (
                get_sailings as po_sailings, is_known_route as po_known,
            )
            # P&O does Larne-Cairnryan (NOT Cairnryan-Belfast); Liverpool-Dublin (NOT in our P&O routes)
            if po_known(pick["uk_port"], pick["ie_port"]):
                sailings = await po_sailings(
                    ctx["client"], date=depart_dt.strftime("%Y-%m-%d"),
                    origin=pick["uk_port"], destination=pick["ie_port"],
                    adults=2, vehicle="car",
                )
                avail = [s["best_price"] for s in sailings if s.get("best_price") is not None]
                live = {
                    "sailings": sailings,
                    "best_price": min(avail) if avail else None,
                    "currency": sailings[0]["currency"] if sailings else "GBP",
                    "source": "po-ferries-live",
                }
        except (Exception,):
            live = None

    # Drive at destination (IE port → final destination)
    drive_target_lat, drive_target_lon, carfree = _resolve_drive_target(dest)
    final_legs = _final_leg_from(carfree)
    final_minutes = sum(l["minutes"] for l in final_legs)

    arrive_ie = depart_dt + timedelta(
        minutes=pick["drive_home_min"] + 60 + pick["crossing_min"] + 30,
    )
    drive_dest = await _drive_or_fallback(
        ctx["client"], pick["ie_port_geo"], f"{drive_target_lat},{drive_target_lon}",
        _to_iso_z(arrive_ie), fallback_min=120,
    )
    drive_min_dest = int(round(drive_dest["duration_minutes"]))

    door_to_door = (
        pick["drive_home_min"] + 60 + pick["crossing_min"] + 30 + drive_min_dest + final_minutes
    )

    ferry_note = f"{pick['crossing_min']} min crossing"
    if live and live.get("best_price"):
        ferry_note += f", from {live['currency']} {live['best_price']}"

    legs = [
        {
            "kind": "drive", "from": origin_label, "to": pick["uk_port"],
            "minutes": pick["drive_home_min"], "operator": "self-drive",
            "note": f"{pick['drive_home_km']} km"
                    + (" (live traffic)" if not pick["drive_home_fallback"] else " (static estimate)"),
        },
        {
            "kind": "wait", "from": pick["uk_port"], "to": pick["uk_port"],
            "minutes": 60, "note": "60-min pre-departure (vehicle check-in)",
        },
        {
            "kind": "ferry", "from": pick["uk_port"], "to": pick["ie_port"],
            "minutes": pick["crossing_min"], "operator": pick["operator"],
            "price": (live or {}).get("best_price"),
            "price_currency": (live or {}).get("currency"),
            "note": ferry_note,
        },
        {
            "kind": "wait", "from": pick["ie_port"], "to": pick["ie_port"],
            "minutes": 30, "note": "Vehicle disembarkation + customs",
        },
        {
            "kind": "drive", "from": pick["ie_port"],
            "to": (carfree["parking_name"] if carfree else dest["display_name"]),
            "minutes": drive_min_dest, "operator": "self-drive",
            "note": f"{drive_dest.get('distance_km','?')} km"
                    + (" (live traffic)" if not drive_dest.get("fallback") else " (static estimate)"),
        },
        *final_legs,
    ]
    return {
        "ok": True,
        "mode": "irish_sea_ferry",
        "door_to_door_minutes": door_to_door,
        "transfers": 1,
        "legs": legs,
        "total_cost_gbp": (
            live["best_price"]
            if live and live.get("currency") == "GBP" and live.get("best_price") is not None
            else None
        ),
        "data_sources": [
            (live or {}).get("source", "static-table") if live else "static-table",
            "google-maps" if not pick["drive_home_fallback"] else "static-fallback",
            "google-maps" if not drive_dest.get("fallback") else "static-fallback",
        ],
        "booking_urls": [],
        "trade_offs": [
            f"Take your own car to Ireland — {pick['operator']} {pick['uk_port']}→{pick['ie_port']}.",
            f"Drive home: {pick['drive_home_min']} min. Crossing: {pick['crossing_min']} min.",
            *([
                f"Faster than via {alt['uk_port']} ({alt['estimated_total_min']} vs "
                f"{pick['estimated_total_min']} min including final drive across Ireland)"
                for alt in scored[1:2]
            ]),
        ],
        "summary": (
            f"{pick['uk_port']} → {pick['ie_port']} ({pick['operator']}): "
            f"{door_to_door} min door-to-door"
        ),
    }


# Swiss airports + their on-site SBB stations.
# (lat, lon, IATA, SBB station name).
_SWISS_AIRPORTS = [
    (46.238, 6.108, "GVA", "Genève-Aéroport"),
    (47.464, 8.549, "ZRH", "Zürich Flughafen"),
    (47.590, 7.529, "BSL", "Basel SBB"),  # 15-min shuttle from EuroAirport
]


def _pick_swiss_airport(dest: dict) -> tuple[str, str, float]:
    """Closest Swiss airport to dest by haversine. Returns (IATA, SBB station name, dist_km)."""
    import math
    lat, lon = dest["lat"], dest["lon"]
    best = None
    for alat, alon, iata, station in _SWISS_AIRPORTS:
        # Cheap haversine — good enough for ranking
        dlat = math.radians(lat - alat)
        dlon = math.radians(lon - alon)
        a = (math.sin(dlat/2)**2 +
             math.cos(math.radians(alat)) * math.cos(math.radians(lat)) *
             math.sin(dlon/2)**2)
        d = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        if best is None or d < best[2]:
            best = (iata, station, d)
    return best


async def _sbb_first_connection(
    client, from_station: str, to_query: str, dt: datetime, max_journeys: int = 3,
) -> dict | None:
    """Query transport.opendata.ch for the first sensible connection.
    Returns a normalised dict {duration_minutes, transfers, products, sections}."""
    try:
        resp = await client.get(
            "http://transport.opendata.ch/v1/connections",
            params={
                "from": from_station, "to": to_query,
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M"),
                "limit": max_journeys,
            },
            timeout=20.0,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        conns = data.get("connections") or []
        if not conns:
            return None
        first = conns[0]
        # SBB returns duration as 'D:HH:MM:SS' — convert to minutes.
        dur = first.get("duration") or "0:00:00:00"
        parts = dur.split(":")
        if len(parts) == 4:
            days, hours, mins = int(parts[0]), int(parts[1]), int(parts[2])
            total_min = days * 1440 + hours * 60 + mins
        else:
            total_min = None
        return {
            "duration_minutes": total_min,
            "transfers": first.get("transfers", 0),
            "products": first.get("products") or [],
            "departure": (first.get("from") or {}).get("departure", ""),
            "arrival": (first.get("to") or {}).get("arrival", ""),
            "from": (first.get("from") or {}).get("station", {}).get("name"),
            "to": (first.get("to") or {}).get("station", {}).get("name"),
            "sections": [
                {
                    "category": (s.get("journey") or {}).get("category", "walk" if s.get("walk") else "?"),
                    "from": (s.get("departure") or {}).get("station", {}).get("name", ""),
                    "to": (s.get("arrival") or {}).get("station", {}).get("name", ""),
                }
                for s in first.get("sections") or []
            ],
        }
    except (httpx.HTTPError, Exception):
        return None


async def build_fly_train(
    ctx, region: str, dest: dict, depart_dt: datetime,
    origin: str = ORIGIN_HOME_DEFAULT, origin_label: str = ORIGIN_HOME_LABEL_DEFAULT,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
) -> dict[str, Any]:
    """Drive to UK airport → fly to nearest Swiss airport → SBB+PostBus
    to destination. The Swiss-end leg is composed via SBB's full
    integrated planner (transport.opendata.ch), so it routes ANY Swiss
    public-transport-served destination — including PostBus to mountain
    villages (Grimentz, Verbier, etc.), cable cars, and the famous
    Wengernalpbahn-style mountain trains.

    Beats fly_geneva_drive on time-cost for car-free destinations
    (Zermatt, Wengen, Mürren) and on total cost everywhere given Swiss
    hire car prices (£100-150/day) + restrictive parking.
    """
    apt_iata, apt_station, apt_dist_km = _pick_swiss_airport(dest)

    # Drive home → origin UK airport (LGW)
    origin_airports = ["LGW", "LHR"]
    origin_iata = origin_airports[0]
    drive_to_apt = await _drive_or_fallback(
        ctx["client"], origin, f"{origin_iata} airport",
        _to_iso_z(depart_dt), fallback_min=50,
    )
    drive_min_uk = int(round(drive_to_apt["duration_minutes"]))

    # Flight LGW → Swiss airport
    flight_depart = depart_dt + timedelta(minutes=drive_min_uk + AIRPORT_OVERHEAD_MIN)
    try:
        offers = await search_offers(
            ctx["client"], origin_iata, apt_iata,
            flight_depart.strftime("%Y-%m-%d"),
            adults=2,
            prefer_carriers=prefer_carriers,
            exclude_carriers=exclude_carriers,
        )
    except DuffelError as e:
        return {"ok": False, "mode": "fly_train",
                "error": f"flight lookup failed: {type(e).__name__}: {str(e)[:200]}"}

    if not offers.get("offers"):
        return {"ok": False, "mode": "fly_train",
                "error": f"no Duffel offers {origin_iata}→{apt_iata}"}

    cheapest = offers["offers"][0]
    sl = (cheapest.get("slices") or [{}])[0]
    flight_min = sl.get("elapsed_minutes") or 120
    flight_price = cheapest.get("total_amount")
    flight_currency = cheapest.get("total_currency", "GBP")
    flight_carrier = cheapest.get("owner") or "?"
    flight_depart_iso = sl.get("depart")
    flight_arrive_iso = sl.get("arrive")

    # Compute when we'll be at the airport SBB station
    # (30 min disembark + walk to SBB platform; +15 for BSL shuttle)
    apt_to_sbb_min = 45 if apt_iata == "BSL" else 30
    sbb_ready = depart_dt + timedelta(
        minutes=drive_min_uk + AIRPORT_OVERHEAD_MIN + flight_min + apt_to_sbb_min,
    )

    # SBB+PostBus from airport to destination — use city/village name
    # if we have one, else lat,lon. Coords are accepted by the API but
    # named stations route more cleanly through the timetable.
    dest_query = dest.get("display_name", "").split(",")[0].strip() or f"{dest['lat']},{dest['lon']}"
    transit = await _sbb_first_connection(ctx["client"], apt_station, dest_query, sbb_ready)

    if not transit:
        return {"ok": False, "mode": "fly_train",
                "error": f"SBB returned no connection {apt_station} → {dest_query}"}

    transit_min = transit["duration_minutes"] or 180

    door_to_door = drive_min_uk + AIRPORT_OVERHEAD_MIN + flight_min + apt_to_sbb_min + transit_min

    legs = [
        {"kind": "drive", "from": origin_label, "to": f"{origin_iata} airport",
         "minutes": drive_min_uk, "operator": "self-drive",
         "note": f"{drive_to_apt.get('distance_km','?')} km"
                 + (" (live traffic)" if not drive_to_apt.get("fallback") else " (static estimate)")},
        {"kind": "wait", "from": f"{origin_iata} airport", "to": f"{origin_iata} airport",
         "minutes": AIRPORT_OVERHEAD_MIN,
         "note": "Check-in + security + walk to gate"},
        {"kind": "flight", "from": origin_iata, "to": apt_iata,
         "minutes": flight_min, "operator": flight_carrier,
         "depart": flight_depart_iso, "arrive": flight_arrive_iso,
         "booking_url": offers.get("booking_deeplink"),
         "note": f"{flight_currency} {flight_price} (cheapest of {len(offers['offers'])} offers)"},
        {"kind": "wait", "from": f"{apt_iata} airport", "to": f"{apt_station}",
         "minutes": apt_to_sbb_min,
         "note": "Disembark + baggage + walk to airport SBB station"
                 + (" (15-min shuttle to Basel SBB)" if apt_iata == "BSL" else "")},
        {"kind": "train", "from": apt_station, "to": transit.get("to") or dest_query,
         "minutes": transit_min, "operator": "SBB+PostBus",
         "depart": transit.get("departure"),
         "arrive": transit.get("arrival"),
         "transfers": transit.get("transfers"),
         "products": transit.get("products"),
         "note": (
             f"{transit['transfers']} transfers via {','.join(transit.get('products') or [])}"
         )},
    ]

    return {
        "ok": True,
        "mode": "fly_train",
        "door_to_door_minutes": door_to_door,
        "transfers": (transit.get("transfers") or 0) + 1,   # flight counts as 1 transfer
        "legs": legs,
        "total_cost_gbp": (
            flight_price if flight_currency == "GBP" and flight_price is not None else None
        ),
        "data_sources": [
            "duffel-live" if offers.get("live") else "duffel-test",
            "sbb-live",
            "google-maps" if not drive_to_apt.get("fallback") else "static-fallback",
        ],
        "booking_urls": [offers.get("booking_deeplink")],
        "trade_offs": [
            f"Fly LGW→{apt_iata}, then SBB+PostBus into Switzerland.",
            f"No hire car needed — Swiss public transport reaches every "
            f"village including {transit.get('to') or dest_query} via "
            f"{','.join(transit.get('products') or [])}.",
            f"Cheaper than hire-car alternative (~£100-150/day saved).",
            *([f"Climate-friendly + scenic — {','.join(transit.get('products') or [])} through the Alps."]
              if any('B' in p or 'IR' in p for p in transit.get('products') or []) else []),
        ],
        "summary": (
            f"Fly LGW→{apt_iata} ({flight_min} min) + SBB {apt_station}→"
            f"{transit.get('to') or dest_query} ({transit_min} min): "
            f"{door_to_door} min door-to-door, {flight_currency} {flight_price}."
        ),
    }


async def build_north_sea_ferry(
    ctx, dest: dict, depart_dt: datetime,
    origin: str = ORIGIN_HOME_DEFAULT, origin_label: str = ORIGIN_HOME_LABEL_DEFAULT,
) -> dict[str, Any]:
    """Drive to a UK east-coast port → overnight North Sea ferry → drive to destination.

    Three viable car-passenger crossings to NL/BE for onward Continental
    drive. We let drive_time pick the best UK port for the origin
    (Hull/Newcastle/Harwich), and pick the right Continental port based
    on the destination country (NL → Rotterdam/Hook, DE/CH/AT/CZ →
    Zeebrugge or Rotterdam).

    For Northern UK origins (Yorkshire, NE, Scotland) this often
    dominates Folkestone+Eurotunnel on door-to-door time.
    """
    # UK port candidates with their crossings
    UK_PORTS = [
        ("Hull, UK",       "Hull",      "Rotterdam Europoort, NL", "Rotterdam",  660, "P&O Ferries", "NL"),
        ("Hull, UK",       "Hull",      "Zeebrugge, BE",            "Zeebrugge",  780, "P&O Ferries", "BE"),
        ("Newcastle, UK",  "Newcastle", "IJmuiden, NL",             "IJmuiden",   900, "DFDS",        "NL"),
        ("Harwich, UK",    "Harwich",   "Hook of Holland, NL",      "Hook of Holland", 420, "Stena Line", "NL"),
    ]
    dest_country = (dest.get("country_code") or "").lower()
    # Score each crossing by drive-home-to-port + crossing — pick the fastest
    async def _score(port_geo, uk_name, dest_geo, dest_name, crossing_min, op, dest_cc):
        d = await _drive_or_fallback(
            ctx["client"], origin, port_geo,
            _to_iso_z(depart_dt), fallback_min=180,
        )
        return {
            "uk_port_geo": port_geo, "uk_port": uk_name,
            "dest_port_geo": dest_geo, "dest_port": dest_name,
            "crossing_min": crossing_min, "operator": op,
            "dest_cc": dest_cc,
            "drive_home_min": int(round(d["duration_minutes"])),
            "drive_home_km": d.get("distance_km"),
            "drive_home_fallback": d.get("fallback", False),
        }
    scored = await asyncio.gather(*[_score(*p) for p in UK_PORTS])
    # Filter by destination country compatibility (NL or BE land port)
    # — NL/BE → either; further-east countries (DE/CH/AT/CZ) → either also,
    # then drive on. So all four are candidates regardless of dest.
    scored.sort(key=lambda x: x["drive_home_min"] + x["crossing_min"])
    pick = scored[0]

    # Drive at destination — Continental port → final destination
    arrive_continent = depart_dt + timedelta(
        minutes=pick["drive_home_min"] + 60 + pick["crossing_min"] + 30,
    )
    # Substitute parking-station coords for car-free destinations.
    drive_target_lat, drive_target_lon, carfree = _resolve_drive_target(dest)
    final_legs = _final_leg_from(carfree)
    final_minutes = sum(l["minutes"] for l in final_legs)

    drive_dest = await _drive_or_fallback(
        ctx["client"], pick["dest_port_geo"], f"{drive_target_lat},{drive_target_lon}",
        _to_iso_z(arrive_continent), fallback_min=240,
    )
    drive_min_dest = int(round(drive_dest["duration_minutes"]))

    # Fetch live prices for the picked crossing per operator.
    live: dict[str, Any] | None = None
    op_lower = pick["operator"].lower()

    if "dfds" in op_lower:
        try:
            from mcp_travel.travel_dfds import (
                get_sailings as dfds_sailings, is_known_route as dfds_known, DFDSError,
            )
            o = pick["uk_port"].lower()
            d = pick["dest_port"].lower().split(" ")[0]
            o_key, d_key = ("newcastle", "amsterdam") if "newcastle" in o else (o, d)
            if dfds_known(o_key, d_key):
                sailings = await dfds_sailings(
                    ctx["client"], date=depart_dt.strftime("%Y-%m-%d"),
                    origin=o_key, destination=d_key, adults=2, vehicle="car",
                )
                avail = [s["best_price"] for s in sailings if s.get("best_price") is not None]
                live = {
                    "sailings": sailings,
                    "best_price": min(avail) if avail else None,
                    "currency": sailings[0]["currency"] if sailings else None,
                }
        except (DFDSError, Exception):
            live = None

    elif "stena" in op_lower:
        try:
            from mcp_travel.travel_stena_line import (
                get_sailings as stena_sailings, is_known_route as stena_known, StenaLineError,
            )
            if stena_known(pick["uk_port"], pick["dest_port"]):
                sailings = await stena_sailings(
                    ctx["client"], date=depart_dt.strftime("%Y-%m-%d"),
                    origin=pick["uk_port"], destination=pick["dest_port"],
                    adults=2, vehicle="car", currency="GBP",
                )
                avail = [s["best_price"] for s in sailings if s.get("best_price") is not None]
                live = {
                    "sailings": sailings,
                    "best_price": min(avail) if avail else None,
                    "currency": sailings[0]["currency"] if sailings else "GBP",
                }
        except (StenaLineError, Exception):
            live = None

    elif "p&o" in op_lower or "p and o" in op_lower:
        try:
            from mcp_travel.travel_po_ferries import (
                get_sailings as po_sailings, is_known_route as po_known,
            )
            # P&O Hull-Rotterdam — only operator on this route
            if po_known(pick["uk_port"], pick["dest_port"]):
                sailings = await po_sailings(
                    ctx["client"], date=depart_dt.strftime("%Y-%m-%d"),
                    origin=pick["uk_port"], destination=pick["dest_port"],
                    adults=2, vehicle="car",
                )
                avail = [s["best_price"] for s in sailings if s.get("best_price") is not None]
                live = {
                    "sailings": sailings,
                    "best_price": min(avail) if avail else None,
                    "currency": sailings[0]["currency"] if sailings else "GBP",
                }
        except (Exception,):
            live = None

    door_to_door = (
        pick["drive_home_min"] + 60 + pick["crossing_min"] + 30 + drive_min_dest + final_minutes
    )

    ferry_note = f"Overnight crossing (~{pick['crossing_min']//60}h{pick['crossing_min']%60:02d}m)"
    if live and live.get("best_price"):
        ferry_note += f", from {live['currency']} {live['best_price']}"

    legs = [
        {
            "kind": "drive", "from": origin_label, "to": pick["uk_port"],
            "minutes": pick["drive_home_min"], "operator": "self-drive",
            "note": f"{pick['drive_home_km']} km"
                    + (" (live traffic)" if not pick["drive_home_fallback"] else " (static estimate)"),
        },
        {
            "kind": "wait", "from": pick["uk_port"], "to": pick["uk_port"],
            "minutes": 60, "note": "60-min pre-departure (vehicle check-in, customs)",
        },
        {
            "kind": "ferry", "from": pick["uk_port"], "to": pick["dest_port"],
            "minutes": pick["crossing_min"], "operator": pick["operator"],
            "price": (live or {}).get("best_price"),
            "price_currency": (live or {}).get("currency"),
            "note": ferry_note,
        },
        {
            "kind": "wait", "from": pick["dest_port"], "to": pick["dest_port"],
            "minutes": 30, "note": "Vehicle disembarkation + customs",
        },
        {
            "kind": "drive", "from": pick["dest_port"],
            "to": (carfree["parking_name"] if carfree else dest["display_name"]),
            "minutes": drive_min_dest, "operator": "self-drive",
            "note": f"{drive_dest.get('distance_km','?')} km Continental drive"
                    + (" (live traffic)" if not drive_dest.get("fallback") else " (static estimate)"),
        },
        *final_legs,
    ]
    return {
        "ok": True,
        "mode": "north_sea_ferry",
        "door_to_door_minutes": door_to_door,
        "transfers": 1,
        "legs": legs,
        "total_cost_gbp": (
            live["best_price"]
            if live and live.get("currency") == "GBP"
               and live.get("best_price") is not None
            else None
        ),
        "data_sources": [
            (
                "dfds-live" if live and "dfds" in op_lower else
                "stena-line-live" if live and "stena" in op_lower else
                "po-ferries-live" if live and ("p&o" in op_lower or "p and o" in op_lower) else
                "static-table"
            ),
            "google-maps" if not pick["drive_home_fallback"] else "static-fallback",
            "google-maps" if not drive_dest.get("fallback") else "static-fallback",
        ],
        "booking_urls": [],   # operator-specific URLs would be added per pick
        "trade_offs": [
            f"Overnight North Sea crossing — sleeper cabin, your car arrives with you.",
            f"Best for Northern-UK origins — picked {pick['uk_port']} over Folkestone "
            f"({pick['drive_home_min']} min drive home → port).",
            f"Saves a full day's continental drive vs Folkestone+Eurotunnel for "
            f"NL/DE/CH/AT destinations.",
        ],
        "summary": (
            f"{pick['uk_port']} → {pick['dest_port']} ({pick['operator']}): "
            f"{door_to_door} min door-to-door "
            f"({pick['drive_home_min']}+60+{pick['crossing_min']}+30+{drive_min_dest})"
        ),
    }


async def build_multiday_drive(
    ctx, dest: dict, depart_dt: datetime, overnight_near: str,
    origin: str = ORIGIN_HOME_DEFAULT, origin_label: str = ORIGIN_HOME_LABEL_DEFAULT,
) -> dict[str, Any]:
    """Drive day 1 to an overnight stop, drive day 2 to the destination.

    Day 1 = home → Eurotunnel → overnight_near (uses the same chain as
    build_eurotunnel with overnight_near as the day's destination).
    Day 2 = overnight_near → destination (drive_time only).
    """
    overnight_geo = await forward_geocode(ctx["client"], ctx["pool"], pool_locations=ctx.get("pool_locations"), query=overnight_near)
    if not overnight_geo:
        return {
            "ok": False, "mode": "multiday-drive",
            "error": f"could not geocode overnight_near={overnight_near!r}",
        }

    fake_dest = {
        "lat": overnight_geo["lat"], "lon": overnight_geo["lon"],
        "display_name": overnight_geo["display_name"],
    }
    day1 = await build_eurotunnel(
        ctx, fake_dest, depart_dt, origin=origin, origin_label=origin_label,
    )
    if not day1.get("ok"):
        return {
            "ok": False, "mode": "multiday-drive",
            "error": f"day-1 leg failed: {day1.get('error','?')}",
        }

    # Day 2 — assume morning departure ~24h after day 1's start
    day2_depart = depart_dt + timedelta(hours=24)
    day2_drive = await _drive_or_fallback(
        ctx["client"],
        f"{overnight_geo['lat']},{overnight_geo['lon']}",
        f"{dest['lat']},{dest['lon']}",
        _to_iso_z(day2_depart), fallback_min=300,
    )
    day2_min = int(round(day2_drive["duration_minutes"]))

    legs = list(day1["legs"]) + [
        {
            "kind": "overnight",
            "from": overnight_geo["display_name"],
            "to": overnight_geo["display_name"],
            "minutes": 0,
            "note": f"Overnight stop in {overnight_near}",
        },
        {
            "kind": "drive",
            "from": overnight_geo["display_name"],
            "to": dest["display_name"],
            "minutes": day2_min,
            "operator": "self-drive",
            "note": f"{day2_drive.get('distance_km','?')} km, day 2"
                    + (" (live traffic)" if not day2_drive.get("fallback") else " (static estimate)"),
        },
    ]
    door_to_door = day1["door_to_door_minutes"] + day2_min
    return {
        "ok": True,
        "mode": "multiday-drive",
        "door_to_door_minutes": door_to_door,
        "transfers": 1,
        "legs": legs,
        "data_sources": list(day1.get("data_sources", [])) + [
            "google-maps" if not day2_drive.get("fallback") else "static-fallback",
        ],
        "booking_urls": list(day1.get("booking_urls", [])),
        "trade_offs": [
            f"Two-day drive — overnight at {overnight_geo['display_name']}.",
            f"Day 1 ~{day1['door_to_door_minutes']} min ({day1['door_to_door_minutes']//60}h{day1['door_to_door_minutes']%60:02d}m). "
            f"Day 2 ~{day2_min} min ({day2_min//60}h{day2_min%60:02d}m).",
        ],
        "summary": (
            f"Multi-day drive via {overnight_near}: "
            f"day1 {day1['door_to_door_minutes']} min + day2 {day2_min} min "
            f"= {door_to_door} min total"
        ),
        "overnight": {
            "near": overnight_near,
            "resolved": overnight_geo["display_name"],
            "lat": overnight_geo["lat"],
            "lon": overnight_geo["lon"],
        },
    }


async def plan_trip_impl(
    ctx, destination: str, depart_date: str,
    return_date: str | None = None, party: list[str] | None = None,
    depart_time: str = "08:00", max_options: int = 6,
    origin: str | None = None, origin_label: str | None = None,
    fly_only: bool = False, dest_airports: list[str] | None = None,
    overnight_near: str | None = None,
    prefer_affiliation: str | None = None,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
) -> dict[str, Any]:
    origin = origin or ORIGIN_HOME_DEFAULT
    origin_label = origin_label or origin
    # 1. Geocode destination
    geo = await forward_geocode(ctx["client"], ctx["pool"], pool_locations=ctx.get("pool_locations"), query=destination)
    if not geo:
        return {"ok": False, "error": f"could not geocode destination {destination!r}"}

    # 2. Classify destination region (mode-set lookup key)
    region = classify_region(geo["lat"], geo["lon"], query=destination)

    # 2b. Geocode origin and classify its region. We only need the
    # country code (or lat/lon fallback) — drives mode filtering below
    # so that a non-UK origin doesn't get UK-only modes proposed.
    origin_geo = await forward_geocode(
        ctx["client"], ctx["pool"], pool_locations=ctx.get("pool_locations"), query=origin,
    )
    origin_region = classify_origin_region(
        (origin_geo or {}).get("country_code"),
        (origin_geo or {}).get("lat"),
        (origin_geo or {}).get("lon"),
    )

    # 3. Resolve party defaults
    if party is None:
        async with ctx["pool"].acquire() as conn:
            rows = await conn.fetch("SELECT name FROM party_member WHERE is_default=true ORDER BY id")
            party = [r["name"] for r in rows]

    # 4. Pick mode set, then filter by origin region.
    modes_raw = REGION_MODES.get(region, ["eurotunnel", "flight"])
    modes = filter_modes_by_origin(modes_raw, origin_region)
    # If origin filter dropped every mode (e.g. Norwegian origin to a UK-only
    # mode set), fall back to flight — we can always fly. Stage 5b will
    # provide cross-EU multi-leg composition for the long-haul rail cases.
    if not modes:
        modes = ["flight"]
    if fly_only:
        modes = [m for m in modes if m in ("flight", "fly_geneva_drive")]
        if not modes:
            modes = ["flight"]
    if not modes:
        return {"ok": False, "error": f"region {region!r} has no candidate modes"}

    # 5. Build options concurrently
    depart_dt = datetime.fromisoformat(f"{depart_date}T{depart_time}:00").replace(tzinfo=timezone.utc)
    # Resolve flight destination airports — explicit override > region default
    region_aps = REGION_AIRPORTS.get(region) or {"origin": ["LGW"], "destination": ["CDG"]}
    flight_dests = dest_airports or region_aps.get("destination", ["CDG"])

    tasks = []
    for m in modes:
        if m == "eurotunnel":
            tasks.append(("eurotunnel",
                          build_eurotunnel(ctx, geo, depart_dt, origin=origin, origin_label=origin_label)))
        elif m == "eurostar":
            tasks.append(("eurostar",
                          build_eurostar(ctx, region, geo, depart_dt, origin=origin, origin_label=origin_label)))
        elif m == "flight":
            for ap in flight_dests:
                tasks.append((f"flight_{ap}",
                              build_flight(ctx, region, geo, depart_dt, alps_geneva=False,
                                           origin=origin, origin_label=origin_label,
                                           dest_iata_override=ap,
                                           prefer_carriers=prefer_carriers,
                                           exclude_carriers=exclude_carriers,
                                           origin_region=origin_region)))
        elif m == "fly_geneva_drive":
            tasks.append(("fly_geneva_drive",
                          build_flight(ctx, region, geo, depart_dt, alps_geneva=True,
                                       origin=origin, origin_label=origin_label,
                                       prefer_carriers=prefer_carriers,
                                       exclude_carriers=exclude_carriers,
                                       origin_region=origin_region)))
        elif m == "north_sea_ferry":
            tasks.append(("north_sea_ferry",
                          build_north_sea_ferry(ctx, geo, depart_dt,
                                                origin=origin, origin_label=origin_label)))
        elif m == "irish_sea_ferry":
            tasks.append(("irish_sea_ferry",
                          build_irish_sea_ferry(ctx, geo, depart_dt,
                                                origin=origin, origin_label=origin_label)))

    # Optional multi-day-drive option triggered by overnight_near
    if overnight_near and not fly_only:
        tasks.append(("multiday-drive",
                      build_multiday_drive(ctx, geo, depart_dt, overnight_near,
                                           origin=origin, origin_label=origin_label)))
        modes = list(modes) + ["multiday-drive"]   # reflect actually-considered set

    raw = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)
    options = []
    for (mode_name, _), result in zip(tasks, raw):
        if isinstance(result, BaseException):
            options.append({"ok": False, "mode": mode_name,
                            "error": f"{type(result).__name__}: {result}"})
        else:
            result["confidence"] = confidence(result)
            options.append(result)

    # 6. Rank
    options.sort(key=score)
    options = options[:max_options]

    # 6b. Optional accommodation suggestions via affiliation_search
    accommodation = None
    if prefer_affiliation:
        from mcp_travel.travel_affiliations import VALID_TAGS, filter_by as _affiliations_filter
        from mcp_travel.travel_drive import drive_time as _drive
        aff = prefer_affiliation.upper()
        if aff in VALID_TAGS:
            # If overnight specified, search near it; otherwise near destination
            anchor_query = overnight_near or destination
            anchor_geo = await forward_geocode(ctx["client"], ctx["pool"], pool_locations=ctx.get("pool_locations"), query=anchor_query)
            if anchor_geo:
                cands = _affiliations_filter(affiliation=aff)
                # rank by direct distance to anchor (fast — avoids extra API calls)
                from math import asin, cos, radians, sin, sqrt
                def _haversine_km(lat1, lon1, lat2, lon2):
                    R = 6371.0
                    dlat = radians(lat2 - lat1); dlon = radians(lon2 - lon1)
                    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
                    return 2 * R * asin(sqrt(a))
                near = sorted(
                    [{"hotel": h, "km": _haversine_km(anchor_geo["lat"], anchor_geo["lon"], h["lat"], h["lon"])}
                     for h in cands],
                    key=lambda x: x["km"],
                )[:5]
                accommodation = {
                    "affiliation": aff,
                    "anchor": anchor_query,
                    "anchor_resolved": anchor_geo["display_name"],
                    "candidates": [
                        {**c["hotel"], "distance_km_from_anchor": round(c["km"], 1)}
                        for c in near
                    ],
                    "note": "Use affiliation_search for drive-time-validated routing.",
                }

    payload = {
        "ok": True,
        "origin": origin,
        "origin_resolved": (origin_geo or {}).get("display_name"),
        "origin_country": (origin_geo or {}).get("country_code"),
        "origin_region": origin_region,
        "destination": destination,
        "destination_resolved": geo["display_name"],
        "destination_lat": geo["lat"],
        "destination_lon": geo["lon"],
        "destination_country": geo.get("country_code"),
        "region": region,
        "modes_considered": modes,
        "modes_dropped_by_origin": [m for m in modes_raw if m not in modes],
        "depart_date": depart_date,
        "return_date": return_date,
        "party": party,
        "options": options,
        "best": options[0]["summary"] if options and options[0].get("ok") else None,
        "overnight_near": overnight_near,
        "accommodation_suggestions": accommodation,
    }

    # 7. Persist to journey_log
    try:
        depart_d = date_type.fromisoformat(depart_date)
        return_d = date_type.fromisoformat(return_date) if return_date else None
        async with ctx["pool"].acquire() as conn:
            await conn.execute(
                """
                INSERT INTO journey_log (destination, depart_date, return_date, party, result)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
                """,
                destination,
                depart_d,
                return_d,
                json.dumps(party),
                json.dumps(payload),
            )
    except Exception as e:
        payload["journey_log_error"] = str(e)

    return payload
