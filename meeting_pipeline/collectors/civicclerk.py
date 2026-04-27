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
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import httpx

from meeting_pipeline.collection_agent.storage import StorageBackend


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

    print(f"Collecting {config.city_name} CivicClerk data (tenant: {config.tenant})...")
    print(f"  Lookback: {config.lookback_days} days (cutoff: {cutoff_date[:10]})")
    print(f"  Saving to: {config.output_prefix}")
    print()

    async with httpx.AsyncClient(timeout=config.request_timeout, follow_redirects=True) as client:

        # 1. FETCH ALL EVENTS
        # NOTE: CivicClerk OData is case-sensitive — use lowercase 'eventDate'.
        # The API returns at most ~15 events per request (internal limit that
        # ignores $top). With eventDate desc ordering, events near the end-date
        # of the filter are returned. We do two queries to capture both recent
        # past meetings and upcoming meetings.
        print("1/4  Fetching events...")
        today = datetime.now().strftime("%Y-%m-%dT00:00:00Z")
        future_cutoff = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z")

        seen_ids: set = set()
        all_events: list = []

        for (start, end) in [(cutoff_date, today), (today, future_cutoff)]:
            resp = await client.get(
                f"{base_url}/Events/",
                params={
                    "$orderby": "eventDate desc",
                    "$top": "500",
                    "$filter": f"eventDate ge {start} and eventDate le {end}",
                },
            )
            resp.raise_for_status()
            for evt in resp.json().get("value", []):
                if evt["id"] not in seen_ids:
                    seen_ids.add(evt["id"])
                    all_events.append(evt)

        print(f"     Found {len(all_events)} total events")

        # Discover categories
        categories = sorted(set(e["categoryName"] for e in all_events))
        print(f"     Categories: {categories}")

        # 2. FILTER BY COUNCIL CATEGORY + DATE
        print("2/4  Filtering council events...")
        council_events = [
            e for e in all_events
            if _is_council_category(e["categoryName"], config.council_categories)
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
                if not any(pat in e["categoryName"].lower() for pat in _EXCLUDE_PATTERNS)
            ]
            if filtered_events:
                print(f"     WARNING: No council-type category found in {categories}")
                print(f"     Falling back to {len(filtered_events)} non-commission events (excluded commission/committee/planning/etc.)")
                council_events = filtered_events
            else:
                # Everything looks like a committee — collect nothing rather than pollute
                print(f"     WARNING: No council-type category found and all categories look like committees.")
                print(f"     Categories: {categories}")
                print(f"     Returning 0 events. Set council_categories explicitly in CivicClerkConfig.")
                council_events = []

        # Filter by date: keep events after cutoff OR in the future
        recent_events = [
            e for e in council_events
            if e["eventDate"] >= cutoff_date
        ]

        print(f"     Council events: {len(council_events)} total, {len(recent_events)} recent/upcoming")
        config.storage.write_json(f"{config.output_prefix}/events.json", recent_events)

        # 3. FETCH EVENT DETAILS (for publishedFiles)
        print("3/4  Fetching event details + published files...")
        events_with_agenda = 0
        meetings = []
        pdf_count = 0

        for i, event in enumerate(recent_events):
            event_id = event["id"]

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
            published_files = detail.get("publishedFiles", [])
            agenda_files = [f for f in published_files if f.get("type") in ("Agenda", "Agenda Packet")]
            minutes_files = [f for f in published_files if f.get("type") == "Minutes"]

            has_agenda = bool(agenda_files)
            if has_agenda:
                events_with_agenda += 1

            meeting = {
                "date": event["eventDate"][:10],
                "title": event["eventName"],
                "categoryName": event["categoryName"],
                "eventId": event_id,
                "hasAgenda": event.get("hasAgenda", False),
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
                    filename = f"{event['eventDate'][:10]}_{file_type}_{file_id}.pdf"
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
                        print(f"     Downloaded: {filename} ({len(pdf_resp.content) // 1024}KB)")
                    except Exception as e:
                        print(f"     WARNING: Failed to download fileId={file_id}: {e}")

            # Firecrawl fallback: scrape portal event page when API returns no files
            elif config.download_pdfs and not agenda_files and event.get("hasAgenda"):
                try:
                    from meeting_pipeline.collection_agent.firecrawl_utils import scrape_civicclerk_event_files
                    portal_base = f"https://{config.tenant}.portal.civicclerk.com"
                    print(f"     [civicclerk] API returned 0 files for event {event_id} — trying Firecrawl")
                    pdf_urls = scrape_civicclerk_event_files(portal_base, str(event_id))
                    for pdf_url in pdf_urls[:3]:
                        try:
                            resp = await client.get(pdf_url, timeout=30, follow_redirects=True)
                            resp.raise_for_status()
                            if len(resp.content) > 5000:
                                date_str = event["eventDate"][:10]
                                filename = pdf_url.split("/")[-1].split("?")[0] or f"{date_str}_{event_id}.pdf"
                                pdf_key = f"{config.output_prefix}/pdfs/{filename}"
                                config.storage.write_bytes(pdf_key, resp.content)
                                pdf_count += 1
                                print(f"     [civicclerk] Firecrawl fallback downloaded: {filename}")
                        except Exception as e:
                            print(f"     [civicclerk] Firecrawl fallback download failed: {e}")
                except ImportError:
                    pass  # Firecrawl not installed — skip fallback

            if i % 5 == 0 and i > 0:
                print(f"     Processed {i + 1}/{len(recent_events)} events...")

            await asyncio.sleep(config.rate_limit_delay)

        # Save meetings list
        config.storage.write_json(f"{config.output_prefix}/meetings.json", meetings)

    # SUMMARY
    print()
    print("=" * 60)
    print(f"Collection complete for {config.city_name}!")
    print(f"  Total events fetched:  {len(all_events)}")
    print(f"  Council events:        {len(council_events)}")
    print(f"  Recent (≤{config.lookback_days}d + future): {len(recent_events)}")
    print(f"  Events with agenda:    {events_with_agenda}")
    print(f"  PDFs downloaded:       {pdf_count}")
    print(f"  Categories found:      {categories}")
    print(f"  Saved to:              {config.output_prefix}")
    print("=" * 60)

    return CivicClerkResult(
        total_events=len(all_events),
        council_events=len(recent_events),
        events_with_agenda=events_with_agenda,
        pdfs_downloaded=pdf_count,
        categories_found=categories,
        output_prefix=config.output_prefix,
    )
