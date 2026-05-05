"""FX conversion to GBP — daily-cached against the Frankfurter API (ECB).

Cross-mode ranking in `travel_compare_modes` and `travel_plan_trip` was
silently comparing GBP ferry fares against EUR Ryanair fares. This
module gives every operator a `best_price_gbp` field so callers can
sort by price across modes without having to do FX themselves.

Frankfurter is free, no API key, ECB-backed reference rates. Daily
publication around 16:00 CET — sub-1% intra-day drift is below the
noise floor of "is this trip viable". One HTTP call per day per
process, cached in-memory; falls through to a hardcoded fallback
table if Frankfurter is unreachable so we never block a price call
on FX.
"""

from __future__ import annotations

import asyncio
from datetime import date as _date

import httpx

_FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base=GBP"

# Hardcoded fallback rates (1 GBP = X) — used only if Frankfurter is
# unreachable. Refreshed manually 2026-05-05; off by at most 5% over
# typical FX cycles, fine for ranking.
_FALLBACK_RATES: dict[str, float] = {
    "GBP": 1.0,
    "EUR": 1.18,
    "USD": 1.27,
    "SEK": 13.7,
    "DKK": 8.8,
    "NOK": 13.9,
    "CHF": 1.10,
    "PLN": 5.10,
}

# Process-wide cache: (date, rates_dict). Refreshed when the date rolls
# over. asyncio.Lock prevents thundering-herd on first miss.
_cache: tuple[_date, dict[str, float]] | None = None
_cache_lock = asyncio.Lock()


async def _fetch_rates(client: httpx.AsyncClient | None = None) -> dict[str, float]:
    """Fetch latest ECB rates with GBP as base. Returns 1.0 for GBP."""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=8.0)
    try:
        resp = await client.get(_FRANKFURTER_URL, timeout=8.0)
        resp.raise_for_status()
        payload = resp.json()
        rates = {k: float(v) for k, v in (payload.get("rates") or {}).items()}
        rates["GBP"] = 1.0
        return rates
    finally:
        if own:
            await client.aclose()


async def _get_rates(client: httpx.AsyncClient | None = None) -> dict[str, float]:
    global _cache
    today = _date.today()
    if _cache is not None and _cache[0] == today:
        return _cache[1]
    async with _cache_lock:
        if _cache is not None and _cache[0] == today:
            return _cache[1]
        try:
            rates = await _fetch_rates(client)
        except Exception:
            # Network blip → use fallback table; don't blow up a price call.
            rates = dict(_FALLBACK_RATES)
        _cache = (today, rates)
        return rates


async def to_gbp(
    amount: float | None,
    currency: str | None,
    client: httpx.AsyncClient | None = None,
) -> float | None:
    """Convert `amount` in `currency` to GBP, rounded to 2dp.

    Returns None if either input is None. Returns the input unchanged
    if currency == 'GBP'. Returns None for unknown currencies (caller
    can decide whether that's a bug or just exotic inventory).
    """
    if amount is None or not currency:
        return None
    cur = currency.upper().strip()
    if cur == "GBP":
        return round(float(amount), 2)
    rates = await _get_rates(client)
    rate = rates.get(cur)
    if rate is None:
        return None
    # rates are GBP-base: 1 GBP = `rate` units of `cur`, so divide.
    return round(float(amount) / rate, 2)
