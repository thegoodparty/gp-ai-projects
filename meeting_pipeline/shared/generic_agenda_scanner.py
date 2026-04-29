"""
generic_agenda_scanner.py — Scan a non-platform agenda page for meetings.

Used by both discovery (to validate URLs) and scan (for unknown-platform cities).
This is the "expensive path" — involves JS rendering and optionally LLM extraction.
For platform cities (Legistar, CivicPlus, etc.), use the platform-specific scanners.

Cost per call:
  - Basic scrape:  ~1 Firecrawl credit
  - JS render:     ~5 Firecrawl credits (if basic returns minimal content)
  - Gemini extract: ~$0.001 (if filename parsing finds no dates)
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta

from meeting_pipeline.shared.date_utils import parse_date_from_filename
from meeting_pipeline.shared.constants import LOOKBACK_DAYS, LOOKAHEAD_DAYS


def build_meetings_from_llm(
    llm_results: list[dict],
    city: str,
    lookback: date,
    lookahead: date,
    today: date,
    seen_dates: set,
    title_key: str = "title",
) -> list[dict]:
    """Convert LLM-extracted meeting dicts into standard scan format."""
    meetings = []
    for m in llm_results:
        try:
            meeting_date = datetime.strptime(m["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if not (lookback <= meeting_date <= lookahead):
            continue
        if meeting_date in seen_dates:
            continue
        seen_dates.add(meeting_date)
        pdf_url = m.get("pdf_url", "")
        meetings.append({
            "date": meeting_date.isoformat(),
            "title": m.get(title_key) or m.get("body") or f"{city} City Council",
            "agenda_posted": bool(pdf_url),
            "agenda_url": pdf_url,
            "status": "upcoming" if meeting_date >= today else "past",
        })
    meetings.sort(key=lambda x: x["date"])
    return meetings


# ── Firecrawl scrape helpers ──────────────────────────────────────────────────

def _basic_scrape(source_url: str, city: str, state: str) -> dict:
    """Basic Firecrawl scrape (1 credit). Returns {valid, pdf_urls, markdown, ...}."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return {"valid": False, "pdf_urls": [], "error": "FIRECRAWL_API_KEY not set"}
    from meeting_pipeline.shared.firecrawl_client import validate_agenda_page
    return validate_agenda_page(source_url, city, state)


