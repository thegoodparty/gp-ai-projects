from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from broker.browser_fetcher import BrowserFetcher
from broker.dynamodb_client import ScopeTicket
from broker.ssrf_guard import validate_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/http", tags=["http"])

MAX_BYTES = 10 * 1024 * 1024  # 10 MB


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


def get_browser_fetcher() -> BrowserFetcher:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/fetch", response_model=HttpFetchResponse)
async def http_fetch(
    req: HttpFetchRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    fetcher: BrowserFetcher = Depends(get_browser_fetcher),
):
    try:
        await validate_url(req.url)

        result = await fetcher.fetch(req.url, capture_download=False)

        await validate_url(result.final_url)

        if len(result.body) > MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"response exceeded {MAX_BYTES} bytes",
            )

        try:
            body_text = result.body.decode("utf-8")
        except UnicodeDecodeError:
            body_text = result.body.decode("utf-8", errors="replace")

        logger.info(
            "http_fetch ok run_id=%s status=%d bytes=%d purpose=%s url=%s",
            ticket.run_id, result.status, len(result.body), req.purpose or "", req.url,
        )

        return HttpFetchResponse(
            status=result.status,
            content_type=result.content_type,
            body=body_text,
            source_url=result.final_url,
            byte_size=len(result.body),
        )
    except HTTPException as e:
        logger.warning(
            "http_fetch failed run_id=%s status=%d purpose=%s url=%s detail=%s",
            ticket.run_id, e.status_code, req.purpose or "", req.url, e.detail,
        )
        raise
