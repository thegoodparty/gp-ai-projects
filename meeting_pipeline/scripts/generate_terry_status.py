"""
generate_terry_status.py — Generate pipeline status for all cities in Terry Users2.csv.

Status categories (mutually exclusive, checked in order):
  1. Has Future Briefing       — briefing exists with meeting date >= today
  2. Agenda Posted-Needs Collection — scan found future meeting with agenda posted, no briefing yet
  3. Scannable-No Upcoming     — supported platform, scan works, no future meetings with agendas
  4. Source Broken             — supported platform configured, but scan returns no data at all
  5. Unsupported Platform      — platform not in supported scanner list
  6. No Source Found           — no source.json, or source is empty/wrong_entity

CSV columns written to Terry Users2.csv:
  Pipeline Last meeting     — most recent past meeting date from scan data (or briefing fallback)
  Pipeline Next Meeting     — soonest upcoming meeting date (any agenda status)
  Pipeline Agenda Posted    — soonest upcoming meeting that has an agenda posted
  Briefing Dates            — comma-separated list of all dates we generated briefings for
  Pipeline Status           — one of the 6 statuses above

Usage:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/generate_terry_status.py
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/generate_terry_status.py --dry-run
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
for p in [str(_ROOT), str(_PROJECT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from meeting_pipeline.collection_agent.config import AgentConfig, city_to_slug, get_storage

TODAY = date.today().isoformat()
TERRY_CSV = _ROOT / "Terry Users2.csv"
STATUS_CSV = _ROOT / "terry-cities-status.csv"

SUPPORTED_PLATFORMS = {"legistar", "civicplus", "civicclerk", "boarddocs", "escribe"}

# Columns added/managed by this script (in order)
PIPELINE_COLUMNS = [
    "Pipeline Last meeting",
    "Pipeline Next Meeting",
    "Pipeline Agenda Posted",
    "Briefing Dates",
    "Pipeline Status",
]


def list_briefing_keys(storage, briefings_prefix: str, slug: str) -> list[str]:
    """Return S3 keys of all briefing JSONs for a city slug."""
    try:
        return storage.list_keys(f"{briefings_prefix}/{slug}_")
    except Exception:
        return []


def extract_briefing_dates(briefing_keys: list[str]) -> list[str]:
    """Extract YYYY-MM-DD dates from briefing S3 key filenames."""
    dates = []
    for k in briefing_keys:
        fname = k.split("/")[-1]  # e.g. "chapel-hill-NC_2026-04-09_briefing.json"
        parts = fname.replace("_briefing.json", "").split("_")
        for part in reversed(parts):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                dates.append(part)
                break
    return sorted(dates)


def get_city_status(slug: str, storage, cfg) -> dict:
    """
    Compute all status fields for a single city slug.

    Returns dict with:
      status, platform, last_meeting, next_meeting, agenda_posted_date, briefing_dates
    """
    sources_prefix = cfg.sources_prefix
    briefings_prefix = getattr(cfg, "briefings_prefix", "meeting_pipeline/output/briefings")

    # ── Source info ───────────────────────────────────────────────────────────
    source_key = f"{sources_prefix}/{slug}/source.json"
    platform = "unknown"
    source_freshness = "unknown"
    source_exists = storage.exists(source_key)

    if source_exists:
        try:
            source = storage.read_json(source_key)
            best = source.get("best_source") or {}
            platform = best.get("platform", "unknown")
            source_freshness = best.get("freshness", "unknown")
        except Exception:
            pass

    # ── Briefings ─────────────────────────────────────────────────────────────
    briefing_keys = list_briefing_keys(storage, briefings_prefix, slug)
    briefing_dates = extract_briefing_dates(briefing_keys)

    has_future_briefing = any(d >= TODAY for d in briefing_dates)
    most_recent_briefing = max(briefing_dates) if briefing_dates else None

    # ── Upcoming meetings ─────────────────────────────────────────────────────
    upcoming_key = f"{sources_prefix}/{slug}/upcoming_meetings.json"
    last_meeting = None        # most recent past meeting from scan
    next_meeting = None        # soonest upcoming meeting (any status)
    agenda_posted_date = None  # soonest upcoming meeting with agenda posted
    scan_has_data = False

    if storage.exists(upcoming_key):
        try:
            um = storage.read_json(upcoming_key)
            all_meetings = um.get("upcoming", [])

            past_dates = [
                m["date"] for m in all_meetings
                if m.get("date") and m["date"] < TODAY
            ]
            if past_dates:
                last_meeting = max(past_dates)
                scan_has_data = True

            future_meetings = [m for m in all_meetings if m.get("date") and m["date"] >= TODAY]

            future_dates = [m["date"] for m in future_meetings]
            if future_dates:
                next_meeting = min(future_dates)

            agenda_dates = [
                m["date"] for m in future_meetings if m.get("agenda_posted")
            ]
            if agenda_dates:
                agenda_posted_date = min(agenda_dates)

        except Exception:
            pass

    # Fall back last_meeting from most recent briefing if scan has no past data
    # (covers unsupported platforms where we generated briefings historically)
    if not last_meeting and most_recent_briefing:
        last_meeting = most_recent_briefing

    # ── Status determination ──────────────────────────────────────────────────
    if has_future_briefing:
        status = "Has Future Briefing"
    elif platform in SUPPORTED_PLATFORMS:
        if agenda_posted_date:
            # Agenda is posted and we haven't generated a briefing yet
            status = "Agenda Posted-Needs Collection"
        elif scan_has_data or next_meeting:
            # Platform works — we've seen meetings — but no agenda posted yet
            status = "Scannable-No Upcoming"
        else:
            # Supported platform but scan returns nothing at all
            status = "Source Broken"
    elif not source_exists or source_freshness in ("wrong_entity", "empty"):
        status = "No Source Found"
    else:
        status = "Unsupported Platform"

    return {
        "slug": slug,
        "platform": platform,
        "status": status,
        "last_meeting": last_meeting or "",
        "next_meeting": next_meeting or "",
        "agenda_posted_date": agenda_posted_date or "",
        "briefing_dates": ", ".join(briefing_dates),
        "briefing_count": len(briefing_keys),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    # ── Load Terry CSV ────────────────────────────────────────────────────────
    with open(TERRY_CSV) as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())

    # Remove the old "Pipeline Next meeting" column if present (renamed to "Pipeline Next Meeting")
    if "Pipeline Next meeting" in fieldnames:
        fieldnames.remove("Pipeline Next meeting")
    # Strip old key from all row dicts so DictWriter doesn't complain
    for row in rows:
        row.pop("Pipeline Next meeting", None)

    # Ensure all pipeline columns exist in the correct order.
    # Strategy: find the anchor ("Pipeline Last meeting"), remove any existing
    # pipeline cols from wherever they are, then re-insert them in order after the anchor.
    anchor = "Pipeline Last meeting"
    if anchor not in fieldnames:
        fieldnames.append(anchor)
    anchor_idx = fieldnames.index(anchor)

    # Remove existing pipeline cols (they may be in wrong positions)
    for col in PIPELINE_COLUMNS:
        if col in fieldnames and col != anchor:
            fieldnames.remove(col)

    # Re-insert in correct order right after anchor
    for i, col in enumerate(PIPELINE_COLUMNS):
        if col == anchor:
            continue  # anchor is already there
        insert_at = fieldnames.index(anchor) + (PIPELINE_COLUMNS.index(col))
        if col not in fieldnames:
            fieldnames.insert(insert_at, col)

    # ── Deduplicate slugs ─────────────────────────────────────────────────────
    slug_order = []
    seen = set()
    for row in rows:
        city = row.get("City", "").strip()
        state = row.get("State", "").strip()
        if city and state:
            slug = city_to_slug(city, state)
            if slug not in seen:
                seen.add(slug)
                slug_order.append(slug)

    print(f"Computing status for {len(slug_order)} unique city slugs...")

    # ── Compute status per slug ───────────────────────────────────────────────
    slug_status: dict[str, dict] = {}
    status_counts: dict[str, int] = {}

    for slug in slug_order:
        result = get_city_status(slug, storage, cfg)
        slug_status[slug] = result
        status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1
        agenda_marker = f" agenda={result['agenda_posted_date']}" if result["agenda_posted_date"] else ""
        print(
            f"  {slug:<45} {result['platform']:<12} {result['status']}"
            f"{agenda_marker}"
        )

    # ── Write terry-cities-status.csv ─────────────────────────────────────────
    status_fieldnames = [
        "Slug", "Platform", "Status",
        "Last Meeting", "Next Meeting", "Agenda Posted", "Briefing Dates", "Briefings",
    ]
    status_rows = [
        {
            "Slug": r["slug"],
            "Platform": r["platform"],
            "Status": r["status"],
            "Last Meeting": r["last_meeting"],
            "Next Meeting": r["next_meeting"],
            "Agenda Posted": r["agenda_posted_date"],
            "Briefing Dates": r["briefing_dates"],
            "Briefings": r["briefing_count"],
        }
        for r in sorted(slug_status.values(), key=lambda x: (x["status"], x["slug"]))
    ]

    if not args.dry_run:
        with open(STATUS_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=status_fieldnames)
            writer.writeheader()
            writer.writerows(status_rows)
        print(f"\nWrote {STATUS_CSV}")

    # ── Update Terry Users2.csv ───────────────────────────────────────────────
    for row in rows:
        city = row.get("City", "").strip()
        state = row.get("State", "").strip()
        if not city or not state:
            continue
        slug = city_to_slug(city, state)
        result = slug_status.get(slug)
        if not result:
            continue
        row["Pipeline Last meeting"] = result["last_meeting"]
        row["Pipeline Next Meeting"] = result["next_meeting"]
        row["Pipeline Agenda Posted"] = result["agenda_posted_date"]
        row["Briefing Dates"] = result["briefing_dates"]
        row["Pipeline Status"] = result["status"]
        # Clear the old column if it exists in row data
        row.pop("Pipeline Next meeting", None)

    if not args.dry_run:
        with open(TERRY_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Updated {TERRY_CSV}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\nStatus breakdown:")
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {status:<35} {count}")


if __name__ == "__main__":
    main()
