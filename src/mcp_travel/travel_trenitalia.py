"""Italian high-speed rail — curated city-pair table.

Why static: Trenitalia (Frecciarossa) and Italo (NTV) have no public
journey-planning API. Their booking sites (lefrecce.it, italotreno.com)
require SPA session/cookie auth that can't be cleanly scraped without
Playwright + selector maintenance. ViaggiaTreno's old solutions
endpoint was retired in 2024.

For trip planning, route durations are the high-value signal — and
durations are extremely stable on the Italian HSR network (the same
Frecciarossa 1000 trains run the same timetable year-round). This
table is the practical answer.

Data source: Trenitalia + Italo published timetables, cross-checked
against seat61.com. Annual review.
"""

from datetime import date as date_type, datetime
from typing import Any
from urllib.parse import quote


# (from, to, minutes, operators_list, frequency, notes)
# Operator codes: FR (Frecciarossa / Trenitalia HSR), IC (Trenitalia
# Intercity), NTV (Italo), FB (Frecciabianca), R (Regional)
_RAW: list[tuple[str, str, int, list[str], str, str]] = [
    # Milano hub
    ("milano", "roma",     180, ["FR", "NTV"],     "very frequent (every 30 min)", "Frecciarossa or Italo, both ~3h"),
    ("milano", "napoli",   270, ["FR", "NTV"],     "several daily",                  "~4h30 direct via Roma"),
    ("milano", "firenze",  105, ["FR", "NTV"],     "very frequent",                  "1h45, every 30 min"),
    ("milano", "venezia",  145, ["FR", "NTV"],     "frequent",                       "2h25 via Verona/Padova"),
    ("milano", "torino",    50, ["FR", "FB"],      "frequent",                       "Direct HSR ~50min"),
    ("milano", "bologna",   65, ["FR", "NTV"],     "very frequent",                  "1h05, every 30 min"),
    ("milano", "verona",    72, ["FR", "FB"],      "frequent",                       "1h12 direct"),
    # Roma hub
    ("roma",   "napoli",    70, ["FR", "NTV"],     "very frequent",                  "1h10 direct"),
    ("roma",   "firenze",   90, ["FR", "NTV"],     "very frequent",                  "1h30 direct"),
    ("roma",   "venezia",  240, ["FR"],            "several daily",                  "~4h, some via Bologna+change"),
    ("roma",   "bologna",  140, ["FR", "NTV"],     "frequent",                       "2h20 direct"),
    ("roma",   "salerno",  120, ["FR", "IC"],      "several daily",                  "2h via Napoli"),
    ("roma",   "torino",   265, ["FR"],            "several daily",                  "4h25 direct"),
    ("roma",   "bari",     250, ["FR", "IC"],      "several daily",                  "Adriatic line, ~4h"),
    # Cross-country
    ("firenze", "venezia", 130, ["FR"],            "frequent",                       "2h10 direct"),
    ("firenze", "bologna",  37, ["FR", "NTV"],     "very frequent",                  "37 min direct"),
    ("napoli",  "bari",    245, ["FR", "IC"],      "several daily",                  "~4h via Caserta"),
    ("bologna", "venezia",  85, ["FR", "FB"],      "frequent",                       "1h25 direct"),
    # Northern access (alpine connections — for Switzerland/Austria interchange)
    ("milano",  "bolzano", 195, ["FR", "EC"],      "several daily",                  "EC trains continue to Munich/Innsbruck"),
    ("milano",  "lugano",   75, ["EC"],            "hourly",                         "Eurocity 1h15 to Switzerland"),
    ("milano",  "zurich",  220, ["EC"],            "hourly",                         "Eurocity Lugano-Zurich, ~3h40"),
]


