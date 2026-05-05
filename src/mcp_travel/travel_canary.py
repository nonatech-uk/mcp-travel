"""Travel canary — weekly health probe of all scraped/hacked + official APIs.

Run: `python -m mcp_travel.travel_canary` (inside the mcp-search-travel
container — needs the same env vars as travel_mcp).

Two tiers of checks:
  - 'scraped' — undocumented endpoints discovered via dev tools. Brittle
    by nature; if these fail it's the first signal an operator changed
    their schema. Fail-fast triage required.
  - 'official' — public APIs with developer programmes. Stable but
    worth confirming.

Each check runs a known-stable query 8 weeks in the future and asserts
response *structure*, not just HTTP 200 — i.e. "did Eurostar actually
return ≥5 journeys with fares?" Catches silent shape drift, not just
endpoint death.

Reports per-tier results and pings the relevant healthchecks.io UUID
(env: TRAVEL_CANARY_HC_SCRAPED, TRAVEL_CANARY_HC_OFFICIAL). Exit 0 on
all-green, 1 on any failure (so systemd / Cronicle marks the run failed).
"""

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Awaitable, Callable

import httpx


def _future_date(weeks_out: int = 8, weekday: int = 1) -> str:
    """ISO date `weeks_out` weeks ahead, advanced to the next occurrence
    of `weekday` (default Tuesday). Far enough to be quotable on every
    operator; close enough to be a real product they're selling."""
    d = date.today() + timedelta(weeks=weeks_out)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d.isoformat()


DATE = _future_date()


@dataclass
class Result:
    name: str
    tier: str
    ok: bool
    duration_ms: int = 0
    detail: str = ""
    error: str = ""


async def _run(name: str, tier: str, fn: Callable[[], Awaitable[str]]) -> Result:
    t0 = time.time()
    try:
        detail = await fn()
        return Result(name=name, tier=tier, ok=True,
                      duration_ms=int((time.time() - t0) * 1000),
                      detail=detail)
    except Exception as e:
        return Result(name=name, tier=tier, ok=False,
                      duration_ms=int((time.time() - t0) * 1000),
                      error=f"{type(e).__name__}: {str(e)[:200]}")


# --- Scraped/hacked endpoint checks ---

async def check_eurostar(client):
    from mcp_travel.travel_eurostar import check
    r = await check(client, "london", "paris", DATE, adults=1, time="10:00")
    if not r.get("ok"):
        raise AssertionError("eurostar response not ok")
    if (r.get("journey_count") or 0) < 5:
        raise AssertionError(f"only {r.get('journey_count')} journeys (expected ≥5)")
    sel = r.get("selected_journey") or {}
    if not sel.get("fares"):
        raise AssertionError("no fares in selected_journey")
    return f"{r['journey_count']} journeys, {r.get('available_count')} available"


async def check_eurotunnel(client):
    from mcp_travel.travel_eurotunnel import check
    r = await check(client, DATE, time="10:00", vehicle="car", passengers=2)
    if not r.get("ok"):
        raise AssertionError("eurotunnel response not ok")
    if "leshuttle-live" not in (r.get("data_sources") or []):
        raise AssertionError(f"fell back to static (no leshuttle-live in {r.get('data_sources')})")
    if (r.get("crossing_count") or 0) < 5:
        raise AssertionError(f"only {r.get('crossing_count')} crossings")
    sel = r.get("selected_crossing") or {}
    bp = sel.get("best_price")
    if not bp or bp <= 0:
        raise AssertionError(f"invalid best_price: {bp}")
    return f"{r['crossing_count']} crossings, cheapest £{bp}"


async def check_dfds_hellman(client):
    from mcp_travel.travel_dfds import get_sailings
    s = await get_sailings(client, DATE, "dover", "calais", adults=1, vehicle="car")
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} sailings")
    avail = [x["best_price"] for x in s if x.get("best_price") is not None]
    if not avail:
        raise AssertionError("no priced sailings")
    return f"{len(s)} sailings, cheapest £{min(avail)}"


async def check_dfds_fares_flow(client):
    from mcp_travel.travel_dfds import get_sailings
    s = await get_sailings(client, DATE, "newhaven", "dieppe", adults=1, vehicle="car")
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} sailings")
    return f"{len(s)} sailings"


