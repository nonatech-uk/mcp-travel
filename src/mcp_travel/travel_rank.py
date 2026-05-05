"""Region classifier + access-leg constants + ranking for plan_trip.

Region classification is by lat/lon bands with a named-resort override
for Alps ski destinations (where the lat/lon band would otherwise put
them in 'Switzerland' or 'France-east' generically). Resort names take
precedence over coordinate lookups.

Mode-set selection per region encodes the brief's heuristics:
  - Paris / Lille / Provence: Eurostar primary, drive+Eurotunnel secondary
  - Côte d'Azur: flight primary, drive secondary
  - Brittany / Normandy / Loire: drive+Eurotunnel primary, flight secondary
  - Alps (winter): fly Geneva + drive primary, Eurostar ski-train secondary
  - Pyrenees / SW France: flight primary, drive secondary

Ranking is by door_to_door minutes + transfer penalty (60 min/transfer).
Cost is reported but not used in the score per user direction
("i just care about times at this point").
"""

from typing import Any

# --- Access-leg defaults (drive minutes from default home origin) ---
# Used as defaults if Google Maps drive_time call fails or no key present.
# Real numbers come from drive_time at runtime.

ACCESS_LEGS_FROM_FARLEY_GREEN: dict[str, dict[str, Any]] = {
    "lgw": {"name": "London Gatwick (LGW)",          "default_drive_min": 50},
    "lhr": {"name": "London Heathrow (LHR)",         "default_drive_min": 75},
    "stn": {"name": "London Stansted (STN)",         "default_drive_min": 95},
    "ltn": {"name": "London Luton (LTN)",            "default_drive_min": 90},
    "lcy": {"name": "London City (LCY)",             "default_drive_min": 90},
    "stp": {"name": "London St Pancras International","default_drive_min": 95},
    "fol": {"name": "Folkestone Eurotunnel Terminal","default_drive_min": 95},
    "dvr": {"name": "Dover Ferry Terminal",          "default_drive_min": 100},
    "prt": {"name": "Portsmouth Ferry Terminal",     "default_drive_min": 75},
}

AIRPORT_OVERHEAD_MIN = 90    # check-in + security + walk to gate; baggage adds 15-30 more
PREDEPARTURE_BUFFER_MIN = 60  # user preference: 60 min before terminal departure

# --- Named ski resorts (Alps) — coords for fly-Geneva / drive lookup ---
SKI_RESORTS: dict[str, dict[str, Any]] = {
    "verbier":          {"lat": 46.0964, "lon": 7.2287, "country": "CH", "nearest_airport": "GVA"},
    "chamonix":         {"lat": 45.9237, "lon": 6.8694, "country": "FR", "nearest_airport": "GVA"},
    "val d'isere":      {"lat": 45.4485, "lon": 6.9803, "country": "FR", "nearest_airport": "GVA"},
    "val d'isère":      {"lat": 45.4485, "lon": 6.9803, "country": "FR", "nearest_airport": "GVA"},
    "tignes":           {"lat": 45.4685, "lon": 6.9061, "country": "FR", "nearest_airport": "GVA"},
    "courchevel":       {"lat": 45.4154, "lon": 6.6347, "country": "FR", "nearest_airport": "GVA"},
    "meribel":          {"lat": 45.3961, "lon": 6.5654, "country": "FR", "nearest_airport": "GVA"},
    "méribel":          {"lat": 45.3961, "lon": 6.5654, "country": "FR", "nearest_airport": "GVA"},
    "la plagne":        {"lat": 45.5078, "lon": 6.6839, "country": "FR", "nearest_airport": "GVA"},
    "les arcs":         {"lat": 45.5731, "lon": 6.7950, "country": "FR", "nearest_airport": "GVA"},
    "morzine":          {"lat": 46.1791, "lon": 6.7068, "country": "FR", "nearest_airport": "GVA"},
    "avoriaz":          {"lat": 46.1933, "lon": 6.7636, "country": "FR", "nearest_airport": "GVA"},
    "les gets":         {"lat": 46.1601, "lon": 6.6695, "country": "FR", "nearest_airport": "GVA"},
    "zermatt":          {"lat": 46.0207, "lon": 7.7491, "country": "CH", "nearest_airport": "GVA"},
    "saas-fee":         {"lat": 46.1083, "lon": 7.9286, "country": "CH", "nearest_airport": "GVA"},
    "st anton":         {"lat": 47.1306, "lon": 10.2662, "country": "AT", "nearest_airport": "INN"},
    "st. anton":        {"lat": 47.1306, "lon": 10.2662, "country": "AT", "nearest_airport": "INN"},
    "saint anton":      {"lat": 47.1306, "lon": 10.2662, "country": "AT", "nearest_airport": "INN"},
}


