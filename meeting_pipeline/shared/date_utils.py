"""
date_utils.py — Date extraction and classification utilities.

Used by discovery (freshness verification), scan (meeting date parsing),
and the generic agenda scanner (PDF filename parsing).
"""

import contextlib
import re
from datetime import date, timedelta

from meeting_pipeline.shared.constants import FRESH_THRESHOLD, STALE_WARNING_THRESHOLD

# ── Freshness Classification ──────────────────────────────────────────────────

def classify_freshness(most_recent: date | None, today: date | None = None) -> str:
    """Classify how fresh a source is based on its most recent date."""
    if most_recent is None:
        return "unknown"
    if today is None:
        today = date.today()
    days = (today - most_recent).days
    if days <= FRESH_THRESHOLD:
        return "fresh"
    if days <= STALE_WARNING_THRESHOLD:
        return "stale_warning"
    return "stale"


# ── PDF Filename Date Parsing ─────────────────────────────────────────────────

_FILENAME_DATE_PATTERNS = [
    (r'(\d{2})-(\d{2})-(\d{4})', "mdy"),      # MM-DD-YYYY
    (r'(\d{4})-(\d{2})-(\d{2})', "ymd"),      # YYYY-MM-DD
    (r'(\d{1,2})\.(\d{1,2})\.(\d{4})', "mdy"),  # M.D.YYYY
    (r'(\d{2})-(\d{2})-(\d{2})(?!\d)', "mdy2"),  # MM-DD-YY (2-digit year)
]


def parse_date_from_filename(filename: str, today: date | None = None) -> date | None:
    """Extract a meeting date from a PDF filename. Returns None if no date found."""
    if today is None:
        today = date.today()
    valid_range = (date(2020, 1, 1), today + timedelta(days=500))
    for pattern, fmt in _FILENAME_DATE_PATTERNS:
        m = re.search(pattern, filename)
        if not m:
            continue
        try:
            g = m.groups()
            if fmt == "ymd":
                d = date(int(g[0]), int(g[1]), int(g[2]))
            elif fmt == "mdy2":
                d = date(2000 + int(g[2]), int(g[0]), int(g[1]))
            else:
                d = date(int(g[2]), int(g[0]), int(g[1]))
            if valid_range[0] <= d <= valid_range[1]:
                return d
        except (ValueError, TypeError):
            continue
    return None


# ── Full-Text Date Extraction ─────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}
_MONTHS_RE = "|".join(sorted(_MONTH_MAP.keys(), key=len, reverse=True))


def extract_dates(text: str, today: date | None = None) -> list[date]:
    """
    Extract recognizable dates from text, return sorted descending.

    Supports: MM/DD/YYYY, MM/DD/YY, M-D-YY, YYYY-MM-DD, Month DD YYYY,
    and year-context "Month Day" patterns (where year is a section header).
    Caps input at 150KB and filters to valid date range.
    """
    if today is None:
        today = date.today()

    text = text[:150_000]
    found: set[date] = set()
    valid_range = (date(2020, 1, 1), today + timedelta(days=500))

    def _add(d: date):
        if valid_range[0] <= d <= valid_range[1]:
            found.add(d)

    # MM/DD/YYYY
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        with contextlib.suppress(ValueError):
            _add(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))

    # MM/DD/YY (2-digit year)
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b", text):
        yy = int(m.group(3))
        full_year = 2000 + yy if yy < 50 else 1900 + yy
        with contextlib.suppress(ValueError):
            _add(date(full_year, int(m.group(1)), int(m.group(2))))

    # M-D-YY (dash separator, 2-digit year)
    for m in re.finditer(r"\b(\d{1,2})-(\d{1,2})-(\d{2})\b", text):
        yy = int(m.group(3))
        full_year = 2000 + yy if yy < 50 else 1900 + yy
        with contextlib.suppress(ValueError):
            _add(date(full_year, int(m.group(1)), int(m.group(2))))

    # YYYY-MM-DD
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        with contextlib.suppress(ValueError):
            _add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))

    # Month DD, YYYY (long and abbreviated)
    for m in re.finditer(
        rf"\b({_MONTHS_RE})\w*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", text, re.IGNORECASE
    ):
        key = m.group(1).lower().rstrip(".")
        month_num = _MONTH_MAP.get(key[:3])
        if month_num:
            with contextlib.suppress(ValueError):
                _add(date(int(m.group(3)), month_num, int(m.group(2))))

    # Year-context "Month Day" (no adjacent year)
    # Handles tables where year is a section header and rows show "Month Day" only.
    no_year_re = re.compile(
        rf"\b({_MONTHS_RE})\w*\.?\s+(\d{{1,2}})\b(?!\s*,?\s*\d{{4}})",
        re.IGNORECASE,
    )
    for m in no_year_re.finditer(text):
        window = text[max(0, m.start() - 3000):m.start()]
        prior_years = re.findall(r"\b(20\d{2})\b", window)
        if not prior_years:
            continue
        inferred_year = int(prior_years[-1])
        key = m.group(1).lower().rstrip(".")
        month_num = _MONTH_MAP.get(key[:3])
        if not month_num:
            continue
        with contextlib.suppress(ValueError):
            _add(date(inferred_year, month_num, int(m.group(2))))

    return sorted(found, reverse=True)


def normalize_table_dates(text: str) -> str:
    """
    Pre-process text for year-header agenda tables.

    When a standalone year ("2026") precedes "Month Day" lines without a year,
    injects "Month Day, Year" strings so extract_dates() can parse them.

    Example: "2026\\n December 8\\t Agenda" → appends "December 8, 2026"
    """
    month_names = (
        "January|February|March|April|May|June|July|August|September|"
        "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    )
    lines = text.split("\n")
    injected: list[str] = []
    current_year: str | None = None
    year_re = re.compile(r"^\s*(20\d{2})\s*$")
    mday_re = re.compile(
        rf"^\s*(?:\xa0\s*)?({month_names})\.?\s+(\d{{1,2}})\b(?!\s*,?\s*\d{{4}})",
        re.IGNORECASE,
    )

    for line in lines:
        ym = year_re.match(line)
        if ym:
            current_year = ym.group(1)
            continue
        if current_year:
            first_field = line.replace("\xa0", " ").split("\t")[0].strip()
            md = mday_re.match(first_field)
            if md:
                injected.append(f"{md.group(1)} {md.group(2)}, {current_year}")

    if injected:
        return text + "\n" + "\n".join(injected)
    return text
