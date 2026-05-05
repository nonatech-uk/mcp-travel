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


# --- Stage 5b: country / city → airport selection ---------------------
# REGION_AIRPORTS above is keyed by destination region from a UK origin
# (covers UK→FR, UK→CH, etc). For non-UK origins or non-UK destinations
# outside the curated regions, fall back to a country-level hub list +
# a small city → IATA override map for known regional airports.

AIRPORTS_BY_COUNTRY: dict[str, list[str]] = {
    # Western Europe
    "GB": ["LHR", "LGW", "STN", "MAN", "EDI", "GLA", "LPL", "BHX", "BRS"],
    "IE": ["DUB", "ORK", "SNN"],
    "FR": ["CDG", "ORY", "LYS", "MRS", "TLS", "BOD", "NTE", "NCE"],
    "BE": ["BRU", "CRL"],
    "NL": ["AMS", "EIN"],
    "LU": ["LUX"],
    "DE": ["FRA", "MUC", "BER", "DUS", "HAM", "STR"],
    "CH": ["ZRH", "GVA", "BSL"],
    "AT": ["VIE", "SZG", "INN"],
    "IT": ["FCO", "MXP", "BLQ", "VCE", "NAP"],
    "ES": ["MAD", "BCN", "AGP", "PMI", "VLC", "SVQ"],
    "PT": ["LIS", "OPO", "FAO"],
    # Nordic
    "NO": ["OSL", "BGO", "TRD", "TOS", "SVG"],
    "SE": ["ARN", "GOT", "MMX"],
    "DK": ["CPH", "BLL"],
    "FI": ["HEL", "TKU"],
    "IS": ["KEF"],
    # Eastern + South-east
    "PL": ["WAW", "KRK", "GDN"],
    "CZ": ["PRG"],
    "HU": ["BUD"],
    "GR": ["ATH", "SKG"],
    "RO": ["OTP"],
    "BG": ["SOF"],
    "HR": ["ZAG", "SPU", "DBV"],
    "SI": ["LJU"],
    "SK": ["BTS"],
    # Outside Europe (long-haul fallbacks)
    "US": ["JFK", "LAX", "ORD"],
    "AR": ["EZE"],
}

# City-name (lowercase substring) → IATA. Used to narrow the country
# list to the actual regional airport. Only well-known/named airports
# go here — generic searches fall back to AIRPORTS_BY_COUNTRY[0].
CITY_TO_IATA: dict[str, str] = {
    # Norway
    "tromso": "TOS", "tromsø": "TOS", "bergen": "BGO",
    "trondheim": "TRD", "stavanger": "SVG", "oslo": "OSL",
    # Sweden
    "stockholm": "ARN", "gothenburg": "GOT", "göteborg": "GOT",
    "malmo": "MMX", "malmö": "MMX",
    # Other Nordic
    "copenhagen": "CPH", "helsinki": "HEL", "reykjavik": "KEF",
    # Western
    "zurich": "ZRH", "zürich": "ZRH", "geneva": "GVA", "basel": "BSL",
    "munich": "MUC", "frankfurt": "FRA", "berlin": "BER",
    "hamburg": "HAM", "düsseldorf": "DUS", "dusseldorf": "DUS",
    "amsterdam": "AMS", "brussels": "BRU", "luxembourg": "LUX",
    "paris": "CDG", "lyon": "LYS", "marseille": "MRS",
    "toulouse": "TLS", "nice": "NCE", "bordeaux": "BOD", "nantes": "NTE",
    "vienna": "VIE", "salzburg": "SZG", "innsbruck": "INN",
    "milan": "MXP", "milano": "MXP",
    "como": "MXP", "lake como": "MXP", "lago di como": "MXP",
    "bergamo": "MXP", "lugano": "MXP",  # Lugano is CH but closer to MXP than ZRH
    "turin": "MXP", "torino": "MXP",
    "rome": "FCO", "roma": "FCO",
    "venice": "VCE", "venezia": "VCE",
    "naples": "NAP", "napoli": "NAP",
    "bologna": "BLQ", "florence": "FLR", "firenze": "FLR",
    "barcelona": "BCN", "madrid": "MAD", "malaga": "AGP",
    "palma": "PMI", "valencia": "VLC", "seville": "SVQ",
    "lisbon": "LIS", "porto": "OPO", "faro": "FAO",
    # Eastern
    "warsaw": "WAW", "krakow": "KRK", "kraków": "KRK",
    "prague": "PRG", "budapest": "BUD", "athens": "ATH",
    "thessaloniki": "SKG", "bucharest": "OTP", "sofia": "SOF",
    "zagreb": "ZAG", "split": "SPU", "dubrovnik": "DBV",
    # UK / Ireland
    "dublin": "DUB", "cork": "ORK", "london": "LGW",  # household uses LGW
    "manchester": "MAN", "edinburgh": "EDI", "glasgow": "GLA",
    "liverpool": "LPL", "birmingham": "BHX", "bristol": "BRS",
}


