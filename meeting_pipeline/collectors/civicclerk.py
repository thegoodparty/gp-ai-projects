"""
civicclerk.py — CivicClerk OData API data collector.

Fetches municipal meeting data from CivicClerk portals using their public
OData API. Collects events, agenda metadata, and downloads agenda/minutes PDFs.

CivicClerk API pattern:
    Base:     https://{tenant}.api.civicclerk.com/v1/
    Events:   GET /Events/?$orderby=EventDate desc&$top=200
    Detail:   GET /Events/{id}  (includes publishedFiles with PDF URLs)
    PDF:      GET /Meetings/GetMeetingFileStream(fileId={id},plainText=false)

Usage:
    from collectors.civicclerk import CivicClerkConfig, collect_civicclerk

    config = CivicClerkConfig(
        tenant="fairfieldoh",
        city_name="Fairfield",
        output_prefix="data/civicclerk",
        storage=storage_backend,
    )
    result = await collect_civicclerk(config)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.storage import StorageBackend

# ============================================================================
# CONFIG AND RESULT DATACLASSES
# ============================================================================

@dataclass
class CivicClerkConfig:
    """Configuration for CivicClerk data collection."""
    tenant: str                        # e.g. "fairfieldoh", "shermantx"
    city_name: str
    output_prefix: str
    storage: StorageBackend
    # CivicClerk has two API generations sharing the same {tenant}.api.civicclerk.com
    # backend but different OData field names:
    #   portal (newer, source URL contains portal.civicclerk.com): camelCase
    #     fields — startDateTime, id, eventName, categoryName
    #   legacy (older, source URL is bare {tenant}.civicclerk.com or unspecified):
    #     PascalCase fields — MeetingStartDate, EventId/Id, EventName/Name
    # The router infers this from the source URL and passes it in.
    is_portal: bool = True
    lookback_days: int = 90
    # Priority patterns for the primary governing body, checked in order.
    # These are exact substring matches (case-insensitive) against categoryName.
    # Override per-city via CivicClerkConfig(council_categories=[...]).
    council_categories: list[str] = field(default_factory=lambda: [
        "city council",
        "common council",
        "town council",
        "village board",
        "city commission",
        "board of trustees",
        "board of aldermen",
        "board of mayor",
        "town board",
        "village council",
        "board of commissioners",   # e.g. Davidson NC — must match before fallback "commission" exclusion
        "select board",             # e.g. New England towns
        "board of selectmen",
    ])
    download_pdfs: bool = True
    request_timeout: int = 30
    rate_limit_delay: float = 0.5


@dataclass
class CivicClerkResult:
    """Summary of collected CivicClerk data."""
    total_events: int = 0
    council_events: int = 0
    events_with_agenda: int = 0
    pdfs_downloaded: int = 0
    categories_found: list[str] = field(default_factory=list)
    output_prefix: str = ""


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _is_council_category(category_name: str, council_patterns: list[str]) -> bool:
    """Check if an event's category matches a council-type body."""
    lower = category_name.lower().strip()
    return any(pat in lower for pat in council_patterns)


# Schema-tolerant accessors. Portal tenants use camelCase; legacy tenants use
# PascalCase. We try the most common variants in each generation and let the
# rest of the function work with normalized values.

def _evt_date(evt: dict) -> str:
    return (
        evt.get("eventDate")
        or evt.get("startDateTime")
        or evt.get("MeetingStartDate")
        or evt.get("EventDate")
        or ""
    )


def _evt_id(evt: dict) -> str:
    return str(
        evt.get("id")
        or evt.get("EventId")
        or evt.get("Id")
        or ""
    )


def _evt_name(evt: dict) -> str:
    return (
        evt.get("eventName")
        or evt.get("EventName")
        or evt.get("Name")
        or ""
    )


def _evt_category(evt: dict) -> str:
    return (
        evt.get("categoryName")
        or evt.get("CategoryName")
        or evt.get("Category")
        or ""
    )


def _evt_has_agenda(evt: dict) -> bool:
    return bool(
        evt.get("hasAgenda")
        or evt.get("HasAgenda")
        or evt.get("AgendaFile")
    )


def _evt_published_files(evt: dict) -> list:
    return (
        evt.get("publishedFiles")
        or evt.get("PublishedFiles")
        or []
    )


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

