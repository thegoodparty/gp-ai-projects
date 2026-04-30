"""
Legistar scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

import re
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKAHEAD_DAYS, LOOKBACK_DAYS


async def scan_legistar(city: str, config: dict, client: httpx.AsyncClient, source_url: str = "") -> list[dict]:
    """
    Legistar: query the events API for future meetings only.
    EventAgendaLastPublishedUTC non-null → agenda is posted.
    """
    slug = config.get("legistar_slug", "")
    if not slug and source_url:
        # Derive slug from URL (e.g. "https://hampton.legistar.com/..." → "hampton")
        m = re.search(r"https?://([^.]+)\.legistar\.com", source_url)
        if m:
            slug = m.group(1)
    if not slug:
        return []

    today_dt = datetime.now()
    start = (today_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    today = today_dt.strftime("%Y-%m-%d")
    cutoff = (today_dt + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    base_url = f"https://webapi.legistar.com/v1/{slug}"

    try:
        resp = await client.get(
            f"{base_url}/events",
            params={
                "$filter": f"EventDate ge datetime'{start}' and EventDate le datetime'{cutoff}'",
                "$orderby": "EventDate asc",
                "$top": 50,
            },
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"    Legistar fetch error for {slug}: {type(e).__name__}: {e}")
        return []

    upcoming = []
    for ev in events:
        date_raw = ev.get("EventDate", "")
        date = date_raw[:10] if date_raw else None
        if not date:
            continue
        agenda_url = ev.get("EventAgendaFile") or None
        published = ev.get("EventAgendaLastPublishedUTC")
        agenda_posted = bool(published and published != "0001-01-01T00:00:00")

        upcoming.append({
            "date": date,
            "title": ev.get("EventBodyName", city),
            "agenda_posted": agenda_posted,
            "agenda_url": agenda_url,
            "event_id": str(ev.get("EventId", "")),
            "status": "past" if date < today else "upcoming",
        })

    return upcoming



