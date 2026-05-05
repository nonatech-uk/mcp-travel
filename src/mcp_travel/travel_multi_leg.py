"""Multi-leg trip planner.

Built after the Argentina-trip stress test surfaced the orchestration gap:
flight_check + hotel_search done by hand for a 5-leg / 4-stay trip is
fiddly and triggers Duffel's 429 rate limit when fanned out in parallel.

This tool takes a flat list of `legs` (flights) and `stops` (hotel stays),
runs Duffel sequentially with configurable pacing (avoids 429s), runs
hotel searches in parallel (LiteAPI is more permissive), and aggregates
everything into a single response + a single journey_log row.

Per-leg / per-stop output mirrors the single-mode tools so callers can
treat results uniformly. Cheapest fare per leg + cheapest 4★+ hotel per
stop is summarised at the top of the response.
"""

import asyncio
import json
from datetime import date as date_type, datetime, timezone
from typing import Any

from mcp_travel.travel_duffel import DuffelError, search_offers
from mcp_travel.travel_hotels import search as hotels_search


def _parse_iso_duration_min(s: str) -> int:
    """Parse ISO 8601 duration to minutes. Handles `PT3H20M`, `P1DT11H35M`, `P2D` etc."""
    if not s or not s.startswith("P"):
        return 0
    rest = s[1:]
    days = 0
    if "T" in rest:
        date_part, time_part = rest.split("T", 1)
    else:
        date_part, time_part = rest, ""
    cur = ""
    for ch in date_part:
        if ch.isdigit():
            cur += ch
        elif ch == "D":
            days = int(cur or "0")
            cur = ""
    total = days * 24 * 60
    cur = ""
    for ch in time_part:
        if ch.isdigit():
            cur += ch
        elif ch == "H":
            total += int(cur or "0") * 60
            cur = ""
        elif ch == "M":
            total += int(cur or "0")
            cur = ""
    return total


async def _run_one_leg(
    ctx: dict, leg: dict, default_adults: int,
    default_prefer: list[str] | None = None,
    default_exclude: list[str] | None = None,
) -> dict[str, Any]:
    orig = leg["orig"].upper().strip()
    dest = leg["dest"].upper().strip()
    date = leg["date"]
    cabin = leg.get("cabin", "economy")
    adults = leg.get("adults", default_adults)
    prefer = leg.get("prefer_carriers", default_prefer)
    exclude = leg.get("exclude_carriers", default_exclude)
    try:
        result = await search_offers(
            ctx["client"], orig, dest, date,
            adults=adults, cabin=cabin,
            prefer_carriers=prefer, exclude_carriers=exclude,
        )
    except DuffelError as e:
        return {
            "ok": False, "leg": f"{orig}→{dest}", "date": date,
            "error": str(e)[:300],
        }
    offers = result.get("offers") or []
    cheapest = offers[0] if offers else None
    slc = cheapest["slices"][0] if cheapest and cheapest.get("slices") else None
    return {
        "ok": True,
        "leg": f"{orig}→{dest}",
        "date": date,
        "cabin": cabin,
        "adults": adults,
        "offers_count": len(offers),
        "cheapest": cheapest,
        "cheapest_price": cheapest["total_amount"] if cheapest else None,
        "cheapest_currency": cheapest["total_currency"] if cheapest else None,
        "cheapest_carrier": cheapest.get("owner") if cheapest else None,
        "cheapest_block_minutes": slc.get("block_minutes") if slc else None,
        "cheapest_elapsed_minutes": slc.get("elapsed_minutes") if slc else None,
        "cheapest_layover_minutes": slc.get("layover_minutes") if slc else None,
        "cheapest_stops": slc.get("stops") if slc else None,
        # legacy field — keep but make explicit it's elapsed (gate-to-gate)
        "cheapest_minutes": slc.get("elapsed_minutes") if slc else None,
        "booking_deeplink": result.get("booking_deeplink"),
    }


async def _run_one_stop(
    ctx: dict, stop: dict, default_adults: int
) -> dict[str, Any]:
    near = stop["city"]
    check_in = stop["check_in"]
    check_out = stop["check_out"]
    min_stars = stop.get("min_stars", 4)
    pet_friendly = stop.get("pet_friendly", False)
    guests = stop.get("guests", default_adults)
    try:
        result = await hotels_search(
            ctx["pool"], ctx["client"],
            near=near, check_in=check_in, check_out=check_out,
            min_stars=min_stars, pet_friendly=pet_friendly,
            radius_km=stop.get("radius_km", 25),
            guests=guests, max_results=stop.get("max_results", 5),
            chain_contains=stop.get("chain_contains"),
            pool_locations=ctx.get("pool_locations"),
        )
    except Exception as e:
        return {
            "ok": False, "stop": near,
            "check_in": check_in, "check_out": check_out,
            "error": f"{type(e).__name__}: {e}",
        }
    if not result.get("ok"):
        return {
            "ok": False, "stop": near,
            "check_in": check_in, "check_out": check_out,
            "error": result.get("error") or result.get("note") or "unknown",
        }
    rows = result.get("results") or []
    cheapest = rows[0] if rows else None
    nights = (date_type.fromisoformat(check_out) - date_type.fromisoformat(check_in)).days
    return {
        "ok": True,
        "stop": near,
        "check_in": check_in,
        "check_out": check_out,
        "nights": nights,
        "shortlist_count": result.get("shortlist_count"),
        "results_count": len(rows),
        "cheapest_4plus_star": cheapest,
        "all_results": rows,
    }


