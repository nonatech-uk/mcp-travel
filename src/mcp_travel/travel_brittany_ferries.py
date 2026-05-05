"""Brittany Ferries crossing times + prices — live API.

Two endpoints (both at brittany-ferries.co.uk/api/bebop/v1, fronted by
Kong proxy):
  GET /crossing — timetable + ship + day/night/full status (no prices)
  POST /crossing/prices — full per-sailing price tiers

Endpoints + port codes discovered by Stu via the website's network
traffic; reference script at /zfs/tank/home/stu/brittany_ferries.py.

No auth required — same headers / Origin / Referer as the website.

PRICE TIERS:
  economyPrice   — cheapest, non-flex, not always available
  standardPrice  — fixed, fully flex changes
  flexiPrice     — free amendments + cancellations

Routes (verified 2026-05-04):
  GB↔FR:   Plymouth-Roscoff, Portsmouth-StMalo/Caen/LeHavre/Cherbourg,
           Poole-Cherbourg
  GB↔ES:   Portsmouth-Bilbao, Portsmouth-Santander (sole UK↔Spain operator)
  IE↔FR:   Cork-Roscoff
  IE↔ES:   Rosslare-Bilbao
"""

from datetime import date as _date, datetime
from typing import Any, Literal

import httpx

_API_BASE = "https://www.brittany-ferries.co.uk/api/bebop/v1"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.brittany-ferries.co.uk",
    "Referer": "https://www.brittany-ferries.co.uk/booking/choose-crossing/outbound",
}

# Port name → 5-letter Brittany Ferries code (ISO country + 3-letter port).
# Includes common aliases so "Caen (Ouistreham)" / "Saint-Malo" / etc.
# from our static table resolve correctly.
PORTS: dict[str, str] = {
    "plymouth":      "GBPLY",
    "portsmouth":    "GBPME",
    "poole":         "GBPOO",
    "roscoff":       "FRROS",
    "st malo":       "FRSML",
    "saint malo":    "FRSML",
    "saint-malo":    "FRSML",
    "ouistreham":    "FROUI",
    "caen":          "FROUI",
    "cherbourg":     "FRCER",
    "le havre":      "FRLEH",
    "santander":     "ESSDR",
    "bilbao":        "ESBIO",
    "cork":          "IEORK",
    "rosslare":      "IEROE",
}


class BrittanyFerriesError(RuntimeError):
    pass


def _resolve_port(name: str) -> str | None:
    """Map a free-text port name to its 5-letter Brittany Ferries code.
    Handles parenthetical disambiguation ('Caen (Ouistreham)' → 'caen')
    and stripped hyphens ('Saint-Malo' → 'saint-malo')."""
    n = name.strip().lower()
    if n in PORTS:
        return PORTS[n]
    # Try stripping parenthetical suffix
    if "(" in n:
        head = n.split("(", 1)[0].strip()
        if head in PORTS:
            return PORTS[head]
        tail = n.split("(", 1)[1].rstrip(")").strip()
        if tail in PORTS:
            return PORTS[tail]
    # Try without hyphens
    no_hyphen = n.replace("-", " ").strip()
    if no_hyphen in PORTS:
        return PORTS[no_hyphen]
    # Loose substring fallback
    for key, code in PORTS.items():
        if key in n or n in key:
            return code
    return None


def is_known_route(origin: str, destination: str) -> bool:
    """True if both ends resolve to a known Brittany Ferries port code."""
    return _resolve_port(origin) is not None and _resolve_port(destination) is not None