def classify_region(lat: float, lon: float, query: str | None = None) -> str:
    """Return one of: paris-iledefrance, lille-nord, provence, cote-dazur,
    languedoc, pyrenees, bordeaux-aquitaine, loire, brittany, normandy,
    rhone-alps, alps-ski, switzerland, belgium-netherlands, generic-fr,
    generic-eu, uk."""
    if query:
        q = query.strip().lower()
        if q in SKI_RESORTS:
            return "alps-ski"
        for k in SKI_RESORTS:
            if k in q:
                return "alps-ski"

    # Ireland (both ROI + NI — same Irish Sea ferry routing logic). Must
    # come BEFORE the UK box, since Ireland sits inside it geographically.
    if 51.4 <= lat <= 55.5 and -10.8 <= lon <= -5.4:
        return "ireland"

    # UK shortcut (rare but plan_trip might still get a UK destination)
    if 49.5 <= lat <= 60.0 and -8.5 <= lon <= 2.0:
        return "uk"

    # Belgium / Netherlands / Luxembourg
    if 49.5 <= lat <= 53.5 and 2.5 <= lon <= 7.2:
        return "belgium-netherlands"

    # Switzerland
    if 45.8 <= lat <= 47.9 and 5.9 <= lon <= 10.5:
        # Most of Switzerland — but Alps overlap, leave that to the named-resort lookup
        return "switzerland"

    # France — split into regions
    if 41.0 <= lat <= 51.5 and -5.5 <= lon <= 9.6:
        # Nord (Lille area)
        if 50.0 <= lat <= 51.5 and 2.0 <= lon <= 4.5:
            return "lille-nord"
        # Île-de-France (Paris)
        if 48.4 <= lat <= 49.4 and 1.5 <= lon <= 3.3:
            return "paris-iledefrance"
        # Côte d'Azur
        if 43.0 <= lat <= 44.0 and 6.0 <= lon <= 7.8:
            return "cote-dazur"
        # Provence (Avignon, Aix, Marseille area but inland)
        if 43.3 <= lat <= 44.5 and 4.0 <= lon <= 6.2:
            return "provence"
        # Languedoc (Montpellier, Nîmes)
        if 42.5 <= lat <= 44.0 and 2.5 <= lon <= 4.5:
            return "languedoc"
        # Pyrenees + far SW
        if 42.3 <= lat <= 43.7 and -1.8 <= lon <= 2.7:
            return "pyrenees"
        # Bordeaux / SW Aquitaine
        if 43.7 <= lat <= 45.5 and -1.5 <= lon <= 1.0:
            return "bordeaux-aquitaine"
        # Loire valley (Tours, Nantes inland)
        if 46.5 <= lat <= 48.2 and -2.5 <= lon <= 2.5:
            return "loire"
        # Brittany
        if 47.0 <= lat <= 49.0 and -5.5 <= lon <= -1.0:
            return "brittany"
        # Normandy
        if 48.5 <= lat <= 50.0 and -2.0 <= lon <= 2.5:
            return "normandy"
        # Rhône-Alpes (Lyon, Grenoble — but watch for ski resort named-lookup)
        if 44.5 <= lat <= 46.5 and 4.0 <= lon <= 7.5:
            return "rhone-alps"
        return "generic-fr"

    return "generic-eu"


