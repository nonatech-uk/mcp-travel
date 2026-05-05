"""LiteAPI client for hotel discovery + live rates.

Picked over Amadeus Self-Service after Amadeus announced its self-serve
portal will be decommissioned 2026-07-17. LiteAPI is self-serve, no
sales call, no minimum spend — they charge commission on bookings, not
per search.

Auth: X-API-Key header. Sandbox keys hit the same hostname as production
(api.liteapi.travel), the key itself dictates which inventory you get.

Endpoints used:
  GET  /v3.0/data/hotels    — radius/city search → hotel content + IDs
  POST /v3.0/hotels/rates   — live rates for given hotel IDs + dates

Pricing: search is free; bookings would be paid via LiteAPI's commission
flow (which we don't use — we deeplink instead).
"""

import os
from typing import Any

import httpx

LITEAPI_BASE = "https://api.liteapi.travel/v3.0"


class LiteAPIError(RuntimeError):
    pass


def _api_key() -> str:
    k = os.environ.get("LITEAPI_API_KEY")
    if not k:
        raise LiteAPIError("LITEAPI_API_KEY is not set")
    return k


def _headers() -> dict[str, str]:
    return {"X-API-Key": _api_key(), "accept": "application/json"}


async def hotels_by_geocode(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    radius_km: int = 20,
    star_min: int | None = None,
    country_code: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Radius hotel search. Returns hotel content records (no rates yet)."""
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "distance": radius_km * 1000,   # metres in v3
        "limit": limit,
    }
    if country_code:
        params["countryCode"] = country_code
    if star_min:
        # LiteAPI supports starRating with multiple values via repeated param
        params["starRating"] = star_min   # singular — minimum
    resp = await client.get(
        f"{LITEAPI_BASE}/data/hotels",
        params=params,
        headers=_headers(),
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise LiteAPIError(f"liteapi /data/hotels {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    return payload.get("data") or []


async def hotel_rates(
    client: httpx.AsyncClient,
    hotel_ids: list[str],
    check_in: str,
    check_out: str,
    adults: int = 2,
    currency: str = "GBP",
    nationality: str = "GB",
) -> list[dict[str, Any]]:
    """Live rates for hotel IDs. POST body in v3 schema."""
    if not hotel_ids:
        return []
    body = {
        "hotelIds": hotel_ids,
        "checkin": check_in,
        "checkout": check_out,
        "occupancies": [{"adults": adults}],
        "currency": currency,
        "guestNationality": nationality,
    }
    resp = await client.post(
        f"{LITEAPI_BASE}/hotels/rates",
        json=body,
        headers={**_headers(), "Content-Type": "application/json"},
        timeout=45.0,
    )
    if resp.status_code >= 400:
        raise LiteAPIError(f"liteapi /hotels/rates {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    return payload.get("data") or []
