"""
Civicclerk scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKBACK_DAYS, LOOKAHEAD_DAYS


async def scan_civicclerk(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    CivicClerk OData API: query for future events.

    Supports two API generations:
    - Legacy ({tenant}.api.civicclerk.com): filter field = MeetingStartDate, datetime format
    - Portal ({tenant}.portal.civicclerk.com): filter field = startDateTime, date-only format
      Both share the same backend at {tenant}.api.civicclerk.com/v1.
    """
    match = re.search(r"https://(\w+)\.(?:api\.|portal\.)?civicclerk\.com", source_url)
    if not match:
        tenant = config.get("tenant", "")
        if not tenant:
            return []
    else:
        tenant = match.group(1)

    is_portal = "portal.civicclerk.com" in source_url

    today_dt = datetime.now()
    today_str = today_dt.strftime("%Y-%m-%d")
    start_date = (today_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    cutoff_date = (today_dt + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    start_dt = (today_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00")
    cutoff_dt = (today_dt + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT00:00:00")

    if is_portal:
        # Portal API uses startDateTime with date-only comparison values
        params = {
            "$filter": f"startDateTime ge {start_date} and startDateTime le {cutoff_date}",
            "$orderby": "startDateTime asc",
            "$top": 100,
        }
    else:
        params = {
            "$filter": f"MeetingStartDate ge {start_dt} and MeetingStartDate le {cutoff_dt}",
            "$orderby": "MeetingStartDate asc",
            "$top": 50,
        }

    try:
        resp = await client.get(
            f"https://{tenant}.api.civicclerk.com/v1/Events/",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data.get("value", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"    CivicClerk fetch error for {tenant}: {type(e).__name__}: {e}")
        return []

    upcoming = []
    for ev in events:
        if is_portal:
            date_raw = ev.get("startDateTime", ev.get("eventDate", ""))
            title = ev.get("eventName", city)
            agenda_url = ev.get("agendaFile") or None
            agenda_posted = bool(agenda_url)  # hasAgenda=True without agendaFile means no downloadable document
            event_id = str(ev.get("id", ""))
        else:
            date_raw = ev.get("MeetingStartDate", ev.get("EventDate", ""))
            title = ev.get("Name", ev.get("EventName", city))
            agenda_url = ev.get("AgendaFile") or ev.get("AgendaUrl") or None
            # Only mark agenda_posted if we have a direct URL or a real posted date
            # (not the CivicClerk default 0001-01-01 which means "scheduled but no file")
            posted_date = ev.get("AgendaPostedDate") or ""
            has_real_posted_date = bool(posted_date) and not posted_date.startswith("0001")
            agenda_posted = bool(agenda_url) or has_real_posted_date
            event_id = str(ev.get("EventId", ev.get("Id", "")))

        date = date_raw[:10] if date_raw else None
        if not date:
            continue
        upcoming.append({
            "date": date,
            "title": title,
            "agenda_posted": agenda_posted,
            "agenda_url": agenda_url,
            "event_id": event_id,
            "status": "past" if date < today_str else "upcoming",
        })

    return upcoming



