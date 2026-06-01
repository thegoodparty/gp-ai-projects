import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from broker.auth import AuthError, BrokerTokenAuth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/braintrust", tags=["braintrust"])

# The Braintrust SDK splits across two hosts: the control plane (app) handles
# login + metadata, the data plane (api) receives /logs3 trace ingest. The
# runner sets BRAINTRUST_APP_URL=${broker}/braintrust/app and
# BRAINTRUST_API_URL=${broker}/braintrust/api so both legs route through here.
_UPSTREAM_HOSTS = {
    "app": "https://www.braintrust.dev",
    "api": "https://api.braintrust.dev",
}

# Hop-by-hop and host-scoped headers that must not be forwarded verbatim — the
# upstream host/length/auth are all set by the proxy, not the client.
# transfer-encoding is stripped because we read the full body and forward it as
# bytes; httpx then sets content-length, and a request carrying both
# transfer-encoding and content-length is a framing contradiction (RFC 7230
# §3.3.3) that upstreams reject with 400.
_STRIP_REQUEST_HEADERS = {"host", "authorization", "content-length", "connection", "transfer-encoding"}


def get_broker_auth() -> BrokerTokenAuth:
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


def get_upstream_client() -> httpx.AsyncClient:
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


def get_braintrust_api_key() -> str:
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


@router.api_route(
    "/{leg}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_braintrust(
    leg: str,
    path: str,
    request: Request,
    broker_auth: BrokerTokenAuth = Depends(get_broker_auth),
    upstream_client: httpx.AsyncClient = Depends(get_upstream_client),
    api_key: str = Depends(get_braintrust_api_key),
):
    upstream_base = _UPSTREAM_HOSTS.get(leg)
    if upstream_base is None:
        raise HTTPException(status_code=404, detail="unknown_braintrust_leg")

    # The Braintrust SDK authenticates with `Authorization: Bearer <key>`. In
    # the PMF runner that key is the per-run broker token, so we verify it here
    # and swap in the real Braintrust key before forwarding — the runner never
    # holds the real credential.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    broker_token = auth_header[len("Bearer ") :]

    try:
        ticket = broker_auth.verify(broker_token)
    except AuthError as exc:
        logger.warning(
            "braintrust auth failure reason_code=%s token_prefix=%s",
            exc.reason_code,
            broker_token[:8] if broker_token else "empty",
        )
        raise HTTPException(status_code=401, detail="Invalid or expired broker token")

    if not api_key:
        # Fail closed when the broker has no Braintrust key configured. The SDK
        # treats a failed login/ingest as non-fatal (logging just disables), so
        # the agent run continues — we just don't capture traces.
        logger.warning(
            "braintrust proxy hit but BRAINTRUST_API_KEY not configured; refusing run_id=%s leg=%s",
            ticket.run_id,
            leg,
        )
        raise HTTPException(status_code=503, detail="braintrust_not_configured")

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS}
    headers["authorization"] = f"Bearer {api_key}"

    upstream_request = upstream_client.build_request(
        request.method,
        f"{upstream_base}/{path}",
        headers=headers,
        params=request.query_params,
        content=body,
    )

    try:
        upstream_response = await upstream_client.send(upstream_request)
        response_body = await upstream_response.aread()
    except httpx.HTTPError as e:
        logger.warning(
            "braintrust upstream error run_id=%s leg=%s path=%s exc_type=%s",
            ticket.run_id,
            leg,
            path,
            type(e).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"braintrust upstream failed: {type(e).__name__}",
        )

    return Response(
        content=response_body,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type"),
    )
