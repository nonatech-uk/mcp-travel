"""LeShuttle (Eurotunnel) — live crossings + prices via the public quote API.

We call the same `nextus-api-prod.leshuttle.com/b2c-api/ExactViewQuote`
endpoint the website's React app uses. No auth, no scraping — it just
needs a generated WebSessionId (random UUID) and standard browser-ish
headers. Returns every crossing slot for the day plus per-ticket-type
prices (Standard, FlexiLongstay, etc.).

Endpoint discovered by Stu by watching the site's XHR traffic; full
flow lives at /zfs/tank/home/stu/leshuttle.py for reference.

Earlier attempt was a Playwright scraper; replaced with static-only
durations; now upgraded to live JSON. Static fallback retained — if
the API ever shifts shape or rate-limits, the tool still returns
useful door-to-door durations + a deeplink.
"""

from datetime import date as date_type, datetime
from typing import Any
import uuid
from urllib.parse import urlencode

import httpx

API_BASE = "https://nextus-api-prod.leshuttle.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.leshuttle.com",
    "Referer": "https://www.leshuttle.com/booking/trip-details",
}

VEHICLE_ALIASES: dict[str, str] = {
    "car": "car",
    "estate": "car",
    "suv": "car",
    "van": "high-vehicle",
    "high": "high-vehicle",
    "high-vehicle": "high-vehicle",
    "caravan": "caravan-trailer",
    "trailer": "caravan-trailer",
    "caravan-trailer": "caravan-trailer",
    "motorhome": "motorhome",
    "campervan": "motorhome",
    "motorcycle": "motorcycle",
    "bike": "motorcycle",
}

# Direction codes used by the LeShuttle API.
# FOCA = Folkestone → Calais; CAFO = Calais → Folkestone.
DIRECTION_OUTBOUND = "FOCA"
DIRECTION_RETURN = "CAFO"

CROSSING_MINUTES = 35
TERMINAL_OVERHEAD_MINUTES = 35  # 30 check-in + 5 disembark/customs (typical)

DEFAULT_DRIVE_TO_FOLKESTONE_MIN = 95
DEFAULT_CALAIS_TERMINAL_MIN = 5


class EurotunnelError(RuntimeError):
    pass


def _resolve_vehicle(v: str) -> str:
    return VEHICLE_ALIASES.get(v.strip().lower(), "car")


def _booking_url(date: str, time: str, vehicle: str, passengers: int) -> str:
    qs = urlencode(
        {
            "journeyType": "oneway",
            "outboundDate": date,
            "outboundTime": time,
            "adults": passengers,
            "vehicle": vehicle,
        }
    )
    return f"https://www.leshuttle.com/booking/?{qs}"