# Airports with a rail station — used by build_flight to compare a
# rail-to-airport leg against the drive ETA and pick whichever is
# faster. Only includes airports where the rail option is actually
# competitive *and* we have a journey planner for the country.
# `extra_min` covers any airport-shuttle leg (e.g. EAP at Basel is on
# French soil, no direct rail; Basel SBB station + 15-20min bus).
# Full geocoder-friendly names for each IATA — used by the drive-leg
# computation. Bare IATA codes ("MAN airport") confuse Google Maps:
# "MAN" gets matched to Manaus (Brazil) and the drive ETA comes back
# as a 93-hour cross-Atlantic absurdity. Always pass the full city +
# "Airport" + country.
AIRPORT_DRIVE_NAMES: dict[str, str] = {
    # UK
    "LHR": "Heathrow Airport, UK",
    "LGW": "Gatwick Airport, UK",
    "STN": "Stansted Airport, UK",
    "MAN": "Manchester Airport, UK",
    "LPL": "Liverpool John Lennon Airport, UK",
    "EDI": "Edinburgh Airport, UK",
    "GLA": "Glasgow Airport, UK",
    "BHX": "Birmingham Airport, UK",
    "BRS": "Bristol Airport, UK",
    # Switzerland
    "ZRH": "Zurich Airport, Switzerland",
    "GVA": "Geneva Airport, Switzerland",
    "BSL": "EuroAirport Basel-Mulhouse-Freiburg, France",
    # Italy
    "MXP": "Milan Malpensa Airport, Italy",
    "FCO": "Rome Fiumicino Airport, Italy",
    "BLQ": "Bologna Airport, Italy",
    "VCE": "Venice Marco Polo Airport, Italy",
    "NAP": "Naples Airport, Italy",
    # France
    "CDG": "Paris Charles de Gaulle Airport, France",
    "ORY": "Paris Orly Airport, France",
    "LYS": "Lyon Saint-Exupéry Airport, France",
    "MRS": "Marseille Provence Airport, France",
    "NCE": "Nice Côte d'Azur Airport, France",
    "TLS": "Toulouse Blagnac Airport, France",
    # Germany
    "FRA": "Frankfurt Airport, Germany",
    "MUC": "Munich Airport, Germany",
    "BER": "Berlin Brandenburg Airport, Germany",
    "DUS": "Düsseldorf Airport, Germany",
    "HAM": "Hamburg Airport, Germany",
    # Other
    "AMS": "Amsterdam Schiphol Airport, Netherlands",
    "BRU": "Brussels Airport, Belgium",
    "VIE": "Vienna Airport, Austria",
    "MAD": "Madrid Barajas Airport, Spain",
    "BCN": "Barcelona El Prat Airport, Spain",
    "LIS": "Lisbon Airport, Portugal",
    "DUB": "Dublin Airport, Ireland",
    # Nordic
    "OSL": "Oslo Gardermoen Airport, Norway",
    "BGO": "Bergen Airport, Norway",
    "TOS": "Tromsø Airport, Norway",
    "ARN": "Stockholm Arlanda Airport, Sweden",
    "CPH": "Copenhagen Airport, Denmark",
    "HEL": "Helsinki Airport, Finland",
    # Eastern EU
    "WAW": "Warsaw Chopin Airport, Poland",
    "PRG": "Prague Airport, Czech Republic",
    "BUD": "Budapest Airport, Hungary",
    "ATH": "Athens International Airport, Greece",
}


