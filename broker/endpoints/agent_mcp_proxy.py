import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from broker.clerk_client import ClerkClient, ClerkClientError
from broker.dynamodb_client import ScopeTicket

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
    upstream = await http.request(
        method=request.method,
        url=f"{base_url.rstrip('/')}/agent/mcp",
        content=body,
        headers={
            "Content-Type": request.headers.get("content-type", "application/json"),
            "Authorization": f"Bearer {jwt}",
            "X-Organization-Slug": ticket.organization_slug,
        },
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            "content-type": upstream.headers.get("content-type", "application/json")
        },
    )
