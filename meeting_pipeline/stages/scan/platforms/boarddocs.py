"""
Boarddocs scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

import re
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKAHEAD_DAYS, LOOKBACK_DAYS


async def scan_boarddocs(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    BoardDocs: reuse the existing collector's committee discovery and meeting-list
    fetch — same logic, no agenda download.
    """
    from meeting_pipeline.collectors.boarddocs import BoardDocsConfig, _fetch_committees, _fetch_meetings

    match = re.search(r"(https://go\.boarddocs\.com/\w+/\w+/Board\.nsf)", source_url)
    base_url = match.group(1) if match else None
    if not base_url:
        return []

    bd_config = BoardDocsConfig(
        base_url=base_url,
        city_name=city,
        output_prefix="",
        storage=None,  # not used by _fetch_committees or _fetch_meetings
        committee_id=config.get("committee_id", ""),
        expected_body=config.get("expected_body", ""),
    )

    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://go.boarddocs.com",
        "Referer": f"{base_url}/Public",
        "User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)",
    }

    try:
        committees = await _fetch_committees(client, bd_config, headers)
    except Exception as e:
        print(f"    BoardDocs committees error: {e}")
        return []

    council_kw = ["city council", "town council", "village council", "board of aldermen", "municipal council"]
    council_committees = [c for c in committees if any(kw in c["name"].lower() for kw in council_kw)]
    if not council_committees:
        council_committees = committees[:1]

    today = datetime.now().date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)
    start_str = start.strftime("%Y%m%d")
    today_str = today.strftime("%Y%m%d")
    cutoff_str = cutoff.strftime("%Y%m%d")
    upcoming = []

    for committee in council_committees:
        meetings = await _fetch_meetings(client, bd_config, headers, committee["id"])
        for m in meetings:
            num_date = str(m.get("numberdate", ""))
            if not num_date or len(num_date) < 8:
                continue
            if num_date < start_str or num_date > cutoff_str:
                continue
            try:
                date_str = datetime.strptime(num_date[:8], "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                continue
            # _fetch_meetings returns raw BoardDocs JSON with lowercase fields
            # (numberdate, name, unique). The collector treats `unique` as the
            # agenda viewer key and constructs the same URL — see
            # collectors/boarddocs.py:265-269.
            unique = m.get("unique", "")
            agenda_url = f"{base_url}/goto?open&id={unique}" if unique else None
            upcoming.append({
                "date": date_str,
                "title": m.get("name", committee["name"]),
                "agenda_posted": bool(agenda_url),
                "agenda_url": agenda_url,
                "event_id": str(unique),
                "status": "past" if num_date < today_str else "upcoming",
            })

    upcoming.sort(key=lambda m: m["date"])
    return upcoming



