# mcp-travel

Door-to-door MCP trip planner that compares flights, rail, ferries, drives,
and hotels in one composable tool surface. Built around FastMCP and exposed
to Claude / any MCP-compatible client.

> Personal project. Tuned for one household's actual travel patterns
> (Eurostar, Eurotunnel, Channel ferries, European rail, Relais & Châteaux
> hotels). Not a general-purpose product, but the tools and the patterns
> are reusable — fork it and point the env vars at your own life.

## What it does

Given a destination, `travel_plan_trip` fans out across modes — flight,
direct rail, multi-leg rail, drive-and-ferry, drive-and-Eurotunnel — and
returns a ranked door-to-door comparison with operator-specific deeplinks
for booking. `travel_compare_modes` does the same shape without the
multi-day routing logic. Around those two orchestrators sit single-mode
tools (one per operator or per network) that can be called directly when
you want narrower output.

Live data sources: Duffel (flights), Google Maps Routes API (drive ETAs),
LiteAPI (hotels), RTT next-gen + Transport API (UK rail), SBB / SNCF /
Trenitalia / Deutsche Bahn / iRail (SNCB) / NS / ResRobot (Sweden) /
Entur (Norway) / ViaggiaTreno (Italy live), DFDS / P&O Ferries / Stena
Line / Brittany Ferries (passenger ferries). Static-timetable fallbacks
for Eurostar, LeShuttle, Renfe (Spain), ÖBB (Austria), Trenitalia
(when live API is rate-limited).

## Tools (38)

### Orchestrators
- `travel_plan_trip` — door-to-door comparison from configured home origin to a destination, with multi-day overnight legs and luxury-affiliation hotel suggestions.
- `travel_plan_multi_leg` — N flights + M hotel stops in one call.
- `travel_compare_modes` — single-day fan-out across all modes for a given route.

### Flights / hotels
- `travel_flight_check` — Duffel offer search, with `prefer_carriers` / `exclude_carriers`.
- `travel_hotel_search` — LiteAPI hotel discovery with stars / pet-friendly / chain / radius filters.
- `travel_affiliation_search` — drive-time-validated nearest hotels from a curated R&C / LHW / SLH / CHC list.

### Rail (live where possible, static-table where not)
- `travel_uk_*` — RTT next-gen + Transport API for UK rail (`find_station`, `stationboard`, `journey`, `service`, `disruptions`).
- `travel_sbb_*` — Swiss public transport via transport.opendata.ch HAFAS (`find_station`, `journey`, `stationboard`, `disruptions`).
- `travel_sncf_journey` — French SNCF Navitia.
- `travel_ns_journey` — Dutch NS Reisinformatie.
- `travel_sncb_journey` — Belgian iRail.
- `travel_db_journey` — German Deutsche Bahn (db-rest community wrapper).
- `travel_norway_journey` — Norwegian Entur (multimodal: rail + bus + water).
- `travel_sweden_journey` — Swedish ResRobot v2.1 (HAFAS).
- `travel_italy_journey` / `travel_italy_status` — Italian static city pairs + ViaggiaTreno live.
- `travel_italy_prices_via_safari` / `travel_eurostar_prices_via_safari` — user-driven scrape fallbacks for operators that block API automation.
- `travel_spain_journey` — Renfe AVE / Avlo (curated).
- `travel_austria_journey` — ÖBB (curated).

### Channel + ferry
- `travel_eurostar_check` / `travel_eurotunnel_check` — durations and recommended check-in.
- `travel_ferry_check` / `travel_ferry_routes_to` — Channel + Irish Sea + North Sea operators.

### London public transport
- `travel_tfl_journey` / `travel_tfl_find_stop` / `travel_tfl_line_status`.

### Drive / car / private
- `travel_drive_time` — Google Maps Routes API, traffic-aware.
- `travel_uber_estimate` — Uber Rides API price + ETA + deeplink (deeplink-only — booking via app).

### Utilities
- `travel_recent_trips` — read journey_log audit history.
- `travel_list_named_places` — exposes mylocation.place rows when read-only Postgres is configured (for short-form names like "the chalet").

## Build

```bash
podman build -t mcp-travel:latest .
```

## Run

Stdio (default — for Claude Desktop / MCP CLIs):

```bash
podman run --rm -i --env-file .env mcp-travel:latest
```

Streamable-HTTP (gateway-friendly):

```bash
podman run --rm -p 8080:8080 \
  -e MCP_TRANSPORT=streamable-http \
  --env-file .env \
  mcp-travel:latest
```

## Configuration