# City → station name + per-operator booking codes.
#
# `italo_booking` is the code biglietti.italotreno.com expects in its
# ricerca-treni URL (osc/dsc params). Two-letter prefix + underscore.
# Only filled in for cities verified against the live site — leave
# missing for cities not yet probed (the safari-pricecheck tool will
# fail-soft and ask for a manual probe).
#
# Verified codes (DATE: 2026-05-04):
#   MC_ = Milano Centrale
#   RT_ = Roma Termini
# Other codes below are educated guesses (Italian city-plate prefixes)
# and need confirmation before use.
CITIES: dict[str, dict[str, str]] = {
    "milano":   {"station": "Milano Centrale",  "trenitalia": "MILANO+CENTRALE",  "italo_legacy": "MILC", "italo_booking": "MC_"},
    "roma":     {"station": "Roma Termini",     "trenitalia": "ROMA+TERMINI",     "italo_legacy": "ROMT", "italo_booking": "RT_"},
    "napoli":   {"station": "Napoli Centrale",  "trenitalia": "NAPOLI+CENTRALE",  "italo_legacy": "NAPC", "italo_booking": ""},
    "firenze":  {"station": "Firenze SMN",      "trenitalia": "FIRENZE+SMN",      "italo_legacy": "FRSN", "italo_booking": ""},
    "venezia":  {"station": "Venezia SL",       "trenitalia": "VENEZIA+S.+LUCIA", "italo_legacy": "VEZL", "italo_booking": ""},
    "torino":   {"station": "Torino Porta Nuova","trenitalia":"TORINO+P.+NUOVA",  "italo_legacy": "TOPN", "italo_booking": ""},
    "bologna":  {"station": "Bologna Centrale", "trenitalia": "BOLOGNA+CENTRALE", "italo_legacy": "BOLO", "italo_booking": ""},
    "verona":   {"station": "Verona Porta Nuova","trenitalia":"VERONA+P.+NUOVA",  "italo_legacy": "VRPN", "italo_booking": ""},
    "bari":     {"station": "Bari Centrale",    "trenitalia": "BARI+CENTRALE",    "italo_legacy": "BARI", "italo_booking": ""},
    "salerno":  {"station": "Salerno",          "trenitalia": "SALERNO",          "italo_legacy": "SALR", "italo_booking": ""},
    "bolzano":  {"station": "Bolzano",          "trenitalia": "BOLZANO",          "italo_legacy": "",     "italo_booking": ""},
    "lugano":   {"station": "Lugano",           "trenitalia": "LUGANO",           "italo_legacy": "",     "italo_booking": ""},
    "zurich":   {"station": "Zürich HB",        "trenitalia": "ZURIGO",           "italo_legacy": "",     "italo_booking": ""},
}


_DIRECT: dict[tuple[str, str], dict[str, Any]] = {
    tuple(sorted([f, t])): {
        "minutes": minutes, "operators": ops, "frequency": freq, "notes": notes,
    }
    for (f, t, minutes, ops, freq, notes) in _RAW
}


class TrenitaliaError(RuntimeError):
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


def _trenitalia_url(o_slug: str, d_slug: str, date: str, adults: int) -> str:
    o = CITIES[o_slug]["trenitalia"]
    d = CITIES[d_slug]["trenitalia"]
    return (
        f"https://www.lefrecce.it/B2CWeb/search.do?"
        f"departureLocation={o}&arrivalLocation={d}"
        f"&departureDate={date}&adults={adults}"
    )


def _italo_url(o_slug: str, d_slug: str, date: str) -> str:
    """Legacy Italo URL — points at the now-deprecated booking subdomain.
    Kept for backwards compatibility with travel_italy_journey output.
    For the working Safari-driven URL see _italo_booking_url() below."""
    o = CITIES[o_slug].get("italo_legacy") or ""
    d = CITIES[d_slug].get("italo_legacy") or ""
    if not o or not d:
        return ""
    return (
        f"https://www.italotreno.com/en/booking?from={o}&to={d}&date={date}"
    )


def _italo_booking_url(o_slug: str, d_slug: str, date: str, adults: int) -> str:
    """Italo's working URL shape (verified 2026-05-04 via Safari).
    biglietti.italotreno.com auto-runs the search on `startSearch=true`
    and lands on /booking/selezione-treno-andata with results rendered.
    URL params get stripped after the search runs, but the SPA reads
    them on initial load. Date format: DD/MM/YYYY URL-encoded."""
    o = CITIES[o_slug].get("italo_booking") or ""
    d = CITIES[d_slug].get("italo_booking") or ""
    if not o or not d:
        return ""
    # Convert YYYY-MM-DD → DD/MM/YYYY
    y, m, dd = date.split("-")
    od = f"{dd}/{m}/{y}"
    return (
        "https://biglietti.italotreno.com/en/booking/ricerca-treni?"
        f"osc={o}&dsc={d}&jt=single&od={quote(od)}&adt={adults}"
        "&yng=0&chd=0&snr=0&inf=0&pet=0&promo=&lang=en&startSearch=true"
    )


