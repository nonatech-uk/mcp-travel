"""Curated ferry-route table — Channel + North Sea + Irish Sea.

Same static-data pattern as travel_eurostar.py / travel_eurotunnel.py.
Ferry operators don't expose public booking APIs; their websites can be
scraped but the legwork isn't worth it for trip-planning when the
durations are stable per season and pricing happens at click-through.

Maintenance: annual review against operator websites. New routes get a
single tuple in `_RAW`. Compiled into `ROUTES` at module load.

Output mirrors `travel_eurotunnel_check`:
  - terminal_to_terminal_minutes (crossing + terminal overhead)
  - access-leg constants (port → typical UK origins) where useful
  - booking_url constructed from a per-operator pattern
"""

from datetime import date as date_type, datetime
from typing import Any
from urllib.parse import urlencode


# Format:
# (origin_port, dest_port, country_to, operator, crossing_min,
#  daily_freq, terminal_overhead_min, vehicle_ok, seasonal, notes)
_RAW: list[tuple[str, str, str, str, int, str, int, bool, str | None, str]] = [
    # === English Channel — short crossings (Dover) ===
    ("Dover",       "Calais",         "FR", "DFDS",            90,  "very frequent", 60, True, None,
     "Most-used short Channel crossing"),
    ("Dover",       "Calais",         "FR", "P&O Ferries",     90,  "very frequent", 60, True, None,
     "Frequent rotation alongside DFDS"),
    ("Dover",       "Calais",         "FR", "Irish Ferries",   90,  "frequent",      60, True, None,
     "Newer Channel operator"),
    ("Dover",       "Dunkerque",      "FR", "DFDS",           120,  "frequent",      60, True, None,
     "Slightly longer crossing, often cheaper"),

    # === English Channel — Sussex ===
    ("Newhaven",    "Dieppe",         "FR", "DFDS",           240,  "3/day",         60, True, None,
     "Convenient for SE England → Normandy"),

    # === English Channel — Hampshire / Solent ===
    ("Portsmouth",  "Le Havre",       "FR", "Brittany Ferries", 300, "1-2/day",      60, True, None,
     "Day or overnight; Le Havre = good for Normandy/Paris"),
    ("Portsmouth",  "Caen (Ouistreham)","FR", "Brittany Ferries", 360, "2-3/day",    60, True, None,
     "Caen for Calvados/D-Day coast/Normandy"),
    ("Portsmouth",  "Cherbourg",      "FR", "Brittany Ferries", 180, "1-2/day",      60, True, None,
     "Fast craft — passenger + car (180 min); cruise ferry 600 min overnight"),
    ("Portsmouth",  "Saint-Malo",     "FR", "Brittany Ferries", 660, "1/day overnight", 60, True, None,
     "Overnight cruise to Brittany"),
    ("Poole",       "Cherbourg",      "FR", "Brittany Ferries", 270, "1/day fast",    60, True, "summer-mainly",
     "Fast craft from Poole; reduced winter service"),
    ("Plymouth",    "Roscoff",        "FR", "Brittany Ferries", 360, "1/day",         60, True, None,
     "Western entry into Brittany; some sailings overnight to 540 min"),

    # === Bay of Biscay (UK ↔ Spain) ===
    ("Portsmouth",  "Bilbao",         "ES", "Brittany Ferries", 1620, "2/week",       90, True, None,
     "27-hour cruise crossing"),
    ("Portsmouth",  "Santander",      "ES", "Brittany Ferries", 1380, "2/week",       90, True, None,
     "23-hour daytime; some sailings up to 40 h overnight"),
    ("Plymouth",    "Santander",      "ES", "Brittany Ferries", 1200, "1/week",       90, True, None,
     "20-hour cruise; weekly only in summer"),

    # === North Sea ===
    ("Hull",        "Rotterdam",      "NL", "P&O Ferries",     660, "1/day overnight", 90, True, None,
     "Hull → Rotterdam Europoort, overnight cruise"),
    ("Hull",        "Zeebrugge",      "BE", "P&O Ferries",     780, "1/day overnight", 90, True, None,
     "Hull → Zeebrugge for Belgium / N. Netherlands"),
    ("Newcastle",   "Amsterdam (IJmuiden)", "NL", "DFDS",       900, "1/day overnight", 90, True, None,
     "DFDS King Seaways / Princess Seaways — N. England → Amsterdam"),
    ("Harwich",     "Hook of Holland","NL", "Stena Line",      420, "2/day",          60, True, None,
     "Daytime + overnight"),

    # === Irish Sea — Wales ↔ Ireland ===
    ("Holyhead",    "Dublin",         "IE", "Irish Ferries",   210, "4/day",          60, True, None,
     "Standard Holyhead → Dublin Port; fast craft 120 min on some sailings"),
    ("Holyhead",    "Dublin",         "IE", "Stena Line",      200, "4/day",          60, True, None,
     "Stena Adventurer / Estrid — same route, different operator"),
    ("Pembroke Dock","Rosslare",      "IE", "Irish Ferries",   240, "2/day",          60, True, None,
     "S. Wales → SE Ireland, day + night sailings"),
    ("Fishguard",   "Rosslare",       "IE", "Stena Line",      210, "2/day",          60, True, None,
     "S. Wales → SE Ireland alternative"),

    # === Irish Sea — England ↔ Ireland ===
    ("Liverpool",   "Dublin",         "IE", "P&O Ferries",     480, "2/day",          60, True, None,
     "8h overnight cruise from Liverpool to Dublin Port"),

    # === Irish Sea — Scotland ↔ Northern Ireland ===
    ("Cairnryan",   "Larne",          "GB-NIR", "P&O Ferries", 120, "6/day",          45, True, None,
     "Shortest Scotland → NI crossing"),
    ("Cairnryan",   "Belfast",        "GB-NIR", "Stena Line",  135, "6/day",          45, True, None,
     "Stena Superfast / Spirit"),

    # === Irish Sea — England ↔ Northern Ireland ===
    ("Birkenhead",  "Belfast",        "GB-NIR", "Stena Line",  480, "2/day",          60, True, None,
     "Liverpool-area → Belfast overnight"),
    ("Heysham",     "Belfast",        "GB-NIR", "Stena Line",  480, "2/day",          60, True, None,
     "Lancashire → Belfast, mixed freight + passenger"),

    # === Irish Sea — Isle of Man ===
    ("Liverpool",   "Douglas",        "IM", "Steam Packet",    165, "1-2/day",        60, True, None,
     "Manannan fastcraft + Ben-my-Chree conventional"),
    ("Heysham",     "Douglas",        "IM", "Steam Packet",    210, "1-2/day",        60, True, None,
     "Year-round freight + passenger"),
    ("Belfast",     "Douglas",        "IM", "Steam Packet",    175, "summer only",    60, True, "summer-only",
     "Summer-only fastcraft NI ↔ IoM"),
    ("Dublin",      "Douglas",        "IM", "Steam Packet",    175, "summer only",    60, True, "summer-only",
     "Summer-only fastcraft IE ↔ IoM"),
]


