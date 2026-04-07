"""
generate_meeting_queue.py — Build normalized meeting JSON for upcoming meetings.

Reads collected platform data + pilot_registry.py, produces a QA-ready JSON
file containing:
  - Official info (name, city, state, role)
  - Upcoming meetings with agenda status
  - Direct source URLs for manual fact-checking
  - Agenda file download URLs

Usage:
    uv run python meeting_pipeline/scripts/generate_meeting_queue.py
    uv run python meeting_pipeline/scripts/generate_meeting_queue.py --from-date 2026-04-07
    uv run python meeting_pipeline/scripts/generate_meeting_queue.py --output /tmp/queue.json

Storage:
    Reads from STORAGE_BACKEND (local or s3). Set S3_BUCKET + STORAGE_BACKEND=s3 in .env for S3.
    Output: {output_prefix}/meeting_queue.json
"""

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.pilot_registry import PILOT_OFFICIALS

# CivicClerk tenant slug derived from portal URL subdomain
def _civicclerk_tenant(portal_url: str) -> str:
    m = re.match(r"https://([^.]+)\.portal\.civicclerk\.com", portal_url)
    return m.group(1) if m else ""

def _civicclerk_portal_url(tenant: str, event_id: int) -> str:
    return f"https://{tenant}.portal.civicclerk.com/event/{event_id}"

def _legistar_meeting_url(event: dict) -> str:
    return event.get("EventInSiteURL", "")


def load_civicclerk_meetings(city_slug: str, from_date: str, storage, sources_prefix: str) -> tuple[list[dict], str]:
    """Returns (meetings_after_date, tenant_slug)."""
    meetings_key = f"{sources_prefix}/{city_slug}/data/civicclerk/meetings.json"
    if not storage.exists(meetings_key):
        return [], ""

    source_key = f"{sources_prefix}/{city_slug}/source.json"
    tenant = ""
    if storage.exists(source_key):
        src = storage.read_json(source_key)
        portal_url = src.get("best_source", {}).get("url", "")
        tenant = _civicclerk_tenant(portal_url)

    meetings = storage.read_json(meetings_key)
    future = [m for m in meetings if m.get("date", "") >= from_date]
    return future, tenant


def load_civicplus_meetings(city_slug: str, from_date: str, storage, sources_prefix: str) -> list[dict]:
    """Returns civicplus meetings after from_date."""
    meetings_key = f"{sources_prefix}/{city_slug}/data/civicplus/meetings.json"
    if not storage.exists(meetings_key):
        return []
    meetings = storage.read_json(meetings_key)
    return [m for m in meetings if m.get("date", "") >= from_date]


def load_granicus_meetings(city_slug: str, from_date: str, storage, sources_prefix: str) -> list[dict]:
    """Returns granicus events after from_date."""
    events_key = f"{sources_prefix}/{city_slug}/data/granicus/events.json"
    if not storage.exists(events_key):
        return []
    events = storage.read_json(events_key)
    return [e for e in events if e.get("date", "") >= from_date]


def format_civicplus_meeting(m: dict) -> dict:
    pdf_url = m.get("packetUrl") or m.get("agendaPdfUrl") or ""
    has_agenda = bool(pdf_url)
    status = "agenda_ready" if has_agenda else "agenda_not_posted"
    agenda_files = []
    if m.get("packetUrl"):
        agenda_files.append({"name": "Agenda Packet", "type": "Agenda Packet", "url": m["packetUrl"]})
    if m.get("agendaPdfUrl") and m.get("agendaPdfUrl") != m.get("packetUrl"):
        agenda_files.append({"name": "Agenda", "type": "Agenda", "url": m["agendaPdfUrl"]})
    return {
        "date": m.get("date", ""),
        "title": m.get("title", ""),
        "body": m.get("categoryName", ""),
        "platform": "civicplus",
        "status": status,
        "source_url": "",
        "agenda_files": agenda_files,
        "notes": "",
    }


