from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from broker.browser_fetcher import MAX_BYTES, BrowserFetcher
from broker.dynamodb_client import ScopeTicket
from broker.ssrf_guard import validate_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/http", tags=["http"])

_DOWNLOAD_STREAM_CHUNK = 1 * 1024 * 1024  # 1 MB

__all__ = ["router", "get_scope_ticket", "get_browser_fetcher", "MAX_BYTES"]


class HttpFetchRequest(BaseModel):
    url: str
    purpose: str = ""


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_browser_fetcher() -> BrowserFetcher:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def _read_chunk(path: str, offset: int, size: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(size)


async def _stream_file(path: str):
    """Stream a file from disk in 1 MB chunks via asyncio.to_thread so we
    never block the event loop on sync I/O. No aiofiles dep — uses the stdlib
    open() shipped over to a worker thread."""
    offset = 0
    while True:
        chunk = await asyncio.to_thread(_read_chunk, path, offset, _DOWNLOAD_STREAM_CHUNK)
        if not chunk:
            return
        offset += len(chunk)
        yield chunk


@router.post("/fetch")
async def http_fetch(
    req: HttpFetchRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    fetcher: BrowserFetcher = Depends(get_browser_fetcher),
):
    try:
        await validate_url(req.url)

        result = await fetcher.fetch(req.url)

        try:
            await validate_url(result.final_url)
        except HTTPException:
            # final URL is a private/blocked target — clean up any temp file
            # the fetcher handed back before raising.
            if result.body_path:
                try:
                    os.unlink(result.body_path)
                except OSError:
                    logger.warning(
                        "failed to unlink download tmp_path=%s after SSRF post-validate",
                        result.body_path,
                    )
            raise

        logger.info(
            "http_fetch ok run_id=%s status=%d content_type=%s bytes=%d purpose=%s url=%s",
            ticket.run_id,
            result.status,
            result.content_type,
            result.byte_size,
            req.purpose or "",
            req.url,
        )

        headers = {
            "X-Source-URL": result.final_url,
            "X-Byte-Size": str(result.byte_size),
            "X-Upstream-Status": str(result.status),
        }

        if result.body_path is not None:
            # Download path: stream from disk; BackgroundTask unlinks the
            # temp file after the response is fully sent.
            return StreamingResponse(
                _stream_file(result.body_path),
                media_type=result.content_type,
                headers=headers,
                background=BackgroundTask(_unlink_quietly, result.body_path),
            )

        # Page-response path: single in-memory chunk.
        body = result.body or b""

        async def _iter():
            yield body

        return StreamingResponse(
            _iter(),
            media_type=result.content_type,
            headers=headers,
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


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        logger.warning("failed to unlink download tmp_path=%s after response", path)
