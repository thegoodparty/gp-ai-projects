from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from broker.dynamodb_client import ScopeTicket
from broker.ssrf_guard import resolve_redirects

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/http", tags=["http"])

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
FETCH_TIMEOUT = 30.0
MAX_REDIRECTS = 5


class HttpFetchRequest(BaseModel):
    url: str
    purpose: str = ""


class HttpFetchResponse(BaseModel):
    status: int
    content_type: str
    body: str
    source_url: str
    byte_size: int


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_httpx_client() -> httpx.AsyncClient:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/fetch", response_model=HttpFetchResponse)
async def http_fetch(
    req: HttpFetchRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    client: httpx.AsyncClient = Depends(get_httpx_client),
):
    try:
        resp, current_url = await resolve_redirects(
            client, "GET", req.url, FETCH_TIMEOUT, MAX_REDIRECTS
        )
    except httpx.HTTPError as e:
        logger.warning(
            "http_fetch upstream error run_id=%s url=%s: %s",
            ticket.run_id, req.url, e,
        )
        raise HTTPException(status_code=502, detail=f"upstream request failed: {e}")

    content_length_header = resp.headers.get("content-length")
    if content_length_header is not None:
        try:
            cl = int(content_length_header)
        except ValueError:
            cl = None
        if cl is not None and cl > MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"response too large: {cl} > {MAX_BYTES}")

    raw = resp.content or b""
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"response exceeded {MAX_BYTES} bytes")

    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        body_text = raw.decode("utf-8", errors="replace")

    logger.info(
        "http_fetch ok run_id=%s status=%d bytes=%d purpose=%s url=%s",
        ticket.run_id, resp.status_code, len(raw), req.purpose or "", req.url,
    )

    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or "application/octet-stream"

    return HttpFetchResponse(
        status=resp.status_code,
        content_type=content_type,
        body=body_text,
        source_url=current_url,
        byte_size=len(raw),
    )