async def _fetch_quote(
    client: httpx.AsyncClient,
    date: str,
    direction: str,
    country_of_residence: str = "GB",
) -> dict[str, Any]:
    params = {
        "WebSessionId": str(uuid.uuid4()),
        "CountryOfResidence": country_of_residence,
        "Direction": direction,
        "OutboundDateTime": f"{date}T06:00:00.000Z",
        # Vehicle-spec params: standard car configuration.
        # The API requires all of these; varying them changes pricing only.
        "Cat": "CAR",
        "CatLen": "L3",
        "CatHeight": "L",
        "SubCat": "NIL",
        "SubCatLen": "L0",
        "Fuel": "CON",
        "Roof": "N",
        "Rear": "N",
        "Special": "false",
        "Camper": "false",
        "MasterTable": "false",
        "SelfDeclared": "true",
    }
    resp = await client.get(
        f"{API_BASE}/b2c-api/ExactViewQuote",
        params=params,
        headers=_HEADERS,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise EurotunnelError(f"leshuttle quote {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    status = data.get("status") or {}
    if status.get("id") != 0:
        raise EurotunnelError(f"leshuttle quote API error: {status.get('desc')!r}")
    return data


def _parse_crossings(quote: dict[str, Any]) -> list[dict[str, Any]]:
    journey = quote.get("outboundJourney") or {}
    crossings: list[dict[str, Any]] = []
    for slot in journey.get("slots", []) or []:
        for mission in slot.get("missions", []) or []:
            prices = []
            for t in mission.get("ticketTypes", []) or []:
                prices.append({
                    "ticket_type": t.get("description"),
                    "price": t.get("price"),
                    "available": t.get("available") == "Yes",
                    "flexi": t.get("flexi"),
                })
            available_prices = [p["price"] for p in prices if p["available"] and p["price"] is not None]
            crossings.append({
                "departure": mission.get("dateTime"),
                "prices": prices,
                "best_price": min(available_prices) if available_prices else None,
            })
    crossings.sort(key=lambda c: c["departure"] or "")
    return crossings


def _parse_dt_naive(s: str) -> datetime | None:
    """Parse ISO datetime, dropping any timezone info for naive comparison."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


def _pick_nearest(crossings: list[dict[str, Any]], target_time: str, date: str) -> dict[str, Any] | None:
    """Pick the crossing closest to (date, target_time) with availability.
    Prefers the first crossing at-or-after the requested time; falls back
    to the closest by absolute minute delta if none at-or-after."""
    if not crossings:
        return None
    try:
        target_dt = datetime.fromisoformat(f"{date}T{target_time}:00")
    except ValueError:
        target_dt = datetime.fromisoformat(f"{date}T10:00:00")

    available = [c for c in crossings if c.get("best_price") is not None]
    pool = available or crossings

    after = [
        c for c in pool
        if (dep := _parse_dt_naive(c.get("departure"))) and dep >= target_dt
    ]
    if after:
        return after[0]

    def _delta(c: dict[str, Any]) -> float:
        dep = _parse_dt_naive(c.get("departure"))
        if dep is None:
            return 10**9
        return abs((dep - target_dt).total_seconds())

    return min(pool, key=_delta)


async def check(
    client: httpx.AsyncClient | None,
    date: str,
    time: str = "10:00",
    vehicle: str = "car",
    passengers: int = 2,
    direction: str = DIRECTION_OUTBOUND,
    country_of_residence: str = "GB",
) -> dict[str, Any]:
    """Live LeShuttle crossings + prices for `date`, returning the
    crossing nearest `time` plus the full timetable for the day."""
    veh = _resolve_vehicle(vehicle)
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise EurotunnelError(f"invalid date {date!r}: {e}") from e

    base_payload: dict[str, Any] = {
        "ok": True,
        "mode": "eurotunnel",
        "from": "Folkestone" if direction == DIRECTION_OUTBOUND else "Calais Coquelles",
        "to": "Calais Coquelles" if direction == DIRECTION_OUTBOUND else "Folkestone",
        "direction": direction,
        "date": date,
        "time": time,
        "vehicle": veh,
        "passengers": passengers,
        "crossing_minutes": CROSSING_MINUTES,
        "terminal_overhead_minutes": TERMINAL_OVERHEAD_MINUTES,
        "terminal_to_terminal_minutes": CROSSING_MINUTES + TERMINAL_OVERHEAD_MINUTES,
        "default_drive_to_folkestone_min": DEFAULT_DRIVE_TO_FOLKESTONE_MIN,
        "default_calais_terminal_min": DEFAULT_CALAIS_TERMINAL_MIN,
        "booking_url": _booking_url(date, time, veh, passengers),
        "as_of": datetime.utcnow().isoformat() + "Z",
    }

    if client is None:
        return {
            **base_payload,
            "source": "static-timetable",
            "data_sources": ["static-table"],
            "note": "No httpx client available; static durations only.",
        }

    try:
        quote = await _fetch_quote(client, date, direction, country_of_residence)
    except (EurotunnelError, httpx.HTTPError) as e:
        return {
            **base_payload,
            "source": "static-timetable",
            "data_sources": ["static-table"],
            "live_error": str(e),
            "note": "Live LeShuttle quote API failed; returning static durations only.",
        }

    crossings = _parse_crossings(quote)
    nearest = _pick_nearest(crossings, time, date)

    return {
        **base_payload,
        "source": "leshuttle-live",
        "data_sources": ["leshuttle-live"],
        "currency": quote.get("currencyCode") or "GBP",
        "country_of_residence": country_of_residence,
        "selected_crossing": nearest,
        "crossings": crossings,
        "crossing_count": len(crossings),
        "available_count": sum(1 for c in crossings if c.get("best_price") is not None),
    }
