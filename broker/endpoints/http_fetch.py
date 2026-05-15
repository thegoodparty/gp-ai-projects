from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from broker.browser_fetcher import BrowserFetcher
from broker.dynamodb_client import ScopeTicket
from broker.ssrf_guard import validate_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/http", tags=["http"])

MAX_BYTES = 250 * 1024 * 1024  # 250 MB


class HttpFetchRequest(BaseModel):
    url: str
    purpose: str = ""


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_browser_fetcher() -> BrowserFetcher:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/fetch")
async def http_fetch(
    req: HttpFetchRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    fetcher: BrowserFetcher = Depends(get_browser_fetcher),
):
    try:
        await validate_url(req.url)

        result = await fetcher.fetch(req.url)

        await validate_url(result.final_url)

        if len(result.body) > MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"response exceeded {MAX_BYTES} bytes",
            )

        logger.info(
            "http_fetch ok run_id=%s status=%d content_type=%s bytes=%d purpose=%s url=%s",
            ticket.run_id,
            result.status,
            result.content_type,
            len(result.body),
            req.purpose or "",
            req.url,
        )

        body = result.body

        async def _iter():
            yield body

        return StreamingResponse(
            _iter(),
            media_type=result.content_type,
            headers={
                "X-Source-URL": result.final_url,
                "X-Byte-Size": str(len(body)),
                "X-Upstream-Status": str(result.status),
            },
        )
    except HTTPException as e:
        logger.warning(
            "http_fetch failed run_id=%s status=%d purpose=%s url=%s detail=%s",
            ticket.run_id,
            e.status_code,
            req.purpose or "",
            req.url,
            e.detail,
        )
        raise
