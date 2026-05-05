"""Forward geocoding via Nominatim, with Postgres-backed cache.

Usage policy: max 1 req/sec, must send a unique User-Agent. We sleep 1.1s
after every live call. Cache hits return instantly. Locations don't
materially change, so cache entries have no TTL — re-resolve only on
miss or on explicit invalidation.

Resolution order (forward_geocode):
  1. Stu's `mylocation.place` named-places (hotels, homes, family,
     restaurants — 58 entries as of 2026-05-03, ILIKE match on name +
     notes). Returns instantly with the curated lat/lon Stu trusts.
  2. travel.geocode_cache (prior Nominatim hits, 30-day implicit TTL).
  3. Live Nominatim call, with 1.1s sleep + UA.

This is a helper, not an exposed MCP tool. `plan_trip` (Phase 5) uses it
to classify destinations into regions (lat/lon bands).
"""

import asyncio
import os
from typing import Any

import asyncpg
import httpx

NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
_LIVE_CALL_DELAY = 1.1


def _normalise(query: str) -> str:
    return " ".join(query.lower().strip().split())


# Generic English locality descriptors that confuse Nominatim's tokeniser
# when appended to a non-English place name. Real-world failure: "Zürich
# city centre" returned a building called "Zurich" on Norfolk Street in
# Manchester city centre because OSM has many nodes literally named
# "city centre" and the tokeniser scored that match higher than the
# proper Swiss city. Stripping the descriptor before the live call
# reliably returns Zürich the city. Only stripped if the descriptor sits
# at the END of the query — "Old Town Prague" is a real POI and stays
# as-is.
_GENERIC_LOCALITY_SUFFIXES = (
    "city centre", "city center",
    "town centre", "town center",
    "downtown",
    "high street",
    "old town",
)


def _strip_generic_locality(query: str) -> str:
    q = query.strip()
    low = q.lower()
    for suffix in _GENERIC_LOCALITY_SUFFIXES:
        for sep in (" " + suffix, ", " + suffix, "," + suffix):
            if low.endswith(sep):
                stripped = q[: len(q) - len(sep)].rstrip(", ").strip()
                if stripped:
                    return stripped
    return q


def _select_best_geocode(items: list[dict]) -> dict | None:
    """Pick the best Nominatim result from a multi-result response.

    Defense-in-depth for the same Zürich/Manchester problem: if the top
    hit is a POI/building (place_rank ≥ 26 = street or smaller), look
    for a city-scale alternative (place_rank ≤ 22 = suburb or larger).
    Only override if the city-scale alternative is in a *different*
    country — same-country POI matches like "Hotel Krone Zermatt" stay
    valid (the hotel and the city are both in CH).
    """
    if not items:
        return None
    top = items[0]
    if int(top.get("place_rank", 99)) <= 22:
        return top
    top_cc = (top.get("address") or {}).get("country_code")
    for it in items[1:]:
        if int(it.get("place_rank", 99)) <= 22:
            it_cc = (it.get("address") or {}).get("country_code")
            if it_cc and it_cc != top_cc:
                return it
    return top


async def lookup_named_place(
    pool_loc: asyncpg.Pool, query: str
) -> dict[str, Any] | None:
    """Look the query up in mylocation.place. ILIKE match on name + notes,
    with exact-match preference. Returns lat/lon/display_name/place_type
    or None.
    """
    if not pool_loc:
        return None
    norm = query.strip()
    if not norm:
        return None
    pat = f"%{norm}%"
    sql = """
        SELECT p.name, p.lat, p.lon, p.notes, pt.name AS place_type
          FROM place p
          JOIN place_type pt ON p.place_type_id = pt.id
         WHERE p.name ILIKE $1 OR p.notes ILIKE $1
         ORDER BY (lower(p.name) = lower($2))::int DESC,
                  (lower(p.name) LIKE lower($2) || '%')::int DESC,
                  length(p.name) ASC
         LIMIT 1
    """
    try:
        async with pool_loc.acquire() as conn:
            row = await conn.fetchrow(sql, pat, norm)
    except Exception:
        return None
    if not row:
        return None
    return {
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "display_name": row["name"],
        "country_code": None,    # mylocation doesn't store country
        "source": f"mylocation:place ({row['place_type']})",
        "notes": row["notes"],
    }


async def forward_geocode(
    client: httpx.AsyncClient, pool: asyncpg.Pool, query: str,
    pool_locations: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    """Resolve free-text query to {lat, lon, display_name, country_code} or None.

    Resolution order: mylocation.place → travel.geocode_cache → Nominatim.
    """
    # 1. Named-places lookup first (instant, free, curated)
    if pool_locations is not None:
        named = await lookup_named_place(pool_locations, query)
        if named:
            return named

    norm = _normalise(query)
    if not norm:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT lat, lon, display_name, country_code FROM geocode_cache WHERE query_norm = $1",
            norm,
        )
        if row:
            return {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "display_name": row["display_name"],
                "country_code": row["country_code"],
            }

    ua = os.environ.get(
        "NOMINATIM_USER_AGENT",
        f"mcp-travel ({os.environ.get('MCP_TRAVEL_CONTACT', 'mcp-travel@example.com')})",
    )
    # Strip generic English locality suffixes ("Zürich city centre"
    # → "Zürich") before the live call. Cache key stays the original
    # normalised query so identical inputs always hit cache.
    nominatim_q = _strip_generic_locality(query)
    resp = await client.get(
        NOMINATIM_BASE,
        params={"q": nominatim_q, "format": "json", "limit": 5, "addressdetails": 1},
        headers={"User-Agent": ua, "Accept-Language": "en"},
        timeout=15.0,
    )
    await asyncio.sleep(_LIVE_CALL_DELAY)
    if resp.status_code >= 400:
        return None

    items = resp.json()
    if not items:
        return None

    top = _select_best_geocode(items)
    if top is None:
        return None
    lat = float(top["lat"])
    lon = float(top["lon"])
    display = top.get("display_name", query)
    cc = (top.get("address") or {}).get("country_code")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO geocode_cache (query_norm, lat, lon, display_name, country_code)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (query_norm) DO UPDATE
              SET lat = EXCLUDED.lat,
                  lon = EXCLUDED.lon,
                  display_name = EXCLUDED.display_name,
                  country_code = EXCLUDED.country_code,
                  created_at = now()
            """,
            norm,
            lat,
            lon,
            display,
            cc,
        )

    return {"lat": lat, "lon": lon, "display_name": display, "country_code": cc}