def _booking_url(operator: str, orig: str, dest: str, date: str, vehicle: str, passengers: int) -> str:
    """Operator homepage URL — every ferry operator's booking SPA strips
    search state from the URL on the results page (DFDS, Brittany Ferries,
    Stena, Trenitalia-style obfuscation across the industry, verified
    2026-05-04). Pre-fill via query string isn't viable; users have to
    fill the form on the operator's site directly. We return the most
    useful landing URL per operator and let them complete the search."""
    op_lower = operator.lower()
    if "dfds" in op_lower:
        return "https://www.dfds.com/en/passenger-ferries"
    if "p&o" in op_lower or "p and o" in op_lower:
        return "https://www.poferries.com/en/"
    if "brittany" in op_lower:
        return "https://www.brittany-ferries.co.uk/"
    if "irish" in op_lower:
        return "https://www.irishferries.com/"
    if "stena" in op_lower:
        return "https://www.stenaline.co.uk/"
    if "steam packet" in op_lower:
        return "https://www.steam-packet.com/"
    return f"https://www.google.com/search?q={urlencode({'q': f'{operator} {orig} to {dest} {date}'})[2:]}"


ROUTES: list[dict[str, Any]] = [
    {
        "operator": op,
        "origin_port": orig,
        "dest_port": dest,
        "country_to": country,
        "crossing_minutes": cross,
        "frequency": freq,
        "terminal_overhead_minutes": term,
        "vehicle_ok": veh,
        "seasonal": seas,
        "notes": notes,
    }
    for (orig, dest, country, op, cross, freq, term, veh, seas, notes) in _RAW
]


