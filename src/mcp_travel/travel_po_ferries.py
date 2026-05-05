"""P&O Ferries sailings + prices — live REST API on Expian B2C platform.

Single endpoint at b2c.api.po.expian.io — Expian is a third-party
booking platform; P&O is one of several operators using it. No auth,
just standard browser-ish headers.

Endpoint + ticket-type semantics discovered by Stu via the website's
Network tab; reference script at /zfs/tank/home/stu/pando_ferries.py.

Three UK-market routes (UK is the market scope; market=uk in the URL):
  Dover ↔ Calais            (Channel — competes with DFDS)
  Larne ↔ Cairnryan         (Irish Sea NI ↔ Scotland)
  Hull ↔ Rotterdam Europoort (North Sea — only P&O does this)

Three fare tiers: Standard / Flexi / Fully Flexi.

Ticket-type semantics:
  - Foot passengers use 'foot-passenger-adult' etc.
  - Vehicle passengers use bare 'adult'/'child'/'infant' + vehicle type;
    adults are included in the vehicle price (show £0 individually).
  - Bicycle uses 'bicycle-dc' on Dover-Calais, 'bicycle' elsewhere.
"""

import re
from datetime import date as _date
from typing import Any, Literal

import httpx

API_BASE = "https://b2c.api.po.expian.io"
MARKET = "uk"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://booking.poferries.com",
    "Referer": "https://booking.poferries.com/",
}

PORTS: dict[str, str] = {
    "dover":      "GBDVR",
    "calais":     "FRCQF",
    "larne":      "GBLAR",
    "cairnryan":  "GBCYN",
    "hull":       "GBHUL",
    "rotterdam":  "NLEUR",
}

_VALID_ROUTES: set[tuple[str, str]] = {
    ("GBDVR", "FRCQF"), ("FRCQF", "GBDVR"),
    ("GBLAR", "GBCYN"), ("GBCYN", "GBLAR"),
    ("GBHUL", "NLEUR"), ("NLEUR", "GBHUL"),
}

_FOOT_TICKET_TYPES: dict[str, str] = {
    "adult":  "foot-passenger-adult",
    "child":  "foot-passenger-child",
    "infant": "foot-passenger-infant",
}

_VEHICLE_TICKET_TYPES: dict[str, str] = {
    "car":                 "car",
    "motorhome":           "motorhome",
    "motorcycle":          "motorcycle",
    "motorcycle-sidecar":  "motorcycle-sidecar",
    "van":                 "van",
    "bicycle":             "bicycle",   # 'bicycle-dc' on Dover-Calais; resolved per-route
}


class POFerriesError(RuntimeError):
    pass


def _resolve_port(name: str) -> str | None:
    n = name.strip().lower()
    if n in PORTS:
        return PORTS[n]
    if len(name) == 5 and name.isupper():
        return name
    for port_name, code in PORTS.items():
        if n in port_name or port_name in n:
            return code
    return None


def is_known_route(origin: str, destination: str) -> bool:
    o = _resolve_port(origin)
    d = _resolve_port(destination)
    return o is not None and d is not None and (o, d) in _VALID_ROUTES


def _parse_iso_duration(d: str | None) -> int | None:
    """Parse ISO 8601 duration 'PT2H30M' → minutes."""
    if not d:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", d)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    return h * 60 + mins


async def get_sailings(
    client: httpx.AsyncClient,
    date: str,
    origin: str,
    destination: str,
    adults: int = 2,
    children: int = 0,
    infants: int = 0,
    vehicle: Literal["none", "car", "motorhome", "motorcycle", "motorcycle-sidecar", "van", "bicycle"] = "car",
) -> list[dict[str, Any]]:
    """Live P&O sailings + prices for date and route.

    Returns:
        List of sailing dicts: departure_date, departure (HH:MM),
        arrival_date, arrival, duration_minutes, ship, currency ('GBP'),
        prices ({fare_name: amount}), best_price.
    """
    from_code = _resolve_port(origin)
    to_code = _resolve_port(destination)
    if not from_code or not to_code:
        raise POFerriesError(
            f"unknown P&O port {origin!r} or {destination!r}; "
            f"known: {sorted(set(PORTS.values()))}"
        )
    if (from_code, to_code) not in _VALID_ROUTES:
        raise POFerriesError(
            f"P&O doesn't run {from_code} → {to_code}; "
            f"valid routes: {sorted(_VALID_ROUTES)}"
        )

    try:
        _date.fromisoformat(date)
    except ValueError as e:
        raise POFerriesError(f"invalid date {date!r}: {e}") from e

    if adults + children + infants == 0:
        raise POFerriesError("at least one passenger required")

    # Build ticket-types string
    ticket_parts = []
    if vehicle == "none":
        for ptype, count in [("adult", adults), ("child", children), ("infant", infants)]:
            if count > 0:
                ticket_parts.append(f"{_FOOT_TICKET_TYPES[ptype]}:{count}")
    elif vehicle == "bicycle":
        for ptype, count in [("adult", adults), ("child", children), ("infant", infants)]:
            if count > 0:
                ticket_parts.append(f"{_FOOT_TICKET_TYPES[ptype]}:{count}")
        bic_type = "bicycle-dc" if from_code in ("GBDVR", "FRCQF") else "bicycle"
        ticket_parts.append(f"{bic_type}:1")
    else:
        for ptype, count in [("adult", adults), ("child", children), ("infant", infants)]:
            if count > 0:
                ticket_parts.append(f"{ptype}:{count}")
        ticket_parts.append(f"{_VEHICLE_TICKET_TYPES[vehicle]}:1")

    params = {
        "dates": date,
        "from": from_code,
        "to": to_code,
        "ticket-types": ",".join(ticket_parts),
    }
    resp = await client.get(
        f"{API_BASE}/markets/{MARKET}/booking-options",
        params=params, headers=_HEADERS, timeout=20.0,
    )
    if resp.status_code >= 400:
        raise POFerriesError(f"P&O Expian {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    # Build product-id → display name map
    prod_titles = {
        p.get("id"): (p.get("title") or p.get("id") or "").strip()
        for p in data.get("products", []) or []
    }

    # Group by (date, time) — each sailing appears once per fare product
    sailings_map: dict[tuple, dict] = {}
    for opt in data.get("options", []) or []:
        start_date = opt.get("start_date", date)
        start_time = opt.get("start_time", "")
        key = (start_date, start_time)

        pricing = opt.get("pricing") or {}
        total_pence = pricing.get("total", 0)
        product_id = opt.get("product_id", "")
        fare_name = prod_titles.get(product_id, product_id)

        if key not in sailings_map:
            sailings_map[key] = {
                "departure_date": start_date,
                "departure": start_time[:5],
                "arrival_date": opt.get("end_date", start_date),
                "arrival": (opt.get("end_time") or "")[:5],
                "duration_minutes": _parse_iso_duration(opt.get("duration")),
                "ship": (opt.get("venue") or {}).get("title", ""),
                "currency": "GBP",
                "prices": {},
            }

        if total_pence:
            sailings_map[key]["prices"][fare_name] = round(total_pence / 100, 2)

    # Final list sorted by (date, time), best_price computed
    out: list[dict[str, Any]] = []
    for key in sorted(sailings_map.keys()):
        s = sailings_map[key]
        prices = s["prices"]
        s["best_price"] = min(prices.values()) if prices else None
        out.append(s)
    return out
