"""mcp-travel-scraper — sidecar for browser-driven ferry scrapers.

Runs Playwright + Chromium so the main mcp-travel image can stay slim.
Exposed only on the internal podman-frontend network; no auth.

Endpoints:
  GET  /health                       — readiness probe
  POST /irish-ferries/sailings       — single-date sailings + prices
  POST /irish-ferries/week           — 7-day carousel sailings + prices
"""

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import irish_ferries

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scraper")

app = FastAPI(title="mcp-travel-scraper", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class IrishFerriesRequest(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")
    route: str = Field(..., description="IRLUK / UKIRL / IRLFRA / FRAIRL or alias")
    adults: int = 1
    children: int = 0
    transport: str = "foot"


@app.post("/irish-ferries/sailings")
def irish_ferries_sailings(req: IrishFerriesRequest) -> dict[str, Any]:
    log.info(
        "irish-ferries/sailings date=%s route=%s adults=%d children=%d transport=%s",
        req.date, req.route, req.adults, req.children, req.transport,
    )
    try:
        sailings = irish_ferries.get_sailings(
            date=req.date,
            route=req.route,
            adults=req.adults,
            children=req.children,
            transport=req.transport,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"scraper failed: {e}")
    return {"sailings": sailings}


@app.post("/irish-ferries/week")
def irish_ferries_week(req: IrishFerriesRequest) -> dict[str, Any]:
    log.info(
        "irish-ferries/week start=%s route=%s adults=%d children=%d transport=%s",
        req.date, req.route, req.adults, req.children, req.transport,
    )
    try:
        by_date = irish_ferries.get_sailings_week(
            start_date=req.date,
            route=req.route,
            adults=req.adults,
            children=req.children,
            transport=req.transport,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"scraper failed: {e}")
    return {"by_date": by_date}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("SCRAPER_HOST", "0.0.0.0"),
        port=int(os.environ.get("SCRAPER_PORT", "8080")),
    )
