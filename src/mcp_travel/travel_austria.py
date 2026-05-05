"""Austrian rail (ÖBB) — curated city-pair table.

Same rationale as travel_trenitalia/renfe: ÖBB's HAFAS endpoint at
fahrplan.oebb.at isn't publicly documented for direct API use, and
their booking site needs SPA session auth. Major OBB routes have
stable timetables (Railjet runs the same schedule year-round on the
Wien-Salzburg-Innsbruck and Wien-Graz corridors).

Includes Austria-internal Railjet routes plus key cross-border services
(Wien-Budapest/Bratislava/Prag/München, Innsbruck-Italy, etc.) since
ÖBB is a major Eurocity operator.

Maintenance: annual; the Koralmbahn tunnel opens late 2025 and will
significantly cut Wien↔Klagenfurt times — refresh that line.
"""

from datetime import date as date_type, datetime
from typing import Any
from urllib.parse import quote


# (from, to, minutes, train_type, frequency, notes)
_RAW: list[tuple[str, str, int, str, str, str]] = [
    # Wien hub
    ("wien", "salzburg",   142, "Railjet",      "very frequent (every 30 min)", "2h22 direct"),
    ("wien", "innsbruck",  252, "Railjet",      "several daily",                "4h12 via Salzburg"),
    ("wien", "graz",       155, "Railjet",      "frequent",                     "2h35 via Semmering pass"),
    ("wien", "linz",        75, "Railjet",      "very frequent",                "1h15 direct"),
    ("wien", "klagenfurt", 240, "Railjet",      "several daily",                "4h via Semmering — note Koralm tunnel opening late 2025 cuts to ~2h"),
    ("wien", "bregenz",    450, "Railjet",      "1-2 daily",                    "7h30 direct"),
    ("wien", "villach",    260, "Railjet",      "several daily",                "4h20 via Semmering"),
    # Salzburg hub
    ("salzburg", "innsbruck", 105, "Railjet",   "frequent",                     "1h45 direct"),
    ("salzburg", "linz",       85, "Railjet",   "frequent",                     "1h25 direct"),
    ("salzburg", "graz",      280, "IC",        "several daily",                "4h40 via Selzthal"),
    # Innsbruck hub
    ("innsbruck", "bregenz", 140, "Railjet",    "frequent",                     "2h20 via Bludenz/Arlberg"),
    ("innsbruck", "linz",    240, "Railjet",    "several daily",                "4h via Salzburg"),
    # Cross-border (well-known Eurocity / Railjet international)
    ("wien", "budapest",     155, "Railjet",    "frequent",                     "2h35 via Hegyeshalom"),
    ("wien", "bratislava",    66, "REX",        "every 30 min",                 "1h06 — short hop, multiple services"),
    ("wien", "praha",        240, "Railjet",    "several daily",                "4h direct via Břeclav"),
    ("wien", "muenchen",     240, "Railjet",    "several daily",                "4h direct via Salzburg"),
    ("salzburg", "muenchen",  90, "Meridian/EC","very frequent",                "1h30 — frequent commuter + EC"),
    ("innsbruck", "muenchen",105, "Eurocity",   "every 2h",                     "1h45 via Kufstein"),
    ("innsbruck", "verona", 210, "Eurocity",    "several daily",                "3h30 via Brennero/Brenner Pass"),
    ("innsbruck", "bolzano",105, "REX/EC",      "hourly",                       "1h45 cross-border to Italy"),
    ("innsbruck", "zurich", 220, "Railjet",     "several daily",                "3h40 via Sankt Anton/Buchs"),
    # Nightjet sleepers (ÖBB's flagship)
    ("wien", "roma",         840, "Nightjet",   "1 daily overnight",            "14h overnight via Mestre"),
    ("wien", "milano",       780, "Nightjet",   "1 daily overnight",            "13h overnight via Villach"),
    ("wien", "zurich",       720, "Nightjet",   "1 daily overnight",            "12h overnight"),
    ("wien", "hamburg",      720, "Nightjet",   "1 daily overnight",            "12h overnight via Praha"),
    ("wien", "amsterdam",    840, "Nightjet",   "several/week",                 "14h overnight via Frankfurt"),
    ("wien", "bruxelles",    870, "Nightjet",   "several/week",                 "14h30 overnight"),
    ("wien", "paris",        870, "Nightjet",   "several/week",                 "14h30 overnight via Strasbourg"),
]