def airport_drive_target(iata: str) -> str:
    """Return a Google-Maps-safe full-name for an airport IATA, falling
    back to bare-IATA-suffix if not in the table."""
    return AIRPORT_DRIVE_NAMES.get(iata.upper(), f"{iata} airport")


AIRPORT_RAIL_STATIONS: dict[str, dict[str, Any]] = {
    # UK
    "LGW": {"station": "Gatwick Airport",          "country": "GB", "extra_min": 0},
    "MAN": {"station": "Manchester Airport",       "country": "GB", "extra_min": 0},
    # LPL has no on-airport rail station; closest is Liverpool South
    # Parkway + the 500 bus (~10 min, runs every 12 min). 15-min shuttle
    # allowance covers walk + wait + bus.
    "LPL": {"station": "Liverpool South Parkway",  "country": "GB", "extra_min": 15},
    # Switzerland
    "ZRH": {"station": "Zürich Flughafen",         "country": "CH", "extra_min": 0},
    "GVA": {"station": "Genève-Aéroport",          "country": "CH", "extra_min": 0},
    "BSL": {"station": "Basel SBB",                "country": "CH", "extra_min": 20},
    # Italy
    "MXP": {"station": "Milano Malpensa Aeroporto","country": "IT", "extra_min": 0},
}


