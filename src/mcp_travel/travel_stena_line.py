"""Stena Line passenger ferry sailings + prices — live GraphQL API.

Single GraphQL endpoint at book.stenaline.co.uk/graphql — cleanest of
the three ferry APIs we've integrated. One query returns the day's
sailings with per-product prices and a `lowestTicketPrice`. Multi-
currency (GBP/EUR/SEK/DKK/NOK), multi-passenger-type (adult/child/
infant/senior), multi-vehicle (car/large/SUV/motorhome/motorbike/
bicycle).

Endpoint + route codes + GraphQL schema discovered by Stu via the
website's Network tab; reference script at /zfs/tank/home/stu/stena_line.py.

No auth required — same headers / Origin / Referer as the website.

Routes (24 directional, 12 bidirectional pairs):
  UK ↔ IE: Holyhead↔Dublin, Fishguard↔Rosslare
  GB ↔ NIR: Belfast↔Cairnryan, Belfast↔Liverpool (Birkenhead)
  GB ↔ NL: Harwich↔Hook of Holland
  Scandinavia: Kiel↔Gothenburg, Frederikshavn↔Gothenburg, Trelleborg↔Rostock,
               Travemünde↔Liepaja, Ventspils↔Nynäshamn, Grenaa↔Halmstad,
               Karlskrona↔Gdynia
"""

from datetime import date as _date
from typing import Any, Literal

import httpx

GRAPHQL_URL = "https://book.stenaline.co.uk/graphql"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://book.stenaline.co.uk",
    "Referer": "https://book.stenaline.co.uk/",
}

_SALES_OWNER_ID = "14"
_LANGUAGE_ID = "EN"

# Port-name → 2-letter Stena Line code. Combined with destination's
# 2-letter code to form a 4-letter route ID. NB: Stena's route codes
# are unique route IDs not strict origin+dest concatenation — there's
# overlap (HA used for both Harwich and Halmstad) but it works because
# no route has both as endpoints. Aliases included for the static
# table's port names ('Birkenhead' → 'liverpool', etc).
_PORTS: dict[str, str] = {
    "belfast":           "BF",
    "cairnryan":         "CN",
    "liverpool":         "LP",
    "birkenhead":        "LP",   # Stena docks at Birkenhead but books as Liverpool
    "holyhead":          "HH",
    "dublin":            "DB",
    "fishguard":         "FI",
    "rosslare":          "RO",
    "harwich":           "HA",
    "hook of holland":   "HO",
    "hookofholland":     "HO",
    "hook":              "HO",
    "kiel":              "KI",
    "gothenburg":        "GO",
    "frederikshavn":     "FR",
    "trelleborg":        "TB",
    "rostock":           "RS",
    "travemunde":        "TR",
    "travemünde":        "TR",
    "liepaja":           "LI",
    "ventspils":         "VE",
    "nynashamn":         "NY",
    "nynäshamn":         "NY",
    "grenaa":            "GR",
    "halmstad":          "HA",   # Note: HA also used for Harwich (different routes)
    "karlskrona":        "KA",
    "gdynia":            "GD",
}

# Set of valid 4-letter route codes (forward + reverse) — used to
# verify a port-pair actually maps to a Stena route (since the HA
# ambiguity above means raw concatenation could produce a fake code).
_VALID_ROUTES: set[str] = {
    "BFCN", "CNBF",   # Belfast ↔ Cairnryan
    "BFLP", "LPBF",   # Belfast ↔ Liverpool/Birkenhead
    "HHDB", "DBHH",   # Holyhead ↔ Dublin
    "FIRO", "ROFI",   # Fishguard ↔ Rosslare
    "HAHO", "HOHA",   # Harwich ↔ Hook of Holland
    "KIGO", "GOKI",   # Kiel ↔ Gothenburg
    "FRGO", "GOFR",   # Frederikshavn ↔ Gothenburg
    "TBRS", "RSTB",   # Trelleborg ↔ Rostock
    "TRLI", "LITR",   # Travemünde ↔ Liepaja
    "VENY", "NYVE",   # Ventspils ↔ Nynäshamn
    "GRHA", "HAGR",   # Grenaa ↔ Halmstad
    "KAGD", "GDKA",   # Karlskrona ↔ Gdynia
}

