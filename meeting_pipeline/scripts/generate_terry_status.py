"""
generate_terry_status.py — Generate pipeline status for all cities in Terry Users2.csv.

Status categories (mutually exclusive, checked in order):
  1. Has Future Briefing       — briefing exists with meeting date >= today
  2. Agenda Posted-Needs Collection — upcoming_meetings.json has upcoming meeting with agenda
  3. Scannable-No Upcoming     — supported platform, scan works, but no upcoming meeting found
  4. Source Broken             — supported platform, but scan returns no data at all
  5. Unsupported Platform      — platform not in supported scanner list
  6. No Source Found           — no source.json or source is empty/wrong_entity

Outputs:
  - meeting_pipeline/terry-cities-status.csv (city-level, one row per city slug)
  - Updates Terry Users2.csv Pipeline columns

Usage:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/generate_terry_status.py
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/generate_terry_status.py --dry-run
"""

import argparse
import csv
import json
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


def list_briefing_keys(storage, briefings_prefix: str, slug: str) -> list[str]:
    """Return S3 keys of all briefing JSONs for a city slug."""
    try:
        return storage.list_keys(f"{briefings_prefix}/{slug}_")
    except Exception:
        return []


def get_city_status(slug: str, storage, cfg) -> dict:
    """
    Compute status for a single city slug. Returns dict with:
      status, platform, last_meeting, next_meeting
    """
    sources_prefix = cfg.sources_prefix
    briefings_prefix = cfg.briefings_prefix if hasattr(cfg, "briefings_prefix") else "meeting_pipeline/output/briefings"

    # --- Source info ---
    source_key = f"{sources_prefix}/{slug}/source.json"
    platform = "unknown"
    source_freshness = "unknown"

    if storage.exists(source_key):
        try:
            source = storage.read_json(source_key)
            best = source.get("best_source") or {}
            platform = best.get("platform", "unknown")
            source_freshness = best.get("freshness", "unknown")
        except Exception:
            pass

    # --- Briefings ---
    briefing_keys = list_briefing_keys(storage, briefings_prefix, slug)
    briefing_dates = []
    for k in briefing_keys:
        # Key format: {prefix}/{slug}_{YYYY-MM-DD}_briefing.json
        fname = k.split("/")[-1]
        parts = fname.replace("_briefing.json", "").split("_")
        # Find the date part (last 10-char YYYY-MM-DD segment)
        for part in reversed(parts):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                briefing_dates.append(part)
                break

    has_future_briefing = any(d >= TODAY for d in briefing_dates)
    most_recent_briefing = max(briefing_dates) if briefing_dates else None

    # --- Upcoming meetings ---
    upcoming_key = f"{sources_prefix}/{slug}/upcoming_meetings.json"
    last_meeting = None
    next_meeting = None
    scan_has_data = False

    if storage.exists(upcoming_key):
        try:
            um = storage.read_json(upcoming_key)
            all_meetings = um.get("upcoming", [])

            # last_meeting: most recent past meeting date
            past = [m["date"] for m in all_meetings if m.get("date") and m["date"] < TODAY]
            if past:
                last_meeting = max(past)
                scan_has_data = True

            # next_meeting: soonest upcoming meeting with agenda posted
            upcoming_with_agenda = [
                m["date"] for m in all_meetings
                if m.get("date") and m["date"] >= TODAY and m.get("agenda_posted")
            ]
            if upcoming_with_agenda:
                next_meeting = min(upcoming_with_agenda)

            # Also count any upcoming (agenda or not)
            upcoming_any = [m["date"] for m in all_meetings if m.get("date") and m["date"] >= TODAY]
            if upcoming_any and not next_meeting:
                next_meeting = min(upcoming_any)  # may not have agenda yet

        except Exception:
            pass

    # Fall back last_meeting from most recent briefing if scan has no past data
    if not last_meeting and most_recent_briefing:
        last_meeting = most_recent_briefing

    # --- Status determination ---
    if has_future_briefing:
        status = "Has Future Briefing"
    elif platform in SUPPORTED_PLATFORMS:
        if next_meeting:
            status = "Agenda Posted-Needs Collection"
        elif scan_has_data or last_meeting:
            status = "Scannable-No Upcoming"
        else:
            status = "Source Broken"
    elif source_freshness in ("wrong_entity", "empty", "blocked") or platform == "unknown":
        if not storage.exists(source_key):
            status = "No Source Found"
        else:
            status = "No Source Found" if source_freshness in ("wrong_entity", "empty") else "Unsupported Platform"
    else:
        status = "Unsupported Platform"

    return {
        "slug": slug,
        "platform": platform,
        "status": status,
        "last_meeting": last_meeting or "",
        "next_meeting": next_meeting or "",
        "briefing_count": len(briefing_keys),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    # --- Load Terry CSV ---
    rows = list(csv.DictReader(open(TERRY_CSV)))
    fieldnames = list(rows[0].keys())

    # Add new columns if missing
    for col in ["Pipeline Last meeting", "Pipeline Next meeting", "Pipeline Status"]:
        if col not in fieldnames:
            fieldnames.append(col)

    # Deduplicate slugs for status computation
    slug_to_row_indices: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        city = row.get("City", "").strip()
        state = row.get("State", "").strip()
        if city and state:
            slug = city_to_slug(city, state)
            slug_to_row_indices.setdefault(slug, []).append(i)

    print(f"Computing status for {len(slug_to_row_indices)} unique city slugs...")

    # --- Compute status per slug ---
    slug_status: dict[str, dict] = {}
    status_counts: dict[str, int] = {}

    for slug in sorted(slug_to_row_indices.keys()):
        result = get_city_status(slug, storage, cfg)
        slug_status[slug] = result
        status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1
        print(f"  {slug:<45} {result['platform']:<12} {result['status']}")

    # --- Write terry-cities-status.csv ---
    status_fieldnames = ["Slug", "Platform", "Status", "Last Meeting", "Next Meeting", "Briefings"]
    status_rows = [
        {
            "Slug": r["slug"],
            "Platform": r["platform"],
            "Status": r["status"],
            "Last Meeting": r["last_meeting"],
            "Next Meeting": r["next_meeting"],
            "Briefings": r["briefing_count"],
        }
        for r in sorted(slug_status.values(), key=lambda x: x["status"])
    ]

    if not args.dry_run:
        with open(STATUS_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=status_fieldnames)
            writer.writeheader()
            writer.writerows(status_rows)
        print(f"\nWrote {STATUS_CSV}")

    # --- Update Terry Users2.csv ---
    for i, row in enumerate(rows):
        city = row.get("City", "").strip()
        state = row.get("State", "").strip()
        if not city or not state:
            continue
        slug = city_to_slug(city, state)
        result = slug_status.get(slug)
        if not result:
            continue
        row["Pipeline Last meeting"] = result["last_meeting"]
        row["Pipeline Next meeting"] = result["next_meeting"]
        row["Pipeline Status"] = result["status"]

    if not args.dry_run:
        with open(TERRY_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Updated {TERRY_CSV}")

    # --- Summary ---
    print("\nStatus breakdown:")
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {status:<35} {count}")


if __name__ == "__main__":
    main()