CITIES: dict[str, dict[str, str]] = {
    "wien":       {"station": "Wien Hauptbahnhof",   "country": "AT"},
    "salzburg":   {"station": "Salzburg Hauptbahnhof","country": "AT"},
    "innsbruck":  {"station": "Innsbruck Hauptbahnhof","country": "AT"},
    "graz":       {"station": "Graz Hauptbahnhof",   "country": "AT"},
    "linz":       {"station": "Linz Hauptbahnhof",   "country": "AT"},
    "klagenfurt": {"station": "Klagenfurt Hbf",      "country": "AT"},
    "villach":    {"station": "Villach Hauptbahnhof","country": "AT"},
    "bregenz":    {"station": "Bregenz",             "country": "AT"},
    "budapest":   {"station": "Budapest Keleti",     "country": "HU"},
    "bratislava": {"station": "Bratislava hl. st.",  "country": "SK"},
    "praha":      {"station": "Praha hlavní nádraží","country": "CZ"},
    "muenchen":   {"station": "München Hauptbahnhof","country": "DE"},
    "verona":     {"station": "Verona Porta Nuova",  "country": "IT"},
    "bolzano":    {"station": "Bolzano",             "country": "IT"},
    "milano":     {"station": "Milano Centrale",     "country": "IT"},
    "roma":       {"station": "Roma Termini",        "country": "IT"},
    "zurich":     {"station": "Zürich HB",           "country": "CH"},
    "hamburg":    {"station": "Hamburg Hauptbahnhof","country": "DE"},
    "amsterdam":  {"station": "Amsterdam Centraal",  "country": "NL"},
    "bruxelles":  {"station": "Bruxelles Midi",      "country": "BE"},
    "paris":      {"station": "Paris Est",           "country": "FR"},
}


_DIRECT: dict[tuple[str, str], dict[str, Any]] = {
    tuple(sorted([f, t])): {
        "minutes": minutes, "train_type": tt, "frequency": freq, "notes": notes,
    }
    for (f, t, minutes, tt, freq, notes) in _RAW
}


class AustriaError(RuntimeError):
    pass


def _resolve_city(query: str) -> str | None:
    q = query.strip().lower()
    for slug, c in CITIES.items():
        if q == slug or q == c["station"].lower():
            return slug
    for slug, c in CITIES.items():
        if slug in q or c["station"].lower() in q or q in c["station"].lower():
            return slug
    return None


def _oebb_url(o: str, d: str, date: str) -> str:
    """ÖBB tickets search-page deeplink (Scotty)."""
    return (
        "https://shop.oebbtickets.at/de/ticket?"
        f"from={quote(o)}&to={quote(d)}&date={date}"
    )


async def search_journey(
    client,
    origin: str,
    destination: str,
    date: str,
    adults: int = 2,
) -> dict[str, Any]:
    o = _resolve_city(origin)
    d = _resolve_city(destination)
    if not o or not d:
        known = sorted(CITIES.keys())
        raise AustriaError(
            f"unknown city {origin!r} or {destination!r}; known: {known}"
        )
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise AustriaError(f"invalid date {date!r}: {e}") from e

    pair = tuple(sorted([o, d]))
    direct = _DIRECT.get(pair)
    o_name = CITIES[o]["station"]
    d_name = CITIES[d]["station"]

    result: dict[str, Any] = {
        "ok": True,
        "mode": "rail",
        "country": "AT-network (incl. cross-border)",
        "operator_data_source": "static-timetable (ÖBB Railjet/Nightjet/Eurocity published schedules)",
        "data_sources": ["static-table"],
        "from": o_name,
        "from_country": CITIES[o]["country"],
        "to": d_name,
        "to_country": CITIES[d]["country"],
        "date": date,
        "adults": adults,
        "as_of": datetime.utcnow().isoformat() + "Z",
        "oebb_booking_url": _oebb_url(o_name, d_name, date),
    }

    if direct:
        result["direct"] = True
        result["minutes"] = direct["minutes"]
        result["train_type"] = direct["train_type"]
        result["frequency"] = direct["frequency"]
        result["notes"] = direct["notes"]
    else:
        result["direct"] = False
        result["minutes"] = None
        result["notes"] = (
            "No curated direct route. Major Austrian-internal pairs and "
            "well-known international Eurocity / Railjet / Nightjet routes "
            "are covered. Use the booking_url for arbitrary station pairs "
            "or smaller regional services."
        )

    return result