# Country/region → list of relevant ISO/region codes for `routes_to` lookup
DEST_GROUPS: dict[str, list[str]] = {
    "FR": ["FR"], "France": ["FR"], "france": ["FR"],
    "ES": ["ES"], "Spain": ["ES"], "spain": ["ES"],
    "NL": ["NL"], "Netherlands": ["NL"], "netherlands": ["NL"], "holland": ["NL"],
    "BE": ["BE"], "Belgium": ["BE"], "belgium": ["BE"],
    "IE": ["IE"], "Ireland": ["IE"], "ireland": ["IE"], "Republic of Ireland": ["IE"],
    "GB-NIR": ["GB-NIR"], "Northern Ireland": ["GB-NIR"], "northern ireland": ["GB-NIR"], "NI": ["GB-NIR"],
    "IM": ["IM"], "Isle of Man": ["IM"], "isle of man": ["IM"], "iom": ["IM"],
    "channel": ["FR"], "Channel": ["FR"],
    "north sea": ["NL", "BE"], "North Sea": ["NL", "BE"],
    "irish sea": ["IE", "GB-NIR", "IM"], "Irish Sea": ["IE", "GB-NIR", "IM"],
    "british isles": ["IE", "GB-NIR", "IM"], "British Isles": ["IE", "GB-NIR", "IM"],
}


class FerryError(RuntimeError):
    pass


def routes_to(country_or_region: str) -> list[dict[str, Any]]:
    """List candidate ferry routes whose destination matches a country/region."""
    codes = DEST_GROUPS.get(country_or_region) or DEST_GROUPS.get(country_or_region.lower()) or [country_or_region.upper()]
    return [r for r in ROUTES if r["country_to"] in codes]


def find_route(origin_port: str, dest_port: str) -> list[dict[str, Any]]:
    """All routes (potentially multiple operators) between named ports.
    Case-insensitive substring match. Bidirectional — finds routes
    listed in either direction, since most operators run both ways. If
    the table has the route in reverse, returns a swapped copy so
    origin/dest match what the caller asked for."""
    o = origin_port.strip().lower()
    d = dest_port.strip().lower()
    matches = []
    for r in ROUTES:
        rop = r["origin_port"].lower()
        rdp = r["dest_port"].lower()
        if o in rop and d in rdp:
            matches.append(r)
        elif o in rdp and d in rop:
            # Match in reverse — return a swapped copy so the caller
            # sees their requested direction.
            swapped = {**r, "origin_port": r["dest_port"], "dest_port": r["origin_port"]}
            matches.append(swapped)
    return matches


