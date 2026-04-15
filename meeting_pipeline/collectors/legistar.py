"""
legistar.py — Reusable Legistar API data collector.

Fetches legislative data (bodies, events, matters, votes, PDFs, persons)
from any Legistar API endpoint. City-agnostic — pass a LegistarConfig
with the target city's API URL, output prefix, and lookback period.

Usage:
    from collectors.legistar import LegistarConfig, collect_legistar

    config = LegistarConfig(
        base_url="https://webapi.legistar.com/v1/charlottenc",
        city_name="Charlotte",
        output_prefix="data/legistar",
        storage=storage_backend,
        lookback_days=180,
    )
    result = await collect_legistar(config)
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
class LegistarConfig:
    """Configuration for Legistar data collection."""
    base_url: str
    city_name: str
    output_prefix: str
    storage: StorageBackend
    lookback_days: int = 180
    page_size: int = 100
    request_timeout: int = 30
    rate_limit_delay: float = 0.25
    expected_body: str = ""  # e.g. "City Council" — filters events/matters to matching bodies
    agendas_only: bool = False  # If True, skip matter attachments (steps 4-5) — much faster


@dataclass
class LegistarResult:
    """Summary of collected Legistar data."""
    bodies_count: int = 0
    events_count: int = 0
    matters_count: int = 0
    pdf_count: int = 0
    vote_count: int = 0
    persons_count: int = 0
    output_prefix: str = ""


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def _fetch_json(client: httpx.AsyncClient, url: str, params: dict = None, retries: int = 3):
    """Make a GET request and return parsed JSON, with retry logic."""
    for attempt in range(retries):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError):
            if attempt < retries - 1:
                wait_time = 2 ** (attempt + 1)
                print(f"  Retry {attempt + 1}/{retries} after {wait_time}s for {url.split('/')[-1]}...")
                await asyncio.sleep(wait_time)
            else:
                raise


async def _fetch_all_pages(
    client: httpx.AsyncClient,
    url: str,
    page_size: int,
    rate_limit_delay: float,
    odata_filter: str = None,
    orderby: str = None,
) -> list:
    """Fetch all pages of results from a Legistar API endpoint."""
    all_results = []
    skip = 0

    while True:
        params = {"$top": page_size, "$skip": skip}
        if odata_filter:
            params["$filter"] = odata_filter
        if orderby:
            params["$orderby"] = orderby

        page = await _fetch_json(client, url, params=params)
        if not page:
            break

        all_results.extend(page)
        print(f"  Fetched {len(all_results)} results so far from {url.split('/')[-1]}...")

        if len(page) < page_size:
            break

        skip += page_size
        await asyncio.sleep(rate_limit_delay)

    return all_results


def _count_existing_pdfs(output_prefix: str, matter_id: int, storage: StorageBackend) -> int:
    """Count already-downloaded PDFs for a matter (used during resume)."""
    att_key = f"{output_prefix}/matter_attachments/{matter_id}.json"
    if not storage.exists(att_key):
        return 0
    existing_atts = storage.read_json(att_key)
    return sum(
        1 for att in existing_atts
        if storage.exists(f"{output_prefix}/attachments/{matter_id}_{att['MatterAttachmentId']}.pdf")
    )


async def _download_file(client: httpx.AsyncClient, url: str, key: str, storage: StorageBackend) -> bool:
    """Download a file (typically a PDF) and save it via storage backend."""
    try:
        response = await client.get(url)
        response.raise_for_status()
        storage.write_bytes(key, response.content)
        return True
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        print(f"  WARNING: Failed to download {url}: {e}")
        return False


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

async def collect_legistar(config: LegistarConfig) -> LegistarResult:
    """
    Collect all legislative data from a Legistar API endpoint.

    Downloads bodies, events, event items, matters, matter histories,
    attachments, PDFs, votes, and persons. Saves as JSON files via
    config.storage under config.output_prefix.

    Supports resumable runs (skips already-downloaded matters).
    """
    base_url = config.base_url
    six_months_ago = (datetime.now() - timedelta(days=config.lookback_days)).strftime("%Y-%m-%d")

    print(f"Collecting {config.city_name} Legistar data from {six_months_ago} to today...")
    print(f"Saving to: {config.output_prefix}")
    print()

    async with httpx.AsyncClient(timeout=config.request_timeout) as client:

        # 1. BODIES
        print("1/7  Fetching legislative bodies...")
        bodies = await _fetch_json(client, f"{base_url}/bodies")
        config.storage.write_json(f"{config.output_prefix}/bodies.json", bodies)
        print(f"     Found {len(bodies)} bodies")
        print()

        # Body filter: if expected_body is set, restrict events/matters to matching bodies
        body_ids_to_keep: set[int] = set()
        if config.expected_body:
            expected_lower = config.expected_body.lower()
            body_ids_to_keep = {
                b["BodyId"] for b in bodies
                if expected_lower in b.get("BodyName", "").lower()
            }
            print(f"  [body filter] expected_body={config.expected_body!r} matched {len(body_ids_to_keep)} of {len(bodies)} bodies")
            if not body_ids_to_keep:
                print(f"  WARNING: [body filter] No bodies matched {config.expected_body!r} — collecting all bodies (degrading gracefully)")

        # 2. EVENTS
        print("2/7  Fetching events (meetings)...")
        events = await _fetch_all_pages(
            client, f"{base_url}/events", config.page_size, config.rate_limit_delay,
            odata_filter=f"EventDate ge datetime'{six_months_ago}'",
            orderby="EventDate desc",
        )
        if body_ids_to_keep:
            events = [e for e in events if e.get("EventBodyId") in body_ids_to_keep]
        config.storage.write_json(f"{config.output_prefix}/events.json", events)
        print(f"     Found {len(events)} events")
        print()

        # 3. EVENT ITEMS
        print("3/7  Fetching agenda items for each event...")
        for i, event in enumerate(events):
            event_id = event["EventId"]
            items = await _fetch_json(client, f"{base_url}/events/{event_id}/eventitems")
            config.storage.write_json(f"{config.output_prefix}/event_items/{event_id}.json", items)
            if i % 10 == 0:
                print(f"     Processing event {i + 1}/{len(events)}...")
            await asyncio.sleep(config.rate_limit_delay)
        print()

        # 4. MATTERS
        matters = []
        pdf_count = 0
        if config.agendas_only:
            print("4/7  Skipping matters (agendas_only=True)")
            print()
        else:
            print("4/7  Fetching recent matters (legislation)...")
            matters = await _fetch_all_pages(
                client, f"{base_url}/matters", config.page_size, config.rate_limit_delay,
                odata_filter=f"MatterIntroDate ge datetime'{six_months_ago}'",
                orderby="MatterIntroDate desc",
            )
            if body_ids_to_keep:
                matters = [m for m in matters if m.get("MatterBodyId") in body_ids_to_keep]
            config.storage.write_json(f"{config.output_prefix}/matters.json", matters)
            print(f"     Found {len(matters)} matters")
            print()

        # 5. MATTER DETAILS + PDFs
        if config.agendas_only:
            print("5/7  Skipping matter histories/attachments (agendas_only=True)")
            print()
        else:
            print("5/7  Fetching matter histories, attachments, and downloading PDFs...")
            skipped_count = 0

            for i, matter in enumerate(matters):
                matter_id = matter["MatterId"]
                history_key = f"{config.output_prefix}/matter_histories/{matter_id}.json"
                if config.storage.exists(history_key):
                    skipped_count += 1
                    pdf_count += _count_existing_pdfs(config.output_prefix, matter_id, config.storage)
                    continue

                histories = await _fetch_json(client, f"{base_url}/matters/{matter_id}/histories")
                config.storage.write_json(history_key, histories)

                attachments = await _fetch_json(client, f"{base_url}/matters/{matter_id}/attachments")
                config.storage.write_json(f"{config.output_prefix}/matter_attachments/{matter_id}.json", attachments)

                for att in attachments:
                    pdf_url = att.get("MatterAttachmentHyperlink")
                    if pdf_url:
                        filename = f"{matter_id}_{att['MatterAttachmentId']}.pdf"
                        pdf_key = f"{config.output_prefix}/attachments/{filename}"
                        success = await _download_file(client, pdf_url, pdf_key, config.storage)
                        if success:
                            pdf_count += 1
                        await asyncio.sleep(config.rate_limit_delay)

                if i % 10 == 0:
                    print(f"     Processing matter {i + 1}/{len(matters)} ({skipped_count} skipped)...")
                await asyncio.sleep(config.rate_limit_delay)

            print(f"     Downloaded {pdf_count} PDF attachments")
            print()

        # 6. VOTES
        print("6/7  Fetching vote records...")
        vote_count = 0
        for event in events:
            event_id = event["EventId"]
            items_key = f"{config.output_prefix}/event_items/{event_id}.json"
            if not config.storage.exists(items_key):
                continue
            items = config.storage.read_json(items_key)
            for item in items:
                if item.get("EventItemRollCallFlag"):
                    item_id = item["EventItemId"]
                    votes = await _fetch_json(client, f"{base_url}/eventitems/{item_id}/votes")
                    if votes:
                        config.storage.write_json(f"{config.output_prefix}/votes/{item_id}.json", votes)
                        vote_count += 1
                    await asyncio.sleep(config.rate_limit_delay)

        print(f"     Found {vote_count} roll-call vote records")
        print()

        # 7. PERSONS
        print("7/7  Fetching persons...")
        persons = await _fetch_json(client, f"{base_url}/persons")
        config.storage.write_json(f"{config.output_prefix}/persons.json", persons)
        print(f"     Found {len(persons)} persons")
        print()

    # SUMMARY
    print("=" * 60)
    print("Collection complete!")
    print(f"  Bodies:      {len(bodies)}")
    print(f"  Events:      {len(events)}")
    print(f"  Matters:     {len(matters)}")
    print(f"  PDFs:        {pdf_count}")
    print(f"  Vote records:{vote_count}")
    print(f"  Persons:     {len(persons)}")
    print(f"  Saved to:    {config.output_prefix}")
    print("=" * 60)

    return LegistarResult(
        bodies_count=len(bodies),
        events_count=len(events),
        matters_count=len(matters),
        pdf_count=pdf_count,
        vote_count=vote_count,
        persons_count=len(persons),
        output_prefix=config.output_prefix,
    )
