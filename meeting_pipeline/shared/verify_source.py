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

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx


# ── Thresholds (avoid magic numbers in logic) ───────────────────────────────
MIN_AGENDA_KEYWORDS = 3
MIN_PDF_SIZE = 5000
MIN_WORD_COUNT = 50
MIN_SCANNED_PDF_SIZE = 10000

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
            "is_agenda": keyword_hits >= MIN_AGENDA_KEYWORDS and word_count >= MIN_WORD_COUNT,
            "is_scanned": word_count < 20 and len(doc) > 0 and len(content) > MIN_SCANNED_PDF_SIZE,
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

        if len(content) < MIN_PDF_SIZE:
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
        elif pdf["words"] > MIN_WORD_COUNT:
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


async def _find_past_agenda_from_platform(
    platform: str, config: dict, source_url: str, client: httpx.AsyncClient,
) -> str | None:
    """Query platform API for the most recent past event with an agenda file."""
    if platform == "civicclerk":
        match = re.search(r"https://(\w+)\.(?:api\.|portal\.)?civicclerk\.com", source_url)
        if not match:
            return None
        tenant = match.group(1)
        try:
            # Get recent events — check multiple field names for agenda files
            resp = await client.get(
                f"https://{tenant}.api.civicclerk.com/v1/Events/",
                params={"$top": "50"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            events = data.get("value", data) if isinstance(data, dict) else data
            if isinstance(events, list):
                for ev in events:
                    for field in ("AgendaFile", "AgendaUrl", "agendaFile"):
                        url = ev.get(field, "")
                        if isinstance(url, str) and url.startswith("http"):
                            return url
        except Exception:
            pass
        return None

    if platform == "granicus":
        # Try RSS feed for past agendas
        match = re.search(r"https://([^.]+)\.granicus\.com", source_url)
        if not match:
            return None
        tenant = match.group(1)
        try:
            resp = await client.get(
                f"https://{tenant}.granicus.com/ViewPublisherRSS.php",
                params={"mode": "agendas"},
                timeout=15,
            )
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                for item in root.iter("item"):
                    enclosure = item.find("enclosure")
                    if enclosure is not None:
                        url = enclosure.get("url", "")
                        if url and ".pdf" in url.lower():
                            return url
        except Exception:
            pass
        return None

    if platform == "legistar":
        slug = config.get("legistar_slug", "")
        if not slug:
            match = re.search(r"https?://([^.]+)\.legistar\.com", source_url)
            if match:
                slug = match.group(1)
        if not slug:
            return None
        try:
            resp = await client.get(
                f"https://webapi.legistar.com/v1/{slug}/events",
                params={"$orderby": "EventDate desc", "$top": "20"},
                timeout=15,
            )
            resp.raise_for_status()
            for ev in resp.json():
                url = ev.get("EventAgendaFile") or ""
                if url and url.startswith("http"):
                    return url
        except Exception:
            pass
        return None

    if platform in ("civicplus",):
        # CivicPlus agenda URLs from scan would already be caught
        # Can't easily query without the full scraper setup
        return None

    return None


async def _find_pdf_links_on_page(url: str, client: httpx.AsyncClient) -> list[str]:
    """Scrape a webpage and find PDF links that look like agendas."""
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code != 200:
            return []
        html = resp.text
        # Find all href values ending in .pdf
        pdf_links = re.findall(r'href=["\']([^"\']*\.pdf[^"\']*)', html, re.IGNORECASE)
        # Filter to ones that look like agendas
        agenda_pdfs = []
        for link in pdf_links:
            link_lower = link.lower()
            if any(kw in link_lower for kw in ["agenda", "packet", "council", "meeting", "minutes"]):
                # Make absolute URL
                if link.startswith("http"):
                    agenda_pdfs.append(link)
                elif link.startswith("/"):
                    parsed = urlparse(url)
                    agenda_pdfs.append(f"{parsed.scheme}://{parsed.netloc}{link}")
        return agenda_pdfs[:10]
    except Exception:
        return []


async def find_and_verify_source(
    slug: str,
    source: dict,
    storage,
    sources_prefix: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """
    Find the most recent agenda URL for a city and verify it.

    Tries in order:
    1. Future posted agenda URLs from upcoming_meetings.json
    2. Past posted agenda URLs from upcoming_meetings.json
    3. Source URL itself if it's a PDF
    4. Scrape the source page for PDF links that look like agendas

    Args:
        slug: City slug
        source: source.json dict
        storage: StorageBackend
        sources_prefix: S3 prefix for sources
        client: Shared httpx client

    Returns:
        Verification result dict.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )

    try:
        best = source.get("best_source", {})

        # Try to find a posted agenda URL from scan results (future first, then past)
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
                    most_recent = sorted(posted, key=lambda m: m.get("date", ""), reverse=True)[0]
                    agenda_url = most_recent["agenda_url"]
            except Exception:
                pass

        # If no posted agenda URL, try platform API for past agendas
        if not agenda_url:
            platform = best.get("platform", "unknown")
            config = best.get("config", {})
            source_url = best.get("url", "")
            if platform not in ("unknown", "generic_html"):
                agenda_url = await _find_past_agenda_from_platform(platform, config, source_url, client)

        # If still nothing, check if source URL itself is a PDF
        if not agenda_url:
            source_url = best.get("url", "")
            if source_url and ".pdf" in source_url.lower():
                agenda_url = source_url

        # If still nothing, scrape the source page for PDF links
        if not agenda_url:
            source_url = best.get("url", "")
            if source_url:
                pdf_links = await _find_pdf_links_on_page(source_url, client)
                if pdf_links:
                    agenda_url = pdf_links[0]

        if not agenda_url:
            return {
                "status": "unverified",
                "reason": "No agenda URL found to verify (no posted agendas, no PDFs on source page)",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        return await verify_agenda_url(agenda_url, client=client)
    finally:
        if owns_client:
            await client.aclose()
