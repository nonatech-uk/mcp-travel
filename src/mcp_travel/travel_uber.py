"""Uber Rides API — price estimates + ETA for a pickup point.

Auth: OAuth 2.0 client_credentials grant. Modern Uber developer apps
(2024+) only issue Client ID / Client Secret pairs — Server Token is
deprecated. We exchange these for a bearer access_token at
`https://login.uber.com/oauth/v2/token`, cache it (TTL ~30 days), and
use it as `Authorization: Bearer <token>` on the v1.2 endpoints.

Endpoints used:
  GET /v1.2/estimates/price  — per-product price ranges between two points
  GET /v1.2/estimates/time   — per-product ETA-to-pickup at start point

Geographic note: Uber coverage is patchy outside major cities. UK
airports + cities = full. European capitals = partial. Rural / smaller
European cities = often empty product list. The /price endpoint
returns prices=[] gracefully in those cases.

Env: UBER_CLIENT_ID, UBER_CLIENT_SECRET (both required for live data).
"""

import os
import time
from typing import Any

import httpx

UBER_BASE = "https://api.uber.com/v1.2"
UBER_OAUTH = "https://login.uber.com/oauth/v2/token"

_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0}
_TOKEN_REFRESH_EARLY = 5 * 60   # refresh 5 min before nominal expiry


class UberError(RuntimeError):
    pass


async def _get_access_token(client: httpx.AsyncClient) -> str:
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > now:
        return _TOKEN_CACHE["token"]

    cid = os.environ.get("UBER_CLIENT_ID")
    cs = os.environ.get("UBER_CLIENT_SECRET")
    if not cid or not cs:
        raise UberError("UBER_CLIENT_ID / UBER_CLIENT_SECRET not set")

    resp = await client.post(
        UBER_OAUTH,
        data={
            "client_id": cid,
            "client_secret": cs,
            "grant_type": "client_credentials",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise UberError(f"uber oauth {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise UberError(f"uber oauth: no access_token in response {data}")
    expires_in = int(data.get("expires_in") or 2_592_000)   # default 30 days
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + max(60, expires_in - _TOKEN_REFRESH_EARLY)
    return token


async def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    token = await _get_access_token(client)
    return {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "en_GB",
    }


async def price_estimates(
    client: httpx.AsyncClient,
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float,
) -> list[dict[str, Any]]:
    resp = await client.get(
        f"{UBER_BASE}/estimates/price",
        params={
            "start_latitude": start_lat,
            "start_longitude": start_lon,
            "end_latitude": end_lat,
            "end_longitude": end_lon,
        },
        headers=await _headers(client),
        timeout=20.0,
    )
    if resp.status_code == 401:
        # token may have expired mid-flight — clear and retry once
        _TOKEN_CACHE["token"] = None
        resp = await client.get(
            f"{UBER_BASE}/estimates/price",
            params={
                "start_latitude": start_lat,
                "start_longitude": start_lon,
                "end_latitude": end_lat,
                "end_longitude": end_lon,
            },
            headers=await _headers(client),
            timeout=20.0,
        )
    if resp.status_code >= 400:
        raise UberError(f"uber /estimates/price {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("prices") or []


async def time_estimates(
    client: httpx.AsyncClient,
    start_lat: float, start_lon: float,
) -> list[dict[str, Any]]:
    resp = await client.get(
        f"{UBER_BASE}/estimates/time",
        params={
            "start_latitude": start_lat,
            "start_longitude": start_lon,
        },
        headers=await _headers(client),
        timeout=20.0,
    )
    if resp.status_code == 401:
        _TOKEN_CACHE["token"] = None
        resp = await client.get(
            f"{UBER_BASE}/estimates/time",
            params={"start_latitude": start_lat, "start_longitude": start_lon},
            headers=await _headers(client),
            timeout=20.0,
        )
    if resp.status_code >= 400:
        raise UberError(f"uber /estimates/time {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("times") or []


def _deeplink(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> str:
    """One-click URL to Uber's app/web with pickup + dropoff prefilled.
    Works without auth — opens the price-estimate page or the app."""
    return (
        "https://m.uber.com/ul/?action=setPickup"
        f"&pickup[latitude]={start_lat}&pickup[longitude]={start_lon}"
        f"&dropoff[latitude]={end_lat}&dropoff[longitude]={end_lon}"
    )
