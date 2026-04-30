"""
Escribe scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

import re
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKAHEAD_DAYS


async def scan_escribe(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    eSCRIBE: POST to MeetingsCalendarView.aspx/UpcomingMeetings for each meeting type.
    Falls back to PastMeetings if UpcomingMeetings returns nothing.
    Uses verify=False because eSCRIBE portals commonly have self-signed/expired SSL certs.
    """
    base_url = source_url.rstrip("/")
    meeting_view_id = config.get("meeting_view_id", "1")
    meeting_types: list[str] = list(config.get("meeting_types", []))

    # eSCRIBE portals often have expired/self-signed SSL certs — use verify=False
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
        timeout=15,
        verify=False,
    ) as ec:
        # Discover meeting types from HTML if not configured
        if not meeting_types:
            try:
                resp = await ec.get(f"{base_url}/?MeetingviewId={meeting_view_id}")
                resp.raise_for_status()
                found = re.findall(
                    r'(?:meetingType|MeetingType|filterType)["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                    resp.text,
                )
                seen: set[str] = set()
                for t in found:
                    t = t.replace("&amp;", "&")
                    if t and t not in seen:
                        seen.add(t)
                        meeting_types.append(t)
            except Exception as e:
                print(f"    eSCRIBE type discovery failed for {city}: {e}")
                return []

        if not meeting_types:
            return []

        today_str = datetime.now().strftime("%Y-%m-%d")
        cutoff_str = (datetime.now() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
        upcoming: list[dict] = []

        for mt in meeting_types:
            for endpoint in ("UpcomingMeetings", "PastMeetings"):
                try:
                    resp = await ec.post(
                        f"{base_url}/MeetingsCalendarView.aspx/{endpoint}?MeetingviewId={meeting_view_id}",
                        json={"type": mt, "pageNumber": 1},
                        headers={
                            "Content-Type": "application/json; charset=utf-8",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json().get("d", {})
                    meetings = data.get("Meetings", [])

                    for m in meetings:
                        date_str = m.get("DateShort") or m.get("DateLong") or ""
                        try:
                            from dateutil import parser as dateparser
                            date = dateparser.parse(date_str).strftime("%Y-%m-%d") if date_str else None
                        except Exception:
                            date = None
                        if not date or not (today_str <= date <= cutoff_str):
                            continue
                        agenda_posted = bool(m.get("Agenda")) or bool(m.get("HasAgenda"))
                        agenda_url = m.get("AgendaUrl") or m.get("Agenda") or None
                        upcoming.append({
                            "date": date,
                            "title": mt,
                            "agenda_posted": agenda_posted,
                            "agenda_url": agenda_url,
                            "event_id": str(m.get("Id", "")),
                            "status": "past" if date < today_str else "upcoming",
                        })

                    if upcoming:
                        break  # got results from this endpoint, skip PastMeetings fallback
                except Exception as e:
                    print(f"    eSCRIBE {endpoint} failed for {city}/{mt}: {e}")
                    continue

    return upcoming



