"""DFDS passenger ferry availability and pricing — live API.

DFDS operates three completely separate booking engines for passenger
routes; this module covers all three. Endpoints + route/code mappings
discovered by Stu via the website's network traffic; reference script
at /zfs/tank/home/stu/dfds.py.

Engines:
  1. Hellman API  — Channel crossings (Dover/Calais/Dunkirk) + Channel Islands
     Base: api.hellman.oxygen.dfds.cloud/.../departures/{date}
  2. SBWAPI fares-flow  — Newhaven-Dieppe (day, foot/car seat tickets)
     Base: dfds.com/sbwapi/booking/fares-flow
  3. SBWAPI cabin-fares-flow — Overnight cabin routes (Rosslare-Dunkirk,
     Newcastle-Amsterdam, Baltic)
     Base: dfds.com/sbwapi/booking/cabin-fares-flow

NOTE: Spain/Morocco routes (Tanger-Algeciras etc.) are operated by
partner FRS and not covered here.

No auth required — same headers / Origin / Referer as the website.
"""

from datetime import date as _date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

_HELLMAN_BASE = (
    "https://api.hellman.oxygen.dfds.cloud/prod/booking-experience-bff"
    "/_api/passenger-ferries/booking-experience/departures"
)
_SBWAPI_BASE = "https://www.dfds.com/sbwapi/booking"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.dfds.com",
    "Referer": "https://www.dfds.com/",
}

_VEHICLE_CODES = {
    "car":        "CAR",
    "motorcycle": "MOTORCYCLE",
    "foot":       "FOOT",
}

# --- Engine 1: Hellman ---------------------------------------------------
# Channel crossings + Channel Islands. Hellman uses internal port codes
# SH (St. Helier = Jersey) and SP (St. Peter Port = Guernsey), and uses
# PO for both Portsmouth and Poole.
_HELLMAN_ROUTES: dict[tuple[str, str], str] = {
    ("dover",        "calais"):     "DVCA",
    ("calais",       "dover"):      "CADV",
    ("dover",        "dunkirk"):    "DVDK",
    ("dunkirk",      "dover"):      "DKDV",
    ("st. malo",     "jersey"):     "SMSH",
    ("jersey",       "st. malo"):   "SHSM",
    ("portsmouth",   "jersey"):     "POSH",
    ("poole",        "jersey"):     "POSH",
    ("jersey",       "portsmouth"): "SHPO",
    ("jersey",       "poole"):      "SHPO",
    ("jersey",       "guernsey"):   "SHSP",
    ("guernsey",     "jersey"):     "SPSH",
}

# --- Engines 2 & 3: SBWAPI -----------------------------------------------
# fares-flow = day crossings with seat tickets
# cabin-fares-flow = overnight crossings with cabin pricing
# Reverse directions reuse the forward direction's product code.
_SBWAPI_ROUTES: dict[tuple[str, str], dict[str, Any]] = {
    ("newhaven",    "dieppe"):     dict(out="NHDP", ret="DPNH", so=22, pc="TNHDP20V", eng="fares", cur="GBP"),
    ("dieppe",      "newhaven"):   dict(out="DPNH", ret="NHDP", so=22, pc="TNHDP20V", eng="fares", cur="GBP"),
    ("rosslare",    "dunkirk"):    dict(out="RODK", ret="DKRO", so=19, pc="TRODK",   eng="cabin", cur="EUR"),
    ("dunkirk",     "rosslare"):   dict(out="DKRO", ret="RODK", so=19, pc="TRODK",   eng="cabin", cur="EUR"),
    ("newcastle",   "amsterdam"):  dict(out="NEAN", ret="ANNE", so=19, pc="TNEAN",   eng="cabin", cur="GBP"),
    ("amsterdam",   "newcastle"):  dict(out="ANNE", ret="NEAN", so=19, pc="TNEAN",   eng="cabin", cur="GBP"),
    ("kiel",        "klaipeda"):   dict(out="KIKL", ret="KLKI", so=19, pc="TKLKIR",  eng="cabin", cur="EUR"),
    ("klaipeda",    "kiel"):       dict(out="KLKI", ret="KIKL", so=19, pc="TKLKIR",  eng="cabin", cur="EUR"),
    ("trelleborg",  "klaipeda"):   dict(out="TRKL", ret="KLTR", so=19, pc="TKLTR",   eng="cabin", cur="EUR"),
    ("klaipeda",    "trelleborg"): dict(out="KLTR", ret="TRKL", so=19, pc="TKLTR",   eng="cabin", cur="EUR"),
    ("karlshamn",   "klaipeda"):   dict(out="KHKL", ret="LKKH", so=14, pc="TKLKHR",  eng="cabin", cur="EUR"),
    ("klaipeda",    "karlshamn"):  dict(out="LKKH", ret="KHKL", so=14, pc="TKLKHR",  eng="cabin", cur="EUR"),
    ("kapellskar",  "paldiski"):   dict(out="KPPA", ret="PAKP", so=14, pc="TKPPAR",  eng="cabin", cur="EUR"),
    ("paldiski",    "kapellskar"): dict(out="PAKP", ret="KPPA", so=14, pc="TKPPAR",  eng="cabin", cur="EUR"),
}

