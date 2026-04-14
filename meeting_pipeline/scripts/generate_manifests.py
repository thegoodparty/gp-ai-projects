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
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage

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
SERVE_CSV = _PIPELINE_DIR / "serve_users.csv"


def parse_expected_body(candidate_office: str) -> str:
    """
    Derive the expected governing body from the Candidate Office column.

    Examples:
        "Fultondale City Council - Place 1"   → "City Council"
        "Pleasant Grove City Mayor"            → "Mayor"
        "Selma City Council - District 7"      → "City Council"
        "City Council"                         → "City Council"
        "Town Council"                         → "Town Council"
        "Village Board"                        → "Village Board"
        "Board of Aldermen"                    → "Board of Aldermen"
        "Common Council"                       → "Common Council"
        "Chino Valley Town Council"            → "Town Council"
        "Mayor"                                → "Mayor"
    """
    office = candidate_office.strip()
    office_lower = office.lower()

    # Mayor check first (most specific)
    if "mayor" in office_lower:
        return "Mayor"

    # Named council types — extract just the council type
    for council_type in ("common council", "town council", "city council"):
        if council_type in office_lower:
            # Title-case the match
            return council_type.title()

    # Generic "council" — extract up to and including "Council"
    if "council" in office_lower:
        # Find position of "council" and extract everything up to that word
        words = office.split()
        for i, w in enumerate(words):
            if w.lower().rstrip(".,") == "council":
                # Include the word before "council" if it looks like a qualifier
                if i > 0 and words[i - 1].lower() in ("city", "town", "village", "common"):
                    return f"{words[i-1].title()} Council"
                return "City Council"
        return "City Council"

    # Board types — exclude school board
    if "board" in office_lower and "school board" not in office_lower:
        # Try to extract the board name
        # "Village Board" → "Village Board"
        # "Board of Aldermen" → "Board of Aldermen"
        words = office.split()
        for i, w in enumerate(words):
            if w.lower() == "board":
                # "Village Board": take word before + Board
                if i > 0:
                    return f"{words[i-1].title()} Board"
                # "Board of Aldermen": take Board + rest up to dash
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

    with open(SERVE_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            city = row.get("City", "").strip()
            state_raw = row.get("State/Region", "").strip()
            candidate_office = row.get("Candidate Office", "").strip()

            if not city or not state_raw:
                continue

            # Normalize state to 2-letter abbreviation
            state = STATE_ABBREVS.get(state_raw, state_raw.upper()[:2])

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
    args = parser.parse_args()

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
