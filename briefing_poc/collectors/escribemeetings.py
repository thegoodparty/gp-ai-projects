"""
escribemeetings.py — Collect legislative data from eSCRIBE Meetings.

eSCRIBE Meetings (escribemeetings.com) is a meeting management platform used
by municipalities. It provides a structured JSON API for fetching past meetings
and HTML agenda pages with parseable agenda items.

Raleigh, NC migrated from BoardDocs to eSCRIBE in July 2025.

API endpoints:
  - PastMeetings: POST, returns JSON with meeting objects and document links
  - Meeting.aspx: GET, returns HTML agenda with parseable item titles
  - FileStream.ashx: GET, direct PDF download by DocumentId

Usage:
    from collectors.escribemeetings import EscribeConfig, collect_escribemeetings

    config = EscribeConfig(
        base_url="https://pub-raleighnc.escribemeetings.com",
        city_name="Raleigh",
        output_dir=Path("data/legistar"),
        meeting_types=["City Council Meeting - First Tuesday - Afternoon & Evening Sessions"],
    )
    result = await collect_escribemeetings(config)
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class EscribeConfig:
    """Configuration for eSCRIBE Meetings data collection."""
    base_url: str           # e.g. "https://pub-raleighnc.escribemeetings.com"
    city_name: str          # e.g. "Raleigh"
    output_dir: Path        # Where to save JSON files
    meeting_types: list[str] = field(default_factory=list)  # Meeting type strings
    meeting_view_id: int = 1
    lookback_days: int = 180
    request_timeout: int = 30
    rate_limit_delay: float = 0.3


@dataclass
class EscribeResult:
    """Summary of collected eSCRIBE data."""
    bodies_count: int = 0
    events_count: int = 0
    matters_count: int = 0
    pdf_count: int = 0
    vote_count: int = 0
    persons_count: int = 0
    output_dir: Path = field(default_factory=lambda: Path("."))


# ============================================================================
# COLLECTOR
# ============================================================================

async def collect_escribemeetings(config: EscribeConfig) -> EscribeResult:
    """Collect legislative data from an eSCRIBE Meetings site.

    Outputs the same JSON schema as the Legistar collector:
      bodies.json, events.json, matters.json, event_items/{id}.json,
      attachments/*.pdf, persons.json, votes/ (stub).
    """
    result = EscribeResult(output_dir=config.output_dir)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    cutoff = datetime.now() - timedelta(days=config.lookback_days)

    # eSCRIBE uses SSL certs that may not validate locally
    async with httpx.AsyncClient(
        timeout=config.request_timeout,
        follow_redirects=True,
        verify=False,
    ) as client:
        print(f"\n{'='*60}")
        print(f"01 — Collect Legislative Data for {config.city_name} (eSCRIBE)")
        print(f"{'='*60}")
        print(f"\nSource: {config.base_url}")
        print(f"Lookback: {config.lookback_days} days (since {cutoff.strftime('%Y-%m-%d')})")

        # ── Step 1: Discover meeting types if not configured ─────────
        meeting_types = config.meeting_types
        if not meeting_types:
            meeting_types = await _discover_meeting_types(client, config)
        print(f"\nMeeting types: {len(meeting_types)}")
        for mt in meeting_types:
            print(f"  - {mt}")

        # Save as bodies.json (one "body" per meeting type)
        bodies = []
        for i, mt in enumerate(meeting_types):
            bodies.append({
                "BodyId": i + 1,
                "BodyName": mt,
                "BodyTypeName": "Committee" if "Committee" in mt else "Council",
                "BodyActiveFlag": 1,
                "_escribemeetings_type": mt,
            })
        _save_json(config.output_dir / "bodies.json", bodies)
        result.bodies_count = len(bodies)

        # ── Step 2: Fetch meetings for each type ────────────────────
        print(f"\nFetching meetings...")
        all_meetings: list[dict] = []
        for mt in meeting_types:
            meetings = await _fetch_past_meetings(client, config, mt)
            for m in meetings:
                # Parse date from the meeting object
                meeting_date = _parse_escribemeetings_date(m)
                if meeting_date and meeting_date >= cutoff:
                    m["_parsed_date"] = meeting_date
                    all_meetings.append(m)
            await asyncio.sleep(config.rate_limit_delay)

        # Sort by date descending
        all_meetings.sort(key=lambda m: m.get("_parsed_date", datetime.min), reverse=True)
        print(f"  {len(all_meetings)} meetings within lookback period")

        # Save as events.json
        events = []
        for i, m in enumerate(all_meetings):
            meeting_date = m["_parsed_date"]
            events.append({
                "EventId": i + 1,
                "EventDate": meeting_date.strftime("%Y-%m-%dT%H:%M:%S"),
                "EventBodyName": m.get("MeetingType", ""),
                "EventComment": f"{m.get('MeetingType', '')} - {m.get('DateLong', '')}",
                "_escribemeetings_id": m.get("Id", ""),
            })
        _save_json(config.output_dir / "events.json", events)
        result.events_count = len(events)

        # ── Step 3: Download agenda PDFs and fetch agenda items ──────
        print(f"\nFetching agendas and documents for {len(all_meetings)} meetings...")
        all_matters: list[dict] = []
        matter_id_counter = 1

        for evt_idx, meeting in enumerate(all_meetings):
            event_id = evt_idx + 1
            meeting_id = meeting.get("Id", "")
            meeting_date = meeting["_parsed_date"]
            meeting_type = meeting.get("MeetingType", "")

            if not meeting_id:
                continue

            # Download agenda/minutes PDFs from MeetingLinks.
            result.pdf_count += await _download_meeting_pdfs(
                client, config, meeting.get("MeetingLinks", []), event_id,
            )

            # Fetch HTML agenda to get individual items
            agenda_items = await _fetch_agenda_items(client, config, meeting_id)
            await asyncio.sleep(config.rate_limit_delay)

            event_items_list = []
            for item_idx, item in enumerate(agenda_items):
                matter_id = matter_id_counter
                matter_id_counter += 1

                iso_date = meeting_date.strftime("%Y-%m-%dT%H:%M:%S")

                matter = {
                    "MatterId": matter_id,
                    "MatterTitle": item.get("title", "").strip(),
                    "MatterTypeName": item.get("category", "Agenda Item"),
                    "MatterStatusName": "Passed",
                    "MatterBodyName": meeting_type,
                    "MatterIntroDate": iso_date,
                }
                all_matters.append(matter)

                event_item = {
                    "EventItemId": matter_id,
                    "EventItemTitle": item.get("title", "").strip(),
                    "EventItemActionText": item.get("detail", ""),
                    "EventItemRollCallFlag": 0,
                    "EventItemMatterId": matter_id,
                }
                event_items_list.append(event_item)

            # Save event items
            if event_items_list:
                ei_dir = config.output_dir / "event_items"
                ei_dir.mkdir(exist_ok=True)
                _save_json(ei_dir / f"{event_id}.json", event_items_list)

            if (evt_idx + 1) % 5 == 0 or evt_idx == len(all_meetings) - 1:
                print(f"  Processed {evt_idx + 1}/{len(all_meetings)} meetings, "
                      f"{len(all_matters)} items so far")

        # Save matters.json
        _save_json(config.output_dir / "matters.json", all_matters)
        result.matters_count = len(all_matters)

        # ── Step 4: Create stub files ────────────────────────────────
        _save_json(config.output_dir / "persons.json", [])
        (config.output_dir / "votes").mkdir(exist_ok=True)
        (config.output_dir / "matter_histories").mkdir(exist_ok=True)
        (config.output_dir / "matter_attachments").mkdir(exist_ok=True)

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Collection complete for {config.city_name} (eSCRIBE)")
    print(f"{'='*60}")
    print(f"  Bodies/meeting types: {result.bodies_count}")
    print(f"  Meetings (events): {result.events_count}")
    print(f"  Agenda items (matters): {result.matters_count}")
    print(f"  PDFs downloaded: {result.pdf_count}")
    print(f"  Output: {config.output_dir.resolve()}")

    return result


# ============================================================================
# INTERNAL FETCHERS
# ============================================================================

async def _download_meeting_pdfs(
    client: httpx.AsyncClient,
    config: EscribeConfig,
    meeting_links: list[dict],
    event_id: int,
) -> int:
    """Download PDF documents from a meeting's links. Returns count of PDFs saved."""
    att_dir = config.output_dir / "attachments"
    att_dir.mkdir(exist_ok=True)

    pdf_count = 0
    for link in meeting_links:
        url = link.get("Url", "")
        if not url or link.get("Format") != ".pdf":
            continue

        doc_id = re.search(r"DocumentId=(\d+)", url)
        if not doc_id:
            continue

        doc_url = f"{config.base_url}/{url}" if not url.startswith("http") else url
        local_path = att_dir / f"meeting_{event_id}_doc_{doc_id.group(1)}.pdf"

        if not local_path.exists():
            try:
                resp = await client.get(doc_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                    local_path.write_bytes(resp.content)
                    pdf_count += 1
            except Exception:
                pass
        await asyncio.sleep(config.rate_limit_delay * 0.3)

    return pdf_count


async def _discover_meeting_types(
    client: httpx.AsyncClient,
    config: EscribeConfig,
) -> list[str]:
    """Discover available meeting types from the eSCRIBE page."""
    try:
        resp = await client.get(
            f"{config.base_url}/?MeetingviewId={config.meeting_view_id}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        # Meeting types are embedded in JS as filter values
        types = re.findall(
            r"(?:meetingType|MeetingType|filterType)[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
            resp.text,
        )
        # Deduplicate while preserving order, filter out empty
        seen = set()
        unique = []
        for t in types:
            t_clean = t.replace("&amp;", "&")
            if t_clean and t_clean not in seen:
                seen.add(t_clean)
                unique.append(t_clean)
        return unique
    except Exception as e:
        print(f"  Warning: Could not discover meeting types: {e}")
        return []


async def _fetch_past_meetings(
    client: httpx.AsyncClient,
    config: EscribeConfig,
    meeting_type: str,
) -> list[dict]:
    """Fetch past meetings for a meeting type via the PastMeetings API."""
    all_meetings: list[dict] = []
    page = 1

    while True:
        try:
            resp = await client.post(
                f"{config.base_url}/MeetingsCalendarView.aspx/PastMeetings"
                f"?MeetingviewId={config.meeting_view_id}",
                json={"type": meeting_type, "pageNumber": page},
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            resp.raise_for_status()
            data = resp.json().get("d", {})
            meetings = data.get("Meetings", [])
            total = data.get("TotalCount", 0)

            all_meetings.extend(meetings)

            if len(all_meetings) >= total or not meetings:
                break
            page += 1
            await asyncio.sleep(config.rate_limit_delay)
        except Exception as e:
            print(f"  Warning: PastMeetings page {page} failed for {meeting_type[:40]}: {e}")
            break

    return all_meetings


async def _fetch_agenda_items(
    client: httpx.AsyncClient,
    config: EscribeConfig,
    meeting_id: str,
) -> list[dict]:
    """Fetch agenda items from the HTML agenda page."""
    try:
        resp = await client.get(
            f"{config.base_url}/Meeting.aspx?Id={meeting_id}&Agenda=Agenda&lang=English",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()

        items = []
        current_category = ""

        # Parse AgendaItemTitle elements and their categories
        # Categories appear as standalone titled sections
        # Items appear with numbered prefixes (A.1, C.2.a, etc.) and descriptive titles
        for m in re.finditer(
            r'class="[^"]*AgendaItemTitle[^"]*"[^>]*>(.*?)</(?:span|div|a|h|td)',
            resp.text,
            re.DOTALL,
        ):
            raw = m.group(1)
            clean = re.sub(r"<[^>]+>", "", raw).strip()
            if not clean or len(clean) < 2:
                continue

            # Check if this is a category header (all caps, no number prefix)
            if clean.isupper() and len(clean) > 5 and not re.match(r"^[A-Z]\.\d", clean):
                current_category = clean.title()
            else:
                # Strip leading number prefix (e.g., "C.2.a")
                title = re.sub(r"^[A-Z](?:\.\d+)*[a-z]?\s*", "", clean).strip()
                if title and len(title) > 3:
                    items.append({
                        "title": title,
                        "category": current_category or "General",
                    })

        return items
    except Exception as e:
        print(f"  Warning: Could not fetch agenda for meeting {meeting_id[:12]}: {e}")
        return []


# ============================================================================
# HELPERS
# ============================================================================

def _parse_escribemeetings_date(meeting: dict) -> datetime | None:
    """Parse a date from an eSCRIBE meeting object."""
    # Try DateLong first: "February 03, 2026"
    for field_name in ("DateLong", "DateMedium", "DateShort"):
        date_str = meeting.get(field_name, "")
        if date_str:
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%b %d, %Y"):
                try:
                    return datetime.strptime(date_str.strip(), fmt)
                except ValueError:
                    continue

    # Try Start field: "/Date(1770123616233)/"
    start = meeting.get("Start", "")
    ms_match = re.search(r"/Date\((\d+)\)/", start)
    if ms_match:
        ts = int(ms_match.group(1)) / 1000
        return datetime.fromtimestamp(ts)

    return None


def _save_json(path: Path, data: list | dict) -> None:
    """Save data as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