Copy `.env.example` to `.env` and fill in what you have. Every API key is
optional — tools that lack credentials return `{"ok": false, "error": …}`
rather than failing the whole comparison.

Key env vars:

| Variable | Purpose |
|---|---|
| `MCP_TRAVEL_CONTACT` | Email used in `User-Agent` for Entur / ViaggiaTreno / DB / iRail / Nominatim. Required by some upstream APIs' acceptable-use policies. |
| `MCP_TRAVEL_DEFAULT_ORIGIN` | Free-text default origin used by `travel_plan_trip` when no explicit origin is passed. Override per-call via `origin=`. |
| `MCP_TRAVEL_DEFAULT_ORIGIN_LABEL` | Human-readable label for the default origin (shown in journey log + ranked output). |
| `TRAVEL_DB_DSN` | Postgres connection string for cache + journey log. Optional. |
| `MCP_READONLY_PASSWORD` | Postgres readonly password for `mylocation.place` lookups. Optional — without it `travel_list_named_places` is unavailable. |
| `DUFFEL_API_TOKEN` / `DUFFEL_MODE` | Flight search. `test` mode for dev. |
| `GOOGLE_MAPS_API_KEY` | Drive ETAs (Routes API, Basic-tier field mask). IP-restrict the key. |
| `LITEAPI_API_KEY` | Hotel discovery + live rates. |
| `RTT_BEARER_TOKEN` | UK rail real-time. Refresh-token from <https://api-portal.rtt.io>. |
| `TRANSPORT_API_APP_ID` / `_KEY` | UK rail station name → CRS. Free tier 1000/day. |
| `NS_API_KEY` | Dutch rail. Free key from <https://apiportal.ns.nl>. |
| `RESROBOT_API_KEY` | Swedish national journey planner. Free 30k/month. |
| `SNCF_API_KEY` | French SNCF Navitia. |
| `UBER_CLIENT_ID` / `_SECRET` | Uber Rides estimates (deeplink fallback works without these). |
| `TRAVEL_HC_UUID` / `TRAVEL_CANARY_HC_*` | Healthchecks.io UUIDs for cache prune + canary. Optional. |

## Schema (optional Postgres)

```sql
CREATE DATABASE travel OWNER travel;
\c travel
CREATE TABLE party_member  (id serial PRIMARY KEY, name text, ...);
CREATE TABLE query_cache   (key text PRIMARY KEY, value jsonb, expires_at timestamptz);
CREATE TABLE geocode_cache (query text PRIMARY KEY, lat float, lon float, ...);
CREATE TABLE journey_log   (id serial PRIMARY KEY, request jsonb, response jsonb, created_at timestamptz default now());
```

(See `travel_cache.py` and `travel_geocode.py` for the columns the tools
actually populate. The DB is purely an optimisation — every tool works
without it; results just hit upstream APIs every time and don't get
audited.)

## Architecture

- **One FastMCP server** (`travel_mcp.py`) folds in all rail backends. The
  `register(mcp)` pattern keeps `travel_sbb.py` and `travel_uk.py` as their
  own modules but registers their tools onto the shared instance.
- **Fail-soft per mode** — every tool returns `{ok: false, error: …}` on
  upstream failure rather than raising, so a single dead scraper or rate
  limit doesn't kill the comparison.
- **Cache-first** — flight 6h, rail 12h, scrapers 24h, geocode 30d.
- **Per-tool `_<tool>_impl()` helper** — so orchestrators (`compare_modes`,
  `plan_trip`, `plan_multi_leg`) can call them as plain async functions
  without going through the MCP serialisation round-trip.

### Multi-source aggregator contract

Tools that fan out to two or more sources for the **same** conceptual
operation (e.g. `travel_ferry_check` querying DFDS + Brittany + Stena
+ P&O + Irish Ferries; `travel_flight_check` querying Duffel + Ryanair)
return this envelope:

```json
{
  "ok": true,                          // at least one source returned data
  "mode": "ferry" | "flight" | ...,
  "data_sources": ["dfds-live", ...],  // sources that contributed
  "options": [
    {
      "source": "dfds",                // or "operator": "DFDS"
      "live_data": true,
      "data_sources": ["dfds-live"],
      "live_error": null,              // string when this source failed
      ...source-specific payload...
    }
  ],
  "as_of": "2026-05-05T..."
}
```

Top-level `ok` means "we got *something* useful from at least one
source"; per-source failures surface as `live_error` strings inside
their own option block, so a single dead scraper never kills the
whole call. Reference implementations: `travel_ferries.check()`
(`src/mcp_travel/travel_ferries.py`) and `_flight_check_impl`
(`src/mcp_travel/travel_mcp.py`).

