"""
novus_scraper.py — Collect meeting data from Novus Agenda portals.

Novus Agenda ({city}.novusagenda.com/agendapublic) is an ASP.NET WebForms
meeting management platform. The initial Meetings.aspx page renders a grid
of recent meetings server-side; no form submission is required for cities
that actively post their meetings.

HTML structure (Telerik RadGrid):
  <table id="...radGridMeetings_ctl00...">
    <thead>
      <tr><th>Meeting Date</th><th>Meeting Type</th>...<th>Download Agenda</th>...</tr>
    </thead>
    <tbody>
      <tr>
        <td>04/06/26</td>                                        <!-- MM/DD/YY -->
        <td>City Council Regular Meeting</td>
        <td>City Hall, 100 W Center St...</td>
        <td>(online link)</td>
        <td><a href="DisplayAgendaPDF.ashx?MeetingID=1149">...</a></td>
        <td>(minutes link)</td>
        <td>(legal minutes)</td>
      </tr>
    </tbody>
  </table>

Agenda PDF URL format:
  {base_url}/DisplayAgendaPDF.ashx?MeetingID={id}

For cities where the initial page shows no meetings (e.g. empty Novus
installations), the collector returns an empty list — the city likely
manages agendas through a different system or hasn't populated Novus.

Usage:
    from collectors.novus_scraper import NovusConfig, collect_novus

    config = NovusConfig(
        portal_url="https://lexington.novusagenda.com/agendapublic",
        city_name="Lexington",
        output_prefix="meeting_pipeline/sources/lexington-MA/data/novus",
        storage=storage_backend,
        lookback_days=180,
    )
    result = await collect_novus(config)
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.collection_agent.storage import StorageBackend


# ============================================================================
# CONFIG AND RESULT
# ============================================================================

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Council-type keywords for filtering board type (sorted by priority)
_COUNCIL_KEYWORDS = [
    "city council regular",
    "city council",
    "town council",
    "town board",
    "village board",
    "village council",
    "city commission",
    "city board",
    "select board",
    "board of selectmen",
    "common council",
    "borough council",
    "county council",
    "county commission",
]


@dataclass
class NovusConfig:
    """Configuration for Novus Agenda scraping."""
    portal_url: str        # e.g. "https://lexington.novusagenda.com/agendapublic"
    city_name: str
    output_prefix: str
    storage: StorageBackend
    lookback_days: int = 365
    download_pdfs: bool = True
    request_timeout: int = 30


@dataclass
class NovusResult:
    """Summary of collected Novus data."""
    meetings_found: int = 0
    pdfs_downloaded: int = 0
    output_prefix: str = ""


# ============================================================================
# MAIN COLLECTOR
# ============================================================================

async def collect_novus(config: NovusConfig) -> NovusResult:
    """Collect meeting data from a Novus Agenda portal.

    Outputs:
      {output_prefix}/meetings.json — list of meeting dicts
      {output_prefix}/pdfs/{date}_{type}.pdf — downloaded agenda PDFs
    """
    cutoff = datetime.now() - timedelta(days=config.lookback_days)
    result = NovusResult(output_prefix=config.output_prefix)

    base_url = config.portal_url.rstrip("/")
    meetings_url = f"{base_url}/Meetings.aspx"

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(
        timeout=config.request_timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        # ── Step 1: Fetch initial page ─────────────────────────────────────
        try:
            resp = await client.get(meetings_url)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR fetching {meetings_url}: {e}")
            return result

        html = resp.text
        meetings = _parse_meetings(html, cutoff, base_url)

        # ── Step 2: Try board-type filtered search if initial page empty ───
        if not meetings:
            board_type_val = _find_council_board_type(html)
            if board_type_val:
                print(f"  {config.city_name}: initial page empty, trying search with board={board_type_val}")
                for date_range in ["l6m", "n6m", "lyr"]:
                    try:
                        search_html = await _search_meetings(
                            client, meetings_url, html, date_range, board_type_val
                        )
                        meetings = _parse_meetings(search_html, cutoff, base_url)
                        if meetings:
                            print(f"  {config.city_name}: found {len(meetings)} meetings with range={date_range}")
                            break
                    except Exception as e:
                        print(f"  WARN: search failed for range={date_range}: {e}")

        print(f"  {config.city_name}: {len(meetings)} meetings found")
        # Note: some Novus portals show 0 meetings when agendas haven't been posted yet;
        # this is expected behavior, not a scraper bug. Verified for Cornelius OR,
        # Hermiston OR, and Union City GA — their Meetings.aspx pages return "No records
        # to display" because no upcoming meetings have been published. Kyle TX (also
        # Novus) is covered via Granicus and does not need this collector.

        if not meetings:
            config.storage.write_json(f"{config.output_prefix}/meetings.json", [])
            return result

        # Save meetings.json
        config.storage.write_json(f"{config.output_prefix}/meetings.json", meetings)
        result.meetings_found = len(meetings)

        # Download PDFs
        if config.download_pdfs:
            pdf_count = await _download_pdfs(client, config, meetings, base_url)
            result.pdfs_downloaded = pdf_count

    return result


# ============================================================================
# HTML PARSING
# ============================================================================

def _parse_meetings(html: str, cutoff: datetime, base_url: str) -> list[dict]:
    """Parse meetings from the Novus RadGrid table.

    Returns a list of dicts with:
      date       — YYYY-MM-DD
      title      — meeting type, e.g. "City Council Regular Meeting"
      location   — meeting location (may be truncated)
      agendaUrl  — PDF URL or ""
      minutesUrl — PDF URL or ""
      meetingId  — Novus MeetingID integer or None
      sourceUrl  — portal URL
    """
    meetings = []

    # Find the meeting grid table
    grid_m = re.search(
        r'<table[^>]*radGridMeetings_ctl00[^>]*>(.*?)</table>',
        html, re.S | re.I
    )
    if not grid_m:
        return meetings

    table_html = grid_m.group(1)

    # Find data rows (all <tr> within <tbody>)
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.S | re.I)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
        if len(cells) < 2:
            continue  # Header row

        # Cell 0: Meeting Date (MM/DD/YY or MM/DD/YYYY)
        date_str = re.sub(r'<[^>]+>', '', cells[0]).strip()
        dt = _parse_novus_date(date_str)
        if not dt or dt < cutoff:
            continue

        # Cell 1: Meeting Type / Title
        title = re.sub(r'<[^>]+>', '', cells[1]).strip()

        # Cell 2: Location (optional)
        location = ""
        if len(cells) > 2:
            location = re.sub(r'<[^>]+>', '', cells[2]).strip()

        # Find agenda PDF link (DisplayAgendaPDF.ashx?MeetingID=N)
        agenda_url = ""
        minutes_url = ""
        meeting_id = None

        all_links = re.findall(r'href="([^"]+)"', row, re.I)
        for link in all_links:
            if "DisplayAgendaPDF.ashx" in link:
                full_url = _resolve_url(base_url, link)
                # Classify as agenda or minutes based on order/column
                if not agenda_url:
                    agenda_url = full_url
                else:
                    minutes_url = full_url
                # Extract MeetingID
                m_id = re.search(r'MeetingID=(\d+)', link, re.I)
                if m_id and not meeting_id:
                    meeting_id = int(m_id.group(1))

        meetings.append({
            "date": dt.strftime("%Y-%m-%d"),
            "title": title,
            "location": location,
            "agendaUrl": agenda_url,
            "minutesUrl": minutes_url,
            "meetingId": meeting_id,
            "sourceUrl": f"{base_url}/Meetings.aspx",
        })

    # Sort by date descending
    meetings.sort(key=lambda m: m["date"], reverse=True)
    return meetings


def _parse_novus_date(date_str: str) -> datetime | None:
    """Parse Novus date string (MM/DD/YY or MM/DD/YYYY) → datetime."""
    date_str = date_str.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    return None


def _find_council_board_type(html: str) -> str | None:
    """Find the best council/board type option value from the meeting type select."""
    board_m = re.search(
        r'<select[^>]*name="[^"]*SearchAgendasMeetings\$ctl00[^"]*"[^>]*>(.*?)</select>',
        html, re.S | re.I
    )
    if not board_m:
        return None

    options = re.findall(
        r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>',
        board_m.group(1), re.S
    )
    # Try keywords in priority order (first match wins)
    for keyword in _COUNCIL_KEYWORDS:
        for val, text in options:
            if keyword in text.strip().lower():
                return val

    return None


def _resolve_url(base_url: str, href: str) -> str:
    """Resolve a potentially relative URL to absolute."""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        # Extract scheme + host from base_url
        m = re.match(r"(https?://[^/]+)", base_url)
        return f"{m.group(1)}{href}" if m else href
    return f"{base_url}/{href.lstrip('/')}"


# ============================================================================
# FORM SEARCH (fallback for initially empty grids)
# ============================================================================

async def _search_meetings(
    client: httpx.AsyncClient,
    url: str,
    initial_html: str,
    date_range: str,
    board_type: str,
) -> str:
    """POST the search form and return the result HTML."""
    vs_m = re.search(r'__VIEWSTATE[^>]*value="([^"]+)"', initial_html)
    vsg_m = re.search(r'__VIEWSTATEGENERATOR[^>]*value="([^"]*)"', initial_html)
    if not vs_m:
        raise ValueError("Could not extract VIEWSTATE")

    search_btn = "ctl00$ContentPlaceHolder1$SearchAgendasMeetings$imageButtonSearch"

    data = {
        "ctl00_ContentPlaceHolder1_radScriptManagerMain_TSM": "",
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__LASTFOCUS": "",
        "__VIEWSTATE": vs_m.group(1),
        "__VIEWSTATEGENERATOR": vsg_m.group(1) if vsg_m else "",
        "ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ddlDateRange": date_range,
        "ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ctl00": board_type,
        "ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ctl01": "-1",
        "ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ctl02": "",
        f"{search_btn}.x": "10",
        f"{search_btn}.y": "10",
        "ctl00_ContentPlaceHolder1_SearchAgendasMeetings_sharedDynamicCalendar_SD": "[]",
        "ctl00_ContentPlaceHolder1_SearchAgendasMeetings_sharedDynamicCalendar_AD": (
            "[[1970,1,1],[2099,12,30],[2026,4,10]]"
        ),
        "ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radCalendarFrom_ClientState": "",
        "ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radCalendarTo_ClientState": "",
    }

    resp = await client.post(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": url,
    })
    resp.raise_for_status()
    return resp.text


# ============================================================================
# PDF DOWNLOAD
# ============================================================================

async def _download_pdfs(
    client: httpx.AsyncClient,
    config: NovusConfig,
    meetings: list[dict],
    base_url: str,
) -> int:
    """Download agenda PDFs. Returns count saved."""
    downloaded = 0
    for meeting in meetings:
        for pdf_type, pdf_url in [
            ("agenda", meeting.get("agendaUrl", "")),
            ("minutes", meeting.get("minutesUrl", "")),
        ]:
            if not pdf_url:
                continue

            date = meeting.get("date", "unknown")
            key = f"{config.output_prefix}/pdfs/{date}_{pdf_type}.pdf"

            if config.storage.exists(key):
                downloaded += 1
                continue

            try:
                resp = await client.get(pdf_url, headers={"Referer": f"{base_url}/Meetings.aspx"})
                if resp.status_code == 200 and len(resp.content) > 5000:
                    ct = resp.headers.get("content-type", "")
                    if resp.content[:4] == b"%PDF" or "pdf" in ct.lower():
                        config.storage.write_bytes(key, resp.content)
                        downloaded += 1
            except Exception as e:
                print(f"    WARN: PDF download failed for {pdf_url}: {e}")

    return downloaded


# ============================================================================
# CLI
# ============================================================================

async def _main_cli():
    import argparse, sys
    from pathlib import Path

    from meeting_pipeline.shared.config import AgentConfig, get_storage

    parser = argparse.ArgumentParser(description="Collect Novus Agenda meeting data")
    parser.add_argument("--city-slug", required=True, help="e.g. lexington-MA")
    parser.add_argument("--url", required=True, help="e.g. https://lexington.novusagenda.com/agendapublic")
    parser.add_argument("--city-name", default="")
    parser.add_argument("--no-pdfs", action="store_true")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    output_prefix = f"{cfg.sources_prefix}/{args.city_slug}/data/novus"
    novus_cfg = NovusConfig(
        portal_url=args.url,
        city_name=args.city_name or args.city_slug,
        output_prefix=output_prefix,
        storage=storage,
        download_pdfs=not args.no_pdfs,
    )
    result = await collect_novus(novus_cfg)
    print(f"\nResult: {result.meetings_found} meetings, {result.pdfs_downloaded} PDFs")
    print(f"Output: {output_prefix}/meetings.json")


if __name__ == "__main__":
    asyncio.run(_main_cli())