def build_booking_urls(
    origin_city: str,
    destination_city: str,
    date: str,
    adults: int = 2,
) -> dict[str, Any]:
    """Resolve city slugs and return Italo's working booking URL plus
    Trenitalia's deeplink (the latter requires manual form-fill since
    lefrecce.it doesn't carry search state in the URL)."""
    o = _resolve_city(origin_city)
    d = _resolve_city(destination_city)
    if not o or not d:
        raise TrenitaliaError(
            f"unknown Italian city {origin_city!r} or {destination_city!r}; "
            f"known: {sorted(CITIES.keys())}"
        )
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise TrenitaliaError(f"invalid date {date!r}: {e}") from e

    italo_url = _italo_booking_url(o, d, date, adults)
    italo_codes_missing = []
    if not CITIES[o].get("italo_booking"):
        italo_codes_missing.append(o)
    if not CITIES[d].get("italo_booking"):
        italo_codes_missing.append(d)

    return {
        "from_slug": o, "from": CITIES[o]["station"],
        "to_slug": d, "to": CITIES[d]["station"],
        "italo_booking_url": italo_url or None,
        "italo_codes_missing": italo_codes_missing or None,
        # Trenitalia/lefrecce.it doesn't accept URL params for autofill;
        # link to the homepage instead. The user must run the search
        # manually before reading prices off the rendered page.
        "trenitalia_homepage": "https://www.lefrecce.it/",
        "trenitalia_note": (
            "Trenitalia's lefrecce.it strips search state from the URL — "
            "auto-fill is not viable. To compare Trenitalia prices, the "
            "user must run the search by hand at the homepage, then call "
            "apple_browser_get_page_text on the rendered results."
        ),
    }


async def search_journey(
    client,           # unused (no live API), kept for signature parity
    origin: str,
    destination: str,
    date: str,
    adults: int = 2,
) -> dict[str, Any]:
    o = _resolve_city(origin)
    d = _resolve_city(destination)
    if not o or not d:
        known = sorted(CITIES.keys())
        raise TrenitaliaError(
            f"unknown Italian city {origin!r} or {destination!r}; "
            f"known: {known}"
        )
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise TrenitaliaError(f"invalid date {date!r}: {e}") from e

    pair = tuple(sorted([o, d]))
    direct = _DIRECT.get(pair)

    result: dict[str, Any] = {
        "ok": True,
        "mode": "rail",
        "country": "IT",
        "operator_data_source": "static-timetable (Trenitalia + Italo published schedules)",
        "data_sources": ["static-table"],
        "scope_warning": (
            "HSR mainlines only — major Frecciarossa/Italo corridors. "
            "Lake Como, Cinque Terre, Sicily, Sardinia and other branch / "
            "regional lines need a separate booking via Trenitalia for the "
            "onward leg."
        ),
        "from": CITIES[o]["station"],
        "to": CITIES[d]["station"],
        "date": date,
        "adults": adults,
        "as_of": datetime.utcnow().isoformat() + "Z",
        "trenitalia_booking_url": _trenitalia_url(o, d, date, adults),
        "italo_booking_url": _italo_url(o, d, date),
    }

    if direct:
        result["direct"] = True
        result["minutes"] = direct["minutes"]
        result["operators"] = direct["operators"]
        result["frequency"] = direct["frequency"]
        result["notes"] = direct["notes"]
    else:
        result["direct"] = False
        result["minutes"] = None
        result["notes"] = (
            f"No curated direct route entry between {CITIES[o]['station']} "
            f"and {CITIES[d]['station']}. Italy's HSR table only carries "
            "well-known major-city pairs; for smaller stations or via-routes, "
            "use the booking URLs to plan with Trenitalia / Italo directly."
        )

    return result
