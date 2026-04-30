"""
Novus scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKBACK_DAYS


async def scan_novus(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    Novus Agenda: fetch the Meetings.aspx page and parse the RadGrid table.
    Reuses the collector's _parse_meetings for HTML parsing.
    """
    from meeting_pipeline.collectors.novus_scraper import _parse_meetings

    base_url = source_url.rstrip("/")
    if "/Meetings.aspx" not in base_url:
        # source_url may be the portal root (e.g. https://kyle.novusagenda.com/agendapublic)
        meetings_url = f"{base_url}/Meetings.aspx"
    else:
        meetings_url = base_url
        base_url = meetings_url.rsplit("/Meetings.aspx", 1)[0]

    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)

    try:
        resp = await client.get(
            meetings_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        return []

    meetings = _parse_meetings(resp.text, cutoff, base_url)

    return [
        {
            "date": m["date"],
            "title": m.get("title", f"{city} City Council"),
            "agenda_posted": bool(m.get("agendaUrl")),
            "agenda_url": m.get("agendaUrl", ""),
            "status": "upcoming",
        }
        for m in meetings
    ]
