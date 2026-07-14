"""Reverse proxy for the Finance service, mounted by ``web_server``.

The Finance trading loop (Loop.md §5.6/§5.9) runs as its OWN process — the
`swing-trader` service in `trader/` — so there is exactly one
server-authoritative confirmation state machine and heavy trading work never
blocks the dashboard event loop. The dashboard exposes it to Web/Desktop at
``/api/finance/*`` by proxying to the local service; requests inherit the
dashboard's auth middleware automatically (all ``/api/`` paths are gated).

Kept out of ``web_server.py`` so the finance surface stays in one file, like
``memory_oauth``. If the service is down, every route answers 503 with a
hint instead of erroring, so the Finance tab can render an offline state.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request, Response

FINANCE_SERVICE_URL = os.environ.get(
    "HERMES_FINANCE_SERVICE_URL", "http://127.0.0.1:9319"
).rstrip("/")

# Hop-by-hop headers must not be forwarded (RFC 9110 §7.6.1).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}

_OFFLINE_BODY = (
    b'{"error": "finance service offline", '
    b'"hint": "start it with: cd trader && uv run python -m swing_trader serve"}'
)

router = APIRouter(prefix="/api/finance")


@router.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
async def proxy(path: str, request: Request) -> Response:
    import httpx  # lazy: dashboard deps already include httpx

    url = f"{FINANCE_SERVICE_URL}/{path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "cookie"
        # The dashboard session cookie/token must never leak to the
        # finance service; auth was already enforced by the middleware.
        and k.lower() != "x-hermes-session-token"
    }
    body = await request.body()
    try:
        # 60s (not 15s): some finance endpoints do many yfinance / fund-NAV
        # calls — a cold `marks/refresh` (~20 symbols) or `/analyze` routinely
        # exceeds 15s and would otherwise 503 as "service offline". (The very
        # slow research/run returns immediately via a background thread.)
        async with httpx.AsyncClient(timeout=60.0) as client:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body if body else None,
                headers=headers,
            )
    except httpx.HTTPError:
        return Response(
            content=_OFFLINE_BODY, status_code=503, media_type="application/json"
        )
    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )
