"""
normalize_all_meetings.py — Unify all meeting data into consistent per-city meetings.json.

Reads from two file locations:
  - sources/{city}/meetings.json (Legistar transform output)
  - sources/{city}/meetings_extracted.json (PDF/LLM extraction output)

Validates every record, normalizes field names, and writes:
  - sources/{city}/meetings.json (unified, validated)
  - sources/all_meetings.json (master file, all cities)
  - sources/normalization_report.json (validation report)

Body name classification uses Gemini Flash LLM instead of hardcoded lists.
Results are cached in sources/body_classification_cache.json to avoid
repeated LLM calls. Use --reclassify to force a fresh LLM call.

Usage:
    uv run python meeting_pipeline/scripts/normalize_all_meetings.py
    uv run python meeting_pipeline/scripts/normalize_all_meetings.py --city apex-NC
    uv run python meeting_pipeline/scripts/normalize_all_meetings.py --dry-run
    uv run python meeting_pipeline/scripts/normalize_all_meetings.py --reclassify
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

SOURCES_DIR = _BRIEFING_ROOT / "sources"
CACHE_PATH = SOURCES_DIR / "body_classification_cache.json"

# Required top-level fields
REQUIRED_FIELDS = {"citySlug", "cityName", "state", "date", "body", "status", "sourceType", "data"}
REQUIRED_DATA_FIELDS = {"version", "agendaItems", "summary", "source"}
REQUIRED_SUMMARY_FIELDS = {"totalItems"}


# ---------------------------------------------------------------------------
# LLM-based body classification (replaces hardcoded lists)
# ---------------------------------------------------------------------------

class BodyClassification(BaseModel):
    """LLM classification of a single body name."""
    original: str = Field(description="The original body name exactly as provided")
    is_council: bool = Field(description="True if this is the primary legislative body (city council, town council, board of commissioners)")
    canonical_name: str = Field(description="Standardized name: 'City Council', 'Town Council', 'Board of Commissioners', or a specific variant like 'City Council Work Session'")
    reason: str = Field(description="One-sentence explanation")


class BodyClassificationBatch(BaseModel):
    """Batch classification of body names."""
    classifications: list[BodyClassification]


# In-memory cache populated from disk or LLM
_body_cache: dict[str, dict] = {}  # lower(body) -> {"is_council": bool, "canonical_name": str}


def _load_cache() -> dict[str, dict]:
    """Load cached body classifications from disk."""
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    """Save body classifications to disk."""
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def classify_bodies_with_llm(body_names: list[str]) -> dict[str, dict]:
    """Use Gemini Flash to classify body names as council/non-council.

    Returns dict mapping lowercase body name -> {"is_council": bool, "canonical_name": str}.
    """
    from shared.llm_gemini import GeminiClient, GeminiModelType

    if not body_names:
        return {}

    # Build numbered list for the prompt
    numbered = "\n".join(f"  {i+1}. \"{name}\"" for i, name in enumerate(body_names))

    prompt = f"""You are classifying government body names from city council meeting systems.

For each body name below, determine:
1. **is_council**: Is this the PRIMARY legislative body of the city? Include: City Council, Town Council, Town Board, Board of Commissioners, and their variants (work sessions, special meetings, workshops, caucus, committee of the whole). Exclude: advisory boards, planning commissions, zoning boards, school boards, committees, neighborhood groups, county bodies.
2. **canonical_name**: The standardized name. Use these canonical forms:
   - "City Council" for regular city council meetings (including "unknown" bodies)
   - "City Council Work Session" for work sessions/workshops
   - "City Council Special Meeting" for special/emergency sessions
   - "Town Council" / "Town Board" / "Board of Commissioners" where appropriate
   - Keep the original if it's already clean

Body names to classify:
{numbered}

