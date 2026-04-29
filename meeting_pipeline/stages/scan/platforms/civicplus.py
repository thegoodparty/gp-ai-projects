"""
Civicplus scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKBACK_DAYS, LOOKAHEAD_DAYS


async def scan_civicplus(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    CivicPlus AgendaCenter: reuse the existing scraper's category discovery and
    meeting-list fetch — same logic, no PDF download.
    Presence of agenda_pdf_url on a CivicPlusMeeting → agenda_posted=True.
    """
    from urllib.parse import urlparse
    from meeting_pipeline.collectors.civicplus_scraper import find_council_category, fetch_meeting_list

    # Extract domain the same way router.py does
    domain = config.get("domain", "") or urlparse(source_url).netloc.replace("www.", "")
    if not domain:
        return []

    cat_id = config.get("council_category_id") or config.get("category_id")

    today = datetime.now().date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)
    current_year = today.year

    try:
        if not cat_id:
            cat_id, _ = await find_council_category(client, domain)

        # Fetch current year meetings
        raw = await fetch_meeting_list(client, domain, cat_id, current_year)

        # If the lookback window spans into last year (Jan–Feb), also fetch prev year
        if start.year < current_year:
            try:
                prev_year_meetings = await fetch_meeting_list(client, domain, cat_id, current_year - 1)
                raw = prev_year_meetings + raw
            except Exception:
                pass  # best-effort

    except Exception as e:
        print(f"    CivicPlus fetch error for {domain}: {type(e).__name__}: {e}")
        return []

    upcoming = []
    for m in raw:
        try:
            date_obj = datetime.strptime(m.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if date_obj < start or date_obj > cutoff:
            continue
        upcoming.append({
            "date": m.date,
            "title": m.title,
            "agenda_posted": bool(m.agenda_pdf_url),
            "agenda_url": m.agenda_pdf_url,
            "event_id": m.agenda_id,
            "status": "past" if date_obj < today else "upcoming",
        })

    upcoming.sort(key=lambda m: m["date"])
    return upcoming



