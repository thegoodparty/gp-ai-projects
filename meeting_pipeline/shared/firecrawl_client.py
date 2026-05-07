"""
firecrawl_client.py — Firecrawl API helpers for source discovery and PDF collection.

Requires FIRECRAWL_API_KEY in environment (meeting_pipeline/.env).

SDK note: firecrawl-py >= 1.0 uses FirecrawlApp.scrape() (not scrape_url).
Returns firecrawl.v2.types.Document with .markdown, .metadata, .links attrs.
Metadata is a Pydantic model — access as dict via model_dump() or getattr.
"""
from __future__ import annotations

import os
from datetime import date


def _client():
    from firecrawl import FirecrawlApp
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    return FirecrawlApp(api_key=api_key)


def get_remaining_credits() -> int | None:
    """
    Return the number of Firecrawl credits remaining this billing period, or None
    if unavailable (no API key, network error, plan doesn't expose this field).
    """
    try:
        app = _client()
        if not hasattr(app, "get_credit_usage"):
            return None
        result = app.get_credit_usage()
        data = getattr(result, "data", None)
        if data is None:
            return None
        return getattr(data, "remaining_credits", None)
    except Exception:
        return None


def _meta_dict(metadata) -> dict:
    """Normalize Firecrawl metadata to a plain dict regardless of SDK version."""
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return metadata
    if hasattr(metadata, "model_dump"):
        return metadata.model_dump()
    if hasattr(metadata, "__dict__"):
        return vars(metadata)
    return {}


def search_agenda_page(city: str, state: str) -> str | None:
    """
    Use Firecrawl search to find the city council agenda page URL.
    Returns the most likely URL or None.
    """
    app = _client()
    query = f"{city} {state} city council meeting agenda {date.today().year}"
    results = app.search(query, limit=5)
    docs = getattr(results, "web", None) or (results.get("data", []) if isinstance(results, dict) else [])
    for doc in docs:
        url = getattr(doc, "url", None) or (doc.get("url", "") if isinstance(doc, dict) else "")
        title = (getattr(doc, "title", None) or (doc.get("title", "") if isinstance(doc, dict) else "")).lower()
        if not url:
            continue
        if any(kw in url.lower() for kw in ["agenda", "meeting", "council", "minutes"]):
            return url
        if any(kw in title for kw in ["agenda", "meeting", "council", "city of"]):
            return url
    if docs:
        first = docs[0]
        return getattr(first, "url", None) or (first.get("url") if isinstance(first, dict) else None)
    return None


def extract_meeting_links(url: str, city: str, state: str) -> list[dict]:
    """
    Use Firecrawl extract to pull structured meeting data from an agenda page.
    Returns list of {date, body, pdf_url} dicts for confirmed city council meetings
    that have a PDF URL.
    """
    app = _client()
    try:
        result = app.extract(
            [url],
            prompt=(
                f"Extract city council meeting dates and agenda PDF URLs for {city}, {state}. "
                "Return a JSON object with a 'meetings' array. Each item should have: "
                "'date' (YYYY-MM-DD), 'body' (governing body name), 'pdf_url' (direct URL to the agenda PDF), "
                "'is_city_council' (true if this is the primary city council — not school board, planning commission, etc.). "
                "Only include meetings with a direct PDF link. Exclude school boards, planning commissions, zoning boards."
            ),
        )
        data = getattr(result, "data", None) or (result.get("data", {}) if isinstance(result, dict) else {})
        if not isinstance(data, dict):
            return []
        meetings = data.get("meetings", [])
        out = []
        for m in meetings:
            if not isinstance(m, dict):
                continue
            pdf_url = m.get("pdf_url") or m.get("agenda_pdf_url") or m.get("agenda_url") or ""
            if pdf_url and m.get("is_city_council", True):
                out.append({"date": m.get("date", ""), "body": m.get("body", "City Council"), "pdf_url": pdf_url})
        return out
    except Exception as e:
        print(f"  [firecrawl] extract failed for {url}: {e}")
        return []


def validate_agenda_page(url: str, city: str, state: str) -> dict:
    """
    Cheap candidate validation: scrape the page (1 credit) and check if it
    contains city council agenda content.
    """
    app = _client()
    try:
        result = app.scrape(url, formats=["markdown", "links"])
        markdown = (getattr(result, "markdown", None) or "")
        metadata = _meta_dict(getattr(result, "metadata", None))
        links = list(getattr(result, "links", None) or [])

        most_recent_date = None
        for date_key in ("article:modified_time", "modifiedTime", "publishedTime", "article:published_time"):
            val = metadata.get(date_key, "") or ""
            if val and len(val) >= 10:
                most_recent_date = val[:10]
                break

        text_lower = markdown.lower()
        agenda_signals = sum([
            "agenda" in text_lower,
            "city council" in text_lower or "council meeting" in text_lower,
            "minutes" in text_lower,
            ".pdf" in text_lower or "pdf" in text_lower,
        ])
        reject_signals = sum([
            "/article/" in url.lower(),
            "news" in url.lower() and "agenda" not in url.lower(),
            len(markdown) < 300,
        ])
        valid = agenda_signals >= 2 and reject_signals == 0

        pdf_urls = [link for link in links if isinstance(link, str) and ".pdf" in link.lower()]

        return {
            "valid": valid,
            "most_recent_date": most_recent_date,
            "pdf_urls": pdf_urls[:10],
            # Surface the raw scrape so callers can run an LLM-based agenda
            # extractor over it without re-fetching from Firecrawl.
            "markdown": markdown,
            "links": [link for link in links if isinstance(link, str)],
        }
    except Exception as e:
        return {
            "valid": False,
            "most_recent_date": None,
            "pdf_urls": [],
            "markdown": "",
            "links": [],
            "error": str(e),
        }