Classify each one. If the body name is vague, ambiguous, or just a city name (like "DurhamNC Gov" or "Cityworks"), classify it as council with canonical name "City Council"."""

    gemini = GeminiClient(default_model=GeminiModelType.FLASH)
    result = gemini.generate_structured_content(
        prompt=prompt,
        response_schema=BodyClassificationBatch,
        temperature=0.1,
        thinking_budget=0,
    )

    # Parse into cache format
    cache = {}
    classifications = result if isinstance(result, list) else getattr(result, "classifications", [])
    if isinstance(result, dict):
        classifications = result.get("classifications", [])

    for item in classifications:
        if isinstance(item, dict):
            original = item.get("original", "")
            cache[original.lower().strip()] = {
                "is_council": item.get("is_council", False),
                "canonical_name": item.get("canonical_name", original),
            }
        else:
            cache[item.original.lower().strip()] = {
                "is_council": item.is_council,
                "canonical_name": item.canonical_name,
            }

    return cache


def ensure_body_cache(all_records: list[dict], force_reclassify: bool = False) -> None:
    """Build the body classification cache. Uses LLM only for uncached names."""
    global _body_cache

    # Load existing cache
    if not force_reclassify:
        _body_cache = _load_cache()

    # Find uncached body names
    unique_bodies = set()
    for record in all_records:
        body = record.get("body", "").strip()
        if body and body.lower() not in _body_cache:
            unique_bodies.add(body)

    if not unique_bodies:
        print(f"  Body cache: {len(_body_cache)} cached, 0 new — no LLM call needed")
        return

    # Classify uncached bodies with LLM
    print(f"  Body cache: {len(_body_cache)} cached, {len(unique_bodies)} new — calling Gemini Flash...")
    new_classifications = classify_bodies_with_llm(sorted(unique_bodies))

    # Merge into cache
    _body_cache.update(new_classifications)
    _save_cache(_body_cache)
    print(f"  Classified {len(new_classifications)} body names, cache now has {len(_body_cache)} entries")


def is_council_body(body: str) -> bool:
    """Check if a body name represents a council/legislative body (using LLM cache)."""
    lower = body.lower().strip()
    if not lower:
        return True  # empty body → assume council
    entry = _body_cache.get(lower)
    if entry:
        return entry["is_council"]
    # Fallback for bodies not in cache (shouldn't happen after ensure_body_cache)
    return True


def normalize_body_name(body: str) -> str:
    """Normalize body name to canonical form (using LLM cache)."""
    lower = body.lower().strip()
    if not lower:
        return "City Council"
    entry = _body_cache.get(lower)
    if entry:
        return entry["canonical_name"]
    # Fallback
    return body


def _is_raw_extraction(record: dict) -> bool:
    """Check if a record is in raw MeetingExtraction format (has 'items' instead of 'data')."""
    return "items" in record and "data" not in record and "agendaItems" not in record


def convert_raw_extraction(record: dict, city_slug: str) -> dict:
    """Convert a raw MeetingExtraction record to the standard MeetingData envelope."""
    # Parse city name and state from slug
    parts = city_slug.rsplit("-", 1)
    state = parts[1] if len(parts) == 2 else ""
    city_name = parts[0].replace("-", " ").title() if parts else city_slug

    # Convert items → agendaItems (rename fields to camelCase)
    agenda_items = []
    for item in record.get("items", []):
        ai = {
            "number": item.get("number"),
            "title": item.get("title", ""),
            "section": item.get("section"),
            "topic": item.get("topic", "other"),
            "isPublicHearing": item.get("is_public_hearing", False),
        }
        if item.get("description"):
            ai["description"] = item["description"]
        if item.get("fiscal_amounts"):
            ai["fiscalAmounts"] = item["fiscal_amounts"]
        if item.get("staff_recommendation"):
            ai["staffRecommendation"] = item["staff_recommendation"]
        if item.get("presenter"):
            ai["presenter"] = item["presenter"]
        agenda_items.append(ai)

    # Build summary
    public_hearings = sum(1 for i in agenda_items if i.get("isPublicHearing"))
    consent_items = sum(1 for i in agenda_items if i.get("section") == "consent")
    action_items = sum(1 for i in agenda_items if i.get("section") == "action")

    # Determine status
    date = record.get("date", "")
    status = "UPCOMING"
    if date and date != "unknown":
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            status = "UPCOMING" if dt.date() > datetime.now().date() else "COMPLETED"
        except ValueError:
            pass

    return {
        "citySlug": city_slug,
        "cityName": city_name,
        "state": state,
        "date": date,
        "time": record.get("time"),
        "body": record.get("body", "City Council"),
        "title": f"{record.get('body', 'City Council')} Meeting",
        "status": status,
        "sourceType": record.get("sourceType", "html_llm"),
        "sourceUrl": record.get("sourceUrl"),
        "data": {
            "version": "1.0",
            "agendaItems": agenda_items,
            "summary": {
                "totalItems": len(agenda_items),
                "publicHearings": public_hearings,
                "consentItems": consent_items,
                "actionItems": action_items,
                "totalFiscalImpact": None,
                "topTopics": [],
            },
            "source": {
                "type": "html_llm",
                "collectedAt": datetime.now().isoformat(),
            },
        },
    }


def validate_meeting(record: dict, city_slug: str) -> list[str]:
    """Validate a single meeting record. Returns list of issues (empty = valid)."""
    issues = []

    # Check required top-level fields
    for field in REQUIRED_FIELDS:
        if field not in record:
            issues.append(f"Missing field: {field}")

    # Validate date format
    date = record.get("date", "")
    if date and date != "unknown":
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            issues.append(f"Invalid date format: {date}")

    # Validate status
    status = record.get("status", "")
    if status not in ("UPCOMING", "COMPLETED", "CANCELLED", ""):
        issues.append(f"Invalid status: {status}")

    # Validate data structure
    data = record.get("data", {})
    if isinstance(data, dict):
        for field in REQUIRED_DATA_FIELDS:
            if field not in data:
                issues.append(f"Missing data.{field}")

        items = data.get("agendaItems", [])
        if not isinstance(items, list):
            issues.append("data.agendaItems is not a list")
        elif len(items) == 0:
            issues.append("data.agendaItems is empty")

        summary = data.get("summary", {})
        if isinstance(summary, dict):
            for field in REQUIRED_SUMMARY_FIELDS:
                if field not in summary:
                    issues.append(f"Missing data.summary.{field}")
    else:
        issues.append("data is not a dict")

    return issues


def normalize_record(record: dict, city_slug: str) -> dict:
    """Normalize a meeting record — fill defaults, fix types."""
    # Ensure citySlug matches directory
    record["citySlug"] = city_slug

    # Default status based on date
    if not record.get("status"):
        date = record.get("date", "")
        if date and date != "unknown":
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                record["status"] = "UPCOMING" if dt.date() > datetime.now().date() else "COMPLETED"
            except ValueError:
                record["status"] = "UPCOMING"
        else:
            record["status"] = "UPCOMING"

    # Default body
    if not record.get("body"):
        record["body"] = "City Council"

    # Normalize body name — clean up ALL CAPS and long prefixes
    body = record["body"]
    # Title-case if ALL CAPS (but preserve short acronyms)
    if body == body.upper() and len(body) > 5:
        body = body.title()
    # Strip common city name prefixes from body
    city_name = record.get("cityName", "")
    prefixes_to_strip = [
        f"City Of {city_name} ",
        f"City of {city_name} ",
        f"{city_name} ",
    ]
    for prefix in prefixes_to_strip:
        if body.startswith(prefix) and len(body) > len(prefix) + 3:
            body = body[len(prefix):]
            break
    # Apply canonical body name normalization
    body = normalize_body_name(body.strip())
    record["body"] = body

    # Default title from body
    if not record.get("title"):
        record["title"] = f"{record['body']} Meeting"

    # Ensure data.version
    data = record.get("data", {})
    if "version" not in data:
        data["version"] = "1.0"

    # Ensure summary exists with at least totalItems
    if "summary" not in data or not isinstance(data.get("summary"), dict):
        items = data.get("agendaItems", [])
        data["summary"] = {
            "totalItems": len(items),
            "publicHearings": sum(1 for i in items if i.get("isPublicHearing")),
            "consentItems": sum(1 for i in items if i.get("section") == "consent"),
            "actionItems": sum(1 for i in items if i.get("section") == "action"),
            "totalFiscalImpact": None,
            "topTopics": [],
        }

    # Ensure source exists
    if "source" not in data or not isinstance(data.get("source"), dict):
        data["source"] = {
            "type": record.get("sourceType", "unknown"),
            "collectedAt": datetime.now().isoformat(),
        }

    record["data"] = data
    return record


def load_city_meetings(city_dir: Path) -> tuple[list[dict], str]:
    """Load meetings from a city directory. Returns (records, source_file)."""
    # Priority: meetings.json > meetings_extracted.json > data/meetings.json
    candidates = [
        city_dir / "meetings.json",
        city_dir / "meetings_extracted.json",
        city_dir / "data" / "meetings.json",
    ]

    for path in candidates:
        if path.exists() and path.stat().st_size > 50:
            try:
                with open(path) as f:
                    data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    return data, path.name
            except (json.JSONDecodeError, KeyError):
                continue

    return [], ""


def process_city(city_dir: Path, dry_run: bool = False) -> dict:
    """Process a single city directory. Returns report."""
    slug = city_dir.name
    records, source_file = load_city_meetings(city_dir)

    if not records:
        return {
            "city": slug,
            "status": "no_data",
            "source_file": None,
            "records": 0,
            "issues": [],
        }

    # Convert raw MeetingExtraction records if needed
    converted = []
    for record in records:
        if _is_raw_extraction(record):
            record = convert_raw_extraction(record, slug)
        converted.append(record)
    records = converted

    # Filter out non-council bodies
    before_filter = len(records)
    records = [r for r in records if is_council_body(r.get("body", "City Council"))]
    filtered_count = before_filter - len(records)

    # Validate and normalize
    all_issues = []
    if filtered_count > 0:
        all_issues.append(f"Filtered {filtered_count} non-council records")
    normalized = []
    for i, record in enumerate(records):
        issues = validate_meeting(record, slug)
        if issues:
            all_issues.extend([f"record[{i}]: {issue}" for issue in issues])

        record = normalize_record(record, slug)
        normalized.append(record)

    # Sort by date (most recent first)
    def sort_key(r):
        d = r.get("date", "")
        return d if d and d != "unknown" else "0000-00-00"
    normalized.sort(key=sort_key, reverse=True)

    # Write unified meetings.json
    output_path = city_dir / "meetings.json"
    if not dry_run:
        with open(output_path, "w") as f:
            json.dump(normalized, f, indent=2)

    return {
        "city": slug,
        "status": "ok" if not all_issues else "warnings",
        "source_file": source_file,
        "records": len(normalized),
        "dates": [r["date"] for r in normalized[:3]],
        "body": normalized[0].get("body", "?") if normalized else "?",
        "issues": all_issues[:5],  # cap issues
    }


def main():
    parser = argparse.ArgumentParser(description="Normalize all meeting data")
    parser.add_argument("--city", help="Process single city (e.g. apex-NC)")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, don't write files")
    parser.add_argument("--reclassify", action="store_true", help="Force fresh LLM classification of all body names")
    args = parser.parse_args()

    if args.city:
        city_dir = SOURCES_DIR / args.city
        if not city_dir.exists():
            print(f"City directory not found: {city_dir}")
            sys.exit(1)
        cities = [city_dir]
    else:
        cities = sorted(
            [d for d in SOURCES_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")],
            key=lambda d: d.name,
        )

    # --- Phase 1: Load all records to discover unique body names ---
    print("Loading records from all cities...")
    all_raw_records = []
    for city_dir in cities:
        records, _ = load_city_meetings(city_dir)
        # Convert raw extractions so we can see their body names
        for r in records:
            if _is_raw_extraction(r):
                r = convert_raw_extraction(r, city_dir.name)
            all_raw_records.append(r)
    print(f"  Loaded {len(all_raw_records)} records from {len(cities)} city directories")

    # --- Phase 2: Classify body names (LLM call if needed) ---
    ensure_body_cache(all_raw_records, force_reclassify=args.reclassify)

    # --- Phase 3: Process each city ---
    reports = []
    all_meetings = []
    total_records = 0
    ok_count = 0
    warn_count = 0
    no_data_count = 0

    for city_dir in cities:
        report = process_city(city_dir, dry_run=args.dry_run)
        reports.append(report)

        if report["status"] == "ok":
            ok_count += 1
        elif report["status"] == "warnings":
            warn_count += 1
        else:
            no_data_count += 1

        total_records += report["records"]

        # Load normalized records for master file
        if not args.dry_run and report["records"] > 0:
            meetings_path = city_dir / "meetings.json"
            if meetings_path.exists():
                with open(meetings_path) as f:
                    all_meetings.extend(json.load(f))

    # Print summary
    print(f"\n{'=' * 60}")
    print("NORMALIZATION REPORT")
    print(f"{'=' * 60}")
    print(f"  Cities processed: {len(reports)}")
    print(f"  OK:        {ok_count}")
    print(f"  Warnings:  {warn_count}")
    print(f"  No data:   {no_data_count}")
    print(f"  Total meeting records: {total_records}")

    # Print per-city details
    print(f"\n{'─' * 60}")
    for r in reports:
        status_icon = {"ok": "OK", "warnings": "!!", "no_data": "--"}[r["status"]]
        dates = ", ".join(r.get("dates", [])[:2]) if r.get("dates") else ""
        body = r.get("body", "")
        source = r.get("source_file", "")
        print(
            f"  [{status_icon}] {r['city']:28s} {r['records']:3d} records  "
            f"{body:20s} {dates:24s} ({source})"
        )
        if r["issues"]:
            for issue in r["issues"][:2]:
                print(f"       WARN: {issue}")

    # Write master file
    if not args.dry_run and all_meetings:
        # Sort all meetings by date descending
        all_meetings.sort(
            key=lambda r: r.get("date", "0000-00-00") if r.get("date") != "unknown" else "0000-00-00",
            reverse=True,
        )
        master_path = SOURCES_DIR / "all_meetings.json"
        with open(master_path, "w") as f:
            json.dump(all_meetings, f, indent=2)
        print(f"\nMaster file: {master_path} ({len(all_meetings)} records)")

    # Write report
    if not args.dry_run:
        report_path = SOURCES_DIR / "normalization_report.json"
        with open(report_path, "w") as f:
            json.dump({
                "generated_at": datetime.now().isoformat(),
                "total_cities": len(reports),
                "total_records": total_records,
                "ok": ok_count,
                "warnings": warn_count,
                "no_data": no_data_count,
                "cities": reports,
            }, f, indent=2)
        print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
