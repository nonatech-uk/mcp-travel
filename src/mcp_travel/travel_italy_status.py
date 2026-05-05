"""ViaggiaTreno live status — Italian rail real-time data.

Separate from travel_trenitalia (static city-pair durations) — this
covers live train tracking and station departures, the still-public
side of ViaggiaTreno's REST endpoints under /infomobilita/resteasy/...

Endpoints used:
  GET /cercaStazione/{prefix}    — station autocomplete (id + nomeLungo)
  GET /partenze/{stationId}/{datetime} — live departures board

The datetime format ViaggiaTreno expects is JS Date.toString()-style:
  'Tue May 05 2026 08:00:00 GMT+0000'
URL-encoded.
"""

import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

VT_BASE = "https://www.viaggiatreno.it/infomobilita/resteasy/viaggiatreno"
VT_UA = f"mcp-travel/1.0 ({os.environ.get('MCP_TRAVEL_CONTACT', 'mcp-travel@example.com')})"


class ItalyStatusError(RuntimeError):
    pass


def _vt_datetime(iso: str) -> str:
    """ISO datetime → JS Date.toString() format ViaggiaTreno expects."""
    if "T" in iso:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(iso + "T00:00:00+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a %b %d %Y %H:%M:%S GMT+0000")


async def resolve_station(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    resp = await client.get(
        f"{VT_BASE}/cercaStazione/{quote(query.strip().upper())}",
        headers={"User-Agent": VT_UA},
        follow_redirects=True,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise ItalyStatusError(f"viaggiatreno /cercaStazione {resp.status_code}: {resp.text[:200]}")
    items = resp.json()
    if not items:
        return None
    return {"id": items[0].get("id"), "name": items[0].get("nomeLungo"), "label": items[0].get("label")}


def _summarise_train(t: dict) -> dict:
    """Pull useful fields off a partenze entry."""
    delay = t.get("ritardo") or 0
    return {
        "train_number": t.get("numeroTreno"),
        "category": t.get("categoria") or t.get("categoriaDescrizione"),
        "destination": t.get("destinazione"),
        "track": t.get("binarioProgrammatoPartenzaDescrizione") or t.get("binarioEffettivoPartenzaDescrizione"),
        "scheduled": t.get("compOrarioPartenza"),
        "delay_minutes": delay,
        "departed": t.get("arrivato"),
        "in_station": t.get("inStazione"),
        "non_partito": t.get("nonPartito"),
        "operator": t.get("compNumeroTreno"),
    }


async def departures(
    client: httpx.AsyncClient,
    station_query: str,
    datetime_iso: str | None = None,
    max_results: int = 20,
) -> dict[str, Any]:
    s = await resolve_station(client, station_query)
    if not s:
        raise ItalyStatusError(f"no station match for {station_query!r}")
    if datetime_iso:
        when = _vt_datetime(datetime_iso)
    else:
        when = datetime.now(timezone.utc).strftime("%a %b %d %Y %H:%M:%S GMT+0000")

    resp = await client.get(
        f"{VT_BASE}/partenze/{s['id']}/{quote(when)}",
        headers={"User-Agent": VT_UA},
        follow_redirects=True,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise ItalyStatusError(f"viaggiatreno /partenze {resp.status_code}: {resp.text[:200]}")
    items = resp.json() or []
    trains = [_summarise_train(t) for t in items[:max_results]]
    return {
        "ok": True,
        "country": "IT",
        "operator_data_source": "ViaggiaTreno (Trenitalia infomobilità live)",
        "data_sources": ["viaggiatreno-live"],
        "station": s["name"],
        "station_id": s["id"],
        "datetime": when,
        "departures": trains,
    }