KNOWN_ROUTES: list[tuple[str, str]] = sorted(
    set(_HELLMAN_ROUTES) | set(_SBWAPI_ROUTES)
)


class DFDSError(RuntimeError):
    pass


def _next_day(date: str) -> str:
    return (_date.fromisoformat(date) + timedelta(days=1)).isoformat()


def _duration_str(dep_iso: str, arr_iso: str) -> str | None:
    try:
        dep = datetime.fromisoformat(dep_iso)
        arr = datetime.fromisoformat(arr_iso)
        h, m = divmod(int((arr - dep).total_seconds() / 60), 60)
        return f"{h}h{m:02d}min" if m else f"{h}h"
    except (ValueError, TypeError):
        return None


def _parse_sbwapi_rows(rows: list, currency: str) -> list[dict[str, Any]]:
    sailings = []
    for row in rows:
        dep, arr = row.get("departureTime", ""), row.get("arrivalTime", "")
        tickets = [
            {
                "product_code": item.get("productCode"),
                "price":        item.get("price"),
                "currency":     currency,
                "sold_out":     (item.get("status") or "").lower() != "available",
            }
            for item in row.get("items", []) or []
        ]
        available = [t["price"] for t in tickets if not t["sold_out"] and t["price"] is not None]
        sailings.append({
            "departure_time": dep[11:16] if len(dep) >= 16 else dep,
            "arrival_time":   arr[11:16] if len(arr) >= 16 else arr,
            "duration":       _duration_str(dep, arr),
            "checkin_time":   None,
            "origin_port":    None,
            "dest_port":      None,
            "vessel":         row.get("ferryName"),
            "tickets":        tickets,
            "best_price":     min(available) if available else None,
            "currency":       currency,
        })
    return sailings