async def check_dfds_cabin_fares(client):
    from mcp_travel.travel_dfds import get_sailings
    s = await get_sailings(client, DATE, "newcastle", "amsterdam", adults=2, vehicle="car")
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} sailings")
    return f"{len(s)} sailings"


async def check_brittany_ferries(client):
    from mcp_travel.travel_brittany_ferries import get_sailings
    r = await get_sailings(client, DATE, "plymouth", "roscoff", adults=2, vehicle="car")
    sailings = r.get("sailings") or []
    if len(sailings) < 1:
        raise AssertionError(f"only {len(sailings)} sailings")
    avail = [s["best_price"] for s in sailings if s.get("best_price") is not None]
    if not avail:
        raise AssertionError("no priced sailings")
    return f"{len(sailings)} sailings, cheapest {r.get('currency')} {min(avail)}"


async def check_stena_line(client):
    from mcp_travel.travel_stena_line import get_sailings
    s = await get_sailings(client, DATE, "holyhead", "dublin",
                           adults=2, vehicle="car", currency="GBP")
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} sailings")
    avail = [x["best_price"] for x in s if x.get("best_price") is not None]
    if not avail:
        raise AssertionError("no priced sailings")
    return f"{len(s)} sailings, cheapest £{min(avail)}"


async def check_po_dover_calais(client):
    from mcp_travel.travel_po_ferries import get_sailings
    s = await get_sailings(client, DATE, "dover", "calais", adults=2, vehicle="car")
    if len(s) < 5:
        raise AssertionError(f"only {len(s)} sailings (expected ≥5 — many daily Dover-Calais)")
    avail = [x["best_price"] for x in s if x.get("best_price") is not None]
    if not avail:
        raise AssertionError("no priced sailings")
    return f"{len(s)} sailings, cheapest £{min(avail)}"


async def check_po_larne_cairnryan(client):
    from mcp_travel.travel_po_ferries import get_sailings
    s = await get_sailings(client, DATE, "larne", "cairnryan", adults=2, vehicle="none")
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} sailings")
    avail = [x["best_price"] for x in s if x.get("best_price") is not None]
    if not avail:
        raise AssertionError("no priced sailings")
    return f"{len(s)} sailings, cheapest £{min(avail)}"


async def check_po_hull_rotterdam(client):
    from mcp_travel.travel_po_ferries import get_sailings
    s = await get_sailings(client, DATE, "hull", "rotterdam", adults=2, vehicle="car")
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} sailings")
    return f"{len(s)} sailings"


async def check_irish_ferries(client):
    """Playwright sidecar — Dublin↔Holyhead is Irish Ferries' busiest route.
    Slow (15-25s) but the most fragile of all live ferry data (Imperva WAF,
    SPA brittleness), so worth a weekly probe."""
    from mcp_travel.travel_irish_ferries import get_sailings
    s = await get_sailings(client, DATE, "dublin", "holyhead", adults=1, vehicle="foot")
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} sailings (sidecar may be down)")
    avail = [x["best_price"] for x in s if x.get("best_price") is not None]
    return f"{len(s)} sailings, {len(avail)} priced"


async def check_ryanair(client):
    """Playwright sidecar — DUB↔STN is one of Ryanair's busiest pairs and
    is always-on year-round, so it's the safest canary route. Closes the
    Duffel-inventory gap (FR not on Duffel)."""
    from mcp_travel.travel_ryanair import get_flights
    f = await get_flights(client, DATE, "DUB", "STN", adults=1)
    if len(f) < 1:
        raise AssertionError(f"only {len(f)} flights (sidecar may be down)")
    avail = [x["best_price"] for x in f if x.get("best_price") is not None]
    if not avail:
        raise AssertionError("no priced flights")
    return f"{len(f)} flights, cheapest €{min(avail)}"


async def check_trenitalia(client):
    """Trenitalia BFF — CSRF-token + gzipped JSON. The most fragile of
    the scraped endpoints (multi-step auth, fully obfuscated SPA), so
    most valuable to canary weekly."""
    from mcp_travel.travel_trenitalia_live import get_solutions
    s = await get_solutions(client, DATE, "milano", "roma", adults=1, limit=3)
    if len(s) < 1:
        raise AssertionError(f"only {len(s)} solutions")
    priced = [x for x in s if x.get("best_price") is not None]
    if not priced:
        raise AssertionError("no priced solutions (CSRF may have broken)")
    return f"{len(s)} solutions, cheapest €{min(p['best_price'] for p in priced)}"


