"""Google Maps Routes API client for traffic-aware drive times.

Endpoint: POST https://routes.googleapis.com/directions/v2:computeRoutes
Auth:     X-Goog-Api-Key header (key restricted to NAS outbound IP).
Field mask kept to Basic tier (duration / staticDuration / distanceMeters)
to stay at $5/1000 calls instead of jumping to the Advanced tier.

Traffic-aware mode (`TRAFFIC_AWARE`) is fast and cheap; `TRAFFIC_AWARE_OPTIMAL`
is slower (~10× latency, more expensive) but more accurate for big trips.
For door-to-door planning queries 'aware' is plenty.

`departure_time` must be in the future for traffic-aware ETAs to apply
(Google's documented behaviour). We pass it as RFC3339 with 'Z' suffix.
"""

import os
from datetime import datetime, timezone
from typing import Any

import httpx

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"


class DriveError(RuntimeError):
    pass


def _api_key() -> str:
    k = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not k:
        raise DriveError("GOOGLE_MAPS_API_KEY is not set")
    return k


def _waypoint(s: str) -> dict[str, Any]:
    """Build a Routes API waypoint from a free-text or 'lat,lon' string."""
    s = s.strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        try:
            lat = float(parts[0])
            lon = float(parts[1])
            return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}
        except ValueError:
            pass
    return {"address": s}


def _normalise_depart_at(depart_at: str | None) -> str:
    if not depart_at:
        # Routes API requires a future timestamp for traffic-aware to apply;
        # default to "now + 5 min" so 'aware' mode stays effective.
        from datetime import timedelta

        return (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if depart_at.endswith("Z") or "+" in depart_at[10:]:
        return depart_at
    # naive ISO → assume UTC
    return depart_at.rstrip("Z") + "Z"


def _parse_seconds(google_duration: str) -> int:
    """Routes API returns duration as 'NNNNs'. Strip the 's'."""
    return int(google_duration.rstrip("s"))


async def drive_time(
    client: httpx.AsyncClient,
    origin: str,
    destination: str,
    depart_at: str | None = None,
    traffic_model: str = "aware",
    avoid_tolls: bool = False,
) -> dict[str, Any]:
    """Run a single Routes API call. Returns a normalised dict."""
    routing_pref = {
        "aware": "TRAFFIC_AWARE",
        "optimal": "TRAFFIC_AWARE_OPTIMAL",
        "static": "TRAFFIC_UNAWARE",
    }.get(traffic_model.lower(), "TRAFFIC_AWARE")

    body: dict[str, Any] = {
        "origin": _waypoint(origin),
        "destination": _waypoint(destination),
        "travelMode": "DRIVE",
        "routingPreference": routing_pref,
        "computeAlternativeRoutes": False,
        "routeModifiers": {"avoidTolls": avoid_tolls},
        "languageCode": "en-GB",
        "units": "METRIC",
    }
    # Google rejects departureTime when routingPreference=TRAFFIC_UNAWARE
    if routing_pref != "TRAFFIC_UNAWARE":
        body["departureTime"] = _normalise_depart_at(depart_at)
    headers = {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": FIELD_MASK,
        "Content-Type": "application/json",
    }
    resp = await client.post(ROUTES_URL, json=body, headers=headers, timeout=30.0)
    if resp.status_code >= 400:
        raise DriveError(f"routes.googleapis.com {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    routes = payload.get("routes") or []
    if not routes:
        raise DriveError(f"no route found between {origin!r} and {destination!r}")

    r = routes[0]
    duration_s = _parse_seconds(r["duration"])
    static_s = _parse_seconds(r.get("staticDuration", r["duration"]))
    distance_m = int(r.get("distanceMeters", 0))

    return {
        "ok": True,
        "mode": "drive",
        "origin": origin,
        "destination": destination,
        "depart_at": body.get("departureTime"),
        "traffic_model": routing_pref,
        "avoid_tolls": avoid_tolls,
        "duration_minutes": round(duration_s / 60, 1),
        "duration_seconds": duration_s,
        "static_duration_minutes": round(static_s / 60, 1),
        "traffic_delay_minutes": round((duration_s - static_s) / 60, 1),
        "distance_km": round(distance_m / 1000, 1),
        "distance_miles": round(distance_m / 1609.344, 1),
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