async def check(
    client,             # httpx.AsyncClient | None — live data fetched when present
    origin_port: str,
    dest_port: str,
    date: str,
    vehicle: str = "car",
    passengers: int = 2,
) -> dict[str, Any]:
    matches = find_route(origin_port, dest_port)
    if not matches:
        raise FerryError(
            f"no curated ferry route between {origin_port!r} and {dest_port!r}; "
            "try travel_ferry_routes_to(<country>) to discover candidates"
        )
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise FerryError(f"invalid date {date!r}: {e}") from e

    # Local imports to avoid cycles if these modules ever need travel_ferries.
    from mcp_travel.travel_dfds import (
        get_sailings as dfds_sailings, is_known_route as dfds_known, DFDSError,
    )
    from mcp_travel.travel_brittany_ferries import (
        get_sailings as bf_sailings, is_known_route as bf_known, BrittanyFerriesError,
    )
    from mcp_travel.travel_stena_line import (
        get_sailings as stena_sailings, is_known_route as stena_known, StenaLineError,
    )
    from mcp_travel.travel_po_ferries import (
        get_sailings as po_sailings, is_known_route as po_known, POFerriesError,
    )
    from mcp_travel.travel_irish_ferries import (
        get_sailings as irish_sailings, is_known_route as irish_known, IrishFerriesError,
    )

    data_sources: set[str] = {"static-table"}

    options = []
    for r in matches:
        url = _booking_url(r["operator"], r["origin_port"], r["dest_port"], date, vehicle, passengers)
        opt = {
            **r,
            "terminal_to_terminal_minutes": r["crossing_minutes"] + r["terminal_overhead_minutes"],
            "booking_url": url,
            "live_data": False,
            "data_sources": ["static-table"],
        }

        op_lower = r["operator"].lower()

        # DFDS routes get enriched with live sailings + prices.
        if (
            client is not None
            and "dfds" in op_lower
            and dfds_known(r["origin_port"], r["dest_port"])
        ):
            try:
                sailings = await dfds_sailings(
                    client,
                    date=date,
                    origin=r["origin_port"],
                    destination=r["dest_port"],
                    adults=passengers,
                    vehicle=vehicle,
                )
                available = [
                    s["best_price"] for s in sailings
                    if s.get("best_price") is not None
                ]
                opt["live_data"] = True
                opt["sailings"] = sailings
                opt["sailing_count"] = len(sailings)
                opt["best_price"] = min(available) if available else None
                opt["currency"] = sailings[0]["currency"] if sailings else None
                opt["data_sources"] = ["dfds-live"]
                data_sources.add("dfds-live")
            except DFDSError as e:
                opt["live_error"] = str(e)

        # Brittany Ferries routes — same pattern, prices + per-tier breakdown.
        elif (
            client is not None
            and "brittany" in op_lower
            and bf_known(r["origin_port"], r["dest_port"])
        ):
            try:
                bf_vehicle = "car" if vehicle in ("car", "high-vehicle", "caravan-trailer") else (
                    "motorbike" if vehicle == "motorcycle" else
                    "bicycle"   if vehicle == "bicycle" else
                    "none"
                )
                bf_data = await bf_sailings(
                    client,
                    date=date,
                    origin=r["origin_port"],
                    destination=r["dest_port"],
                    adults=passengers,
                    vehicle=bf_vehicle,
                )
                sailings = bf_data["sailings"]
                available = [
                    s["best_price"] for s in sailings
                    if s.get("best_price") is not None
                ]
                opt["live_data"] = True
                opt["sailings"] = sailings
                opt["sailing_count"] = len(sailings)
                opt["best_price"] = min(available) if available else None
                opt["currency"] = bf_data.get("currency", "GBP")
                opt["data_sources"] = ["brittany-ferries-live"]
                data_sources.add("brittany-ferries-live")
            except BrittanyFerriesError as e:
                opt["live_error"] = str(e)

        # P&O Ferries — Expian B2C REST. Channel + Larne-Cairnryan + Hull-Rotterdam.
        elif (
            client is not None
            and ("p&o" in op_lower or "p and o" in op_lower)
            and po_known(r["origin_port"], r["dest_port"])
        ):
            try:
                po_vehicle = (
                    "car" if vehicle in ("car", "high-vehicle", "caravan-trailer") else
                    "motorhome" if vehicle == "motorhome" else
                    "motorcycle" if vehicle == "motorcycle" else
                    "van" if vehicle == "van" else
                    "bicycle" if vehicle == "bicycle" else
                    "none"
                )
                sailings = await po_sailings(
                    client, date=date,
                    origin=r["origin_port"], destination=r["dest_port"],
                    adults=passengers, vehicle=po_vehicle,
                )
                available = [
                    s["best_price"] for s in sailings
                    if s.get("best_price") is not None
                ]
                opt["live_data"] = True
                opt["sailings"] = sailings
                opt["sailing_count"] = len(sailings)
                opt["best_price"] = min(available) if available else None
                opt["currency"] = sailings[0]["currency"] if sailings else "GBP"
                opt["data_sources"] = ["po-ferries-live"]
                data_sources.add("po-ferries-live")
            except POFerriesError as e:
                opt["live_error"] = str(e)

        # Stena Line routes — GraphQL, multi-currency, full passenger/vehicle matrix.
        elif (
            client is not None
            and "stena" in op_lower
            and stena_known(r["origin_port"], r["dest_port"])
        ):
            try:
                # Stena vehicle vocabulary differs slightly from our static one.
                stena_vehicle = (
                    "car" if vehicle in ("car",) else
                    "large_car" if vehicle in ("high-vehicle", "caravan-trailer") else
                    "motorhome" if vehicle == "motorhome" else
                    "motorbike" if vehicle == "motorcycle" else
                    "bicycle" if vehicle == "bicycle" else
                    "none"
                )
                # Stena's GraphQL accepts any currency and converts server-side.
                # GBP works for every route (verified Holyhead-Dublin gives GBP
                # quotes; EUR for that route returns 0 sailings). Stu is UK-
                # based so GBP is the most useful default everywhere.
                stena_cur = "GBP"
                sailings = await stena_sailings(
                    client,
                    date=date,
                    origin=r["origin_port"],
                    destination=r["dest_port"],
                    adults=passengers,
                    vehicle=stena_vehicle,
                    currency=stena_cur,
                )
                available = [
                    s["best_price"] for s in sailings
                    if s.get("best_price") is not None
                ]
                opt["live_data"] = True
                opt["sailings"] = sailings
                opt["sailing_count"] = len(sailings)
                opt["best_price"] = min(available) if available else None
                opt["currency"] = sailings[0]["currency"] if sailings else stena_cur
                opt["data_sources"] = ["stena-line-live"]
                data_sources.add("stena-line-live")
            except StenaLineError as e:
                opt["live_error"] = str(e)

        # Irish Ferries — Playwright-driven scrape via mcp-travel-scraper
        # sidecar. Irish Sea (Dublin/Holyhead, Rosslare/Pembroke) +
        # Rosslare↔Cherbourg. 15-25s per scrape — caller's 24h cache absorbs.
        elif (
            client is not None
            and "irish ferries" in op_lower
            and irish_known(r["origin_port"], r["dest_port"])
        ):
            try:
                sailings = await irish_sailings(
                    client,
                    date=date,
                    origin=r["origin_port"],
                    destination=r["dest_port"],
                    adults=passengers,
                    vehicle=vehicle,
                )
                available = [
                    s["best_price"] for s in sailings
                    if s.get("best_price") is not None
                ]
                opt["live_data"] = True
                opt["sailings"] = sailings
                opt["sailing_count"] = len(sailings)
                opt["best_price"] = min(available) if available else None
                opt["currency"] = sailings[0]["currency"] if sailings else "EUR"
                opt["data_sources"] = ["irish-ferries-live"]
                data_sources.add("irish-ferries-live")
            except IrishFerriesError as e:
                opt["live_error"] = str(e)

        options.append(opt)

    # Sort by total time
    options.sort(key=lambda o: o["terminal_to_terminal_minutes"])

    return {
        "ok": True,
        "mode": "ferry",
        "from": matches[0]["origin_port"],
        "to": matches[0]["dest_port"],
        "country_to": matches[0]["country_to"],
        "date": date,
        "vehicle": vehicle,
        "passengers": passengers,
        "source": (
            "live+static"
            if any(s.endswith("-live") for s in data_sources)
            else "static-timetable"
        ),
        "data_sources": sorted(data_sources),
        "options": options,
        "note": (
            "DFDS and Brittany Ferries sailings carry live prices + per-"
            "sailing availability. Other operators (P&O, Stena, Irish "
            "Ferries, Steam Packet) remain static — click their booking_url "
            "for live data."
        ),
        "as_of": datetime.utcnow().isoformat() + "Z",
    }
