"""
Municode scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKBACK_DAYS


async def scan_municode(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    Municode Meetings: fetch the portal page and parse the meeting table.
    Reuses the collector's _parse_meetings for HTML parsing.
    """
    from meeting_pipeline.collectors.municode import _parse_meetings

    url = source_url.rstrip("/")
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)

    try:
        resp = await client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        return []

    meetings = _parse_meetings(resp.text, cutoff, url)
    today = datetime.now().date()

    return [
        {
            "date": m["date"],
            "title": m.get("title", f"{city} City Council"),
            "agenda_posted": bool(m.get("agendaUrl")),
            "agenda_url": m.get("agendaUrl", ""),
            "status": "past" if datetime.strptime(m["date"], "%Y-%m-%d").date() < today else "upcoming",
        }
        for m in meetings
    ]
