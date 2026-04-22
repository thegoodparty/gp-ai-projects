from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from broker.dynamodb_client import ScopeTicket
from broker.ssrf_guard import reject_if_private as _reject_if_private
from broker.ssrf_guard import validate_url as _validate_url

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
    # Manual redirect loop with per-hop SSRF re-validation. Using
    # follow_redirects=True would let a 302 into 169.254.169.254 or 10.x.x.x
    # bypass the initial _validate_url check.
    current_url = req.url
    head = None
    try:
        for hop in range(MAX_REDIRECTS + 1):
            await _validate_url(current_url)
            head = await client.head(
                current_url, timeout=HEAD_TIMEOUT, follow_redirects=False
            )
            if head.status_code not in (301, 302, 303, 307, 308):
                break
            location = head.headers.get("location")
            if not location:
                raise HTTPException(
                    status_code=502,
                    detail="redirect response missing Location header",
                )
            if hop == MAX_REDIRECTS:
                raise HTTPException(
                    status_code=400,
                    detail=f"too many redirects (max {MAX_REDIRECTS})",
                )
            current_url = location
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
    stream_ctx = client.stream("GET", current_url, timeout=STREAM_TIMEOUT, follow_redirects=False)

    async def _iter():
        bytes_seen = 0
        async with stream_ctx as resp:
            if resp.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Upstream GET returned {resp.status_code}")
            async for chunk in resp.aiter_bytes(STREAM_CHUNK):
                bytes_seen += len(chunk)
                if bytes_seen > MAX_BYTES:
                    logger.warning(
                        "pdf_fetch exceeded byte cap run_id=%s url=%s", ticket.run_id, req.url,
                    )
                    raise HTTPException(
                        status_code=413,
                        detail=f"PDF stream exceeded {MAX_BYTES} bytes",
                    )
                yield chunk
        logger.info(
            "pdf_fetch ok run_id=%s purpose=%s bytes=%d url=%s",
            ticket.run_id, req.purpose or "", bytes_seen, req.url,
        )

    headers = {
        "X-Source-URL": req.url,
    }
    if content_length is not None:
        headers["X-Byte-Size"] = str(content_length)

    return StreamingResponse(_iter(), media_type="application/pdf", headers=headers)
