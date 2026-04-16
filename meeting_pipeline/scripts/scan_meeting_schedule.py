"""
scan_meeting_schedule.py — Lightweight scan for upcoming meeting dates.

For each city with a valid source.json, makes a minimal API/scraper call to
discover upcoming meeting dates and whether an agenda has been posted. Writes
per-city upcoming_meetings.json without downloading any PDFs.

This is the first stage of a two-stage pipeline:
  1. scan_meeting_schedule.py (daily, cheap) — discover dates + agenda status
  2. Full collection scripts (triggered only when agenda_posted=true)

Output: sources/{city}/upcoming_meetings.json
  {
    "city_slug": "chapel-hill-NC",
    "city": "Chapel Hill",
    "state": "NC",
    "body": "Town Council",
    "platform": "legistar",
    "scanned_at": "2026-04-15T...",
    "upcoming": [
      {
        "date": "2026-04-22",
        "title": "Town Council Regular Meeting",
        "agenda_posted": true,
        "agenda_url": "https://...",
        "event_id": "12345"
      }
    ]
  }

Usage:
    # Scan all cities with a known source
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py

    # Scan a single city
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --city chapel-hill-NC

    # Dry-run: list what would be scanned
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --dry-run

    # Only show cities where agenda_posted changed from false → true since last scan
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --report-new
"""

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage

LOOKAHEAD_DAYS = 90  # How many days ahead to look for meetings
SUPPORTED_PLATFORMS = {"legistar", "civicplus", "boarddocs", "civicclerk", "escribe"}


# ============================================================================
# PER-PLATFORM LIGHTWEIGHT SCANNERS
# Each returns list of upcoming meeting dicts (no PDFs downloaded).
# ============================================================================

async def scan_legistar(city: str, config: dict, client: httpx.AsyncClient) -> list[dict]:
    """
    Legistar: query the events API for future meetings only.
    EventAgendaLastPublishedUTC non-null → agenda is posted.
    """
    slug = config.get("legistar_slug", "")
    if not slug:
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    base_url = f"https://webapi.legistar.com/v1/{slug}"

    try:
        resp = await client.get(
            f"{base_url}/events",
            params={
                "$filter": f"EventDate ge datetime'{today}' and EventDate le datetime'{cutoff}'",
                "$orderby": "EventDate asc",
                "$top": 20,
            },
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"    Legistar fetch error for {slug}: {e}")
        return []

    upcoming = []
    for ev in events:
        date_raw = ev.get("EventDate", "")
        date = date_raw[:10] if date_raw else None
        if not date:
            continue
        # agenda_url from EventAgendaFile (viewer link) or EventVideoPath
        agenda_url = ev.get("EventAgendaFile") or None
        # EventAgendaLastPublishedUTC non-null means the agenda was published
        published = ev.get("EventAgendaLastPublishedUTC")
        agenda_posted = bool(published and published != "0001-01-01T00:00:00")

        upcoming.append({
            "date": date,
            "title": ev.get("EventBodyName", city),
            "agenda_posted": agenda_posted,
            "agenda_url": agenda_url,
            "event_id": str(ev.get("EventId", "")),
        })

    return upcoming