async def collect_civicclerk(config: CivicClerkConfig) -> CivicClerkResult:
    """
    Collect meeting data from a CivicClerk OData API.

    1. Fetch all events from the API
    2. Filter by council-type categories and date range
    3. Fetch individual event details (includes publishedFiles)
    4. Download agenda/packet/minutes PDFs

    Output structure:
        {output_prefix}/
            events.json          — all council events (metadata)
            events_detail/       — per-event JSON with publishedFiles
            pdfs/                — downloaded agenda PDFs
            meetings.json        — simplified meeting list for downstream
    """
    base_url = f"https://{config.tenant}.api.civicclerk.com/v1"

    cutoff_date = (datetime.now() - timedelta(days=config.lookback_days)).strftime("%Y-%m-%dT00:00:00Z")


    async with httpx.AsyncClient(timeout=config.request_timeout, follow_redirects=True) as client:

        # 1. FETCH ALL EVENTS
        # CivicClerk OData is case-sensitive. Portal tenants accept lowercase
        # 'eventDate'; legacy tenants need PascalCase 'MeetingStartDate'.
        # The API returns at most ~15 events per request (internal limit that
        # ignores $top). Order matters: with `desc`, results are pinned to the
        # END of the date window. Two queries with different orderings:
        #   past window  → `desc` → most-recent past events first
        #   future window → `asc`  → nearest upcoming events first (otherwise
        #     the cap can drop the closest meetings — bug 18)
        today = datetime.now().strftime("%Y-%m-%dT00:00:00Z")
        future_cutoff = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z")

        date_field = "eventDate" if config.is_portal else "MeetingStartDate"

        seen_ids: set = set()
        all_events: list = []

        for (start, end, direction) in [
            (cutoff_date, today, "desc"),
            (today, future_cutoff, "asc"),
        ]:
            resp = await client.get(
                f"{base_url}/Events/",
                params={
                    "$orderby": f"{date_field} {direction}",
                    "$top": "500",
                    "$filter": f"{date_field} ge {start} and {date_field} le {end}",
                },
            )
            resp.raise_for_status()
            for evt in resp.json().get("value", []):
                evt_id = _evt_id(evt)
                if evt_id and evt_id not in seen_ids:
                    seen_ids.add(evt_id)
                    all_events.append(evt)


        # Discover categories. Legacy tenants without category metadata
        # produce empty strings here; the council-filter falls through to the
        # exclude-pattern fallback below.
        categories = sorted({_evt_category(e) for e in all_events if _evt_category(e)})

        # 2. FILTER BY COUNCIL CATEGORY + DATE
        council_events = [
            e for e in all_events
            if _is_council_category(_evt_category(e), config.council_categories)
        ]

        if not council_events:
            # Fallback: no primary governing body category found.
            # Exclude obvious sub-committees/commissions rather than collecting everything.
            # This prevents collecting planning commissions, fire/police commissions, etc.
            _EXCLUDE_PATTERNS = [
                "planning", "zoning", "parks", "fire", "police",
                "commission", "committee", "library", "water", "utilities",
                "historic", "ethics", "civil service", "personnel", "housing",
                "economic development", "redevelopment", "design", "aviation",
            ]
            filtered_events = [
                e for e in all_events
                if not any(pat in _evt_category(e).lower() for pat in _EXCLUDE_PATTERNS)
            ]
            if filtered_events:
                print(f"     WARNING: No council-type category found in {categories}")
                council_events = filtered_events
            else:
                # Everything looks like a committee — collect nothing rather than pollute
                print("     WARNING: No council-type category found and all categories look like committees.")
                council_events = []

        # Filter by date: keep events after cutoff OR in the future
        recent_events = [
            e for e in council_events
            if _evt_date(e) >= cutoff_date
        ]

        config.storage.write_json(f"{config.output_prefix}/events.json", recent_events)

        # 3. FETCH EVENT DETAILS (for publishedFiles)
        events_with_agenda = 0
        meetings = []
        pdf_count = 0

        for i, event in enumerate(recent_events):
            event_id = _evt_id(event)
            if not event_id:
                continue

            try:
                detail_resp = await client.get(f"{base_url}/Events/{event_id}")
                detail_resp.raise_for_status()
                detail = detail_resp.json()
            except httpx.HTTPStatusError as e:
                print(f"     WARNING: Failed to get details for event {event_id}: {e}")
                detail = event  # fallback to basic event data

            # Save per-event detail
            config.storage.write_json(f"{config.output_prefix}/events_detail/{event_id}.json", detail)

            # Extract meeting info
            published_files = _evt_published_files(detail)
            agenda_files = [f for f in published_files if f.get("type") in ("Agenda", "Agenda Packet")]
            minutes_files = [f for f in published_files if f.get("type") == "Minutes"]

            has_agenda = bool(agenda_files)
            if has_agenda:
                events_with_agenda += 1

            event_date = _evt_date(event)
            meeting = {
                "date": event_date[:10],
                "title": _evt_name(event),
                "categoryName": _evt_category(event),
                "eventId": event_id,
                "hasAgenda": _evt_has_agenda(event),
                "agendaFiles": [
                    {
                        "fileId": f["fileId"],
                        "name": f.get("name", ""),
                        "type": f["type"],
                        "url": f.get("streamUrl") or f.get("url", ""),
                    }
                    for f in agenda_files
                ],
                "minutesFiles": [
                    {
                        "fileId": f["fileId"],
                        "name": f.get("name", ""),
                        "url": f.get("streamUrl") or f.get("url", ""),
                    }
                    for f in minutes_files
                ],
                "displayMessage": detail.get("displayMessage", ""),
                "publishedAgendaTimeStamp": detail.get("publishedAgendaTimeStamp", ""),
            }
            meetings.append(meeting)

            # 4. DOWNLOAD PDFs
            if config.download_pdfs and agenda_files:
                for af in agenda_files:
                    file_id = af["fileId"]
                    file_type = af["type"].lower().replace(" ", "_")
                    filename = f"{event_date[:10]}_{file_type}_{file_id}.pdf"
                    pdf_key = f"{config.output_prefix}/pdfs/{filename}"

                    if config.storage.exists(pdf_key):
                        pdf_count += 1
                        continue

                    download_url = af.get("streamUrl") or af.get("url", "")
                    if not download_url:
                        continue

                    try:
                        pdf_resp = await client.get(download_url)
                        pdf_resp.raise_for_status()

                        config.storage.write_bytes(pdf_key, pdf_resp.content)
                        pdf_count += 1
                    except Exception as e:
                        print(f"     WARNING: Failed to download fileId={file_id}: {e}")

            # Firecrawl fallback: scrape portal event page when API returns no files.
            # Only meaningful for portal tenants — legacy tenants don't have a
            # portal.civicclerk.com site to scrape.
            elif config.download_pdfs and not agenda_files and _evt_has_agenda(event) and config.is_portal:
                try:
                    from meeting_pipeline.shared.firecrawl_client import scrape_civicclerk_event_files
                    portal_base = f"https://{config.tenant}.portal.civicclerk.com"
                    pdf_urls = scrape_civicclerk_event_files(portal_base, str(event_id))
                    for pdf_url in pdf_urls[:3]:
                        try:
                            resp = await client.get(pdf_url, timeout=30, follow_redirects=True)
                            resp.raise_for_status()
                            if len(resp.content) > 5000:
                                date_str = event_date[:10]
                                filename = pdf_url.split("/")[-1].split("?")[0] or f"{date_str}_{event_id}.pdf"
                                pdf_key = f"{config.output_prefix}/pdfs/{filename}"
                                config.storage.write_bytes(pdf_key, resp.content)
                                pdf_count += 1
                        except Exception as e:
                            print(f"     [civicclerk] Firecrawl fallback download failed: {e}")
                except ImportError:
                    pass  # Firecrawl not installed — skip fallback

            if i % 5 == 0 and i > 0:
                pass

            await asyncio.sleep(config.rate_limit_delay)

        # Save meetings list
        config.storage.write_json(f"{config.output_prefix}/meetings.json", meetings)

    # SUMMARY

    return CivicClerkResult(
        total_events=len(all_events),
        council_events=len(recent_events),
        events_with_agenda=events_with_agenda,
        pdfs_downloaded=pdf_count,
        categories_found=categories,
        output_prefix=config.output_prefix,
    )
