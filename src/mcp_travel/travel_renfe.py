"""Spanish high-speed rail — curated city-pair table.

Same rationale as travel_trenitalia.py: Renfe / Iryo / Ouigo Spain have
no public journey-planning API. Renfe's data.renfe.com publishes static
GTFS but only for AVE network and not in a journey-planner shape;
ingesting + querying GTFS would be 2-3h of additional engineering.

For trip planning, AVE corridor durations are stable across operators
(all three run on the same RFI / Adif track at the same line speed).
This table is the practical answer.

Data source: published Renfe / Iryo / Ouigo Spain timetables, cross-
checked against seat61.com. Annual review.
"""

from datetime import date as date_type, datetime
from typing import Any
from urllib.parse import quote


# (from, to, minutes, operators_list, frequency, notes)
# Operators: AVE (Renfe AVE), AVLO (Renfe low-cost AVE), IRYO (Iryo),
# OUIGO (Ouigo Spain), AVE-FR (AVE-Fr — Madrid-Marseille via Barcelona),
# IC (Renfe Intercity), R (Renfe regional)
_RAW: list[tuple[str, str, int, list[str], str, str]] = [
    # Madrid hub — primary HSR corridor
    ("madrid", "barcelona",  150, ["AVE", "AVLO", "IRYO", "OUIGO"], "very frequent (every 15-30 min)",  "2h30 direct, 4 operators compete"),
    ("madrid", "sevilla",    150, ["AVE", "IRYO"],                  "frequent",                          "2h30 via Córdoba"),
    ("madrid", "malaga",     160, ["AVE", "IRYO"],                  "frequent",                          "2h40 direct"),
    ("madrid", "valencia",   105, ["AVE", "IRYO", "OUIGO"],         "very frequent",                     "1h45 direct"),
    ("madrid", "alicante",   145, ["AVE"],                          "several daily",                     "2h25 direct"),
    ("madrid", "zaragoza",    75, ["AVE", "AVLO", "IRYO", "OUIGO"], "very frequent",                     "1h15 — corridor stop on Madrid-Barcelona"),
    ("madrid", "cordoba",    105, ["AVE", "IRYO"],                  "frequent",                          "1h45 direct, intermediate to Sevilla/Málaga"),
    ("madrid", "valladolid",  56, ["AVE"],                          "frequent",                          "56 min direct on the Madrid-NW corridor"),
    ("madrid", "leon",       125, ["AVE", "ALV"],                   "several daily",                     "Madrid→León 2h05"),
    ("madrid", "santiago",   320, ["AVE"],                          "several daily",                     "Madrid→Santiago de Compostela ~5h20"),
    # Barcelona corridor
    ("barcelona", "valencia", 180, ["EUROMED", "IC"],                "several daily",                    "Mediterranean coast EuroMed train ~3h"),
    ("barcelona", "sevilla",  330, ["AVE"],                          "1-2 daily",                        "5h30 direct via Madrid"),
    ("barcelona", "zaragoza",  85, ["AVE", "AVLO", "IRYO", "OUIGO"], "very frequent",                    "1h25 — corridor stop"),
    ("barcelona", "malaga",   330, ["AVE"],                          "1 daily",                          "5h30 direct via Madrid"),
    ("barcelona", "girona",    37, ["AVE", "IC"],                    "frequent",                         "37 min on the way to French border"),
    # France-Spain (the only cross-border high-speed)
    ("barcelona", "perpignan", 80, ["AVE-FR", "TGV"],                "1-2 daily",                        "1h20 cross-border AVE-Fr / TGV inOui"),
    ("barcelona", "lyon",     310, ["AVE-FR"],                       "1 daily",                          "5h10 direct (seasonal)"),
    ("barcelona", "marseille",290, ["AVE-FR"],                       "1 daily",                          "4h50 direct"),
    # Sevilla / Málaga south
    ("sevilla", "malaga",     115, ["IC"],                            "several daily",                   "Andalusian regional AVANT"),
    ("sevilla", "cordoba",     45, ["AVE", "IRYO"],                  "very frequent",                    "45 min — Madrid corridor stop"),
]


