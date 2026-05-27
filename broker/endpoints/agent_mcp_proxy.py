import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from broker.clerk_client import ClerkClient, ClerkClientError
from broker.dynamodb_client import ScopeTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/mcp", tags=["agent_mcp"])


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_clerk_client() -> ClerkClient:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_gp_api_base_url() -> str:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_http_client() -> httpx.AsyncClient:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.api_route(
    "",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy_mcp_root(
    request: Request,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    clerk: ClerkClient = Depends(get_clerk_client),
    base_url: str = Depends(get_gp_api_base_url),
    http: httpx.AsyncClient = Depends(get_http_client),
):
    if not ticket.clerk_session_id:
        raise HTTPException(
            status_code=500,
            detail={"reason": "ticket_missing_clerk_session_id"},
        )

    try:
        jwt = await clerk.get_session_jwt(ticket.clerk_session_id)
    except ClerkClientError as exc:
        raise HTTPException(
            status_code=502,
            detail={"reason": "clerk_session_jwt_mint_failed", "err": str(exc)},
        )

    body = await request.body()
    upstream_request = http.build_request(
        method=request.method,
        url=f"{base_url.rstrip('/')}/v1/mcp",
        content=body,
        headers={
            "Content-Type": request.headers.get("content-type", "application/json"),
            # gp-api's MCP Streamable HTTP transport returns 406 without this
            # exact Accept value (per MCP spec). Hardcoded rather than
            # forwarded because callers (incl. FastAPI TestClient) routinely
            # send `*/*`, which gp-api would still reject. Inviting SSE here
            # means the upstream may respond with `text/event-stream`; the
            # branch below preserves streaming end-to-end.
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {jwt}",
            "X-Organization-Slug": ticket.organization_slug,
        },
    )
    try:
        upstream = await http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning(
            "gp-api upstream send error run_id=%s org=%s exc_type=%s",
            ticket.run_id, ticket.organization_slug, type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "reason": "gp_api_upstream_failed",
                "err": type(exc).__name__,
            },
        )

    upstream_content_type = upstream.headers.get("content-type", "application/json")

    if "text/event-stream" in upstream_content_type.lower():
        async def stream_sse():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            except httpx.HTTPError as exc:
                logger.error(
                    "mcp upstream stream truncated run_id=%s org=%s exc_type=%s: %s",
                    ticket.run_id, ticket.organization_slug,
                    type(exc).__name__, exc,
                    exc_info=True,
                )
                # 200 status is already on the wire. Without an in-band
                # error event the downstream MCP client treats a
                # truncated stream as a complete tool result. Yield a
                # synthetic SSE error so failure is observable.
                yield (
                    b'event: error\n'
                    b'data: {"type":"error","error":'
                    b'{"type":"upstream_stream_truncated",'
                    b'"message":"upstream stream ended unexpectedly"}}\n\n'
                )
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream_sse(),
            status_code=upstream.status_code,
            media_type=upstream_content_type,
        )

    try:
        content = await upstream.aread()
    finally:
        await upstream.aclose()

    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=upstream_content_type,
    )