# Mode-set selection per region.
# Modes: 'eurostar', 'flight', 'eurotunnel', 'fly_geneva_drive'
REGION_MODES: dict[str, list[str]] = {
    "paris-iledefrance":      ["eurostar", "flight", "eurotunnel"],
    "lille-nord":             ["eurostar", "eurotunnel"],
    "provence":               ["eurostar", "flight", "eurotunnel"],
    "cote-dazur":             ["flight", "eurostar", "eurotunnel"],
    "languedoc":              ["flight", "eurostar", "eurotunnel"],
    "pyrenees":               ["flight", "eurotunnel"],
    "bordeaux-aquitaine":     ["flight", "eurotunnel"],
    "loire":                  ["eurotunnel", "flight", "eurostar"],
    "brittany":               ["eurotunnel", "flight"],
    "normandy":               ["eurotunnel", "flight"],
    "rhone-alps":             ["flight", "eurostar"],
    "alps-ski":               ["fly_geneva_drive", "eurostar", "eurotunnel", "north_sea_ferry"],
    "switzerland":            ["flight", "eurostar", "eurotunnel", "north_sea_ferry"],
    "belgium-netherlands":    ["eurostar", "flight", "north_sea_ferry"],
    "ireland":                ["irish_sea_ferry", "flight"],
    "generic-fr":             ["eurotunnel", "flight", "eurostar"],
    "generic-eu":             ["flight", "north_sea_ferry"],
    "uk":                     [],
}


# Default airport / carrier IATA codes per region (used for flight queries
# when plan_trip auto-picks a flight target).
REGION_AIRPORTS: dict[str, dict[str, list[str]]] = {
    "paris-iledefrance":      {"origin": ["LGW", "LHR"], "destination": ["CDG", "ORY"]},
    "cote-dazur":             {"origin": ["LGW", "LHR"], "destination": ["NCE"]},
    "provence":               {"origin": ["LGW", "LHR"], "destination": ["MRS", "AVN", "FNI", "MPL"]},
    "languedoc":              {"origin": ["LGW", "LHR"], "destination": ["MPL", "FNI", "MRS"]},
    "pyrenees":               {"origin": ["LGW", "LHR"], "destination": ["TLS", "PUF"]},
    "bordeaux-aquitaine":     {"origin": ["LGW", "LHR"], "destination": ["BOD"]},
    "loire":                  {"origin": ["LGW", "LHR"], "destination": ["NTE"]},
    "brittany":               {"origin": ["LGW", "LHR"], "destination": ["NTE", "RNS"]},
    "normandy":               {"origin": ["LGW", "LHR"], "destination": ["DOL", "CFR"]},
    "rhone-alps":             {"origin": ["LGW", "LHR"], "destination": ["LYS"]},
    "alps-ski":               {"origin": ["LGW", "LHR"], "destination": ["GVA"]},
    "switzerland":            {"origin": ["LGW", "LHR"], "destination": ["GVA", "ZRH", "BSL"]},
    "belgium-netherlands":    {"origin": ["LGW", "LHR"], "destination": ["BRU", "AMS"]},
    "ireland":                {"origin": ["LGW", "LHR", "STN"], "destination": ["DUB", "ORK", "SNN", "BFS"]},
}


# Default Eurostar destination city slug per region.
REGION_EUROSTAR: dict[str, str] = {
    "paris-iledefrance":      "paris",
    "lille-nord":             "lille",
    "provence":               "avignon",     # seasonal direct May-Sep; otherwise Lille→TGV
    "cote-dazur":             "marseille",   # transfer to TER coast train
    "languedoc":              "marseille",
    "loire":                  "paris",       # transfer at Gare du Nord → Gare Montparnasse
    "rhone-alps":             "paris",
    "alps-ski":               "bourg-saint-maurice",
    "switzerland":            "paris",
    "belgium-netherlands":    "brussels",
    "generic-fr":             "paris",
}


