"""
generate_terry_status.py — Generate pipeline status for all cities in Terry Users2.csv.

Status categories (mutually exclusive, checked in order):
  1. Has Future Briefing    — briefing exists for a meeting date >= today
  2. Agenda Ready           — future meeting with agenda posted, no briefing yet
  3. Awaiting Agenda        — future meeting visible but no agenda posted yet
  4. Scannable-No Upcoming  — supported platform, scan works, no future meetings visible yet
  5. Source Broken          — supported platform configured, scan returns nothing
  6. Unknown                — has past briefings but now on unsupported/broken platform
  7. Unsupported Platform   — platform not in supported scanner list, no past briefings
  8. No Source Found        — no source.json, or source is empty/wrong_entity

CSV columns written to Terry Users2.csv:
  Pipeline Last Meeting  — most recent past council meeting date we have seen
  Pipeline Next Meeting  — soonest FUTURE meeting date (any agenda status); blank if none
  Next Briefing Date     — date of the next future briefing we have (today or later); blank if none
  Briefings              — comma-separated list of ALL briefing dates we have generated
  Pipeline Status        — one of the 8 statuses above

Usage:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/generate_terry_status.py
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/generate_terry_status.py --dry-run
"""

import argparse
import csv
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig, city_to_slug, get_storage

TODAY = date.today().isoformat()
TERRY_CSV = _ROOT / "Terry Users2.csv"
STATUS_CSV = _ROOT / "terry-cities-status.csv"

# Platforms the pipeline can actively collect from — must stay in sync with
# LOADABLE_PLATFORMS in run_serve_users_pipeline.py
SUPPORTED_PLATFORMS = {
    "legistar", "civicplus", "civicclerk", "granicus", "swagit",
    "escribe", "boarddocs", "municode", "novus",
    "unknown", "generic_html",
}

# Columns managed by this script (in order after "Pipeline Last Meeting")
PIPELINE_COLUMNS = [
    "Pipeline Last Meeting",
    "Pipeline Next Meeting",
    "Next Briefing Date",
    "Briefings",
    "Pipeline Status",
    "QA Pass",
]

# Old column names to migrate away from
OLD_COLUMN_NAMES = {
    "Pipeline Last meeting": "Pipeline Last Meeting",
    "Pipeline Agenda Posted": "Next Briefing Date",
    "Briefing Dates": "Briefings",
    "Pipeline Next meeting": "Pipeline Next Meeting",
}


_QA_S3_PREFIX = "meeting_pipeline/qa_outputs"