async def plan_multi_leg_impl(
    ctx: dict,
    name: str,
    legs: list[dict],
    stops: list[dict] | None = None,
    party: list[str] | None = None,
    pacing_seconds: float = 6.0,
    prefer_carriers: list[str] | None = None,
    exclude_carriers: list[str] | None = None,
) -> dict[str, Any]:
    if not legs:
        return {"ok": False, "error": "legs list is empty — provide at least one flight leg"}

    if party is None:
        async with ctx["pool"].acquire() as conn:
            rows = await conn.fetch("SELECT name FROM party_member WHERE is_default=true ORDER BY id")
            party = [r["name"] for r in rows]
    default_adults = max(1, sum(1 for p in party if p))

    # 1. Flights — sequential with pacing
    flight_results: list[dict] = []
    for i, leg in enumerate(legs):
        if i > 0:
            await asyncio.sleep(pacing_seconds)
        flight_results.append(await _run_one_leg(
            ctx, leg, default_adults,
            default_prefer=prefer_carriers, default_exclude=exclude_carriers,
        ))

    # 2. Hotels — parallel (LiteAPI tolerates it)
    hotel_results: list[dict] = []
    if stops:
        hotel_results = await asyncio.gather(
            *[_run_one_stop(ctx, s, default_adults) for s in stops]
        )

    # 3. Aggregate
    total_flight_cost = sum(
        (f.get("cheapest_price") or 0) for f in flight_results if f.get("ok")
    )
    # Use block time (sum of per-segment in-air) for "total flight time" — more
    # honest than elapsed which double-counts long layovers.
    total_block_minutes = sum(
        (f.get("cheapest_block_minutes") or 0) for f in flight_results if f.get("ok")
    )
    total_elapsed_minutes = sum(
        (f.get("cheapest_elapsed_minutes") or 0) for f in flight_results if f.get("ok")
    )
    # Legacy aggregate name — kept for back-compat, now preferring block time
    total_flight_minutes = total_block_minutes
    total_hotel_cost = 0.0
    for h in hotel_results:
        if not h.get("ok"):
            continue
        cheapest = h.get("cheapest_4plus_star") or {}
        total_hotel_cost += (cheapest.get("cheapest_total") or 0) * (h.get("nights") or 0)

    flight_currency = next(
        (f["cheapest_currency"] for f in flight_results
         if f.get("ok") and f.get("cheapest_currency")),
        None,
    )

    payload: dict[str, Any] = {
        "ok": True,
        "name": name,
        "party": party,
        "legs_count": len(legs),
        "stops_count": len(stops) if stops else 0,
        "total_flight_cost": round(total_flight_cost, 2),
        "total_flight_currency": flight_currency,
        "total_flight_minutes": total_flight_minutes,           # legacy (== block)
        "total_block_minutes": total_block_minutes,             # in-air only
        "total_elapsed_minutes": total_elapsed_minutes,         # gate-to-gate w/ layovers
        "total_flight_hours": round(total_block_minutes / 60, 1) if total_block_minutes else 0,
        "total_elapsed_hours": round(total_elapsed_minutes / 60, 1) if total_elapsed_minutes else 0,
        "total_hotel_cost": round(total_hotel_cost, 2),
        "total_estimated_cost": round(total_flight_cost + total_hotel_cost, 2),
        "flights": flight_results,
        "hotels": hotel_results,
    }

    # 4. Persist as one journey_log row
    try:
        first_dep = min(
            (date_type.fromisoformat(l["date"]) for l in legs if l.get("date")),
            default=None,
        )
        last_dep = max(
            (date_type.fromisoformat(l["date"]) for l in legs if l.get("date")),
            default=None,
        )
        async with ctx["pool"].acquire() as conn:
            await conn.execute(
                """
                INSERT INTO journey_log (destination, depart_date, return_date, party, result)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
                """,
                f"[multi-leg] {name}",
                first_dep,
                last_dep,
                json.dumps(party),
                json.dumps(payload),
            )
    except Exception as e:
        payload["journey_log_error"] = str(e)

    return payload
