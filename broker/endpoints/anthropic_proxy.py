import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from broker.auth import AuthError, BrokerTokenAuth, get_broker_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/anthropic", tags=["anthropic"])

UPSTREAM_BASE = "https://api.anthropic.com"


def get_broker_auth():
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


def get_upstream_client():
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


def get_anthropic_api_key():
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


@router.post("/api/event_logging/batch")
async def event_logging_noop():
    # Claude Agent SDK pings this internal telemetry endpoint per run. We don't
    # proxy it to Anthropic (no value to us, costs a roundtrip). A 204 short-
    # circuits the SDK's retry loop without polluting logs with 404s.
    return Response(status_code=204)


@router.post("/v1/messages")
async def proxy_messages(
    request: Request,
    broker_auth: BrokerTokenAuth = Depends(get_broker_auth),
    upstream_client: httpx.AsyncClient = Depends(get_upstream_client),
    api_key: str = Depends(get_anthropic_api_key),
):
    # The Claude CLI sends ANTHROPIC_API_KEY as x-api-key. In v2 the agent's
    # ANTHROPIC_API_KEY is set to the broker_token, so we authenticate via the
    # x-api-key header the CLI already sends — no custom header needed.
    broker_token = request.headers.get("x-api-key", "")
    if not broker_token:
        raise HTTPException(status_code=401, detail="Missing x-api-key header")
    x_broker_token = request.headers.get("x-broker-token", "")
    if x_broker_token and x_broker_token != broker_token:
        raise HTTPException(status_code=400, detail="header_token_mismatch")
    try:
        ticket = broker_auth.verify(broker_token)
    except AuthError as exc:
        logger.warning(
            "auth failure reason_code=%s token_prefix=%s",
            exc.reason_code, broker_token[:8] if broker_token else "empty",
        )
        raise HTTPException(status_code=401, detail="Invalid or expired broker token")

    body = await request.body()

    headers = {
        "x-api-key": api_key,
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "content-type": "application/json",
    }
    # Forward the client's anthropic-beta header verbatim. The bundled Claude
    # CLI (Agent SDK >=0.2.x) gates request fields like context_management and
    # output_config behind this header; dropping it makes api.anthropic.com
    # reject those fields with 400 "Extra inputs are not permitted", killing
    # every agent run on turn 1. Only forward when the client sent one — never
    # invent a beta header, which could itself trigger a 400.
    client_beta = request.headers.get("anthropic-beta")
    if client_beta:
        headers["anthropic-beta"] = client_beta

    try:
        parsed_body = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    is_streaming = parsed_body.get("stream", False)

    upstream_request = upstream_client.build_request(
        "POST",
        "/v1/messages",
        headers=headers,
        content=body,
    )

    try:
        upstream_response = await upstream_client.send(upstream_request, stream=is_streaming)
    except httpx.HTTPError as e:
        logger.warning(
            "anthropic upstream error run_id=%s model=%s exc_type=%s",
            ticket.run_id, parsed_body.get("model", "?"), type(e).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=f"anthropic upstream failed: {type(e).__name__}",
        )

    if not is_streaming:
        try:
            response_body = await upstream_response.aread()
        except httpx.HTTPError as e:
            logger.warning(
                "anthropic upstream read error run_id=%s model=%s exc_type=%s",
                ticket.run_id, parsed_body.get("model", "?"), type(e).__name__,
            )
            raise HTTPException(
                status_code=502,
                detail=f"anthropic upstream failed: {type(e).__name__}",
            )
        return Response(
            content=response_body,
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get("content-type", "application/json"),
        )

    async def stream_sse():
        try:
            async for chunk in upstream_response.aiter_bytes():
                yield chunk
        except httpx.HTTPError as e:
            exc_type = type(e).__name__
            model = parsed_body.get("model", "?")
            logger.error(
                "anthropic upstream stream truncated run_id=%s org=%s model=%s exc_type=%s: %s",
                ticket.run_id, ticket.organization_slug, model, exc_type, e,
                exc_info=True,
            )
            # Yield a synthetic SSE error event so the downstream SDK fails
            # loudly instead of silently treating a truncated stream as a
            # complete assistant turn. 200 is already on the wire; this is
            # the only way to signal failure mid-stream.
            yield (
                b'event: error\n'
                b'data: {"type":"error","error":{"type":"upstream_stream_truncated",'
                b'"message":"upstream stream ended unexpectedly"}}\n\n'
            )
        finally:
            await upstream_response.aclose()

    return StreamingResponse(
        stream_sse(),
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type", "text/event-stream"),
    )