def get_qa_result(storage, slug: str, briefing_date: str) -> str:
    """Return QA delivery status for the given briefing: 'Pass', 'Block', or ''."""
    if not briefing_date:
        return ""
    # Try both stem formats: with and without _briefing suffix
    for stem in (f"{slug}_{briefing_date}", f"{slug}_{briefing_date}_briefing"):
        key = f"{_QA_S3_PREFIX}/{stem}/qa_result.json"
        try:
            data = storage.read_json(key)
            status = data.get("delivery_status", "")
            if status == "OK":
                return "Pass"
            if status == "Block":
                return "Block"
            return status
        except Exception:
            pass
    return ""


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

    Returns dict with keys:
      status, platform, last_meeting, next_meeting, next_briefing_date, briefings
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

    # Future briefing = date >= today
    future_briefing_dates = [d for d in briefing_dates if d >= TODAY]
    has_future_briefing = len(future_briefing_dates) > 0
    next_briefing_date = min(future_briefing_dates) if future_briefing_dates else None

    # Had any briefings ever (for Unknown status detection)
    had_any_briefing = len(briefing_dates) > 0

    # ── Upcoming meetings ─────────────────────────────────────────────────────
    upcoming_key = f"{sources_prefix}/{slug}/upcoming_meetings.json"
    last_meeting = None       # most recent past meeting date
    next_meeting = None       # soonest future meeting date (any agenda status)
    agenda_posted_date = None # soonest future meeting with agenda posted
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
                scan_has_data = True

            agenda_dates = [
                m["date"] for m in future_meetings if m.get("agenda_posted")
            ]
            if agenda_dates:
                agenda_posted_date = min(agenda_dates)

        except Exception:
            pass

    # Fall back last_meeting from most recent past briefing if scan has no past data
    if not last_meeting and briefing_dates:
        past_briefings = [d for d in briefing_dates if d < TODAY]
        if past_briefings:
            last_meeting = max(past_briefings)

    # ── Status determination ──────────────────────────────────────────────────
    if has_future_briefing:
        # We have a briefing ready for an upcoming meeting
        status = "Has Future Briefing"
    elif platform in SUPPORTED_PLATFORMS:
        if agenda_posted_date:
            # Agenda is posted for a future meeting but no briefing generated yet
            status = "Agenda Ready"
        elif next_meeting:
            # Future meeting visible but no agenda posted yet
            status = "Awaiting Agenda"
        elif scan_has_data:
            # Supported platform, scan found past meetings, but nothing upcoming yet
            status = "Scannable-No Upcoming"
        else:
            # Supported platform but scan returns nothing at all
            status = "Source Broken"
    elif had_any_briefing:
        # We've generated briefings before but platform is now unsupported/broken
        status = "Unknown"
    elif not source_exists or source_freshness in ("wrong_entity", "empty"):
        status = "No Source Found"
    else:
        # Known platform but no scanner built for it
        status = "Unsupported Platform"

    qa_pass = get_qa_result(storage, slug, next_briefing_date) if next_briefing_date else ""

    return {
        "slug": slug,
        "platform": platform,
        "status": status,
        "last_meeting": last_meeting or "",
        "next_meeting": next_meeting or "",
        "next_briefing_date": next_briefing_date or "",
        "briefings": ", ".join(briefing_dates),
        "briefing_count": len(briefing_keys),
        "qa_pass": qa_pass,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    # ── Load Terry CSV ────────────────────────────────────────────────────────
    with open(TERRY_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())

    # ── Migrate old column names ──────────────────────────────────────────────
    for old_name, new_name in OLD_COLUMN_NAMES.items():
        if old_name in fieldnames:
            idx = fieldnames.index(old_name)
            fieldnames[idx] = new_name
        # Rename keys in all row dicts
        for row in rows:
            if old_name in row:
                row[new_name] = row.pop(old_name)

    # ── Ensure pipeline columns exist in correct order ────────────────────────
    # Anchor on "Pipeline Last Meeting"; remove any existing pipeline cols then
    # re-insert them in order right after the anchor.
    anchor = "Pipeline Last Meeting"
    if anchor not in fieldnames:
        fieldnames.append(anchor)

    for col in PIPELINE_COLUMNS:
        if col in fieldnames and col != anchor:
            fieldnames.remove(col)

    anchor_idx = fieldnames.index(anchor)
    for _i, col in enumerate(PIPELINE_COLUMNS):
        if col == anchor:
            continue
        insert_at = anchor_idx + PIPELINE_COLUMNS.index(col)
        if col not in fieldnames:
            fieldnames.insert(insert_at, col)

    # ── Deduplicate city slugs ────────────────────────────────────────────────
    slug_order = []
    seen: set[str] = set()
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
        marker = ""
        if result["next_briefing_date"]:
            marker = f" briefing={result['next_briefing_date']}"
        elif result["next_meeting"]:
            marker = f" next={result['next_meeting']}"
        print(
            f"  {slug:<45} {result['platform']:<12} {result['status']}{marker}"
        )

    # ── Write terry-cities-status.csv ─────────────────────────────────────────
    status_fieldnames = [
        "Slug", "Platform", "Status",
        "Last Meeting", "Next Meeting", "Next Briefing Date", "Briefings", "Briefing Count", "QA Pass",
    ]
    status_rows = [
        {
            "Slug": r["slug"],
            "Platform": r["platform"],
            "Status": r["status"],
            "Last Meeting": r["last_meeting"],
            "Next Meeting": r["next_meeting"],
            "Next Briefing Date": r["next_briefing_date"],
            "Briefings": r["briefings"],
            "Briefing Count": r["briefing_count"],
            "QA Pass": r["qa_pass"],
        }
        for r in sorted(slug_status.values(), key=lambda x: (x["status"], x["slug"]))
    ]

    if not args.dry_run:
        with open(STATUS_CSV, "w", newline="", encoding="utf-8") as f:
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
            # City has no S3 data — leave existing values, don't drop the row
            continue
        row["Pipeline Last Meeting"] = result["last_meeting"]
        row["Pipeline Next Meeting"] = result["next_meeting"]
        row["Next Briefing Date"] = result["next_briefing_date"]
        row["Briefings"] = result["briefings"]
        row["Pipeline Status"] = result["status"]
        row["QA Pass"] = result["qa_pass"]

    if not args.dry_run:
        # Ensure every row has all expected fieldname keys (fill missing with "")
        # and strip any stale keys not in fieldnames to prevent DictWriter errors.
        clean_rows = []
        set(fieldnames)
        for row in rows:
            clean_row = {k: row.get(k, "") for k in fieldnames}
            clean_rows.append(clean_row)
        with open(TERRY_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(clean_rows)
        print(f"Updated {TERRY_CSV}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\nStatus breakdown:")
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {status:<35} {count}")
    print(f"  {'Total unique cities':<35} {len(slug_order)}")


if __name__ == "__main__":
    main()
