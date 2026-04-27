"""
update_csv_meeting_dates.py — Update next_meeting dates in serve_users_unified.csv
from scan_meeting_schedule.py upcoming_meetings.json results.

Reads upcoming_meetings.json for each city slug in the CSV and updates
the next_meeting column with the soonest upcoming meeting date.

Usage:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/update_csv_meeting_dates.py
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/update_csv_meeting_dates.py --dry-run
"""

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage

CSV_PATH = _ROOT / "serve_users_unified.csv"


def get_next_meeting_date(slug: str, storage, sources_prefix: str) -> str | None:
    """Return the soonest upcoming meeting date for a city, or None."""
    key = f"{sources_prefix}/{slug}/upcoming_meetings.json"
    if not storage.exists(key):
        return None
    try:
        data = storage.read_json(key)
    except Exception:
        return None

    today = date.today().isoformat()
    upcoming = [
        m["date"] for m in data.get("upcoming", [])
        if m.get("date", "") >= today
    ]
    return min(upcoming) if upcoming else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    rows = list(csv.DictReader(CSV_PATH.open()))
    fieldnames = list(rows[0].keys())

    updated = 0
    not_found = 0
    unchanged = 0

    for row in rows:
        slug = row.get("city_slug", "").strip()
        if not slug:
            continue

        new_date = get_next_meeting_date(slug, storage, cfg.sources_prefix)
        old_date = row.get("next_meeting", "").strip()

        if new_date is None:
            not_found += 1
            continue

        if new_date == old_date:
            unchanged += 1
            continue

        print(f"  {slug:<40} {old_date or '(none)':<12} → {new_date}")
        row["next_meeting"] = new_date
        updated += 1

    print(f"\nSummary: {updated} updated, {unchanged} unchanged, {not_found} no scan data")

    if args.dry_run:
        print("(dry-run — CSV not written)")
        return

    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV updated: {CSV_PATH}")


if __name__ == "__main__":
    main()