async def _hellman_sailings(
    client: httpx.AsyncClient, date: str, route_code: str, vehicle: str, adults: int,
) -> list[dict[str, Any]]:
    params = {
        "route":       route_code,
        "vehicleCode": _VEHICLE_CODES.get(vehicle.lower(), "CAR"),
        "adults":      adults,
        "leg":         1,
        "locale":      "en-gb",
    }
    resp = await client.get(
        f"{_HELLMAN_BASE}/{date}", params=params, headers=_HEADERS, timeout=20.0,
    )
    if resp.status_code >= 400:
        raise DFDSError(f"DFDS Hellman {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    sailings = []
    for d in data.get("departures", []) or []:
        tickets = [
            {
                "product_code": t.get("productCode"),
                "price":        t.get("price"),
                "currency":     t.get("currency"),
                "sold_out":     bool(t.get("soldOut")),
            }
            for t in d.get("tickets", []) or []
        ]
        available = [t["price"] for t in tickets if not t["sold_out"] and t["price"] is not None]
        sailings.append({
            "departure_time": d.get("departureTime"),
            "arrival_time":   d.get("arrivalTime"),
            "duration":       d.get("tripDuration"),
            "checkin_time":   d.get("checkinTime"),
            "origin_port":    d.get("departurePortCode"),
            "dest_port":      d.get("arrivalPortCode"),
            "vessel":         None,
            "tickets":        tickets,
            "best_price":     min(available) if available else None,
            "currency":       (tickets[0]["currency"] if tickets else "GBP"),
        })
    return sailings


async def _fares_flow_sailings(
    client: httpx.AsyncClient, date: str, cfg: dict, adults: int, vehicle: str,
) -> list[dict[str, Any]]:
    params = {
        "outboundDepartureDate": date,
        "returnDepartureDate":   _next_day(date),
        "outboundRouteCode":     cfg["out"],
        "returnRouteCode":       cfg["ret"],
        "salesOwnerId":          cfg["so"],
        "localeCode":            "en",
        "salesChannelCode":      "PIB",
        "adults":      adults,
        "children":    0,
        "infants":     0,
        "pets":        0,
        "isAmendment": "false",
        "productCode": cfg["pc"],
        "vehicleType": _VEHICLE_CODES.get(vehicle.lower(), "CAR"),
    }
    resp = await client.get(
        f"{_SBWAPI_BASE}/fares-flow", params=params, headers=_HEADERS, timeout=20.0,
    )
    if resp.status_code >= 400:
        raise DFDSError(f"DFDS fares-flow {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return _parse_sbwapi_rows(data.get("out", {}).get("rows", []), cfg["cur"])


async def _cabin_fares_sailings(
    client: httpx.AsyncClient, date: str, cfg: dict, adults: int, vehicle: str,
) -> list[dict[str, Any]]:
    params = {
        "outboundDepartureDate": date,
        "returnDepartureDate":   _next_day(date),
        "outboundRouteCode":     cfg["out"],
        "returnRouteCode":       cfg["ret"],
        "salesOwnerId":          cfg["so"],
        "localeCode":            "en",
        "salesChannelCode":      "PIB",
        "adults":      adults,
        "children":    0,
        "infants":     0,
        "isAmendment": "false",
        "productCode":  cfg["pc"],
        "vehicleType":  _VEHICLE_CODES.get(vehicle.lower(), "CAR"),
        "vehicleCount": 1,
    }
    resp = await client.get(
        f"{_SBWAPI_BASE}/cabin-fares-flow", params=params, headers=_HEADERS, timeout=20.0,
    )
    if resp.status_code >= 400:
        raise DFDSError(f"DFDS cabin-fares-flow {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return _parse_sbwapi_rows(data.get("out", {}).get("rows", []), cfg["cur"])


def is_known_route(origin: str, destination: str) -> bool:
    """True if the (origin, destination) pair is a DFDS route this module
    knows how to query. Used by travel_ferries.check to decide whether
    to enrich a static-table entry with live DFDS data."""
    key = (origin.lower().strip(), destination.lower().strip())
    return key in _HELLMAN_ROUTES or key in _SBWAPI_ROUTES


async def get_sailings(
    client: httpx.AsyncClient,
    date: str,
    origin: str,
    destination: str,
    adults: int = 1,
    vehicle: str = "car",
) -> list[dict[str, Any]]:
    """Live DFDS sailings + prices for a date and route.

    Args:
        client:      Shared httpx async client.
        date:        Departure date YYYY-MM-DD.
        origin:      Port name (case-insensitive). See KNOWN_ROUTES.
        destination: Same.
        adults:      Pax count (default 1).
        vehicle:     'car' / 'motorcycle' / 'foot'.

    Returns:
        List of sailing dicts: departure_time, arrival_time, duration,
        checkin_time (Channel only), tickets[], best_price, currency.
    """
    key = (origin.lower().strip(), destination.lower().strip())

    hellman_code = _HELLMAN_ROUTES.get(key)
    if hellman_code:
        return await _hellman_sailings(client, date, hellman_code, vehicle, adults)

    sbwapi_cfg = _SBWAPI_ROUTES.get(key)
    if sbwapi_cfg:
        if sbwapi_cfg["eng"] == "fares":
            return await _fares_flow_sailings(client, date, sbwapi_cfg, adults, vehicle)
        return await _cabin_fares_sailings(client, date, sbwapi_cfg, adults, vehicle)

    raise DFDSError(
        f"unknown DFDS route {origin!r} → {destination!r}; "
        f"known DFDS routes: {KNOWN_ROUTES}"
    )
