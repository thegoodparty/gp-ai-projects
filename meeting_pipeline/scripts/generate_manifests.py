"""
generate_manifests.py — Write one manifest JSON per city to S3.

Reads serve_users.csv and for each unique city writes a static spec manifest
to S3 at meeting_pipeline/sources/{city-slug}/manifest.json.

The manifest describes what we EXPECT this city's data source to be about.
It is written once and never auto-updated (use --force to overwrite).

Usage:
    uv run python meeting_pipeline/scripts/generate_manifests.py           # write new manifests
    uv run python meeting_pipeline/scripts/generate_manifests.py --dry-run # preview only
    uv run python meeting_pipeline/scripts/generate_manifests.py --force   # overwrite existing
    uv run python meeting_pipeline/scripts/generate_manifests.py --city "Loveland"  # single city
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Bootstrap path so we can import meeting_pipeline as a package ──────────────
_ROOT = Path(__file__).resolve().parent.parent.parent

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig, get_storage

# ── State abbreviation table (from source_discover.py) ────────────────────────
STATE_ABBREVS = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
    # Common abbreviations that may already appear in the CSV
    "AL": "AL", "AK": "AK", "AZ": "AZ", "AR": "AR", "CA": "CA", "CO": "CO",
    "CT": "CT", "DE": "DE", "FL": "FL", "GA": "GA", "HI": "HI", "ID": "ID",
    "IL": "IL", "IN": "IN", "IA": "IA", "KS": "KS", "KY": "KY", "LA": "LA",
    "ME": "ME", "MD": "MD", "MA": "MA", "MI": "MI", "MN": "MN", "MS": "MS",
    "MO": "MO", "MT": "MT", "NE": "NE", "NV": "NV", "NH": "NH", "NJ": "NJ",
    "NM": "NM", "NY": "NY", "NC": "NC", "ND": "ND", "OH": "OH", "OK": "OK",
    "OR": "OR", "PA": "PA", "RI": "RI", "SC": "SC", "SD": "SD", "TN": "TN",
    "TX": "TX", "UT": "UT", "VT": "VT", "VA": "VA", "WA": "WA", "WV": "WV",
    "WI": "WI", "WY": "WY",
}

_PIPELINE_DIR = Path(__file__).resolve().parent.parent
SERVE_CSV = _PIPELINE_DIR / "serve_users_unified.csv"
if not SERVE_CSV.exists():
    SERVE_CSV = _PIPELINE_DIR / "serve_users.csv"

# Overridden by --csv flag at runtime
_csv_override: Path | None = None


def parse_expected_body(candidate_office: str) -> str:
    """
    Derive the expected governing body from the Candidate Office column.

    The goal is to name the meeting BODY we want to scan on the platform —
    not the candidate's personal role. Body validation will self-heal the
    manifest if the platform uses a different name.

    Examples:
        "Fultondale City Council - Place 1"   → "City Council"
        "Pleasant Grove City Mayor"            → "City Council"  (mayor chairs council)
        "Selma City Council - District 7"      → "City Council"
        "City Council"                         → "City Council"
        "Town Council"                         → "Town Council"
        "Village Board"                        → "Village Board of Trustees"
        "Trustee"                              → "Village Board of Trustees"
        "Board of Aldermen"                    → "Board of Aldermen"
        "Common Council"                       → "Common Council"
        "Chino Valley Town Council"            → "Town Council"
        "Town Meeting Member"                  → "Town Meeting"
        "Selectman"                            → "Select Board"
        "Alderman"                             → "Board of Aldermen"
    """
    office = candidate_office.strip()
    office_lower = office.lower()

    # Town Meeting Member (before "town" check so it doesn't fall through)
    if "town meeting" in office_lower:
        return "Town Meeting"

    # Select Board / Selectman (New England towns)
    if "select board" in office_lower or "selectman" in office_lower or "selectmen" in office_lower:
        return "Select Board"

    # Alderman / Alderperson → Board of Aldermen
    if "alderman" in office_lower or "alderperson" in office_lower or "aldermen" in office_lower:
        return "Board of Aldermen"

    # Trustee → Village Board of Trustees
    if "trustee" in office_lower:
        return "Village Board of Trustees"

    # Mayor: the Mayor leads council meetings; map to City Council so body
    # validation can find the right meeting body on the platform. If the
    # platform uses a different name (e.g. "Town Council"), body validation
    # will self-heal the manifest automatically.
    if "mayor" in office_lower:
        return "City Council"

    # Named council types — extract just the council type
    for council_type in ("common council", "town council", "city council"):
        if council_type in office_lower:
            return council_type.title()

    # Generic "council" — extract up to and including "Council"
    if "council" in office_lower:
        words = office.split()
        for i, w in enumerate(words):
            if w.lower().rstrip(".,") == "council":
                if i > 0 and words[i - 1].lower() in ("city", "town", "village", "common"):
                    return f"{words[i-1].title()} Council"
                return "City Council"
        return "City Council"

    # Board types — exclude school board
    if "board" in office_lower and "school board" not in office_lower:
        words = office.split()
        for i, w in enumerate(words):
            if w.lower() == "board":
                if i > 0:
                    return f"{words[i-1].title()} Board"
                rest = " ".join(words[i:])
                rest = rest.split(" - ")[0].split(",")[0].strip()
                return rest
        return "City Council"

    # Fallback
    return "City Council"


def city_to_slug(city: str) -> str:
    """Convert city name to lowercase hyphenated slug (no state suffix)."""
    return city.lower().replace(" ", "-").replace(".", "").replace("'", "")


def load_cities_from_csv(filter_city: str | None = None) -> list[dict]:
    """
    Read serve_users.csv and return one record per unique city.

    Returns list of dicts with keys: city, state, city_slug, expected_body.
    """
    seen: dict[str, dict] = {}  # city_slug → record

    csv_path = _csv_override if _csv_override else SERVE_CSV
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Support unified CSV (lowercase) and legacy formats
            city = (row.get("city") or row.get("City") or "").strip()
            state_raw = (row.get("state") or row.get("State") or row.get("State/Region") or "").strip()
            candidate_office = (row.get("office") or row.get("Office") or row.get("Candidate Office") or "").strip()

            if not city or not state_raw:
                continue

            # Normalize state to 2-letter abbreviation
            state = STATE_ABBREVS.get(state_raw, state_raw.upper()[:2] if len(state_raw) > 2 else state_raw.upper())

            if filter_city and city.lower() != filter_city.lower():
                continue

            slug = city_to_slug(city)
            city_state_key = f"{slug}-{state}"

            if city_state_key not in seen:
                seen[city_state_key] = {
                    "city": city,
                    "state": state,
                    "city_slug": city_state_key,
                    "expected_body": parse_expected_body(candidate_office),
                }

    return list(seen.values())


def build_manifest(record: dict) -> dict:
    """Build the manifest dict for a city record."""
    return {
        "city_slug": record["city_slug"],
        "expected_city": record["city"],
        "expected_state": record["state"],
        "expected_body": record["expected_body"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate city manifests and write to S3")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to S3")
    parser.add_argument("--force", action="store_true", help="Overwrite existing manifests")
    parser.add_argument("--city", help="Only process this city (exact name)")
    parser.add_argument("--csv", dest="csv_path", help="Alternate CSV file (must have City, State, Office columns)")
    args = parser.parse_args()

    global _csv_override
    if args.csv_path:
        _csv_override = Path(args.csv_path)

    # Set up storage
    cfg = AgentConfig.from_env()
    if not args.dry_run:
        storage = get_storage(cfg)
    else:
        storage = None

    cities = load_cities_from_csv(filter_city=args.city)
    if not cities:
        print(f"No cities found{f' matching --city {args.city!r}' if args.city else ''}.")
        return

    print(f"Found {len(cities)} unique cities in serve_users.csv")
    if args.dry_run:
        print("[DRY RUN] No writes will be made.\n")

    written = 0
    skipped = 0
    errors = 0

    for record in cities:
        city_slug = record["city_slug"]
        manifest_key = f"{cfg.sources_prefix}/{city_slug}/manifest.json"
        manifest = build_manifest(record)

        if args.dry_run:
            print(
                f"  WOULD WRITE  {manifest_key}\n"
                f"               city={manifest['expected_city']!r}  "
                f"state={manifest['expected_state']}  "
                f"body={manifest['expected_body']!r}"
            )
            written += 1
            continue

        # Check if already exists
        if not args.force and storage.exists(manifest_key):
            print(f"  SKIP  {city_slug}  (already exists; use --force to overwrite)")
            skipped += 1
            continue

        try:
            storage.write_json(manifest_key, manifest)
            action = "OVERWROTE" if args.force else "WROTE"
            print(f"  {action}  {manifest_key}  body={manifest['expected_body']!r}")
            written += 1
        except Exception as e:
            print(f"  ERROR  {city_slug}: {e}")
            errors += 1

    print(
        f"\nSummary: {written} {'would write' if args.dry_run else 'written'}  |  "
        f"{skipped} skipped  |  {errors} errors"
    )


if __name__ == "__main__":
    main()
