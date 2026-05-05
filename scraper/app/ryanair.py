"""Ryanair flights — Playwright + browser-API interception.

Direct calls to /api/booking/v4/*/availability return 409 (WAF-blocked).
We navigate the booking selector page in a real Chromium session and
intercept the availability response to extract structured JSON.

Returns total prices for all queried passengers combined (Ryanair's
quoted prices are aggregated by pax type, not per-person).
"""

import urllib.parse
from typing import Any

from playwright.sync_api import sync_playwright, Route
from playwright_stealth.stealth import Stealth


SELECTOR_BASE = "https://www.ryanair.com/gb/en/trip/flights/select"
_AVAIL_PATTERN = "**/api/booking/v4/**/availability**"

_FARE_KEYS = [
    ("regularFare",  "Standard"),
    ("businessFare", "Business"),
    ("racFare",      "Corporate"),
]


def get_flights(
    date: str,
    origin: str,
    destination: str,
    adults: int = 1,
    teens: int = 0,
    children: int = 0,
    infants: int = 0,
    headless: bool = True,
    include_sold_out: bool = False,
) -> list[dict[str, Any]]:
    """Live Ryanair flights for a date + IATA pair.

    Returns flight dicts with keys: date, flight_number, departure (HH:MM),
    arrival (HH:MM), duration_minutes, origin, destination, available,
    fares_left, currency, best_price, prices.
    """
    origin = origin.upper()
    destination = destination.upper()

    captured: dict[str, Any] = {}

    def handle_route(route: Route) -> None:
        try:
            resp = route.fetch()
            if not captured.get("done"):
                try:
                    body = resp.json()
                    if "trips" in body:
                        captured["data"] = body
                        captured["done"] = True
                except Exception:
                    pass
            try:
                route.fulfill(response=resp)
            except Exception:
                pass
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    params = urllib.parse.urlencode({
        "adults":          adults,
        "teens":           teens,
        "children":        children,
        "infants":         infants,
        "dateOut":         date,
        "isReturn":        "false",
        "discount":        0,
        "promoCode":       "",
        "originIata":      origin,
        "destinationIata": destination,
        "tpAdults":        adults,
        "tpStartDate":     date,
        "tpOriginIata":    origin,
        "tpDestinationIata": destination,
    })
    url = f"{SELECTOR_BASE}?{params}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-GB",
                timezone_id="Europe/London",
            )
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)

            page.route(_AVAIL_PATTERN, handle_route)
            page.goto(url, timeout=30000, wait_until="domcontentloaded")

            for _ in range(200):
                if captured.get("done"):
                    break
                page.wait_for_timeout(100)
        finally:
            browser.close()

    data = captured.get("data")
    if not data:
        return []

    return _parse(data, date, include_sold_out)


def _parse(data: dict, filter_date: str = "", include_sold_out: bool = False) -> list[dict[str, Any]]:
    currency = data.get("currency", "EUR")
    flights: list[dict[str, Any]] = []

    for trip in data.get("trips", []):
        origin = trip.get("origin", "")
        destination = trip.get("destination", "")

        for date_block in trip.get("dates", []):
            date_out = date_block.get("dateOut", "")[:10]
            if filter_date and date_out and date_out != filter_date:
                continue

            for fl in date_block.get("flights", []) or []:
                if not fl:
                    continue

                flight_number = fl.get("flightNumber", "")
                fares_left_raw = fl.get("faresLeft")

                times = fl.get("time") or []
                dep_iso = times[0] if times else ""
                arr_iso = times[1] if len(times) > 1 else ""
                dep_time = dep_iso[11:16] if len(dep_iso) > 10 else dep_iso[:5]
                arr_time = arr_iso[11:16] if len(arr_iso) > 10 else arr_iso[:5]
                dep_date = dep_iso[:10] if dep_iso else date_out

                if filter_date and dep_date and dep_date != filter_date:
                    continue

                dur = fl.get("duration", "")
                dur_mins = None
                if dur:
                    parts = dur.split(":")
                    if len(parts) >= 2:
                        try:
                            dur_mins = int(parts[0]) * 60 + int(parts[1])
                        except ValueError:
                            pass

                prices: dict[str, float] = {}
                for api_key, label in _FARE_KEYS:
                    fare_obj = fl.get(api_key)
                    if not fare_obj:
                        continue
                    total = sum(
                        (f.get("amount") or 0)
                        for f in fare_obj.get("fares", [])
                        if f.get("amount") is not None
                    )
                    if total > 0:
                        prices[label] = round(total, 2)

                available = bool(prices)
                if not available and not include_sold_out:
                    continue

                best = min(prices.values()) if prices else None
                fares_left = (
                    fares_left_raw
                    if (fares_left_raw is not None and fares_left_raw >= 0)
                    else None
                )

                flights.append({
                    "date":             dep_date,
                    "flight_number":    flight_number,
                    "departure":        dep_time,
                    "arrival":          arr_time,
                    "duration_minutes": dur_mins,
                    "origin":           origin,
                    "destination":      destination,
                    "available":        available,
                    "fares_left":       fares_left,
                    "currency":         currency,
                    "best_price":       best,
                    "prices":           prices,
                })

    return sorted(flights, key=lambda f: f["departure"])