_PASSENGER_GENERIC_IDS: dict[str, str] = {
    "ADULT":   "866B4D8A-8B67-48BF-85CC-BB72C019683C",
    "CHILD":   "9F6DD06C-E88D-49F2-BDD4-98778EF2AA9E",
    "INFANT":  "C8A6ABD0-480A-4A18-A7E2-0525EBA9F6EA",
    "SENIOR":  "7E8567FC-7477-4dab-893D-F2C49CA251E9",
    "STUDENT": "5122e778-3116-4504-aa5c-d99ad5dd8487",
}

_VEHICLE_TYPE_CODES: dict[str, str | None] = {
    "none":       None,
    "car":        "CAR470200",
    "large_car":  "CAR600200",
    "suv":        "MPV600400",
    "motorhome":  "MOH600400",
    "motorbike":  "MBK",
    "bicycle":    "BIC",
}

_QUERY = """
query OutwardSailingSearch(
  $salesOwnerId: ID!
  $languageId: ID!
  $currencyId: ID!
  $routeId: ID!
  $date: LocalDate!
  $numPassengers: [NumberOfPassengerCategoryInput!]!
  $numVehicles: [NumberOfVehicleTypeInput!]!
  $numTrailers: [NumberOfTrailerTypeInput!]!
  $numPetsSelected: Int!
) {
  searchSailings {
    searchOneWay(
      salesOwnerId: $salesOwnerId
      languageId: $languageId
      currencyId: $currencyId
      routeId: $routeId
      date: $date
      isHomeward: false
      numPassengers: $numPassengers
      numVehiclesOfTypes: $numVehicles
      numTrailersOfTypes: $numTrailers
      numberOfPets: $numPetsSelected
    ) {
      __typename
      ... on SearchOneWaySailingsSuccess {
        oneWaySailings {
          allSailings {
            id
            sailing {
              departureDate
              departureInstant
              arrivalDate
              arrivalInstant
              duration
              ferry { name }
            }
            lowestTicketPrice { amount }
            products {
              elementCode
              price { amount }
              currency { id }
              sailingProduct(salesOwnerId: $salesOwnerId) {
                code
                name(languageId: $languageId)
              }
            }
          }
        }
      }
      ... on SearchOneWaySailingsError {
        errorMessages { technicalMessage }
      }
    }
  }
}
"""


class StenaLineError(RuntimeError):
    pass


def _resolve_port(name: str) -> str | None:
    """Map a port name to its 2-letter Stena code. Strips spaces +
    hyphens, then tries exact match, then loose substring."""
    n = name.strip().lower().replace("-", "").replace(" ", "")
    if n in _PORTS:
        return _PORTS[n]
    for key, code in _PORTS.items():
        if n in key or key in n:
            return code
    return None


def _resolve_route(origin: str, destination: str) -> str | None:
    """Return a 4-letter Stena route ID, or None if the pair isn't a
    valid route. Accepts direct route codes (e.g. 'HHDB') as origin."""
    o = origin.strip().upper()
    if len(o) == 4 and o in _VALID_ROUTES:
        return o
    o_code = _resolve_port(origin)
    d_code = _resolve_port(destination)
    if not o_code or not d_code:
        return None
    route = o_code + d_code
    return route if route in _VALID_ROUTES else None


def is_known_route(origin: str, destination: str) -> bool:
    return _resolve_route(origin, destination) is not None


def _parse_duration(s: str | None) -> int | None:
    """Stena returns 'D:HH:MM:SS' — convert to total minutes."""
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 4:
        return None
    try:
        days, hours, mins = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return days * 1440 + hours * 60 + mins


