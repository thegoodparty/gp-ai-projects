"""
manifest.py — Manifest reader and validator for the city collection pipeline.

The manifest is a static spec written once per city that describes what we
expect the data source to be about. Validation is best-effort: if the manifest
doesn't exist or can't be loaded, collection proceeds unblocked.
"""

from meeting_pipeline.shared.body_validation import GOVERNING_KEYWORDS
from meeting_pipeline.shared.storage import StorageBackend

# Keywords that indicate a wrong-entity match (school district, not city council)
WRONG_ENTITY_KEYWORDS = [
    "school district",
    "board of education",
    "school board",
    "isd",
    "unified school",
    "elementary",
    "high school",
    "superintendent",
]


def _is_wrong_entity(name: str) -> bool:
    """Return True if the name contains a wrong-entity keyword (school district, etc.)."""
    return any(kw in name for kw in WRONG_ENTITY_KEYWORDS)


def load_manifest(city_slug: str, storage: StorageBackend, sources_prefix: str) -> dict | None:
    """
    Load manifest.json for a city from S3.

    Returns the manifest dict, or None if not found or unreadable.
    """
    key = f"{sources_prefix}/{city_slug}/manifest.json"
    try:
        if not storage.exists(key):
            return None
        return storage.read_json(key)
    except Exception:
        return None


def validate_against_manifest(
    manifest: dict,
    collected_body_names: list[str],
    collected_city: str | None = None,
) -> tuple[bool, str | None]:
    """
    Check collected data against the manifest expectations.

    Returns (is_valid, reason_if_invalid).

    Checks:
    1. Body name: at least one collected_body_names fuzzy-matches expected_body
       - Case-insensitive substring matching
       - "City Council" matches "Regular City Council Meeting", etc.
       - If expected_body is "City Council", REJECT if all names contain school/district/education keywords
    2. City name: if collected_city provided, check it matches expected_city (loose match)
    """
    expected_body = manifest.get("expected_body", "")
    expected_city = manifest.get("expected_city", "")

    # ── Body name validation ──────────────────────────────────────────────────

    if expected_body and collected_body_names:
        expected_lower = expected_body.lower()
        names_lower = [n.lower() for n in collected_body_names]

        # If ALL collected names are wrong-entity, reject
        if all(_is_wrong_entity(n) for n in names_lower):
            offenders = [n for n in names_lower if _is_wrong_entity(n)]
            return False, (
                f"All collected bodies appear to be wrong entity (school/district): "
                f"{offenders[:3]}"
            )

        # At least one name must fuzzy-match the expected body
        def matches_expected(name: str) -> bool:
            # Direct substring match
            if expected_lower in name:
                return True
            # "Mayor" also matches "Mayoral", "Mayor's", etc.
            if expected_lower == "mayor" and "mayor" in name:
                return True
            # Governing body synonym: if expected was "City Council" but collected name
            # is another recognized governing body (e.g. "Board of Mayor and Aldermen"),
            # accept it — don't reject valid governments for not using the default name.
            return any(kw in expected_lower for kw in GOVERNING_KEYWORDS) and any(kw in name for kw in GOVERNING_KEYWORDS)

        if not any(matches_expected(n) for n in names_lower):
            return False, (
                f"No collected body matches expected '{expected_body}'. "
                f"Got: {collected_body_names[:5]}"
            )

    # ── City name validation (optional, loose) ────────────────────────────────

    if collected_city and expected_city and expected_city.lower() not in collected_city.lower() and collected_city.lower() not in expected_city.lower():
        return False, (
            f"Collected city '{collected_city}' does not match expected '{expected_city}'"
        )

    return True, None
