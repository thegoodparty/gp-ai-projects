from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from broker.browser_fetcher import BrowserFetcher
from broker.dynamodb_client import ScopeTicket
from broker.ssrf_guard import validate_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdf", tags=["pdf"])

MAX_BYTES = 250 * 1024 * 1024  # 250 MB


class PdfFetchRequest(BaseModel):
    url: str
    purpose: str = ""


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_browser_fetcher() -> BrowserFetcher:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/fetch")
async def pdf_fetch(
    req: PdfFetchRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    fetcher: BrowserFetcher = Depends(get_browser_fetcher),
):
    await validate_url(req.url)

    result = await fetcher.fetch(req.url, capture_download=True)

    await validate_url(result.final_url)

    if result.content_type != "application/pdf":
        raise HTTPException(
            status_code=415,
            detail=f"Upstream content-type is {result.content_type!r}, expected application/pdf",
        )

    if len(result.body) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF too large: {len(result.body)} bytes > {MAX_BYTES}",
        )

    logger.info(
        "pdf_fetch ok run_id=%s purpose=%s bytes=%d url=%s",
        ticket.run_id, req.purpose or "", len(result.body), req.url,
    )

    headers = {
        "X-Source-URL": req.url,
        "X-Byte-Size": str(len(result.body)),
    }

    body = result.body

    async def _iter():
        yield body

    return StreamingResponse(_iter(), media_type="application/pdf", headers=headers)