def _js_scrape(source_url: str) -> dict | None:
    """JS-rendered Firecrawl scrape (~5 credits) with retry on timeout.
    Returns {markdown, links, pdf_urls} or None."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return None
    from firecrawl import FirecrawlApp
    app = FirecrawlApp(api_key=os.environ["FIRECRAWL_API_KEY"])

    for attempt in range(2):
        try:
            result = app.scrape(
                source_url,
                formats=["markdown", "links"],
                actions=[{"type": "wait", "milliseconds": 5000}],
            )
            md = getattr(result, "markdown", "") or ""
            links = list(getattr(result, "links", None) or [])
            pdfs = [l for l in links if ".pdf" in l.lower() or "viewfile" in l.lower()]
            return {"markdown": md, "links": links, "pdf_urls": pdfs}
        except Exception as e:
            if attempt == 0 and "timed out" in str(e).lower():
                time.sleep(2)
                continue
            print(f"  [generic_scan] JS scrape failed: {str(e)[:60]}")
            return None
    return None


def _gemini_extract(markdown: str, pdf_urls: list[str], city: str, state: str) -> list[dict]:
    """Use Gemini to extract meeting dates from rendered page content.
    Returns list of {date, title, pdf_url} dicts."""
    from google import genai

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
    pdf_list = "\n".join(pdf_urls[:50]) if pdf_urls else "(no PDFs found)"

    prompt = (
        f"Extract city council meeting dates from this webpage for {city}, {state}.\n"
        f"Return ONLY a JSON array of objects: "
        f'[{{"date": "YYYY-MM-DD", "title": "meeting title", "pdf_url": "direct URL to agenda PDF"}}]\n'
        f"Include both past and future meetings. Only include the primary governing body.\n"
        f"If a meeting has an agenda PDF link, include it. Otherwise leave pdf_url as empty string.\n\n"
        f"Page content (first 6000 chars):\n{markdown[:6000]}\n\n"
        f"PDF links on page:\n{pdf_list}\n"
    )

    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    text = response.text.strip()

    # Parse JSON from response (may be wrapped in ```json ... ```)
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    result = json.loads(text)
    if not isinstance(result, list):
        result = result.get("meetings", []) if isinstance(result, dict) else []
    return result


# ── Main scanner ──────────────────────────────────────────────────────────────

# Cost tracking — callers should update these
cost = {
    "firecrawl_basic": 0,
    "firecrawl_js": 0,
    "gemini_extract": 0,
    "firecrawl_llm_extract": 0,
}

async def scan_generic(
    source_url: str,
    city: str,
    state: str,
    lookback_days: int = LOOKBACK_DAYS,
    lookahead_days: int = LOOKAHEAD_DAYS,
) -> list[dict]:
    """
    Scan a non-platform agenda page for meetings.

    Three-tier approach:
    1. Basic scrape → parse dates from PDF filenames (cheap, 1 credit)
    2. JS render → parse dates from PDF filenames (if basic returned no content, ~5 credits)
    3. Gemini LLM extraction from rendered content (if no dates in filenames, ~$0.001)

    Returns list of meeting dicts in standard scan format.
    """
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return []

    today = date.today()
    lookback = today - timedelta(days=lookback_days)
    lookahead = today + timedelta(days=lookahead_days)

    # ── Tier 1: Basic scrape ──────────────────────────────────────────────
    try:
        cost["firecrawl_basic"] += 1
        fc = _basic_scrape(source_url, city, state)
    except Exception as e:
        print(f"  [generic_scan] Firecrawl failed for {source_url[:50]}: {e}")
        return []

    pdf_urls = fc.get("pdf_urls") or []
    markdown = fc.get("markdown", "") or ""

    # ── Tier 2: JS render (if basic returned minimal content) ─────────────
    if len(markdown) < 1000:
        cost["firecrawl_js"] += 1
        js = _js_scrape(source_url)
        if js and (js["pdf_urls"] or len(js["markdown"]) > 1000):
            pdf_urls = js["pdf_urls"]
            markdown = js["markdown"]

    # ── Parse dates from PDF filenames ────────────────────────────────────
    meetings = []
    seen_dates: set = set()

    for pdf_url in pdf_urls:
        filename = pdf_url.split("/")[-1].split("?")[0].lower()

        # Skip minutes-only PDFs
        if "minute" in filename and "agenda" not in filename:
            continue

        meeting_date = parse_date_from_filename(filename)
        if not meeting_date:
            continue
        if not (lookback <= meeting_date <= lookahead):
            continue
        if meeting_date in seen_dates:
            continue
        seen_dates.add(meeting_date)

        meetings.append({
            "date": meeting_date.isoformat(),
            "title": f"{city} City Council",
            "agenda_posted": True,
            "agenda_url": pdf_url,
            "status": "upcoming" if meeting_date >= today else "past",
        })

    meetings.sort(key=lambda m: m["date"])

    # ── Tier 3: Gemini LLM extraction (if filename parsing found nothing) ─
    if not meetings and len(markdown) > 500:
        try:
            cost["gemini_extract"] += 1
            llm_results = _gemini_extract(markdown, pdf_urls, city, state)
            meetings = build_meetings_from_llm(
                llm_results, city, lookback, lookahead, today, seen_dates
            )
            if meetings:
                print(f"  [generic_scan] Gemini extracted {len(meetings)} meetings for {city}")
        except Exception as e:
            print(f"  [generic_scan] Gemini extract failed for {city}: {e}")

    # ── Tier 3b: Firecrawl LLM extract (last resort, no rendered content) ─
    if not meetings and len(markdown) <= 500:
        try:
            from meeting_pipeline.shared.firecrawl_client import extract_meeting_links
            cost["firecrawl_llm_extract"] += 1
            llm_results = extract_meeting_links(source_url, city, state)
            meetings = build_meetings_from_llm(
                llm_results, city, lookback, lookahead, today, seen_dates,
                title_key="body",
            )
            if meetings:
                print(f"  [generic_scan] Firecrawl LLM found {len(meetings)} meetings for {city}")
        except Exception as e:
            print(f"  [generic_scan] Firecrawl LLM failed for {city}: {e}")

    return meetings