This contract applies only to multi-source aggregators. Single-source
tools (per-operator rail, per-operator scrapers) keep the simpler
`{ok: bool, error?: str, ...payload}` shape; cross-mode orchestrators
(`travel_compare_modes`, `travel_plan_trip`) keep their dict-of-modes
shape because each mode is uniquely named and known to the caller.

Prices in option blocks should also carry `best_price_gbp` alongside
the native `best_price` + `currency` so callers can rank cross-source
without doing FX themselves — see `travel_fx.to_gbp`.

### Passenger spec & age cutoffs

Tools that take passenger counts accept either a flat `adults: int`
(legacy) or a `passengers: dict` of the form
`{adults, teens, children, infants}` (preferred). Per-operator
interpretation of the bands varies — the tool wrapper does the
right thing for each backend, but if you're constructing requests
manually it helps to know the cutoffs:

| Operator         | Adult | Teen / Youth | Child       | Infant         |
| ---------------- | ----- | ------------ | ----------- | -------------- |
| Eurostar         | 12+   | (folded into adult) | 4-11        | under 4 (lap)  |
| Duffel (default) | 18+   | (folded into adult) | 2-17        | under 2 (lap)  |
| Ryanair          | 16+   | 12-15        | 2-11        | under 2 (lap)  |
| Brittany Ferries | 14+   | (folded into adult) | 4-13        | under 4 (free) |
| Stena Line       | 16+   | (folded into adult) | 4-15        | under 4 (free) |
| DFDS             | 16+   | (folded into adult) | 4-15        | under 4 (free) |
| P&O Ferries      | 18+   | (folded into adult) | 4-17        | under 4 (free) |
| Irish Ferries    | 16+   | (folded into adult) | 3-15        | under 3 (free) |
| Trenitalia       | 15+   | (folded into adult) | 4-14        | under 4 (free) |
| SBB / DB / NS / Entur / SNCB / SNCF / ResRobot | n/a — journey-only, no pricing in API |||

For tools whose underlying API doesn't distinguish bands (most rail
journey planners; ferry top-level headcount), the wrapper sums the
dict to a total. For band-aware tools (Eurostar, Ryanair) per-band
counts are passed through. Eurostar treats teens as adults inside
the wrapper.

### Leg shape (canonical)

Rail journey tools that return JSON (`travel_rail_<iso2>_journey` for
NL/BE/DE/FR/NO/SE) emit a per-leg breakdown under `legs[]` (or
`sections[]` for SNCF, which Navitia calls them). Every leg has the
same keys regardless of the upstream API:

```
{
  "from": "Brussels-South",          // origin station name
  "to":   "Antwerp-Central",
  "from_platform": "5",              // string or null
  "to_platform":   "13",
  "depart": "2026-06-15T09:30:00",   // ISO datetime
  "arrive": "2026-06-15T10:08:00",
  "duration_minutes": 38,             // null if upstream doesn't expose
  "operator": "SNCB",                 // carrier name
  "category": "IC",                   // service tier (IC/ICE/IR/RE/R/...)
  "train_number": "1832",             // specific train number
  "line_name": "IC 1832",             // descriptive name where applicable
  "is_walking": false                 // true for walk transfers between stops
}
```

Operator-specific extras (Navitia's `headsign`, Entur's `distance_m`,
SNCF's `stops`, NS's `cancelled`, etc.) live alongside the canonical
fields. Text-output rail tools (`travel_rail_ch_journey`,
`travel_rail_gb_journey`) emit the same data as indented bullet lines
under each connection summary instead of a JSON `legs[]` array.

### Datetime spec

Rail journey tools (`travel_rail_<iso>_journey`) historically took
`datetime_iso` (full ISO datetime). They now also accept `date`
(YYYY-MM-DD) + `depart_time` (HH:MM, default `08:00`) as an
alternative. `datetime_iso` wins if both are given. Other tools
(flight, ferry, eurotunnel, eurostar) keep the simpler `date`
shape — they query a whole day and don't need a departure time.

## Development

The codebase relies only on `fastmcp`, `httpx`, and `asyncpg` at runtime.
Module-level state (HTTP clients, asyncpg pools, RTT token cache) lives in
the FastMCP lifespan; no global mutable state outside that.

Pull requests welcome for additional rail networks, ferry operators, or
hotel-affiliation curations — keep the existing fail-soft + cache-first +
`_impl` patterns and you'll match the rest of the codebase.

## License

[MIT](LICENSE).