def format_granicus_meeting(e: dict) -> dict:
    agenda_url = e.get("agendaUrl") or ""
    if agenda_url and "ViewerPhp" not in agenda_url and agenda_url.endswith(".pdf"):
        status = "agenda_ready"
        agenda_files = [{"name": "Agenda", "type": "Agenda", "url": agenda_url}]
    elif agenda_url:
        status = "agenda_posted_no_files"
        agenda_files = [{"name": "Agenda Viewer", "type": "Agenda", "url": agenda_url}]
    else:
        status = "agenda_not_posted"
        agenda_files = []
    return {
        "date": e.get("date", ""),
        "title": e.get("title", ""),
        "body": e.get("body", "City Council"),
        "platform": "granicus",
        "status": status,
        "source_url": agenda_url,
        "agenda_files": agenda_files,
        "notes": "",
    }


def load_legistar_meetings(city_slug: str, from_date: str, storage, sources_prefix: str) -> list[dict]:
    """Returns council events after from_date."""
    events_key = f"{sources_prefix}/{city_slug}/data/legistar/events.json"
    if not storage.exists(events_key):
        return []

    events = storage.read_json(events_key)
    future = []
    for e in events:
        event_date = e.get("EventDate", "")[:10]
        if event_date < from_date:
            continue
        body = e.get("EventBodyName", "")
        if "council" not in body.lower() and "board" not in body.lower():
            continue
        future.append(e)

    return sorted(future, key=lambda e: e.get("EventDate", ""))


def format_civicclerk_meeting(m: dict, tenant: str) -> dict:
    event_id = m.get("eventId")
    portal_url = _civicclerk_portal_url(tenant, event_id) if tenant and event_id else ""
    agenda_files = m.get("agendaFiles", [])
    has_agenda = m.get("hasAgenda", False)

    if has_agenda and agenda_files:
        status = "agenda_ready"
    elif has_agenda:
        status = "agenda_posted_no_files"
    else:
        status = "agenda_not_posted"

    return {
        "date": m.get("date", ""),
        "title": m.get("title", ""),
        "body": m.get("categoryName", ""),
        "platform": "civicclerk",
        "status": status,
        "source_url": portal_url,
        "agenda_files": [
            {
                "name": f.get("name", ""),
                "type": f.get("type", ""),
                "url": f.get("url", ""),
            }
            for f in agenda_files
        ],
        "notes": m.get("displayMessage", "") or "",
    }


def format_legistar_meeting(e: dict) -> dict:
    agenda_url = e.get("EventAgendaFile") or ""
    has_agenda = bool(agenda_url) or bool(e.get("EventAgendaLastPublishedUTC"))
    status = "agenda_ready" if has_agenda and agenda_url else (
        "agenda_posted_no_files" if has_agenda else "agenda_not_posted"
    )

    return {
        "date": e.get("EventDate", "")[:10],
        "title": e.get("EventBodyName", ""),
        "body": e.get("EventBodyName", ""),
        "time": e.get("EventTime", ""),
        "location": (e.get("EventLocation") or "").replace("\r\n", ", "),
        "platform": "legistar",
        "status": status,
        "source_url": _legistar_meeting_url(e),
        "agenda_files": (
            [{"name": "Agenda", "type": "Agenda", "url": agenda_url}]
            if agenda_url else []
        ),
        "minutes_url": e.get("EventMinutesFile") or "",
        "notes": e.get("EventComment") or "",
    }


