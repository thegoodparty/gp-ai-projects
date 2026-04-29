"""
verify_source.py — Verify that a discovered source can actually serve agenda PDFs.

Called by discovery after finding the best candidate, and by the verify_agenda_urls
tool for batch verification. Downloads the most recent agenda and checks if it's
a real, extractable PDF with agenda content.

Results are stored in source.json as best_source.verification:
    {
        "status": "verified" | "verified_ocr_needed" | "verified_non_pdf" | "unverified",
        "reason": "...",
        "sample_url": "https://...",
        "sample_size_kb": 142,
        "sample_words": 1200,
        "agenda_keywords": 7,
        "checked_at": "2026-04-29T..."
    }
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx


AGENDA_KEYWORDS = [
    "agenda", "meeting", "council", "motion", "approve", "resolution",
    "ordinance", "public hearing", "consent", "roll call", "adjournment",
    "minutes", "action item", "discussion", "vote", "quorum",
]


def _check_pdf_content(content: bytes) -> dict:
    """Extract text from PDF bytes and check if it looks like an agenda."""
    try:
        import fitz
        doc = fitz.open(stream=content, filetype="pdf")
        pages = min(len(doc), 5)
        text = "\n".join(doc[i].get_text() for i in range(pages))
        word_count = len(text.split())
        text_lower = text.lower()
        keyword_hits = sum(1 for kw in AGENDA_KEYWORDS if kw in text_lower)
        return {
            "pages": len(doc),
            "words": word_count,
            "keyword_hits": keyword_hits,
            "is_agenda": keyword_hits >= 3 and word_count >= 50,
            "is_scanned": word_count < 20 and len(doc) > 0 and len(content) > 10000,
        }
    except Exception as e:
        return {
            "pages": 0, "words": 0, "keyword_hits": 0,
            "is_agenda": False, "is_scanned": False, "error": str(e),
        }


async def verify_agenda_url(
    url: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """
    Download a URL and verify it serves a real agenda document.

    Args:
        url: Direct URL to an agenda PDF or document
        client: Shared httpx client (created if not provided)

    Returns:
        Verification result dict with status, reason, and metrics.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )

    result = {
        "sample_url": url[:200],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        resp = await client.get(url, timeout=20)

        if resp.status_code != 200:
            result["status"] = "unverified"
            result["reason"] = f"HTTP {resp.status_code}"
            return result

        content_type = resp.headers.get("content-type", "")
        content = resp.content
        result["sample_size_kb"] = len(content) // 1024

        # Check if PDF
        is_pdf = "pdf" in content_type.lower() or content[:5] == b"%PDF-"

        # Check for other document types
        is_docx = "openxmlformats" in content_type or "officedocument" in content_type
        is_html = "text/html" in content_type

        if is_html and len(content) < 50000:
            result["status"] = "unverified"
            result["reason"] = "URL serves HTML page, not a document"
            return result

        if is_docx:
            result["status"] = "verified_non_pdf"
            result["reason"] = "Document is Word/Office format (not PDF)"
            result["sample_words"] = 0
            result["agenda_keywords"] = 0
            return result

        if not is_pdf:
            result["status"] = "unverified"
            result["reason"] = f"Not a PDF (content-type: {content_type[:50]})"
            return result

        if len(content) < 5000:
            result["status"] = "unverified"
            result["reason"] = f"PDF too small ({len(content)} bytes) — likely placeholder"
            return result

        # Extract and check content
        pdf = _check_pdf_content(content)
        result["sample_words"] = pdf["words"]
        result["agenda_keywords"] = pdf["keyword_hits"]

        if pdf["is_agenda"]:
            result["status"] = "verified"
            result["reason"] = f"Real agenda PDF ({pdf['words']} words, {pdf['keyword_hits']} agenda keywords)"
        elif pdf["is_scanned"]:
            result["status"] = "verified_ocr_needed"
            result["reason"] = f"Scanned PDF ({len(content) // 1024}KB, {pdf['pages']} pages, needs OCR)"
        elif pdf["words"] > 50:
            result["status"] = "verified"
            result["reason"] = f"PDF with text ({pdf['words']} words, {pdf['keyword_hits']} agenda keywords — low but extractable)"
        else:
            result["status"] = "verified_ocr_needed"
            result["reason"] = f"PDF with minimal text ({pdf['words']} words) — likely scanned"

        return result

    except httpx.TimeoutException:
        result["status"] = "unverified"
        result["reason"] = "Request timed out"
        return result
    except Exception as e:
        result["status"] = "unverified"
        result["reason"] = f"{type(e).__name__}: {str(e)[:80]}"
        return result
    finally:
        if owns_client:
            await client.aclose()


async def find_and_verify_source(
    slug: str,
    source: dict,
    storage,
    sources_prefix: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """
    Find the most recent agenda URL for a city and verify it.

    Checks upcoming_meetings.json for posted agendas. If none found,
    checks the source URL directly.

    Args:
        slug: City slug
        source: source.json dict
        storage: StorageBackend
        sources_prefix: S3 prefix for sources
        client: Shared httpx client

    Returns:
        Verification result dict.
    """
    best = source.get("best_source", {})
    platform = best.get("platform", "unknown")

    # Try to find a posted agenda URL from scan results
    um_key = f"{sources_prefix}/{slug}/upcoming_meetings.json"
    agenda_url = None

    if storage.exists(um_key):
        try:
            um = storage.read_json(um_key)
            posted = [
                m for m in um.get("upcoming", [])
                if m.get("agenda_posted")
                and isinstance(m.get("agenda_url"), str)
                and m["agenda_url"].startswith("http")
            ]
            if posted:
                # Most recent posted meeting
                most_recent = sorted(posted, key=lambda m: m.get("date", ""), reverse=True)[0]
                agenda_url = most_recent["agenda_url"]
        except Exception:
            pass

    # If no posted agenda from scan, check if source URL itself is a PDF
    if not agenda_url:
        source_url = best.get("url", "")
        if source_url and ".pdf" in source_url.lower():
            agenda_url = source_url

    if not agenda_url:
        return {
            "status": "unverified",
            "reason": "No agenda URL found to verify",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    return await verify_agenda_url(agenda_url, client=client)