# --- Official-API checks ---

async def check_duffel(client):
    from mcp_travel.travel_duffel import search_offers
    r = await search_offers(client, "LGW", "NCE", DATE, adults=2, max_offers=3)
    offers = r.get("offers") or []
    if len(offers) < 1:
        raise AssertionError(f"only {len(offers)} offers")
    return f"{len(offers)} offers (mode={r.get('mode')})"


async def check_drive_time(client):
    from mcp_travel.travel_drive import drive_time
    # Probe with the same future date the rail/ferry tests use, so we
    # exercise predictive-traffic routing (not just live current).
    # Probe destination — uses the configured default origin so the canary
    # exercises whatever home address is set. Falls back to a generic London
    # postcode if the env var isn't populated.
    canary_dest = os.environ.get("MCP_TRAVEL_DEFAULT_ORIGIN", "EC1A 1BB, UK")
    r = await drive_time(client, "LGW airport", canary_dest,
                         depart_at=f"{DATE}T09:00:00Z")
    if not r.get("duration_minutes") or r["duration_minutes"] <= 0:
        raise AssertionError(f"invalid duration: {r.get('duration_minutes')}")
    return f"{r['duration_minutes']:.1f} min, {r.get('distance_km')} km"


async def check_norway(client):
    from mcp_travel.travel_norway import search_journey
    r = await search_journey(client, "Oslo S", "Bergen",
                             datetime_iso=f"{DATE}T09:00", max_journeys=3)
    j = r.get("journeys") or []
    if len(j) < 1:
        raise AssertionError(f"only {len(j)} journeys")
    return f"{len(j)} journeys"


async def check_ns(client):
    from mcp_travel.travel_ns import search_journey
    r = await search_journey(client, "Amsterdam Centraal", "Rotterdam Centraal",
                             datetime_iso=f"{DATE}T09:00", max_journeys=3)
    j = r.get("journeys") or []
    if len(j) < 1:
        raise AssertionError(f"only {len(j)} journeys")
    return f"{len(j)} journeys"


async def check_irail(client):
    from mcp_travel.travel_sncb import search_journey
    r = await search_journey(client, "Brussels-Midi", "Antwerpen-Centraal",
                             datetime_iso=f"{DATE}T09:00", max_journeys=3)
    j = r.get("journeys") or []
    if len(j) < 1:
        raise AssertionError(f"only {len(j)} journeys")
    return f"{len(j)} journeys"


async def check_db(client):
    from mcp_travel.travel_db import search_journey
    r = await search_journey(client, "Berlin Hbf", "München Hbf",
                             datetime_iso=f"{DATE}T09:00", max_journeys=3)
    j = r.get("journeys") or []
    if len(j) < 1:
        raise AssertionError(f"only {len(j)} journeys")
    return f"{len(j)} journeys"


async def check_resrobot(client):
    from mcp_travel.travel_sweden import search_journey
    r = await search_journey(client, "Stockholm Central", "Göteborg Central",
                             datetime_iso=f"{DATE}T09:00", max_journeys=3)
    j = r.get("journeys") or []
    if len(j) < 1:
        raise AssertionError(f"only {len(j)} journeys")
    return f"{len(j)} journeys"


async def check_viaggiatreno(client):
    from mcp_travel.travel_italy_status import departures
    r = await departures(client, "Roma Termini", max_results=10)
    deps = r.get("departures") or []
    if len(deps) < 1:
        raise AssertionError(f"only {len(deps)} departures")
    return f"{len(deps)} live departures"


async def check_tfl(client):
    """TfL journey planner — multi-modal London routing. Tests the
    disambiguation handler too (St Pancras + Canary Wharf both have
    multiple matches)."""
    from mcp_travel.travel_tfl import journey
    j = await journey(client, "St Pancras", "Canary Wharf",
                      datetime_iso=f"{DATE}T09:00", max_journeys=2)
    if len(j) < 1:
        raise AssertionError(f"only {len(j)} journeys")
    if not j[0].get("legs"):
        raise AssertionError("first journey has no legs")
    return f"{len(j)} journeys, {j[0]['duration_minutes']}min cheapest"