# City → station + Renfe deeplink station code (different operators use different codes;
# Renfe's are 5-char like 'MADRI', 'BARNA', 'VALEN'.)
CITIES: dict[str, dict[str, str]] = {
    "madrid":     {"station": "Madrid Atocha",        "renfe": "MADRI"},
    "barcelona":  {"station": "Barcelona Sants",      "renfe": "BARNA"},
    "sevilla":    {"station": "Sevilla Santa Justa",  "renfe": "SEVIL"},
    "malaga":     {"station": "Málaga María Zambrano","renfe": "MALAG"},
    "valencia":   {"station": "Valencia Joaquín Sorolla","renfe":"VALEN"},
    "alicante":   {"station": "Alicante",             "renfe": "ALICA"},
    "zaragoza":   {"station": "Zaragoza Delicias",    "renfe": "ZARGZ"},
    "cordoba":    {"station": "Córdoba",              "renfe": "CORDO"},
    "valladolid": {"station": "Valladolid",           "renfe": "VALLA"},
    "leon":       {"station": "León",                 "renfe": "LEON_"},
    "santiago":   {"station": "Santiago de Compostela","renfe":"SANTI"},
    "girona":     {"station": "Girona",               "renfe": "GIRON"},
    "perpignan":  {"station": "Perpignan",            "renfe": ""},  # SNCF, deeplink Renfe won't help
    "lyon":       {"station": "Lyon Part-Dieu",       "renfe": ""},
    "marseille":  {"station": "Marseille Saint-Charles","renfe":""},
}


_DIRECT: dict[tuple[str, str], dict[str, Any]] = {
    tuple(sorted([f, t])): {
        "minutes": minutes, "operators": ops, "frequency": freq, "notes": notes,
    }
    for (f, t, minutes, ops, freq, notes) in _RAW
}


class RenfeError(RuntimeError):
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


def _renfe_url(o_slug: str, d_slug: str, date: str, adults: int) -> str:
    o = CITIES[o_slug].get("renfe") or ""
    d = CITIES[d_slug].get("renfe") or ""
    if not o or not d:
        return ""
    # Renfe URL format YYYYMMDD
    yyyymmdd = date.replace("-", "")
    return (
        f"https://venta.renfe.com/vol/buscarTren.do?"
        f"desOrigen={o}&desDestino={d}&cdgoOrigen={o}&cdgoDestino={d}"
        f"&fechaIda={yyyymmdd}&numAdultos={adults}"
    )


def _iryo_url(o: str, d: str, date: str) -> str:
    return f"https://iryo.eu/en/search?origin={quote(o)}&destination={quote(d)}&departure={date}"


def _ouigo_url(o: str, d: str, date: str) -> str:
    return f"https://www.ouigo.com/es/search?origin={quote(o)}&destination={quote(d)}&date={date}"


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
        raise RenfeError(
            f"unknown Spanish city {origin!r} or {destination!r}; "
            f"known: {known}"
        )
    try:
        date_type.fromisoformat(date)
    except ValueError as e:
        raise RenfeError(f"invalid date {date!r}: {e}") from e

    pair = tuple(sorted([o, d]))
    direct = _DIRECT.get(pair)
    o_name = CITIES[o]["station"]
    d_name = CITIES[d]["station"]

    result: dict[str, Any] = {
        "ok": True,
        "mode": "rail",
        "country": "ES",
        "operator_data_source": "static-timetable (Renfe + Iryo + Ouigo published schedules)",
        "data_sources": ["static-table"],
        "scope_warning": (
            "HSR mainlines only — AVE/AVLO/Iryo/Ouigo. Regional Cercanías, "
            "AVANT and rural / branch lines (Galicia interior, the Pyrenees, "
            "the Balearics) need a separate Renfe booking for the onward leg."
        ),
        "from": o_name,
        "to": d_name,
        "date": date,
        "adults": adults,
        "as_of": datetime.utcnow().isoformat() + "Z",
        "renfe_booking_url":  _renfe_url(o, d, date, adults),
        "iryo_booking_url":   _iryo_url(o_name, d_name, date),
        "ouigo_booking_url":  _ouigo_url(o_name, d_name, date),
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
            f"No curated direct route between {o_name} and {d_name}. "
            "Spain's HSR table only carries the major AVE/AVLO/Iryo/Ouigo "
            "corridor pairs. For smaller stations, regional rail or "
            "via-routes, use a booking URL to plan with the operator directly."
        )

    return result