def main():
    parser = argparse.ArgumentParser(description="Generate meeting queue JSON")
    parser.add_argument("--from-date", default=date.today().isoformat(), help="Start date YYYY-MM-DD (default: today)")
    parser.add_argument("--output", help="Output storage key override")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    from_date = args.from_date
    queue_key = args.output if args.output else f"{cfg.output_prefix}/meeting_queue.json"

    officials = PILOT_OFFICIALS
    print(f"Loaded {len(officials)} officials from pilot_registry")

    # Build city -> source platform lookup by scanning source.json files
    city_platform = {}
    all_keys = storage.list_keys(cfg.sources_prefix)
    source_keys = [k for k in all_keys if k.endswith("/source.json")]
    for source_key in source_keys:
        src = storage.read_json(source_key)
        city = src.get("city", "").lower()
        state = src.get("state", "")
        platform = src.get("best_source", {}).get("platform", "")
        # Derive slug from key: meeting_pipeline/sources/{slug}/source.json
        parts = source_key.split("/")
        slug = parts[-2] if len(parts) >= 2 else ""
        city_platform[(city, state)] = {"platform": platform, "slug": slug}

    # Build queue per official
    queue = []
    skipped = []

    for official in officials:
        key = (official["city"].lower(), official["state"])
        src_info = city_platform.get(key)
        if not src_info:
            skipped.append({**official, "reason": "city not in sources"})
            continue

        platform = src_info["platform"]
        city_slug = src_info["slug"]
        meetings = []

        if platform == "civicclerk":
            raw_meetings, tenant = load_civicclerk_meetings(city_slug, from_date, storage, cfg.sources_prefix)
            seen = set()
            for m in sorted(raw_meetings, key=lambda x: x.get("date", "")):
                key2 = (m.get("date"), m.get("title"), m.get("hasAgenda"))
                if key2 in seen:
                    continue
                seen.add(key2)
                meetings.append(format_civicclerk_meeting(m, tenant))

        elif platform == "legistar":
            raw_events = load_legistar_meetings(city_slug, from_date, storage, cfg.sources_prefix)
            for e in raw_events:
                meetings.append(format_legistar_meeting(e))

        elif platform == "civicplus":
            raw_meetings = load_civicplus_meetings(city_slug, from_date, storage, cfg.sources_prefix)
            for m in raw_meetings:
                meetings.append(format_civicplus_meeting(m))

        elif platform == "granicus":
            raw_events = load_granicus_meetings(city_slug, from_date, storage, cfg.sources_prefix)
            for e in raw_events:
                meetings.append(format_granicus_meeting(e))

        else:
            skipped.append({**official, "reason": f"platform '{platform}' not yet supported in queue"})
            continue

        if not meetings:
            skipped.append({**official, "reason": "no upcoming meetings found after " + from_date})
            continue

        queue.append({
            "official": {
                "name": official["name"],
                "city": official["city"],
                "state": official["state"],
                "role": official["role"],
            },
            "platform": platform,
            "city_slug": city_slug,
            "upcoming_meetings": meetings,
        })

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "from_date": from_date,
        "total_officials_in_queue": len(queue),
        "total_skipped": len(skipped),
        "meetings_ready": sum(
            1 for o in queue for m in o["upcoming_meetings"] if m["status"] == "agenda_ready"
        ),
        "queue": queue,
        "skipped": skipped,
    }

    storage.write_json(queue_key, output)

    print(f"\n{'='*60}")
    print(f"MEETING QUEUE SUMMARY")
    print(f"{'='*60}")
    print(f"  From date:          {from_date}")
    print(f"  Officials in queue: {len(queue)}")
    print(f"  Skipped:            {len(skipped)}")
    print(f"  Meetings with agenda ready: {output['meetings_ready']}")
    print(f"\nPer official:")
    for o in queue:
        ready = [m for m in o["upcoming_meetings"] if m["status"] == "agenda_ready"]
        pending = [m for m in o["upcoming_meetings"] if m["status"] != "agenda_ready"]
        print(f"  {o['official']['name']:25s}  {o['official']['city']}, {o['official']['state']}  "
              f"[{o['platform']}]  {len(ready)} ready, {len(pending)} pending")
    if skipped:
        print(f"\nSkipped:")
        for s in skipped:
            print(f"  {s['name']:25s}  {s['city']}, {s['state']}  — {s['reason']}")
    print(f"\nOutput: {queue_key}")


if __name__ == "__main__":
    main()
