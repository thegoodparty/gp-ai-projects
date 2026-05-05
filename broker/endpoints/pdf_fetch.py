from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from broker.dynamodb_client import ScopeTicket
from broker.ssrf_guard import resolve_redirects

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdf", tags=["pdf"])

MAX_BYTES = 250 * 1024 * 1024  # 250 MB
STREAM_CHUNK = 64 * 1024
HEAD_TIMEOUT = 10.0
STREAM_TIMEOUT = 180.0
MAX_REDIRECTS = 5


class PdfFetchRequest(BaseModel):
    url: str
    purpose: str = ""


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_httpx_client() -> httpx.AsyncClient:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/fetch")
async def pdf_fetch(
    req: PdfFetchRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    client: httpx.AsyncClient = Depends(get_httpx_client),
):
    try:
        head, current_url = await resolve_redirects(
            client, "HEAD", req.url, HEAD_TIMEOUT, MAX_REDIRECTS
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"HEAD request failed: {e}")

    if head.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Upstream HEAD returned {head.status_code}")

    content_type = (head.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/pdf":
        raise HTTPException(
            status_code=415,
            detail=f"Upstream content-type is {content_type!r}, expected application/pdf",
        )

    content_length_header = head.headers.get("content-length")
    content_length: int | None = None
    if content_length_header is not None:
        try:
            content_length = int(content_length_header)
        except ValueError:
            content_length = None
        if content_length is not None and content_length > MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"PDF too large: {content_length} bytes > {MAX_BYTES}",
            )

    # GET uses the already-validated final URL (current_url) — redirect chain
    # was resolved by the HEAD loop above. follow_redirects=False prevents the
    # GET from chasing any new redirects beyond what HEAD approved.
    # Enter the stream context manually so we can inspect status + headers
    # BEFORE constructing StreamingResponse. Once StreamingResponse is built
    # and returned, Starlette has committed status 200 to the wire — any raise
    # inside the generator becomes a truncated body with a 200 status.
    stream_ctx = client.stream("GET", current_url, timeout=STREAM_TIMEOUT, follow_redirects=False)
    try:
        resp = await stream_ctx.__aenter__()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"GET request failed: {e}")

    try:
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Upstream GET returned {resp.status_code}",
            )

        get_content_length_header = resp.headers.get("content-length")
        if get_content_length_header is not None:
            try:
                get_cl = int(get_content_length_header)
            except ValueError:
                get_cl = None
            if get_cl is not None and get_cl > MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"PDF too large: {get_cl} bytes > {MAX_BYTES}",
                )
    except BaseException:
        await stream_ctx.__aexit__(None, None, None)
        raise

    async def _iter():
        bytes_seen = 0
        try:
            async for chunk in resp.aiter_bytes(STREAM_CHUNK):
                bytes_seen += len(chunk)
                if bytes_seen > MAX_BYTES:
                    # Status 200 is already on the wire — can't 413 now.
                    # Truncate cleanly and log; downstream will see a short PDF.
                    logger.warning(
                        "pdf_fetch truncated at byte cap run_id=%s url=%s bytes_seen=%d cap=%d",
                        ticket.run_id, req.url, bytes_seen, MAX_BYTES,
                    )
                    return
                yield chunk
            logger.info(
                "pdf_fetch ok run_id=%s purpose=%s bytes=%d url=%s",
                ticket.run_id, req.purpose or "", bytes_seen, req.url,
            )
        finally:
            await stream_ctx.__aexit__(None, None, None)

    headers = {
        "X-Source-URL": req.url,
    }
    if content_length is not None:
        headers["X-Byte-Size"] = str(content_length)

    return StreamingResponse(_iter(), media_type="application/pdf", headers=headers)