def find_agenda_pdf_via_llm(
    markdown: str,
    links: list[str],
    city: str,
    state: str,
) -> str | None:
    """LLM fallback when regex/heuristic PDF detection fails.

    Many big-city `unknown`-platform sites bury the agenda PDF link behind a
    nav structure or use ambiguous filenames our scoring can't rank. Hand the
    landing-page markdown + all extracted links to Gemini Flash Lite and ask
    it to return the most-recent council agenda PDF URL.

    Returns the URL string, or None if no plausible agenda PDF was identified.
    Does NOT verify the PDF — that's the caller's job (verify_agenda_url).

    Cheap (Flash Lite, ~$0.003/call). Only invoke when the cheap heuristic
    paths failed.
    """
    if not markdown:
        return None

    # Truncate to keep input bounded — Gemini handles the size, we cap to
    # control cost and stay under context limits.
    md_excerpt = markdown[:30_000]
    link_excerpt = links[:80]

    prompt = (
        f"You are looking at the agenda landing page of {city}, {state}.\n\n"
        f"Page markdown (truncated):\n{md_excerpt}\n\n"
        f"All links extracted from the page:\n"
        + "\n".join(f"- {link}" for link in link_excerpt)
        + "\n\n"
        "Return the full URL of the most recent City Council meeting agenda "
        "PDF (or packet). Rules:\n"
        "  - Prefer the City Council, not Planning Commission, School Board, or "
        "    other sub-bodies.\n"
        "  - Prefer the most recent meeting available.\n"
        "  - Return the URL exactly as it appears.\n"
        "  - If no agenda PDF is visible, return null."
    )

    # Gemini's structured-output API doesn't accept JSON-Schema-style nullable
    # unions like ["string","null"]. Use a Pydantic model with Optional[str]
    # — that's what the GeminiClient is set up to translate cleanly.
    from pydantic import BaseModel

    class _AgendaUrlResult(BaseModel):
        agenda_url: str | None = None

    try:
        # Use Flash (not Flash Lite) — this path runs at low frequency
        # (only when heuristic discovery already failed) and Flash Lite has
        # been hitting 503 capacity errors more often than Flash.
        from shared.llm_gemini import GeminiClient, GeminiModelType
        client = GeminiClient(default_model=GeminiModelType.FLASH)
        result = client.generate_structured_content(prompt, response_schema=_AgendaUrlResult)
    except Exception as e:
        print(f"  [llm_agenda_finder] failed: {e}")
        return None

    if isinstance(result, dict):
        url = result.get("agenda_url")
    else:
        url = getattr(result, "agenda_url", None)

    if isinstance(url, str) and url.startswith("http"):
        return url
    return None


def scrape_pdf_text(pdf_url: str) -> str | None:
    """
    Use Firecrawl to extract text from a PDF URL, including OCR for scanned PDFs.
    """
    app = _client()
    try:
        result = app.scrape(
            pdf_url,
            formats=["markdown"],
            parsers=["pdf"],
        )
        content = getattr(result, "markdown", None) or ""
        if isinstance(result, dict):
            content = result.get("markdown", "") or ""
        return content if len(content.strip()) > 100 else None
    except Exception as e:
        print(f"  [firecrawl] PDF scrape failed for {pdf_url}: {e}")
        return None


def scrape_civicclerk_event_files(portal_url: str, event_id: str) -> list[str]:
    """
    Scrape a CivicClerk event page to find agenda PDF links when the API returns 0 files.
    """
    app = _client()
    event_url = f"{portal_url}/event/{event_id}/files"
    try:
        result = app.scrape(
            event_url,
            formats=["links"],
            actions=[
                {"type": "wait", "milliseconds": 2000},
                {"type": "click", "selector": "[class*='agenda'], [class*='Agenda'], a[href*='pdf']"},
                {"type": "wait", "milliseconds": 1000},
            ],
        )
        links = list(getattr(result, "links", None) or [])
        if isinstance(result, dict):
            links = result.get("links", []) or []
        return [link for link in links if isinstance(link, str) and (".pdf" in link.lower() or "agenda" in link.lower())]
    except Exception as e:
        print(f"  [firecrawl] CivicClerk event scrape failed for event {event_id}: {e}")
        return []