# --- Registry ---

CHECKS_SCRAPED: list[tuple[str, Callable]] = [
    ("eurostar-graphql",       check_eurostar),
    ("leshuttle-quote",        check_eurotunnel),
    ("dfds-hellman",           check_dfds_hellman),
    ("dfds-fares-flow",        check_dfds_fares_flow),
    ("dfds-cabin-fares",       check_dfds_cabin_fares),
    ("brittany-ferries",       check_brittany_ferries),
    ("stena-line-graphql",     check_stena_line),
    ("po-dover-calais",        check_po_dover_calais),
    ("po-larne-cairnryan",     check_po_larne_cairnryan),
    ("po-hull-rotterdam",      check_po_hull_rotterdam),
    ("irish-ferries-sidecar",  check_irish_ferries),
    ("ryanair-sidecar",        check_ryanair),
    ("trenitalia-bff",         check_trenitalia),
]

CHECKS_OFFICIAL: list[tuple[str, Callable]] = [
    ("duffel",              check_duffel),
    ("google-maps-routes",  check_drive_time),
    ("entur-norway",        check_norway),
    ("ns-netherlands",      check_ns),
    ("irail-belgium",       check_irail),
    ("db-rest-germany",     check_db),
    ("resrobot-sweden",     check_resrobot),
    ("viaggiatreno-italy",  check_viaggiatreno),
    ("tfl-journey-planner", check_tfl),
]


def _format_results(results: list[Result]) -> str:
    """Two-column tier-grouped report, suitable for both stdout + HC body."""
    lines = []
    for tier in ("scraped", "official"):
        tier_results = [r for r in results if r.tier == tier]
        passed = sum(1 for r in tier_results if r.ok)
        total = len(tier_results)
        lines.append(f"=== {tier.upper()} ({passed}/{total} passed) ===")
        for r in tier_results:
            mark = "✓" if r.ok else "✗"
            line = f"  {mark} {r.name:24} {r.duration_ms:5}ms"
            if r.ok:
                line += f"  {r.detail}"
            else:
                line += f"  ERROR: {r.error}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


async def _ping_hc(client: httpx.AsyncClient, uuid: str | None, ok: bool, body: str):
    """POST a healthcheck ping with the report body. Soft-fail on
    network errors — a HC outage shouldn't kill the canary."""
    if not uuid:
        return
    suffix = "" if ok else "/fail"
    url = f"https://hc.mees.st/ping/{uuid}{suffix}"
    try:
        await client.post(url, content=body.encode("utf-8"), timeout=10.0)
    except (httpx.HTTPError, OSError):
        pass


async def main() -> int:
    print(f"Travel canary — date probe={DATE}")
    print()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Run scraped + official in parallel; per-tier ordering doesn't matter
        all_checks = (
            [(name, "scraped",  fn) for (name, fn) in CHECKS_SCRAPED] +
            [(name, "official", fn) for (name, fn) in CHECKS_OFFICIAL]
        )
        results = await asyncio.gather(*[
            _run(name, tier, lambda fn=fn: fn(client))
            for (name, tier, fn) in all_checks
        ])

    report = _format_results(results)
    print(report)

    scraped_ok  = all(r.ok for r in results if r.tier == "scraped")
    official_ok = all(r.ok for r in results if r.tier == "official")

    # Healthcheck pings
    async with httpx.AsyncClient(timeout=15.0) as hc_client:
        scraped_body = "\n".join(
            f"{('OK' if r.ok else 'FAIL'):>4}  {r.name:24}  {r.detail or r.error}"
            for r in results if r.tier == "scraped"
        )
        official_body = "\n".join(
            f"{('OK' if r.ok else 'FAIL'):>4}  {r.name:24}  {r.detail or r.error}"
            for r in results if r.tier == "official"
        )
        await asyncio.gather(
            _ping_hc(hc_client, os.environ.get("TRAVEL_CANARY_HC_SCRAPED"),
                     scraped_ok, scraped_body),
            _ping_hc(hc_client, os.environ.get("TRAVEL_CANARY_HC_OFFICIAL"),
                     official_ok, official_body),
        )

    return 0 if (scraped_ok and official_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
