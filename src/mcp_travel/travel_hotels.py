"""Hotel search orchestrator (LiteAPI-backed).

Pipeline:
  1. forward_geocode `near` to lat/lon (Nominatim cache)
  2. LiteAPI radius search → hotel content records
  3. Client-side filters: min_stars, pet_friendly (amenity name match)
  4. Optional drive-time filter: parallel Google Maps `drive_time` calls,
     drop hotels over the budget
  5. LiteAPI hotel-rates for the shortlist → live rates
  6. Rank by stars desc, then cheapest rate asc

Pet-friendly detection is heuristic — LiteAPI's facility list isn't fully
standardised. We match "pet" in any amenity / facility / hotelDescription
field. Future: maintain a known-good facility ID set if false positives
become a problem.
"""

import asyncio
from typing import Any
from urllib.parse import urlencode

from mcp_travel.travel_drive import DriveError, drive_time as drive_route
from mcp_travel.travel_geocode import forward_geocode
from mcp_travel.travel_liteapi import LiteAPIError, hotel_rates, hotels_by_geocode


def _star_int(rec: dict) -> int:
    """Pull star rating off a LiteAPI hotel content record."""
    for k in ("starRating", "stars", "rating"):
        v = rec.get(k)
        if v is None:
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return 0


# LiteAPI canonical facility IDs (verified 2026-05-03 against /v3.0/data/facilities)
PET_FACILITY_IDS = {
    4,    # "Pets allowed" (the binary signal)
    956,  # Dog exercise area
    217,  # Pet basket
    218,  # Pet bowls
    2293, # Pet-sitting services
    2455, # Pet grooming services
}


def _is_pet_friendly(rec: dict) -> bool:
    """LiteAPI primary signal: facility ID 4 ('Pets allowed') in facilityIds."""
    fids = rec.get("facilityIds") or []
    if 4 in fids:
        return True
    if any(fid in PET_FACILITY_IDS for fid in fids):
        return True
    # Fallback: accessibilityAttributes.petFriendly when populated as a real string
    attrs = rec.get("accessibilityAttributes") or {}
    pf = attrs.get("petFriendly")
    if isinstance(pf, str) and pf.strip().lower() in ("true", "yes", "1", "y"):
        return True
    if isinstance(pf, bool) and pf:
        return True
    return False


def _booking_deeplink(name: str, city: str | None, check_in: str, check_out: str, guests: int) -> str:
    """Construct a Booking.com search URL pre-filled with the hotel name + dates."""
    ss = name if not city else f"{name} {city}"
    qs = urlencode({
        "ss": ss,
        "checkin": check_in,
        "checkout": check_out,
        "group_adults": guests,
        "no_rooms": 1,
        "group_children": 0,
    })
    return f"https://www.booking.com/searchresults.html?{qs}"


def _flatten_rate(rate_block: dict, hotel_records: dict[str, dict]) -> dict[str, Any]:
    """Pull hotel content + cheapest rate into a single flat output row."""
    hotel_id = rate_block.get("hotelId") or rate_block.get("id")
    record = hotel_records.get(hotel_id, {})
    room_types = rate_block.get("roomTypes") or []
    cheapest = None
    cheapest_price = None
    for rt in room_types:
        rates = rt.get("rates") or []
        for r in rates:
            price = r.get("retailRate", {}).get("total") or r.get("totalPrice")
            if not price:
                continue
            try:
                amount = float(price[0]["amount"]) if isinstance(price, list) else float(price)
            except (TypeError, ValueError, KeyError, IndexError):
                continue
            if cheapest_price is None or amount < cheapest_price:
                cheapest_price = amount
                cheapest = r
                cheapest["_room_name"] = rt.get("name")
    out: dict[str, Any] = {
        "hotel_id": hotel_id,
        "name": record.get("name"),
        "stars": _star_int(record),
        "lat": record.get("latitude") or (record.get("location") or {}).get("latitude"),
        "lon": record.get("longitude") or (record.get("location") or {}).get("longitude"),
        "address": record.get("address") or record.get("hotelDescription"),
        "city": record.get("city"),
        "pet_friendly": _is_pet_friendly(record),
    }
    if cheapest:
        out.update({
            "cheapest_total": cheapest_price,
            "currency": (cheapest.get("retailRate", {}).get("total") or [{}])[0].get("currency", "GBP"),
            "room_name": cheapest.get("_room_name"),
            "refundable": cheapest.get("cancellationPolicies", {}).get("refundableTag"),
            "board_basis": cheapest.get("boardName") or cheapest.get("boardType"),
            "rate_id": cheapest.get("rateId") or cheapest.get("offerId"),
        })
    return out