# Airport coordinates (lat, lon) for the airports in AIRPORTS_BY_COUNTRY.
# Used by pick_airport_nearest to choose the closest airport when the
# destination/origin doesn't match a known city in CITY_TO_IATA — fixes
# cases like "Koppangen, Lyngen, Norway" where the country-list default
# (OSL = Oslo Gardermoen, 1670km drive) is absurd vs the actual nearest
# airport (TOS = Tromsø, ~120km).
AIRPORT_COORDINATES: dict[str, tuple[float, float]] = {
    # UK
    "LHR": (51.4700, -0.4543),  "LGW": (51.1481, -0.1903),
    "STN": (51.8860,  0.2389),  "MAN": (53.3537, -2.2750),
    "LPL": (53.3336, -2.8497),  "EDI": (55.9500, -3.3725),
    "GLA": (55.8721, -4.4331),  "BHX": (52.4539, -1.7480),
    "BRS": (51.3827, -2.7191),
    # Ireland
    "DUB": (53.4213, -6.2701),  "ORK": (51.8413, -8.4911),
    "SNN": (52.7019, -8.9248),
    # Switzerland
    "ZRH": (47.4647,  8.5492),  "GVA": (46.2381,  6.1090),
    "BSL": (47.5896,  7.5298),
    # Italy
    "FCO": (41.8003, 12.2389),  "MXP": (45.6306,  8.7281),
    "BLQ": (44.5354, 11.2887),  "VCE": (45.5053, 12.3519),
    "NAP": (40.8861, 14.2908),  "FLR": (43.8100, 11.2050),
    # France
    "CDG": (49.0097,  2.5479),  "ORY": (48.7233,  2.3794),
    "LYS": (45.7256,  5.0811),  "MRS": (43.4393,  5.2214),
    "NCE": (43.6584,  7.2158),  "TLS": (43.6291,  1.3636),
    "BOD": (44.8283, -0.7156),  "NTE": (47.1532, -1.6107),
    # Germany
    "FRA": (50.0379,  8.5622),  "MUC": (48.3538, 11.7861),
    "BER": (52.3667, 13.5033),  "DUS": (51.2895,  6.7668),
    "HAM": (53.6304,  9.9882),  "STR": (48.6899,  9.2220),
    # Other Western Europe
    "AMS": (52.3105,  4.7683),  "EIN": (51.4583,  5.3922),
    "BRU": (50.9014,  4.4844),  "CRL": (50.4592,  4.4538),
    "LUX": (49.6233,  6.2044),  "VIE": (48.1103, 16.5697),
    "SZG": (47.7933, 13.0043),  "INN": (47.2602, 11.3439),
    # Iberia
    "MAD": (40.4983, -3.5676),  "BCN": (41.2974,  2.0833),
    "AGP": (36.6749, -4.4991),  "PMI": (39.5517,  2.7388),
    "VLC": (39.4893, -0.4816),  "SVQ": (37.4180, -5.8931),
    "LIS": (38.7813, -9.1359),  "OPO": (41.2481, -8.6814),
    "FAO": (37.0144, -7.9659),
    # Nordic
    "OSL": (60.1939, 11.1004),  "BGO": (60.2934,  5.2181),
    "TRD": (63.4578, 10.9239),  "TOS": (69.6833, 18.9189),
    "SVG": (58.8767,  5.6378),  "ARN": (59.6519, 17.9186),
    "GOT": (57.6628, 12.2798),  "MMX": (55.5364, 13.3762),
    "CPH": (55.6181, 12.6561),  "BLL": (55.7400,  9.1518),
    "HEL": (60.3172, 24.9633),  "TKU": (60.5141, 22.2628),
    "KEF": (63.9850, -22.6056),
    # Eastern + South-east
    "WAW": (52.1657, 20.9671),  "KRK": (50.0777, 19.7848),
    "GDN": (54.3776, 18.4662),  "PRG": (50.1008, 14.2632),
    "BUD": (47.4369, 19.2611),  "ATH": (37.9364, 23.9444),
    "SKG": (40.5197, 22.9709),  "OTP": (44.5711, 26.0850),
    "SOF": (42.6951, 23.4114),  "ZAG": (45.7429, 16.0688),
    "SPU": (43.5389, 16.2980),  "DBV": (42.5614, 18.2683),
    "LJU": (46.2237, 14.4576),  "BTS": (48.1702, 17.2127),
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in km."""
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def pick_airport_nearest(
    lat: float, lon: float,
    country_code: str | None = None,
) -> str | None:
    """Return the airport (from AIRPORTS_BY_COUNTRY[country_code], or
    globally if no country given) whose coords are closest to (lat, lon).
    None if no candidates have known coords."""
    if country_code:
        candidates = AIRPORTS_BY_COUNTRY.get(country_code.upper()) or []
    else:
        candidates = list(AIRPORT_COORDINATES.keys())
    best: tuple[str, float] | None = None
    for iata in candidates:
        coords = AIRPORT_COORDINATES.get(iata)
        if not coords:
            continue
        d = _haversine_km(lat, lon, coords[0], coords[1])
        if best is None or d < best[1]:
            best = (iata, d)
    return best[0] if best else None


def pick_airport_for_city(hint_text: str | None) -> str | None:
    """City-hint-only pick — no country fallback. Use this when you
    want to override an existing airport choice ONLY when a known city
    name appears in the hint text (e.g. UK origin defaulting to LGW
    via REGION_AIRPORTS, but switching to MAN for "Manchester, UK")."""
    if not hint_text:
        return None
    h = hint_text.strip().lower()
    for city, iata in CITY_TO_IATA.items():
        if city in h:
            return iata
    return None


def pick_airport_by_country(
    country_code: str | None,
    hint_text: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    """Pick an airport IATA. Precedence:
      1. City-name hint (CITY_TO_IATA — explicit user intent wins)
      2. Geographic distance from (lat, lon) within the country
         (handles small places like 'Koppangen, Lyngen' that aren't
         in CITY_TO_IATA — picks TOS not OSL)
      3. Country list[0] fallback (the national hub)
    Returns None if no mapping exists at any level.
    """
    hinted = pick_airport_for_city(hint_text)
    if hinted:
        return hinted
    if lat is not None and lon is not None:
        nearest = pick_airport_nearest(lat, lon, country_code)
        if nearest:
            return nearest
    if country_code:
        airports = AIRPORTS_BY_COUNTRY.get(country_code.upper())
        if airports:
            return airports[0]
    return None


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
