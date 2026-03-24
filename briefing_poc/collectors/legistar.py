"""
legistar.py — Reusable Legistar API data collector.

Fetches legislative data (bodies, events, matters, votes, PDFs, persons)
from any Legistar API endpoint. City-agnostic — pass a LegistarConfig
with the target city's API URL, output directory, and lookback period.

Usage:
    from collectors.legistar import LegistarConfig, collect_legistar

    config = LegistarConfig(
        base_url="https://webapi.legistar.com/v1/charlottenc",
        city_name="Charlotte",
        output_dir=Path("data/legistar"),
        lookback_days=180,
    )
    result = await collect_legistar(config)
"""

import asyncio
import json
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import httpx


# ============================================================================
# CONFIG AND RESULT DATACLASSES
# ============================================================================

@dataclass
class LegistarConfig:
    """Configuration for Legistar data collection."""
    base_url: str
    city_name: str
    output_dir: Path
    lookback_days: int = 180
    page_size: int = 100
    request_timeout: int = 30
    rate_limit_delay: float = 0.25


@dataclass
class LegistarResult:
    """Summary of collected Legistar data."""
    bodies_count: int = 0
    events_count: int = 0
    matters_count: int = 0
    pdf_count: int = 0
    vote_count: int = 0
    persons_count: int = 0
    output_dir: Path = field(default_factory=lambda: Path("."))


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _save_json(data, output_dir: Path, relative_path: str) -> Path:
    """Save data as a JSON file inside output_dir."""
    file_path = output_dir / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return file_path


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


def _count_existing_pdfs(output_dir: Path, matter_id: int) -> int:
    """Count already-downloaded PDFs for a matter (used during resume)."""
    att_file = output_dir / f"matter_attachments/{matter_id}.json"
    if not att_file.exists():
        return 0
    existing_atts = json.loads(att_file.read_text())
    return sum(
        1 for att in existing_atts
        if (output_dir / f"attachments/{matter_id}_{att['MatterAttachmentId']}.pdf").exists()
    )


async def _download_file(client: httpx.AsyncClient, url: str, output_dir: Path, relative_path: str) -> Path | None:
    """Download a file (typically a PDF) and save it to disk."""
    file_path = output_dir / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        response = await client.get(url)
        response.raise_for_status()
        with open(file_path, "wb") as f:
            f.write(response.content)
        return file_path
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        print(f"  WARNING: Failed to download {url}: {e}")
        return None


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

async def collect_legistar(config: LegistarConfig) -> LegistarResult:
    """
    Collect all legislative data from a Legistar API endpoint.

    Downloads bodies, events, event items, matters, matter histories,
    attachments, PDFs, votes, and persons. Saves as JSON files in
    config.output_dir.

    Supports resumable runs (skips already-downloaded matters).
    """
    base_url = config.base_url
    output_dir = config.output_dir
    six_months_ago = (datetime.now() - timedelta(days=config.lookback_days)).strftime("%Y-%m-%d")

    print(f"Collecting {config.city_name} Legistar data from {six_months_ago} to today...")
    print(f"Saving to: {output_dir.resolve()}")
    print()

    async with httpx.AsyncClient(timeout=config.request_timeout) as client:

        # 1. BODIES
        print("1/7  Fetching legislative bodies...")
        bodies = await _fetch_json(client, f"{base_url}/bodies")
        _save_json(bodies, output_dir, "bodies.json")
        print(f"     Found {len(bodies)} bodies")
        print()

        # 2. EVENTS
        print("2/7  Fetching events (meetings)...")
        events = await _fetch_all_pages(
            client, f"{base_url}/events", config.page_size, config.rate_limit_delay,
            odata_filter=f"EventDate ge datetime'{six_months_ago}'",
            orderby="EventDate desc",
        )
        _save_json(events, output_dir, "events.json")
        print(f"     Found {len(events)} events")
        print()

        # 3. EVENT ITEMS
        print("3/7  Fetching agenda items for each event...")
        for i, event in enumerate(events):
            event_id = event["EventId"]
            items = await _fetch_json(client, f"{base_url}/events/{event_id}/eventitems")
            _save_json(items, output_dir, f"event_items/{event_id}.json")
            if i % 10 == 0:
                print(f"     Processing event {i + 1}/{len(events)}...")
            await asyncio.sleep(config.rate_limit_delay)
        print()

        # 4. MATTERS
        print("4/7  Fetching recent matters (legislation)...")
        matters = await _fetch_all_pages(
            client, f"{base_url}/matters", config.page_size, config.rate_limit_delay,
            odata_filter=f"MatterIntroDate ge datetime'{six_months_ago}'",
            orderby="MatterIntroDate desc",
        )
        _save_json(matters, output_dir, "matters.json")
        print(f"     Found {len(matters)} matters")
        print()

        # 5. MATTER DETAILS + PDFs
        print("5/7  Fetching matter histories, attachments, and downloading PDFs...")
        pdf_count = 0
        skipped_count = 0

        for i, matter in enumerate(matters):
            matter_id = matter["MatterId"]
            history_file = output_dir / f"matter_histories/{matter_id}.json"
            if history_file.exists():
                skipped_count += 1
                pdf_count += _count_existing_pdfs(output_dir, matter_id)
                continue

            histories = await _fetch_json(client, f"{base_url}/matters/{matter_id}/histories")
            _save_json(histories, output_dir, f"matter_histories/{matter_id}.json")

            attachments = await _fetch_json(client, f"{base_url}/matters/{matter_id}/attachments")
            _save_json(attachments, output_dir, f"matter_attachments/{matter_id}.json")

            for att in attachments:
                pdf_url = att.get("MatterAttachmentHyperlink")
                if pdf_url:
                    filename = f"{matter_id}_{att['MatterAttachmentId']}.pdf"
                    result = await _download_file(client, pdf_url, output_dir, f"attachments/{filename}")
                    if result:
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
            items_file = output_dir / f"event_items/{event_id}.json"
            if not items_file.exists():
                continue
            items = json.loads(items_file.read_text())
            for item in items:
                if item.get("EventItemRollCallFlag"):
                    item_id = item["EventItemId"]
                    votes = await _fetch_json(client, f"{base_url}/eventitems/{item_id}/votes")
                    if votes:
                        _save_json(votes, output_dir, f"votes/{item_id}.json")
                        vote_count += 1
                    await asyncio.sleep(config.rate_limit_delay)

        print(f"     Found {vote_count} roll-call vote records")
        print()

        # 7. PERSONS
        print("7/7  Fetching persons...")
        persons = await _fetch_json(client, f"{base_url}/persons")
        _save_json(persons, output_dir, "persons.json")
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
    print(f"  Saved to:    {output_dir.resolve()}")
    print("=" * 60)

    return LegistarResult(
        bodies_count=len(bodies),
        events_count=len(events),
        matters_count=len(matters),
        pdf_count=pdf_count,
        vote_count=vote_count,
        persons_count=len(persons),
        output_dir=output_dir,
    )
