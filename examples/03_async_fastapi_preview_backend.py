"""An async FastAPI backend that serves music previews to a browser without exposing your API key.

The pattern:
  * One ``rightsfoundry.AsyncClient`` per app, created in the FastAPI lifespan and closed on shutdown.
  * The API key (``TRF_API_KEY``) stays in the server's environment. The browser receives only
    ``id``, ``title``, ``artist``, ``duration`` and the signed MP3 ``preview_url`` (valid for 1 hour).
  * Licensing (``tracks.sync``) is NOT exposed here. It spends money, so put it behind your own auth,
    approval and audit trail.

Costs: every proxied search spends YOUR credits (``costs.tracks_list`` or ``costs.deep_search_first_page``),
so put authentication and rate limiting in front of these routes in production. Preview playback is
intended to be billed per stream (``costs.stream``) from API credits in a future update (metered today,
not yet deducted), so do not auto-play previews nobody asked for.

FastAPI is not a dependency of rightsfoundry. To run this example:
    pip install rightsfoundry fastapi uvicorn
    export TRF_API_KEY="…"
    uvicorn --app-dir examples 03_async_fastapi_preview_backend:app --port 8000
    # then: curl 'http://127.0.0.1:8000/api/tracks?mood=uplifting'
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

import rightsfoundry

try:
    from fastapi import FastAPI, HTTPException, Query, Request
except ImportError as exc:  # pragma: no cover - documentation example
    raise SystemExit("This example needs FastAPI: pip install fastapi uvicorn") from exc

log = logging.getLogger("preview-backend")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One client per event loop. The key is read from TRF_API_KEY here and never leaves this process.
    async with rightsfoundry.AsyncClient(
        max_credits_per_request=10,  # a single proxied call can never cost more than this
        low_credit_warning_threshold=500,
        user_agent_suffix="preview-backend-example/1.0",
    ) as client:
        app.state.trf = client
        yield


app = FastAPI(title="Rights Foundry preview backend (example)", lifespan=lifespan)


def trf(request: Request) -> rightsfoundry.AsyncClient:
    client: rightsfoundry.AsyncClient = request.app.state.trf
    return client


def browser_safe(track: rightsfoundry.Track) -> dict[str, Any]:
    """The only fields a browser needs. Never forward the raw API response."""
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "duration": track.duration,
        "preview_url": track.asset_url,  # signed, expires in 1 hour; the browser plays it without a token
    }


def to_http_error(exc: rightsfoundry.RightsFoundryError) -> HTTPException:
    """Map SDK errors to generic browser-facing errors; keep details (and request_id) in server logs."""
    log.warning("Rights Foundry call failed: %s (charge_state=%s)", exc, exc.charge_state)
    if isinstance(exc, rightsfoundry.ValidationError):
        return HTTPException(status_code=400, detail="invalid search parameters")
    if isinstance(exc, (rightsfoundry.CreditGuardError, rightsfoundry.InsufficientCreditsError)):
        return HTTPException(status_code=503, detail="music search is temporarily unavailable")
    if isinstance(exc, rightsfoundry.RateLimitError):
        return HTTPException(status_code=429, detail="too many requests; try again later")
    return HTTPException(status_code=502, detail="music search failed")


@app.get("/api/tracks")
async def list_tracks(
    request: Request,
    mood: Annotated[Optional[str], Query(max_length=100)] = None,
    genre: Annotated[Optional[str], Query(max_length=100)] = None,
    vocals: Annotated[Optional[str], Query(pattern="^(instrumental|male|female|any)$")] = None,
    page: Annotated[int, Query(ge=1, le=50)] = 1,
) -> list[dict[str, Any]]:
    """Filter the catalogue. Charged per call at ``costs.tracks_list``."""
    try:
        result = await trf(request).tracks.list(mood=mood, genre=genre, vocals=vocals, page=page, limit=20)
    except rightsfoundry.RightsFoundryError as exc:
        raise to_http_error(exc) from None
    return [browser_safe(t) for t in result.data]


@app.get("/api/brief")
async def deep_search(
    request: Request,
    q: Annotated[str, Query(min_length=3, max_length=500)],
) -> list[dict[str, Any]]:
    """Natural-language search. Charged at ``costs.deep_search_first_page`` every time; cache it on your side."""
    try:
        result = await trf(request).tracks.deep_search(q, limit=24)
    except rightsfoundry.RightsFoundryError as exc:
        raise to_http_error(exc) from None
    return [browser_safe(t) for t in result.data]


@app.get("/api/tracks/{track_id}/preview")
async def fresh_preview(request: Request, track_id: int) -> dict[str, Any]:
    """Re-sign an expired preview. Store track IDs, not URLs. Charged at ``costs.track_detail``."""
    try:
        track = await trf(request).tracks.get(track_id)
    except rightsfoundry.TrackNotFoundError:
        raise HTTPException(status_code=404, detail="track not found") from None
    except rightsfoundry.RightsFoundryError as exc:
        raise to_http_error(exc) from None
    return browser_safe(track)


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    """Free, unauthenticated probe of the upstream API (the key is never sent on health calls)."""
    status = await trf(request).health.live()
    return {"upstream": status.status}