async def get_crossings(
    client: httpx.AsyncClient,
    date_from: str,
    date_to: str,
    origin: str,
    destination: str,
) -> list[dict[str, Any]]:
    """Timetable for a date range — no prices. Fast lookup."""
    dep = _resolve_port(origin)
    arr = _resolve_port(destination)
    if not dep or not arr:
        raise BrittanyFerriesError(
            f"unknown Brittany Ferries port {origin!r} or {destination!r}; "
            f"known: {sorted(set(PORTS.values()))}"
        )

    resp = await client.get(
        f"{_API_BASE}/crossing",
        params={
            "outboundDeparturePort": dep,
            "outboundArrivalPort": arr,
            "dateFrom": date_from,
            "dateTo": date_to,
        },
        headers=_HEADERS,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise BrittanyFerriesError(
            f"Brittany /crossing {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()

    out: list[dict[str, Any]] = []
    for c in data.get("outbound", []) or []:
        dep_dt = c.get("departureScheduledDateTime", "")
        arr_dt = c.get("arrivalScheduledDateTime", "")
        out.append({
            "departure": dep_dt[11:16] if len(dep_dt) >= 16 else dep_dt,
            "arrival": arr_dt[11:16] if len(arr_dt) >= 16 else arr_dt,
            "departure_date": c.get("departureLocalDate", date_from),
            "ship": c.get("shipName") or "",
            "sailing_status": c.get("sailingStatus") or "",
            "full": bool(c.get("full")),
            "foot_allowed": bool(c.get("footPaxAllowed", True)),
            "bicycle_allowed": bool(c.get("bicycleAllowed", True)),
        })
    return out


async def get_sailings(
    client: httpx.AsyncClient,
    date: str,
    origin: str,
    destination: str,
    adults: int = 2,
    children: int = 0,
    infants: int = 0,
    vehicle: Literal["none", "car", "motorbike", "bicycle"] = "car",
    vehicle_height_cm: int | None = None,
    vehicle_length_cm: int | None = None,
) -> dict[str, Any]:
    """Live sailings + per-tier prices for a single date.

    Vehicle defaults: car=150x450 (standard), motorbike=120x220.
    Pass `vehicle='none'` for foot-passenger pricing.

    Returns:
        {currency: 'GBP'|'EUR', sailings: [{departure, arrival, ship,
        ship_type, full, economy, standard, flexi, best_price}]}
    """
    dep = _resolve_port(origin)
    arr = _resolve_port(destination)
    if not dep or not arr:
        raise BrittanyFerriesError(
            f"unknown Brittany Ferries port {origin!r} or {destination!r}; "
            f"known: {sorted(set(PORTS.values()))}"
        )

    try:
        _date.fromisoformat(date)
    except ValueError as e:
        raise BrittanyFerriesError(f"invalid date {date!r}: {e}") from e

    body: dict[str, Any] = {
        "departurePort": dep,
        "arrivalPort": arr,
        "passengers": {"adults": adults, "children": children, "infants": infants},
        "direction": "outbound",
        "fromDate": f"{date}T00:00:00",
        "toDate": f"{date}T23:59:59",
    }

    if vehicle == "car":
        body["vehicle"] = {
            "type": "CAR",
            "height": vehicle_height_cm or 150,
            "length": vehicle_length_cm or 450,
            "registrations": ["TBC"],
        }
    elif vehicle == "motorbike":
        body["vehicle"] = {
            "type": "MOTORBIKE",
            "height": vehicle_height_cm or 120,
            "length": vehicle_length_cm or 220,
            "registrations": ["TBC"],
        }
    elif vehicle == "bicycle":
        body["vehicle"] = {"type": "BICYCLE"}

    resp = await client.post(
        f"{_API_BASE}/crossing/prices",
        json=body,
        headers=_HEADERS,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise BrittanyFerriesError(
            f"Brittany /crossing/prices {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()

    sailings: list[dict[str, Any]] = []
    for day in data.get("crossings", []) or []:
        for entry in day.get("prices", []) or []:
            cp = entry.get("crossingPrices", {}) or {}
            std = cp.get("standardPrice") or {}
            flexi = cp.get("flexiPrice") or {}
            eco = cp.get("economyPrice") or {}
            tier_amounts = [
                p["amount"] for p in (eco, std, flexi)
                if p and p.get("amount") is not None
            ]
            sailings.append({
                "departure": (cp.get("departureDateTime") or {}).get("time", ""),
                "arrival":   (cp.get("arrivalDateTime") or {}).get("time", ""),
                "ship":      cp.get("shipName") or "",
                "ship_type": cp.get("shipType") or "",
                "full":      bool(cp.get("full")),
                "economy":   eco.get("amount") if eco else None,
                "standard":  std.get("amount") if std else None,
                "flexi":     flexi.get("amount") if flexi else None,
                "best_price": min(tier_amounts) if tier_amounts else None,
            })

    return {
        "currency": data.get("currency", "GBP"),
        "sailings": sailings,
    }