async def search(
    pool, client,
    near: str,
    check_in: str,
    check_out: str,
    min_stars: int = 4,
    pet_friendly: bool = False,
    max_drive_min_from: tuple[str, int] | None = None,
    radius_km: int = 25,
    guests: int = 2,
    max_results: int = 10,
    chain_contains: str | None = None,
    max_drive_min_to: tuple[str, int] | None = None,
    pool_locations=None,
) -> dict[str, Any]:
    # 1. Geocode 'near'
    geo = await forward_geocode(client, pool, near, pool_locations=pool_locations)
    if not geo:
        return {"ok": False, "error": f"could not geocode near={near!r}"}

    # 2. LiteAPI radius search
    try:
        records = await hotels_by_geocode(
            client, geo["lat"], geo["lon"],
            radius_km=radius_km, star_min=min_stars,
            country_code=(geo.get("country_code") or "").upper() or None,
        )
    except LiteAPIError as e:
        return {"ok": False, "near": near, "error": f"liteapi search: {e}"}

    # 3. Defensive filter — LiteAPI may return below-min stars for some sources
    records = [r for r in records if _star_int(r) >= min_stars]
    if pet_friendly:
        records = [r for r in records if _is_pet_friendly(r)]
    if chain_contains:
        needle = chain_contains.strip().lower()
        records = [
            r for r in records
            if needle in str(r.get("chain") or "").lower()
            or needle in str(r.get("name") or "").lower()
        ]
    if not records:
        return {
            "ok": True, "near": near, "near_resolved": geo["display_name"],
            "results": [], "shortlist_count": 0,
            "note": f"No {min_stars}★+{' pet-friendly ' if pet_friendly else ' '}hotels found within {radius_km} km of {near}",
        }

    # 4. Optional drive-time filter (static traffic — fast + free for budgeting)
    drive_filter = None
    if max_drive_min_from:
        budget_origin, budget_min = max_drive_min_from
        async def _check(rec):
            try:
                lat = rec.get("latitude") or (rec.get("location") or {}).get("latitude")
                lon = rec.get("longitude") or (rec.get("location") or {}).get("longitude")
                if lat is None or lon is None:
                    return rec, None
                d = await drive_route(
                    client, budget_origin, f"{lat},{lon}",
                    traffic_model="static",
                )
                return rec, d["duration_minutes"]
            except DriveError:
                return rec, None
        results = await asyncio.gather(*[_check(r) for r in records])
        within = [(r, m) for r, m in results if m is not None and m <= budget_min]
        records = [r for r, _ in within]
        drive_filter = {
            "origin": budget_origin, "max_minutes": budget_min,
            "before": len(results), "after": len(records),
        }

    if not records:
        return {
            "ok": True, "near": near, "near_resolved": geo["display_name"],
            "drive_filter": drive_filter, "results": [], "shortlist_count": 0,
            "note": f"No hotels survived the drive-time filter ({max_drive_min_from[0]} ≤ {max_drive_min_from[1]} min).",
        }

    # 4b. Optional onward drive-time filter (e.g. hotel → next-day destination)
    drive_filter_to = None
    if max_drive_min_to:
        budget_dest, budget_min = max_drive_min_to
        async def _check_to(rec):
            try:
                lat = rec.get("latitude") or (rec.get("location") or {}).get("latitude")
                lon = rec.get("longitude") or (rec.get("location") or {}).get("longitude")
                if lat is None or lon is None:
                    return rec, None
                d = await drive_route(
                    client, f"{lat},{lon}", budget_dest,
                    traffic_model="static",
                )
                return rec, d["duration_minutes"]
            except DriveError:
                return rec, None
        results2 = await asyncio.gather(*[_check_to(r) for r in records])
        within2 = [(r, m) for r, m in results2 if m is not None and m <= budget_min]
        records = [r for r, _ in within2]
        # Stash onward-drive minutes onto each record for ranking
        onward_map = {r.get("id") or r.get("hotelId"): m for r, m in within2}
        for r in records:
            r["_onward_drive_min"] = onward_map.get(r.get("id") or r.get("hotelId"))
        drive_filter_to = {
            "destination": budget_dest, "max_minutes": budget_min,
            "before": len(results2), "after": len(records),
        }

    if not records:
        return {
            "ok": True, "near": near, "near_resolved": geo["display_name"],
            "drive_filter": drive_filter, "drive_filter_to": drive_filter_to,
            "results": [], "shortlist_count": 0,
            "note": f"No hotels survived the onward drive-time filter ({max_drive_min_to[0]} ≤ {max_drive_min_to[1]} min).",
        }

    # 5. Live rates for top candidates
    hotel_id_key = "id" if records and "id" in records[0] else "hotelId"
    candidates = records[: max_results * 3]
    hotel_ids = [r[hotel_id_key] for r in candidates if hotel_id_key in r]
    record_map = {r[hotel_id_key]: r for r in candidates if hotel_id_key in r}

    rates_data: list[dict] = []
    try:
        # Most LiteAPI plans accept ~50 IDs per call; chunk to be safe.
        for i in range(0, len(hotel_ids), 50):
            chunk = await hotel_rates(client, hotel_ids[i:i + 50], check_in, check_out, adults=guests)
            rates_data.extend(chunk)
    except LiteAPIError as e:
        return {
            "ok": False, "near": near, "error": f"liteapi rates: {e}",
            "shortlist_hotel_ids": hotel_ids,
        }

    # 6. Enrich + rank
    enriched = [_flatten_rate(r, record_map) for r in rates_data]
    enriched = [e for e in enriched if e.get("cheapest_total") is not None]
    enriched.sort(key=lambda e: (-e.get("stars", 0), e.get("cheapest_total", 1e9)))

    # Annotate enriched rows with onward-drive minutes if we computed them
    if max_drive_min_to:
        for e in enriched:
            e["onward_drive_min"] = next(
                (r.get("_onward_drive_min") for r in records if (r.get("id") or r.get("hotelId")) == e["hotel_id"]),
                None,
            )

    # Add Booking.com deeplink per result — gives a one-click path to live availability
    for e in enriched:
        if e.get("name"):
            e["booking_deeplink"] = _booking_deeplink(
                e["name"], e.get("city"), check_in, check_out, guests
            )

    return {
        "ok": True,
        "near": near,
        "near_resolved": geo["display_name"],
        "near_lat": geo["lat"],
        "near_lon": geo["lon"],
        "check_in": check_in,
        "check_out": check_out,
        "guests": guests,
        "filters": {
            "min_stars": min_stars,
            "pet_friendly": pet_friendly,
            "radius_km": radius_km,
            "chain_contains": chain_contains,
        },
        "drive_filter": drive_filter,
        "drive_filter_to": drive_filter_to,
        "shortlist_count": len(records),
        "results": enriched[:max_results],
    }