async def scan_civicplus(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    CivicPlus AgendaCenter: reuse the existing scraper's category discovery and
    meeting-list fetch — same logic, no PDF download.
    Presence of agenda_pdf_url on a CivicPlusMeeting → agenda_posted=True.
    """
    from urllib.parse import urlparse
    from meeting_pipeline.collectors.civicplus_scraper import find_council_category, fetch_meeting_list

    # Extract domain the same way router.py does
    domain = config.get("domain", "") or urlparse(source_url).netloc.replace("www.", "")
    if not domain:
        return []

    cat_id = config.get("council_category_id") or config.get("category_id")

    try:
        if not cat_id:
            cat_id, _ = await find_council_category(client, domain)

        meetings = await fetch_meeting_list(client, domain, cat_id, datetime.now().year)
    except Exception as e:
        print(f"    CivicPlus fetch error for {domain}: {e}")
        return []

    today = datetime.now().date()
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)
    upcoming = []

    for m in meetings:
        try:
            date_obj = datetime.strptime(m.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if date_obj < today or date_obj > cutoff:
            continue
        upcoming.append({
            "date": m.date,
            "title": m.title,
            "agenda_posted": bool(m.agenda_pdf_url),
            "agenda_url": m.agenda_pdf_url,
            "event_id": m.agenda_id,
        })

    upcoming.sort(key=lambda m: m["date"])
    return upcoming


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
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)
    today_str = today.strftime("%Y%m%d")
    cutoff_str = cutoff.strftime("%Y%m%d")
    upcoming = []

    for committee in council_committees:
        meetings = await _fetch_meetings(client, bd_config, headers, committee["id"])
        for m in meetings:
            num_date = str(m.get("numberdate", ""))
            if not num_date or len(num_date) < 8:
                continue
            if num_date < today_str or num_date > cutoff_str:
                continue
            try:
                date_str = datetime.strptime(num_date[:8], "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                continue
            agenda_url = m.get("EventAgendaFile") or None
            upcoming.append({
                "date": date_str,
                "title": m.get("EventComment", committee["name"]),
                "agenda_posted": bool(agenda_url),
                "agenda_url": agenda_url,
                "event_id": str(m.get("EventId", m.get("unique", ""))),
            })

    upcoming.sort(key=lambda m: m["date"])
    return upcoming


async def scan_civicclerk(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    CivicClerk OData API: query for future events.
    """
    match = re.search(r"https://(\w+)\.(?:api\.)?civicclerk\.com", source_url)
    if not match:
        # Try from config
        tenant = config.get("tenant", "")
        if not tenant:
            return []
    else:
        tenant = match.group(1)

    today = datetime.now().strftime("%Y-%m-%dT00:00:00")
    cutoff = (datetime.now() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT00:00:00")

    try:
        resp = await client.get(
            f"https://{tenant}.api.civicclerk.com/v1/Events/",
            params={
                "$filter": f"MeetingStartDate ge {today} and MeetingStartDate le {cutoff}",
                "$orderby": "MeetingStartDate asc",
                "$top": 20,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data.get("value", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"    CivicClerk fetch error for {tenant}: {e}")
        return []

    upcoming = []
    for ev in events:
        date_raw = ev.get("MeetingStartDate", ev.get("EventDate", ""))
        date = date_raw[:10] if date_raw else None
        if not date:
            continue
        # Agenda is posted if AgendaFile or AgendaPostedDate is present
        agenda_url = ev.get("AgendaFile") or ev.get("AgendaUrl") or None
        agenda_posted = bool(agenda_url) or bool(ev.get("AgendaPostedDate"))
        upcoming.append({
            "date": date,
            "title": ev.get("Name", ev.get("EventName", city)),
            "agenda_posted": agenda_posted,
            "agenda_url": agenda_url,
            "event_id": str(ev.get("EventId", ev.get("Id", ""))),
        })

    return upcoming


# ============================================================================
# MAIN SCAN DISPATCHER
# ============================================================================

async def scan_city(slug: str, source: dict, client: httpx.AsyncClient) -> dict | None:
    """Scan one city's upcoming meetings. Returns the upcoming_meetings record."""
    best = source.get("best_source") or {}
    platform = best.get("platform", "")
    config = best.get("config", {})
    source_url = best.get("url", "")
    city = source.get("city", slug)
    state = source.get("state", "")

    # Derive body name from source
    body = best.get("expected_body", config.get("expected_body", ""))

    upcoming: list[dict] = []

    if platform == "legistar":
        upcoming = await scan_legistar(city, config, client)
    elif platform == "civicplus":
        upcoming = await scan_civicplus(city, config, source_url, client)
    elif platform == "boarddocs":
        upcoming = await scan_boarddocs(city, config, source_url, client)
    elif platform == "civicclerk":
        upcoming = await scan_civicclerk(city, config, source_url, client)
    else:
        # Unsupported platform — record that we know it exists but can't scan
        pass

    return {
        "city_slug": slug,
        "city": city,
        "state": state,
        "body": body,
        "platform": platform,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "upcoming": upcoming,
    }


# ============================================================================
# BATCH RUNNER
# ============================================================================

async def run_batch(
    city_slug: str | None,
    dry_run: bool,
    report_new: bool,
    cfg: AgentConfig,
    storage,
):
    # Load all city source.json files
    all_source_keys = storage.list_keys(cfg.sources_prefix)
    source_keys = [k for k in all_source_keys if k.endswith("/source.json")]

    if city_slug:
        source_keys = [k for k in source_keys if f"/{city_slug}/" in k]

    print(f"Schedule Scanner: {len(source_keys)} cities")
    print()

    if dry_run:
        for k in source_keys:
            slug = k.split("/")[-2]
            try:
                src = storage.read_json(k)
                platform = (src.get("best_source") or {}).get("platform", "?")
            except Exception:
                platform = "?"
            print(f"  {slug:<35} [{platform}]")
        return

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True,
        timeout=20,
    ) as client:

        results = {"scanned": 0, "skipped": 0, "errors": 0, "new_agendas": []}

        for i, key in enumerate(source_keys, 1):
            slug = key.split("/")[-2]
            try:
                source = storage.read_json(key)
            except Exception:
                results["errors"] += 1
                continue

            if not source:
                results["errors"] += 1
                continue

            platform = (source.get("best_source") or {}).get("platform", "")
            if platform not in SUPPORTED_PLATFORMS:
                print(f"[{i}/{len(source_keys)}] {slug} — skip ({platform} not supported)")
                results["skipped"] += 1
                continue

            # Load previous scan to detect agenda_posted changes
            prev_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
            prev = {}
            if storage.exists(prev_key):
                try:
                    prev = storage.read_json(prev_key)
                except Exception:
                    pass

            prev_posted = {m["date"]: m.get("agenda_posted", False)
                          for m in prev.get("upcoming", [])}

            print(f"[{i}/{len(source_keys)}] {slug} ({platform})...", end=" ", flush=True)
            try:
                record = await scan_city(slug, source, client)
                storage.write_json(prev_key, record)

                upcoming = record.get("upcoming", [])
                posted = [m for m in upcoming if m.get("agenda_posted")]
                unposted = [m for m in upcoming if not m.get("agenda_posted")]
                print(f"{len(upcoming)} upcoming ({len(posted)} posted, {len(unposted)} pending)")

                # Detect newly-posted agendas
                for m in upcoming:
                    if m.get("agenda_posted") and not prev_posted.get(m["date"], False):
                        results["new_agendas"].append({"city": slug, "date": m["date"], "title": m["title"]})

                results["scanned"] += 1

            except Exception as e:
                print(f"ERROR: {e}")
                results["errors"] += 1

        print()
        print("=" * 60)
        print(f"SUMMARY: {results['scanned']} scanned, {results['skipped']} skipped, {results['errors']} errors")

        if results["new_agendas"]:
            print(f"\nNEW AGENDAS POSTED ({len(results['new_agendas'])}):")
            for item in results["new_agendas"]:
                print(f"  {item['city']:<35} {item['date']}  {item['title']}")
        elif report_new:
            print("\nNo newly-posted agendas detected.")

        print("=" * 60)


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Scan upcoming meeting schedules for all cities")
    parser.add_argument("--city", help="Scan a single city slug (e.g. chapel-hill-NC)")
    parser.add_argument("--dry-run", action="store_true", help="List cities without making HTTP requests")
    parser.add_argument("--report-new", action="store_true",
                        help="Highlight cities where agenda_posted flipped true since last scan")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    asyncio.run(run_batch(args.city, args.dry_run, args.report_new, cfg, storage))


if __name__ == "__main__":
    main()