# --- Origin classification (Stage 5a) ----------------------------------
# Mode availability depends on where you're starting from, not just where
# you're going. plan_trip historically assumed a UK origin and therefore
# offered Eurostar / Eurotunnel / Irish-sea / North-sea ferries
# unconditionally. With explicit non-UK origins (e.g. Zermatt → Tromsø),
# those modes need to be gated out.

# Coarse origin regions, named for what they unlock — not for geographic
# precision. Only the distinctions that change mode availability matter.
def classify_origin_region(
    country_code: str | None,
    lat: float | None = None,
    lon: float | None = None,
) -> str:
    """Return one of: 'uk', 'ireland', 'continental-eu', 'nordic', 'other'.

    Country code is the cheap path; lat/lon fallback only kicks in when
    forward_geocode didn't return a country_code (rare).
    """
    if country_code:
        cc = country_code.upper()
        if cc == "GB":
            return "uk"
        if cc == "IE":
            return "ireland"
        if cc in {"NO", "SE", "DK", "FI", "IS"}:
            return "nordic"
        if cc in {
            "FR", "BE", "NL", "LU", "DE", "CH", "AT", "IT", "ES", "PT",
            "PL", "CZ", "HU", "SK", "SI", "HR", "EE", "LV", "LT", "RO", "BG",
            "GR", "MT", "CY", "MC", "AD", "LI", "SM", "VA",
        }:
            return "continental-eu"
        return "other"
    # Lat/lon fallback (loose boxes — only used when country_code missing)
    if lat is not None and lon is not None:
        if 49.5 <= lat <= 60.0 and -8.5 <= lon <= 2.0:
            return "uk"
        if 51.4 <= lat <= 55.5 and -10.8 <= lon <= -5.4:
            return "ireland"
        if 54.0 <= lat <= 71.5 and 4.0 <= lon <= 31.0:
            return "nordic"
        if 35.0 <= lat <= 60.0 and -10.0 <= lon <= 30.0:
            return "continental-eu"
    return "other"


# Modes that are only meaningful from specific origin regions. Anything
# not in this map is available from every origin (flight,
# fly_geneva_drive, multiday-drive).
ORIGIN_GATED_MODES: dict[str, set[str]] = {
    "eurostar":         {"uk"},
    "eurotunnel":       {"uk"},
    "north_sea_ferry":  {"uk"},
    "irish_sea_ferry":  {"uk", "ireland"},
}


def filter_modes_by_origin(modes: list[str], origin_region: str) -> list[str]:
    """Drop modes that don't make sense from this origin region.

    Empty result is possible (e.g. a Norwegian origin with a destination
    region whose only modes are eurostar + eurotunnel) — the caller
    should fall back to flight in that case.
    """
    return [
        m for m in modes
        if m not in ORIGIN_GATED_MODES or origin_region in ORIGIN_GATED_MODES[m]
    ]


def score(option: dict[str, Any]) -> float:
    """Lower is better. Door-to-door minutes plus 60 min penalty per transfer."""
    if not option.get("ok"):
        return 99_999.0
    minutes = option.get("door_to_door_minutes", 99_999)
    transfers = option.get("transfers", 0)
    return minutes + 60 * transfers


def confidence(option: dict[str, Any]) -> str:
    """high/medium/low based on data sources used in the option."""
    sources = set(option.get("data_sources", []))
    if "duffel-live" in sources or "duffel-test" in sources:
        return "high" if "google-maps" in sources else "medium"
    if "google-maps" in sources and "static-timetable" in sources:
        return "medium"
    return "low"
