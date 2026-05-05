"""Curated list of luxury-affiliation hotels (Relais & Châteaux, LHW, SLH).

Why this exists: LiteAPI's chain field doesn't track these affiliations, and
the relevant boutique properties are largely absent from OTA inventory pipes.
For affiliation-specific queries ("R&C on the Calais → Tasch route") we
maintain a curated list and validate drive times via Google Maps.

Maintenance: annual review against affiliation member directories. Adding
a new entry needs only a name + city + country + (lat, lon) + tag set.
The url field is generated from a search-URL pattern so we don't have to
chase per-hotel slugs.

Affiliations:
  RC   = Relais & Châteaux
  LHW  = The Leading Hotels of the World
  SLH  = Small Luxury Hotels of the World
  CHC  = Châteaux & Hôtels Collection (Les Collectionneurs)
A single property can carry multiple tags.
"""

from typing import Any
from urllib.parse import quote


# Format: (name, city, country, lat, lon, tags, optional_known_url)
_RAW: list[tuple[str, str, str, float, float, list[str], str | None]] = [
    # France — Champagne
    ("L'Assiette Champenoise",        "Tinqueux",            "FR", 49.2528,  3.9963, ["RC","LHW"], None),
    ("Domaine Les Crayères",          "Reims",               "FR", 49.2453,  4.0492, ["RC"], None),
    ("Royal Champagne Hotel & Spa",   "Champillon",          "FR", 49.1011,  3.9650, ["RC","LHW"], None),

    # France — Burgundy
    ("Hostellerie de Levernois",      "Levernois",           "FR", 47.0036,  4.8839, ["RC"], None),
    ("Hôtel Le Cep",                  "Beaune",              "FR", 47.0234,  4.8392, ["RC"], None),
    ("Le Relais Bernard Loiseau",     "Saulieu",             "FR", 47.2814,  4.2306, ["RC"], None),
    ("Maison Lameloise",              "Chagny",              "FR", 46.9089,  4.7507, ["RC"], None),
    ("Château de Vault-de-Lugny",     "Avallon",             "FR", 47.5083,  3.8439, ["RC"], None),

    # France — Alsace
    ("Domaine Lalique",               "Wingen-sur-Moder",    "FR", 48.9119,  7.3631, ["RC"], None),
    ("Château d'Isenbourg & Spa",     "Rouffach",            "FR", 47.9544,  7.2964, ["RC"], None),
    ("Hostellerie La Cheneaudière",   "Colroy-la-Roche",     "FR", 48.4005,  7.2167, ["RC"], None),
    ("Hôtel & Spa Le Chambard",       "Kaysersberg",         "FR", 48.1389,  7.2686, ["RC"], None),
    ("Auberge de l'Ill",              "Illhaeusern",         "FR", 48.1819,  7.4308, ["RC"], None),

    # France — Normandy
    ("Le Manoir des Impressionnistes","Honfleur",            "FR", 49.4222,  0.2353, ["RC"], None),
    ("Château d'Audrieu",             "Audrieu",             "FR", 49.2389, -0.6275, ["RC"], None),

    # France — Brittany / Loire
    ("Castel Marie-Louise",           "La Baule",            "FR", 47.2867, -2.4083, ["RC"], None),
    ("Castel Clara",                  "Belle-Île-en-Mer",    "FR", 47.3408, -3.1683, ["RC"], None),
    ("Domaine de Rochevilaine",       "Billiers",            "FR", 47.5025, -2.5511, ["RC"], None),

    # France — Provence / Côte d'Azur
    ("Auberge de Cassagne",           "Le Pontet",           "FR", 43.9758,  4.8739, ["RC"], None),
    ("L'Oustau de Baumanière",        "Les Baux-de-Provence","FR", 43.7456,  4.7942, ["RC"], None),
    ("La Bastide de Gordes",          "Gordes",              "FR", 43.9117,  5.2008, ["RC"], None),
    ("Coquillade Provence",           "Gargas",              "FR", 43.8750,  5.3431, ["RC"], None),
    ("Hôtel du Castellet",            "Le Castellet",        "FR", 43.2025,  5.7692, ["RC"], None),
    ("Le Mas de Pierre",              "Saint-Paul-de-Vence", "FR", 43.6961,  7.1286, ["RC","LHW"], None),
    ("Château Saint-Martin & Spa",    "Vence",               "FR", 43.7314,  7.0975, ["RC"], None),

    # France — Bordeaux / SW
    ("Hostellerie de Plaisance",      "Saint-Émilion",       "FR", 44.8939, -0.1567, ["RC"], None),
    ("Château Cordeillan-Bages",      "Pauillac",            "FR", 45.1850, -0.7475, ["RC"], None),
    ("Les Sources de Caudalie",       "Martillac",           "FR", 44.7378, -0.5547, ["RC","SLH"], None),
    ("Château de la Treyne",          "Lacave",              "FR", 44.8333,  1.5500, ["RC"], None),

    # France — Alps
    ("Le Mont d'Arbois",              "Megève",              "FR", 45.8569,  6.6086, ["RC"], None),
    ("La Bouitte",                    "Saint-Martin-de-Belleville", "FR", 45.3917, 6.5233, ["RC"], None),

    # Switzerland
    ("Park Gstaad",                   "Gstaad",              "CH", 46.4736,  7.2864, ["RC","LHW"], None),
    ("Beau-Rivage Palace",            "Lausanne",            "CH", 46.5067,  6.6364, ["RC","LHW"], None),
    ("Le Mirador Resort & Spa",       "Le Mont-Pèlerin",     "CH", 46.4683,  6.8633, ["RC"], None),
    ("Park Hotel Vitznau",            "Vitznau",             "CH", 47.0094,  8.4844, ["RC"], None),
    ("Castello del Sole",             "Ascona",              "CH", 46.1497,  8.7486, ["RC"], None),

    # Italy — northern (likely on Stu's broader European routes)
    ("Castello di Casole",            "Casole d'Elsa",       "IT", 43.3458, 11.0286, ["RC","LHW"], None),
    ("Villa La Massa",                "Bagno a Ripoli",      "IT", 43.7517, 11.3069, ["RC","LHW"], None),
    ("Castel Fragsburg",              "Merano",              "IT", 46.6781, 11.1872, ["RC"], None),

    # Spain — Rioja (notable, on a broader French/Iberian itinerary)
    ("Hotel Marqués de Riscal",       "Elciego",             "ES", 42.5119, -2.6133, ["RC","LHW"], None),
]


# Stable derived dataclass-like dicts
HOTELS: list[dict[str, Any]] = [
    {
        "name": name,
        "city": city,
        "country": country,
        "lat": lat,
        "lon": lon,
        "tags": tags,
        "url": url or f"https://www.relaischateaux.com/us/search?qs={quote(name)}",
    }
    for (name, city, country, lat, lon, tags, url) in _RAW
]


VALID_TAGS = {"RC", "LHW", "SLH", "CHC"}


def filter_by(
    affiliation: str | None = None,
    countries: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return curated entries optionally filtered by affiliation tag and country."""
    out = HOTELS
    if affiliation:
        tag = affiliation.upper()
        out = [h for h in out if tag in h["tags"]]
    if countries:
        ccs = [c.upper() for c in countries]
        out = [h for h in out if h["country"] in ccs]
    return out