async def get_sailings(
    client: httpx.AsyncClient,
    date: str,
    origin: str,
    destination: str,
    adults: int = 2,
    children: int = 0,
    infants: int = 0,
    seniors: int = 0,
    vehicle: Literal["none", "car", "large_car", "suv", "motorhome", "motorbike", "bicycle"] = "car",
    currency: str = "GBP",
) -> list[dict[str, Any]]:
    """Live Stena Line sailings + prices for date and route.

    Args:
        client:      Shared httpx async client.
        date:        Departure date YYYY-MM-DD.
        origin:      City name or 4-letter route code (e.g. 'HHDB').
        destination: City name (ignored if origin is a route code).
        adults:      Adult pax count (16+). Default 2.
        children:    Child pax count (4-15).
        infants:     Infant pax count (0-3).
        seniors:     Senior pax count (65+).
        vehicle:     'none' (foot), 'car', 'large_car', 'suv',
                     'motorhome', 'motorbike', 'bicycle'.
        currency:    'GBP' (default), 'EUR', 'SEK', 'DKK', 'NOK'.

    Returns:
        List of sailing dicts: {departure, arrival, departure_date,
        arrival_date, duration_minutes, ferry, currency, best_price,
        prices: {product_name: amount}}.
    """
    route_id = _resolve_route(origin, destination)
    if not route_id:
        raise StenaLineError(
            f"unknown Stena Line route {origin!r} → {destination!r}; "
            f"valid routes: {sorted(_VALID_ROUTES)}"
        )

    try:
        _date.fromisoformat(date)
    except ValueError as e:
        raise StenaLineError(f"invalid date {date!r}: {e}") from e

    if adults + children + infants + seniors == 0:
        raise StenaLineError("at least one passenger is required")

    passengers = []
    for ptype, count in [
        ("ADULT", adults), ("CHILD", children),
        ("INFANT", infants), ("SENIOR", seniors),
    ]:
        if count > 0:
            passengers.append({
                "numberSelected": count,
                "passengerCategory": ptype,
                "typeGenericId": _PASSENGER_GENERIC_IDS[ptype],
            })

    vehicle_code = _VEHICLE_TYPE_CODES.get(vehicle)
    vehicles = (
        [{"numberSelected": 1, "vehicleTypeCode": vehicle_code}]
        if vehicle_code else []
    )

    payload = {
        "operationName": "OutwardSailingSearch",
        "variables": {
            "salesOwnerId": _SALES_OWNER_ID,
            "languageId": _LANGUAGE_ID,
            "currencyId": currency,
            "routeId": route_id,
            "date": date,
            "numPassengers": passengers,
            "numVehicles": vehicles,
            "numTrailers": [],
            "numPetsSelected": 0,
        },
        "query": _QUERY,
    }

    resp = await client.post(GRAPHQL_URL, json=payload, headers=_HEADERS, timeout=20.0)
    if resp.status_code >= 400:
        raise StenaLineError(f"Stena GraphQL {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("errors"):
        raise StenaLineError(f"Stena GraphQL errors: {data['errors']}")

    result = (
        (data.get("data") or {}).get("searchSailings", {})
        .get("searchOneWay", {}) or {}
    )
    if result.get("__typename") == "SearchOneWaySailingsError":
        msgs = [m.get("technicalMessage") for m in result.get("errorMessages", []) or []]
        raise StenaLineError(f"Stena API error: {msgs}")

    sailings: list[dict[str, Any]] = []
    for s in result.get("oneWaySailings", {}).get("allSailings", []) or []:
        sail = s.get("sailing", {}) or {}
        lowest_raw = s.get("lowestTicketPrice") or {}
        best_price = float(lowest_raw["amount"]) if lowest_raw.get("amount") is not None else None

        prices: dict[str, float] = {}
        sailing_currency = currency
        for p in s.get("products", []) or []:
            prod = p.get("sailingProduct") or {}
            name = prod.get("name") or prod.get("code") or p.get("elementCode") or ""
            price = p.get("price") or {}
            amt = price.get("amount")
            if amt is not None and name:
                prices[name] = float(amt)
            cur = (p.get("currency") or {}).get("id")
            if cur:
                sailing_currency = cur

        sailings.append({
            "departure_date": sail.get("departureDate"),
            "departure": sail.get("departureInstant"),
            "arrival_date": sail.get("arrivalDate"),
            "arrival": sail.get("arrivalInstant"),
            "duration_minutes": _parse_duration(sail.get("duration")),
            "ferry": (sail.get("ferry") or {}).get("name", ""),
            "currency": sailing_currency,
            "best_price": best_price,
            "prices": prices,
        })

    return sailings
