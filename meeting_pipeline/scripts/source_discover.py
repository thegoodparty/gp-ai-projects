"""
source_discover.py — Source-discover skill for all 67 pilot cities.

Finds the freshest, most active agenda source for each city and outputs a
structured JSON record the briefing-collect skill can consume.

Usage:
    uv run python meeting_pipeline/scripts/source_discover.py                       # all cities
    uv run python meeting_pipeline/scripts/source_discover.py --city "Chapel Hill"  # single city
    uv run python meeting_pipeline/scripts/source_discover.py --state NC            # one state
    uv run python meeting_pipeline/scripts/source_discover.py --resume              # skip existing

Output:
    meeting_pipeline/sources/{city-slug}-{state}/source.json   per-city records
    meeting_pipeline/sources/discovery-summary.json            batch summary

Algorithm (3-phase with retry loop):
  Phase 1 — Discover candidates (known sources registry + Tavily + URL probing)
  Phase 2 — Verify freshness by platform
  Phase 3 — Rank and select best source
  Retry loop — up to 2 retries with escalating strategies
  Phase 4 — Deep platform API probes (CivicClerk REST, BoardDocs POST, eSCRIBE,
             CivicPlus year-filter) — only runs if still no fresh source after retries
  Phase 5 — Playwright browser rendering — last resort for JS SPAs (CivicClerk,
             PrimeGov) and bot-blocked pages where httpx cannot extract dates.
             Requires: pip install playwright && playwright install chromium
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# ── Paths ──────────────────────────────────────────────────────────────────────
SOURCES_DIR = Path(__file__).resolve().parent.parent / "sources"
REGISTRY_S3_KEY = "meeting_pipeline/config/known-sources-registry.json"

# ── Constants ──────────────────────────────────────────────────────────────────
TODAY = date.today()

PLATFORM_PATTERNS = {
    "legistar": ["legistar.com"],
    "civicplus": ["/AgendaCenter", "/agendacenter"],
    "civicclerk": ["civicclerk.com", "portal.civicclerk"],
    "boarddocs": ["boarddocs.com"],
    "granicus": ["granicus.com", "swagit.com"],
    "municode": ["municode.com", "municodemeetings.com"],
    "primegov": ["primegov.com"],
    "novus": ["novusagenda.com"],
    "diligent": ["diligentoneplatform.com"],
    "escribe": ["escribemeetings.com"],
}

COLLECTION_METHODS = {
    "legistar": "rest_api",
    "civicplus": "html_scrape_pdf",
    "escribe": "post_api_html",
    "boarddocs": "post_api_html",
    "granicus": "html_scrape_pdf",
    "municode": "html_scrape_pdf",
    "civicclerk": "spa_render",
    "primegov": "spa_render",
    "novus": "html_scrape_pdf",
    "diligent": "html_scrape",
    "unknown": "fetch_and_parse",
}

# Freshness thresholds (days)
FRESH_THRESHOLD = 90
STALE_WARNING_THRESHOLD = 365

# Known wrong-city URL fragments for ambiguous city names
WRONG_CITY_PATTERNS: dict[str, list[str]] = {
    "El Paso": ["elpasoil", "el-paso-il", "el paso, il", "el paso illinois"],
    "Burlington": ["burlington.ca", "burlington.on", "burlington ontario", "burlington, vt", "burlington vermont"],
    "Medina": ["cityofmedinatn", "medina, tn", "medina, tennessee", "medinatn",
               "planning-zoning", "planning_zoning", "boards-and-commission"],  # P&Z board ≠ city council
    "Loveland": ["lovelandco", "cilovelandco", "loveland, co", "loveland colorado", "loveland co.gov"],
    "Belton": ["belton.org", "belton, mo", "belton missouri", "belton, sc", "beltonedc.org"],
    "Hamilton": ["hamilton.ca", "hamilton, ontario", "hamilton ontario"],
    "Lancaster": ["lancaster, pa", "lancaster pa", "lancasterpa", "lancaster, ca", "lancaster california"],
    "Delaware": ["state.de.us", ".de.gov", "delaware state", "regionalplanning.co.delaware",
                 "co.delaware.oh.us", "delaware county"],
    "La Porte": ["laportecac.org", "la porte industry", "citizens advisory council"],
    "Euclid": ["euclidlibrary.org"],
    "Jacksonville": ["jaxcityc", "duval county", "jacksonville, fl", "jacksonville florida"],
    "Louisville": ["louisvillecity.org", "louisville, ky", "louisville kentucky", "louisvilleky"],
    "Poland": ["poland spring", "poland, me", "poland maine", "poland, ny", "poland new york"],
    "Lexington": [],  # disambiguated by state — NC vs OH handled separately
    "Pflugerville": [],  # only one Pflugerville TX
    "Sandy Oaks": [],  # only one Sandy Oaks TX
    "Windcrest": [],   # only one Windcrest TX
    # Wrong county/entity patterns
    "Hickory": ["catawbacountync", "catawba county", "hickorynutchamber.org", "lake-lure", "lake lure"],
    "Greenville": ["greenvillecounty.org", "greenville county", "greenville, sc", "greenvillesc"],
    "Westerville": ["westerville.tv", "westerville.k12"],
    # School district boards (ISD) — not city government
    "Duncanville": ["duncanville isd", "boardbook.org/public/organization/858"],
    "Killeen": ["killeen isd", "killeen independent school district", "boardbook.org/public/organization/1051"],
}

# Global wrong-entity patterns — apply to ALL cities regardless of name.
# Any result matching one of these is not a city council agenda source.
WRONG_ENTITY_PATTERNS = [
    # School boards / ISDs — extremely common false positives from Tavily
    " isd public",        # "Duncanville ISD Public View", "Killeen ISD Public"
    "isd board",
    "independent school district",
    "school district board",
    "school board meeting",
    # Other non-city-council entities
    "public library board",
    "library board of trustees",
    "library advisory board",  # e.g. Duncanville Library Advisory Board
]

# Keywords that identify a legislative body (used for Legistar EventBodyName validation)
COUNCIL_BODY_KEYWORDS = [
    "city council", "town council", "board of aldermen", "village council",
    "city commission", "town commission", "board of commissioners",
    "common council", "council at large", "city council workshop",
    "council workshop", "special council",
]

# Keywords that identify a council view on Granicus RSS feed titles
GRANICUS_COUNCIL_KEYWORDS = [
    "city council", "town council", "board of aldermen", "village council",
    "city commission", "common council",
]


def _is_council_body(name: str) -> bool:
    """Return True if EventBodyName looks like a city council / legislative body."""
    lower = name.lower()
    return any(kw in lower for kw in COUNCIL_BODY_KEYWORDS)


# URL patterns that are never official municipal agenda sources
REJECT_URL_PATTERNS = [
    "facebook.com/",
    "youtube.com/watch",
    "citizenportal.ai/",
    "twitter.com/",
    "x.com/",
    "nextdoor.com/",
    "instagram.com/",
    "linkedin.com/",
    # News / journalism sites — not official sources
    "hickoryrecord.com/",
    "documenters.org/",
    "ballotpedia.org/",  # election info, not agendas
    "patch.com/",
    "govtech.com/",
]

PILOT_CITIES = [
    # NC
    {"city": "Apex", "state": "NC"},
    {"city": "Asheville", "state": "NC"},
    {"city": "Burlington", "state": "NC"},
    {"city": "Chapel Hill", "state": "NC"},
    {"city": "Clayton", "state": "NC"},
    {"city": "Concord", "state": "NC"},
    {"city": "Durham", "state": "NC"},
    {"city": "Fayetteville", "state": "NC"},
    {"city": "Gastonia", "state": "NC"},
    {"city": "Gibsonville", "state": "NC"},
    {"city": "Granite Quarry", "state": "NC"},
    {"city": "Greensboro", "state": "NC"},
    {"city": "Greenville", "state": "NC"},
    {"city": "Hickory", "state": "NC"},
    {"city": "Huntersville", "state": "NC"},
    {"city": "Indian Trail", "state": "NC"},
    {"city": "Jacksonville", "state": "NC"},
    {"city": "Lexington", "state": "NC"},
    {"city": "Locust", "state": "NC"},
    {"city": "Marvin", "state": "NC"},
    {"city": "Matthews", "state": "NC"},
    {"city": "Monroe", "state": "NC"},
    {"city": "New Bern", "state": "NC"},
    {"city": "Pembroke", "state": "NC"},
    {"city": "Pittsboro", "state": "NC"},
    {"city": "Rocky Mount", "state": "NC"},
    {"city": "Salisbury", "state": "NC"},
    {"city": "Stallings", "state": "NC"},
    {"city": "Statesville", "state": "NC"},
    # OH
    {"city": "Brecksville", "state": "OH"},
    {"city": "Canal Fulton", "state": "OH"},
    {"city": "Canal Winchester", "state": "OH"},
    {"city": "Centerville", "state": "OH"},
    {"city": "Cleveland", "state": "OH"},
    {"city": "Cuyahoga Falls", "state": "OH"},
    {"city": "Delaware", "state": "OH"},
    {"city": "Dublin", "state": "OH"},
    {"city": "Euclid", "state": "OH"},
    {"city": "Fairborn", "state": "OH"},
    {"city": "Fairfield", "state": "OH"},
    {"city": "Hamilton", "state": "OH"},
    {"city": "Hartville", "state": "OH"},
    {"city": "Hillsboro", "state": "OH"},
    {"city": "Johnstown", "state": "OH"},
    {"city": "Lexington", "state": "OH"},
    {"city": "Lima", "state": "OH"},
    {"city": "Louisville", "state": "OH"},
    {"city": "Loveland", "state": "OH"},
    {"city": "Maple Heights", "state": "OH"},
    {"city": "Marysville", "state": "OH"},
    {"city": "Mason", "state": "OH"},
    {"city": "Medina", "state": "OH"},
    {"city": "Mount Sterling", "state": "OH"},
    {"city": "North Canton", "state": "OH"},
    {"city": "Parma", "state": "OH"},
    {"city": "Perrysburg", "state": "OH"},
    {"city": "Poland", "state": "OH"},
    {"city": "Powell", "state": "OH"},
    {"city": "Stow", "state": "OH"},
    {"city": "Troy", "state": "OH"},
    {"city": "Vermilion", "state": "OH"},
    {"city": "Walbridge", "state": "OH"},
    {"city": "Walton Hills", "state": "OH"},
    {"city": "Warren", "state": "OH"},
    {"city": "Westerville", "state": "OH"},
    # TX
    {"city": "Austin", "state": "TX"},
    {"city": "Beaumont", "state": "TX"},
    {"city": "Belton", "state": "TX"},
    {"city": "Cibolo", "state": "TX"},
    {"city": "Cleburne", "state": "TX"},
    {"city": "Coleman", "state": "TX"},
    {"city": "Dallas", "state": "TX"},
    {"city": "Dickinson", "state": "TX"},
    {"city": "Duncanville", "state": "TX"},
    {"city": "El Paso", "state": "TX"},
    {"city": "Farmers Branch", "state": "TX"},
    {"city": "Grand Prairie", "state": "TX"},
    {"city": "Killeen", "state": "TX"},
    {"city": "Kyle", "state": "TX"},
    {"city": "La Porte", "state": "TX"},
    {"city": "Lago Vista", "state": "TX"},
    {"city": "Lancaster", "state": "TX"},
    {"city": "Longview", "state": "TX"},
    {"city": "Lufkin", "state": "TX"},
    {"city": "Midland", "state": "TX"},
    {"city": "Mount Vernon", "state": "TX"},
    {"city": "New Braunfels", "state": "TX"},
    {"city": "Palestine", "state": "TX"},
    {"city": "Pflugerville", "state": "TX"},
    {"city": "Refugio", "state": "TX"},
    {"city": "Sandy Oaks", "state": "TX"},
    {"city": "Sherman", "state": "TX"},
    {"city": "Temple", "state": "TX"},
    {"city": "Texarkana", "state": "TX"},
    {"city": "Tomball", "state": "TX"},
    {"city": "Windcrest", "state": "TX"},
    # NC additions
    {"city": "Elm City", "state": "NC"},
    # OH townships
    {"city": "Clearcreek Township", "state": "OH"},
    {"city": "Etna Township", "state": "OH"},
    {"city": "Rootstown Township", "state": "OH"},
    {"city": "Chardon Township", "state": "OH"},
    {"city": "Beavercreek Township", "state": "OH"},
]


# ── Utilities ──────────────────────────────────────────────────────────────────

def city_to_slug(city: str) -> str:
    return city.lower().replace(" ", "-").replace(".", "").replace("'", "")


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pat in patterns:
            if pat.lower() in url_lower:
                return platform
    return "unknown"


def classify_freshness(most_recent: Optional[date]) -> str:
    if most_recent is None:
        return "unknown"
    days = (TODAY - most_recent).days
    if days <= FRESH_THRESHOLD:
        return "fresh"
    elif days <= STALE_WARNING_THRESHOLD:
        return "stale_warning"
    return "stale"


def extract_dates(text: str) -> list[date]:
    """Extract recognizable dates from text, return sorted descending. Cap at 150KB."""
    text = text[:150_000]
    found: set[date] = set()
    # Cap future dates at TODAY + 500 days to avoid false positives (term limits,
    # fiscal year references, etc.) while still capturing pre-scheduled meetings
    # up to ~16 months out (e.g. CivicClerk cities that publish full year schedules).
    valid_range = (date(2020, 1, 1), TODAY + __import__("datetime").timedelta(days=500))

    # MM/DD/YYYY
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        try:
            d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            if valid_range[0] <= d <= valid_range[1]:
                found.add(d)
        except ValueError:
            pass

    # MM/DD/YY  (2-digit year, e.g. "03/10/26" → 2026-03-10)
    # Interprets YY 00-49 as 20YY, 50-99 as 19YY.
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b", text):
        yy = int(m.group(3))
        full_year = 2000 + yy if yy < 50 else 1900 + yy
        try:
            d = date(full_year, int(m.group(1)), int(m.group(2)))
            if valid_range[0] <= d <= valid_range[1]:
                found.add(d)
        except ValueError:
            pass

    # M-D-YY  (dash separator, 2-digit year, e.g. "3-25-26" → 2026-03-25)
    # Common in Google Drive file names for government agendas.
    for m in re.finditer(r"\b(\d{1,2})-(\d{1,2})-(\d{2})\b", text):
        yy = int(m.group(3))
        full_year = 2000 + yy if yy < 50 else 1900 + yy
        try:
            d = date(full_year, int(m.group(1)), int(m.group(2)))
            if valid_range[0] <= d <= valid_range[1]:
                found.add(d)
        except ValueError:
            pass

    # YYYY-MM-DD
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if valid_range[0] <= d <= valid_range[1]:
                found.add(d)
        except ValueError:
            pass

    # Month DD, YYYY (long and abbreviated)
    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4,
        "june": 6, "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }
    months_re = "|".join(sorted(month_map.keys(), key=len, reverse=True))
    for m in re.finditer(
        rf"\b({months_re})\w*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", text, re.IGNORECASE
    ):
        key = m.group(1).lower().rstrip(".")
        month_num = month_map.get(key[:3])
        if month_num:
            try:
                d = date(int(m.group(3)), month_num, int(m.group(2)))
                if valid_range[0] <= d <= valid_range[1]:
                    found.add(d)
            except ValueError:
                pass

    # ── Year-context "Month Day" (no adjacent year) ──────────────────────────
    # Handles government agenda tables where the year is a section header row
    # and individual rows only show "Month Day" (e.g. "December 8").
    # Pattern: find all "Month Day" without an adjacent year, then look back
    # up to 3 000 chars for the most recent 4-digit year to infer the full date.
    no_year_mday_re = re.compile(
        rf"\b({months_re})\w*\.?\s+(\d{{1,2}})\b(?!\s*,?\s*\d{{4}})",
        re.IGNORECASE,
    )
    for m in no_year_mday_re.finditer(text):
        window_back = text[max(0, m.start() - 3000) : m.start()]
        prior_years = re.findall(r"\b(20\d{2})\b", window_back)
        if not prior_years:
            continue
        inferred_year = int(prior_years[-1])
        key = m.group(1).lower().rstrip(".")
        month_num = month_map.get(key[:3])
        if not month_num:
            continue
        try:
            d = date(inferred_year, month_num, int(m.group(2)))
            if valid_range[0] <= d <= valid_range[1]:
                found.add(d)
        except ValueError:
            pass

    return sorted(found, reverse=True)


def _normalize_table_dates(text: str) -> str:
    """
    Pre-process rendered plain text for year-header agenda tables.
    When a standalone year ("2026") precedes "Month Day" lines (without a year),
    inject a complete "Month Day, Year" string so extract_dates() can parse it.

    Example input:  "2026\n December 8\t Agenda\n November 24\t Agenda"
    Example output: same text + "\nDecember 8, 2026\nNovember 24, 2026"
    """
    month_names = (
        "January|February|March|April|May|June|July|August|September|"
        "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    )
    lines = text.split("\n")
    injected: list[str] = []
    current_year: str | None = None
    year_line_re = re.compile(r"^\s*(20\d{2})\s*$")
    month_day_re = re.compile(
        rf"^\s*(?:\xa0\s*)?({month_names})\.?\s+(\d{{1,2}})\b(?!\s*,?\s*\d{{4}})",
        re.IGNORECASE,
    )
    for line in lines:
        ym = year_line_re.match(line)
        if ym:
            current_year = ym.group(1)
            continue
        if current_year:
            first_field = line.replace("\xa0", " ").split("\t")[0].strip()
            md = month_day_re.match(first_field)
            if md:
                injected.append(f"{md.group(1)} {md.group(2)}, {current_year}")
    if injected:
        return text + "\n" + "\n".join(injected)
    return text


def normalize_platform_url(url: str, platform: str) -> str:
    """Normalize Tavily URLs to the canonical portal base for platforms that use SPAs."""
    if platform == "escribe" and ".escribemeetings.com" in url:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    if platform == "granicus" and "granicus.com" in url and "/ViewPublisher" not in url:
        # Granicus ViewPublisher is the right landing page; individual clips are not.
        # Do NOT hardcode view_id=1 — the council view may be at a different ID.
        # probe_granicus_views() will enumerate IDs to find the correct one.
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}/ViewPublisher.php"
    return url


def is_wrong_city(url: str, title: str, city: str) -> bool:
    """Return True if this URL/title is recognizably from the wrong city or entity."""
    combined = (url + " " + title).lower()
    # Global wrong-entity patterns apply to all cities (school boards, ISDs, etc.)
    for pattern in WRONG_ENTITY_PATTERNS:
        if pattern.lower() in combined:
            return True
    # City-specific patterns
    for pattern in WRONG_CITY_PATTERNS.get(city, []):
        if pattern.lower() in combined:
            return True
    return False


def is_non_agenda_url(url: str) -> bool:
    """Return True if the URL is clearly not an official municipal agenda source."""
    url_lower = url.lower()
    for pat in REJECT_URL_PATTERNS:
        if pat in url_lower:
            return True
    # Reject .tv domains (local TV stations) unless explicitly government
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.endswith(".tv") or ".tv/" in url_lower:
        return True
    return False


def make_candidate(
    url: str,
    platform: str,
    source: str,
    http_status: int = 0,
    display_url: Optional[str] = None,
    config: Optional[dict] = None,
    notes: str = "",
    body: str = "",
) -> dict:
    return {
        "url": url,
        "platform": platform,
        "source": source,
        "http_status": http_status,
        "display_url": display_url or url,
        "config": config or {},
        "freshness": None,
        "most_recent_date": None,
        "days_since_update": None,
        "date_source": None,
        "notes": notes,
        "rank": None,
        "_body": body,  # cached response body for Phase 2 (stripped before output)
    }


async def safe_fetch(
    client: httpx.AsyncClient,
    url: str,
    timeout: float = 15.0,
    max_bytes: int = 200_000,
) -> tuple[int, str]:
    """Fetch URL and return (status_code, body). Negative status on network error.

    Status codes:
      -1  timeout (server likely exists)
      -2  connection error (DNS, SSL, refused)
      -5  SSL certificate error specifically
      -3  too many redirects
      -4  other error
    """
    try:
        resp = await asyncio.wait_for(
            client.get(url, follow_redirects=True),
            timeout=timeout,
        )
        body = resp.text[:max_bytes] if resp.text else ""
        return resp.status_code, body
    except asyncio.TimeoutError:
        return -1, "timeout"
    except httpx.ConnectError as e:
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg:
            return -5, f"ssl_error: {msg[:100]}"
        return -2, "connection_error"
    except httpx.TooManyRedirects:
        return -3, "too_many_redirects"
    except Exception as e:
        return -4, str(e)[:200]


# ── Phase 1: Discover Candidates ───────────────────────────────────────────────

async def discover_from_known_sources(known: dict, http: httpx.AsyncClient) -> list[dict]:
    """Build candidates from known_sources, fetching each URL to confirm reachability."""
    specs: list[tuple[str, str, str, dict]] = []  # (url, platform, display_url, config)

    if "legistar_slug" in known:
        slug = known["legistar_slug"]
        api_url = f"https://webapi.legistar.com/v1/{slug}/events?$top=3&$orderby=EventDate+desc"
        specs.append((api_url, "legistar", f"https://{slug}.legistar.com", {"legistar_slug": slug}))

    if "civicplus_domain" in known:
        domain = known["civicplus_domain"]
        url = f"https://{domain}/AgendaCenter"
        specs.append((url, "civicplus", url, {"domain": domain}))
    elif "domain" in known:
        # Probe AgendaCenter on the city domain — lower priority than explicit *_url keys.
        # Mark with _probe=True so scoring can penalize it.
        domain = known["domain"]
        url = f"https://{domain}/AgendaCenter"
        specs.append((url, "civicplus", url, {"domain": domain, "_probe": True}))

    for key, platform in [
        ("civicclerk_url", "civicclerk"),
        ("boarddocs_url", "boarddocs"),
        ("granicus_url", "granicus"),
        ("municode_url", "municode"),
        ("primegov_url", "primegov"),
        ("novus_url", "novus"),
        ("escribe_url", "escribe"),
        ("custom_agenda_url", "unknown"),
    ]:
        if key in known:
            url = known[key]
            detected = detect_platform(url)
            actual_platform = detected if detected != "unknown" else platform
            specs.append((url, actual_platform, url, {key: url}))

    async def fetch_spec(spec):
        url, platform, display_url, config = spec
        is_probe = config.pop("_probe", False)
        status, body = await safe_fetch(http, url, timeout=12.0)
        return make_candidate(
            url=url, platform=platform,
            source="known_probe" if is_probe else "known",
            http_status=status, display_url=display_url,
            config=config, body=body,
        )

    results = await asyncio.gather(*[fetch_spec(s) for s in specs], return_exceptions=True)
    candidates = []
    for r in results:
        if isinstance(r, Exception):
            continue
        # Exclude hard DNS failures (only keep if server responded or timed out)
        if r["http_status"] not in (-2, -3, -4):
            candidates.append(r)
    return candidates


async def discover_from_tavily(
    city: str,
    state: str,
    tavily: TavilyClient,
    search_depth: str = "basic",
    query: Optional[str] = None,
) -> tuple[list[dict], str]:
    """Run one Tavily search. Return (candidates, query_used)."""
    if query is None:
        query = f"{city} {state} city council meeting agendas minutes"
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                tavily.search,
                query=query,
                search_depth=search_depth,
                max_results=5,
                include_answer=False,
            ),
            timeout=30.0,
        )
    except Exception:
        return [], query

    candidates = []
    for r in result.get("results", []):
        url = r.get("url", "")
        title = r.get("title", "")
        content = r.get("content", "")  # text snippet from the indexed page
        if not url:
            continue
        # Include snippet in wrong-city check — content often names the city/state
        if is_wrong_city(url, f"{title} {content}", city):
            continue
        if is_non_agenda_url(url):
            continue
        platform = detect_platform(url)
        url = normalize_platform_url(url, platform)

        # Build note from title + snippet prefix
        note_parts = [title[:80]]
        if content:
            note_parts.append(content[:100])
        c = make_candidate(
            url=url, platform=platform, source="tavily",
            notes=" | ".join(p for p in note_parts if p).strip(" |")[:200],
        )

        # Pre-populate freshness from Tavily snippet text.
        # This is the key gap vs. LLM agents: they read snippet content to get dates;
        # we were discarding it. Handles JS SPAs and bot-blocked pages where we
        # can't fetch dates directly from the live URL.
        if content:
            snippet_dates = extract_dates(content)
            if snippet_dates:
                c["_snippet_date"] = snippet_dates[0].isoformat()
            # Flag migration hints so Phase 4 probes harder for new platform
            content_lower = content.lower()
            if any(kw in content_lower for kw in (
                "moved to", "migrated to", "new portal", "new website",
                "new system", "now located at", "relocated",
            )):
                c["notes"] += " [migration_hint_in_snippet]"

        candidates.append(c)
    return candidates, query


async def probe_granicus_views(
    subdomain: str,
    http: httpx.AsyncClient,
    max_view_id: int = 20,
) -> dict:
    """
    Enumerate Granicus RSS feeds (view_id=1..max_view_id) to find the City Council view.

    Granicus portals have multiple publisher views — view_id=1 is often a stale archive
    or a different body (e.g. Planning Board). The City Council view may be at any ID.

    Returns a result dict with keys:
      view_id, rss_url, display_url, freshness, most_recent_date, title
    Or {"view_id": None, "error": ...} if no council view found.
    """
    best_result: dict | None = None

    for view_id in range(1, max_view_id + 1):
        rss_url = (
            f"https://{subdomain}.granicus.com/ViewPublisherRSS.php"
            f"?view_id={view_id}&mode=agendas"
        )
        status, body = await safe_fetch(http, rss_url, timeout=8.0)
        if status != 200 or "<channel>" not in body:
            continue

        # Extract RSS channel title (handles both CDATA and plain text)
        title = ""
        m = re.search(r"<title><!\[CDATA\[([^\]]+)\]\]>", body)
        if m:
            title = m.group(1).strip()
        else:
            m = re.search(r"<title>([^<]+)</title>", body)
            if m:
                title = m.group(1).strip()

        title_lower = title.lower()
        is_council = any(kw in title_lower for kw in GRANICUS_COUNCIL_KEYWORDS)
        if not is_council:
            continue

        dates = extract_dates(body)
        most_recent = dates[0] if dates else None
        freshness = classify_freshness(most_recent) if most_recent else "unknown"

        result = {
            "view_id": view_id,
            "rss_url": rss_url,
            "display_url": (
                f"https://{subdomain}.granicus.com/ViewPublisher.php?view_id={view_id}"
            ),
            "freshness": freshness,
            "most_recent_date": most_recent.isoformat() if most_recent else None,
            "title": title,
            "_body": body,  # cache for Phase 2
        }

        # Fresh council view — ideal, return immediately
        if freshness == "fresh":
            return result

        # Keep as best candidate but continue looking (fresher view may exist)
        if best_result is None or (
            freshness == "stale_warning" and best_result.get("freshness") == "unknown"
        ):
            best_result = result

    if best_result:
        return best_result
    return {"view_id": None, "error": f"no council view found in view_id 1-{max_view_id}"}


_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# Common paths where city councils post agendas, ordered by frequency
_AGENDA_PATHS = [
    "/AgendaCenter",
    "/government/city-council/agendas-and-minutes",
    "/government/city-council/agendas-minutes",
    "/government/city-council/agendas",
    "/city-council/agendas-and-minutes",
    "/city-council/agendas",
    "/government/agendas-and-minutes",
    "/government/agendas",
    "/government/meetings",
    "/government/city-clerk/agendas-minutes",
    "/agendas-and-minutes",
    "/agendas-minutes",
    "/agendas",
    "/meetings",
    "/council/agendas",
    "/council-meetings",
    "/city-government/city-council",
    "/government/city-council",
    "/departments/city-clerk/agendas",
    "/city-clerk/agendas",
]


async def discover_official_domain(
    city: str, state: str, tavily: TavilyClient
) -> Optional[str]:
    """
    Find the official city government domain by searching for the city homepage.

    Agenda-content searches fail for small cities because their agenda pages aren't
    indexed. But every city's homepage is indexed. Searching for the official site
    gives us the domain; we then probe it directly for agenda paths.

    Returns the domain string (e.g. "dickinsontexas.gov"), or None.
    """
    state_full = _STATE_NAMES.get(state, state)
    city_lower = city.lower().replace(" ", "")
    query = f"{city} {state_full} official city government website"
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                tavily.search, query=query, max_results=5,
                search_depth="basic", include_answer=False,
            ),
            timeout=30.0,
        )
    except Exception:
        return None

    candidates_found = result.get("results", [])

    # Pass 1: prefer .gov domains or domains containing the city name
    for r in candidates_found:
        url = r.get("url", "")
        if not url:
            continue
        if is_wrong_city(url, (r.get("title") or "") + " " + (r.get("content") or ""), city):
            continue
        if is_non_agenda_url(url):
            continue
        domain = urlparse(url).netloc.replace("www.", "")
        if domain.endswith(".gov") or city_lower in domain.lower().replace("-", "").replace(".", ""):
            return domain

    # Pass 2: take first non-noise result (excludes social, news, ballotpedia)
    noise = {"facebook.", "twitter.", "x.com", "wikipedia.", "ballotpedia.",
             "patch.com", "yelp.", "linkedin.", "nextdoor."}
    for r in candidates_found:
        url = r.get("url", "")
        if not url:
            continue
        domain = urlparse(url).netloc.replace("www.", "")
        if not any(n in domain for n in noise):
            return domain

    return None


async def discover_official_domain_via_claude(city: str, state: str) -> Optional[str]:
    """
    Fallback domain discovery using Claude's web_search tool.

    Used when Tavily returns 0 results for a city — common for small townships
    and obscure municipalities that aren't well-indexed by Tavily's crawler.
    Claude's web search covers more of the long-tail web.

    Returns the domain string (e.g. "clearcreektownship.com"), or None.
    """
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    state_full = _STATE_NAMES.get(state, state)
    query = (
        f"What is the official government website for {city}, {state_full}? "
        f"I need the URL where they post city/town/township council or trustee meeting agendas."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            messages=[{"role": "user", "content": query}],
        )
        # Extract text from response
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        if not text:
            return None

        # Parse domain from any URL found in the response
        noise = {"facebook.", "twitter.", "x.com", "wikipedia.", "ballotpedia.",
                 "patch.com", "yelp.", "linkedin.", "nextdoor.", "google."}
        for url_match in re.finditer(r"https?://([^\s/\"'<>]+)", text):
            domain = url_match.group(1).split("/")[0].replace("www.", "")
            if not any(n in domain for n in noise):
                return domain

    except Exception:
        pass

    return None


async def probe_domain_for_agendas(
    domain: str,
    http: httpx.AsyncClient,
) -> list[dict]:
    """
    Probe common agenda URL paths on a known official city domain.

    This is the automated equivalent of "go to the city website and find the
    agendas section." Tries paths in frequency order; stops on the first hit
    for a named platform (CivicPlus, etc.) since the path is deterministic.
    """
    candidates = []
    # Try bare domain first, then www. prefix — some city sites only respond on one.
    domain_variants = [domain] if domain.startswith("www.") else [domain, f"www.{domain}"]

    for path in _AGENDA_PATHS:
        status, body, url = 0, "", ""
        for variant in domain_variants:
            candidate_url = f"https://{variant}{path}"
            s, b = await safe_fetch(http, candidate_url, timeout=6.0)
            if s == 200 and b:
                status, body, url = s, b, candidate_url
                break
        if status != 200 or not body:
            continue
        platform = detect_platform(url)
        # Require some evidence of agenda content (dates or known-platform path)
        dates = extract_dates(body[:30_000])
        has_dates = bool(dates)
        has_keywords = bool(re.search(
            r"agenda|minute|meeting|council|ordinance|resolution",
            body[:5_000], re.IGNORECASE,
        ))
        if not (has_dates or has_keywords or platform != "unknown"):
            continue
        c = make_candidate(
            url=url, platform=platform, source="domain_probe",
            http_status=status, body=body,
            notes=f"domain_probe:{domain}{path}",
        )
        candidates.append(c)
        # Stop on a named platform only if it has RECENT dates — if the page has only
        # stale dates (city migrated away from this platform), keep probing other paths.
        # This prevents a stale /AgendaCenter from blocking the correct current URL.
        has_recent = dates and (TODAY - dates[0]).days <= FRESH_THRESHOLD
        if platform != "unknown" and has_recent:
            break
    return candidates


async def discover_from_probes(
    city: str, state: str, known: dict, http: httpx.AsyncClient
) -> list[dict]:
    """Try common platform URL patterns to find an uncovered source."""
    city_nospace = city.lower().replace(" ", "").replace(".", "")
    state_lower = state.lower()
    domain = known.get("domain") or known.get("civicplus_domain")

    probe_specs: list[tuple[str, str, dict]] = [
        (
            f"https://webapi.legistar.com/v1/{city_nospace}/events?$top=3&$orderby=EventDate+desc",
            "legistar",
            {"legistar_slug": city_nospace, "display_url": f"https://{city_nospace}.legistar.com"},
        ),
        (
            f"https://{city_nospace}{state_lower}.portal.civicclerk.com",
            "civicclerk",
            {},
        ),
        (
            f"https://go.boarddocs.com/{state_lower}/{city_nospace}/Board.nsf/Public",
            "boarddocs",
            {},
        ),
        (
            f"https://{city_nospace}.novusagenda.com/agendapublic",
            "novus",
            {},
        ),
    ]
    if domain:
        probe_specs.insert(0, (f"https://{domain}/AgendaCenter", "civicplus", {"domain": domain}))

    candidates = []
    for url, platform, config in probe_specs:
        status, body = await safe_fetch(http, url, timeout=8.0)
        # Legistar: accept 400 responses — Legistar returns HTTP 400 (not 404) for
        # cities that haven't configured "Agenda Draft Status" in their admin panel.
        # The slug is still valid. verify_freshness will call /bodies to confirm.
        accept = status == 200 or (platform == "legistar" and status == 400)
        if not accept:
            continue

        # Novus: reject if body contains Application_Error
        if platform == "novus" and "Application_Error" in body:
            continue

        # CivicClerk: portal.civicclerk.com returns HTTP 200 for ANY subdomain
        # (React SPA shell loads regardless of whether the city uses CivicClerk).
        # Confirm the actual backend API responds before adding as a candidate —
        # otherwise every city gets a dead CivicClerk false-positive that scores 0
        # but still poisons the candidate list and sets the wrong platform label.
        if platform == "civicclerk":
            api_url = url.replace(".portal.civicclerk.com", ".api.civicclerk.com") + "/v1/Events/"
            api_status, _ = await safe_fetch(http, api_url, timeout=5.0)
            # 200 = open API, 401/403 = auth-gated but real, anything else = dead portal
            if api_status not in (200, 401, 403):
                continue  # dead portal — skip, don't add as candidate

        display_url = config.pop("display_url", url)
        c = make_candidate(
            url=url, platform=platform, source="probe",
            http_status=status, display_url=display_url,
            config=config, body=body,
        )
        candidates.append(c)

    # Granicus multi-view probe: enumerate view_id=1..20 to find City Council view.
    # (Hardcoding view_id=1 is wrong — council may be at any ID, e.g. Greenville NC=10.)
    granicus_sub = city_nospace
    gran_result = await probe_granicus_views(granicus_sub, http)
    if gran_result.get("view_id"):
        gran_body = gran_result.pop("_body", "")
        c = make_candidate(
            url=gran_result["rss_url"],
            platform="granicus",
            source="probe",
            http_status=200,
            display_url=gran_result["display_url"],
            config={"subdomain": granicus_sub, "view_id": gran_result["view_id"]},
            notes=f"Granicus RSS council view: {gran_result.get('title', '')}",
            body=gran_body,
        )
        candidates.append(c)

    return candidates


# ── Phase 2: Verify Freshness ──────────────────────────────────────────────────

async def verify_freshness(candidate: dict, http: httpx.AsyncClient) -> dict:
    """Determine freshness of a candidate. Uses cached body if available."""
    platform = candidate["platform"]
    url = candidate["url"]
    status = candidate.get("http_status", 0)
    cached_body = candidate.pop("_body", "")  # consume cache
    # Snippet date pre-populated from Tavily content — used as fallback when
    # the live URL is a JS SPA or bot-blocked (can't fetch dates directly).
    snippet_date_str = candidate.pop("_snippet_date", None)

    # ── Already confirmed blocked ──
    if status in (401, 403):
        # eSCRIBE portals block GET requests — POST API still works.
        if platform == "escribe":
            candidate["freshness"] = "unknown_spa"
            candidate["notes"] = (candidate.get("notes") or "") + " eSCRIBE portal (POST API required, GET=403)"
        else:
            candidate["freshness"] = "blocked"
            candidate["notes"] = (candidate.get("notes") or "") + " HTTP 403 — bot protection"
        return candidate
    if status == 404:
        # 404 = URL was indexed (maybe once real) but now returns nothing.
        # Score as empty (0) not stale (15) so live pages always beat dead ones,
        # even when the dead URL has a high-value platform label (e.g. civicplus).
        candidate["freshness"] = "empty"
        candidate["notes"] = (candidate.get("notes") or "") + " 404"
        return candidate
    if status == -5:
        # SSL cert error — if platform is known, we know the portal exists
        if platform != "unknown":
            candidate["freshness"] = "unknown_spa"
            candidate["notes"] = (candidate.get("notes") or "") + " SSL cert error — portal likely active"
        else:
            candidate["freshness"] = "blocked"
            candidate["notes"] = (candidate.get("notes") or "") + " SSL cert error"
        return candidate
    if status < 0 and status not in (-1,):  # -1 = timeout (server likely exists)
        candidate["freshness"] = "blocked"
        candidate["notes"] = (candidate.get("notes") or "") + f" network error {status}"
        return candidate

    # ── Legistar REST API ──
    if platform == "legistar":
        config = candidate.get("config", {})
        slug = config.get("legistar_slug", "")
        if not slug:
            m = re.search(r"/v1/([^/]+)/events", url)
            slug = m.group(1) if m else ""

        api_url = (
            f"https://webapi.legistar.com/v1/{slug}/events?$top=3&$orderby=EventDate+desc"
            if slug else url
        )
        body = cached_body if (cached_body and "EventDate" in cached_body) else None
        if body is None:
            fetch_status, body = await safe_fetch(http, api_url, timeout=15.0)
            if fetch_status != 200:
                # Legistar returns 400 when "Agenda Draft Status" is not configured
                # in their admin settings — the slug IS valid but the events endpoint
                # is broken. Confirm via /bodies endpoint before giving up.
                if fetch_status == 400 and slug and "Agenda" in (body or ""):
                    bodies_url = f"https://webapi.legistar.com/v1/{slug}/bodies"
                    bodies_status, bodies_body = await safe_fetch(http, bodies_url, timeout=10.0)
                    if bodies_status == 200 and bodies_body.strip().startswith("["):
                        # Slug is valid — Legistar instance exists but events API is
                        # misconfigured. Mark as unknown (not empty) so it's still preferred
                        # over a dead CivicClerk portal.
                        candidate["freshness"] = "unknown"
                        candidate["notes"] = "Legistar slug valid, events API returns 400 (Agenda Status misconfigured)"
                        return candidate
                candidate["freshness"] = "blocked" if fetch_status in (401, 403) else "stale"
                candidate["notes"] = f"Legistar API HTTP {fetch_status}"
                return candidate

        try:
            events = json.loads(body)
            if not events:
                candidate["freshness"] = "empty"
                candidate["notes"] = "Legistar API returned empty list"
                return candidate

            # Body-name validation: if top-3 events are all non-council bodies,
            # fetch top-20 and filter to council-type meetings specifically.
            # Prevents false-fresh when Planning Board/Advisory has recent meetings
            # but City Council is stale (e.g. Lexington NC: body 194 vs body 138).
            council_note = ""
            dates_events = events  # default: use top-N events
            body_names = [e.get("EventBodyName", "") for e in events if e.get("EventBodyName")]
            top_council = [e for e in events if _is_council_body(e.get("EventBodyName", ""))]

            if body_names and not top_council:
                # Top events are non-council — fetch more to find council meetings
                expanded_url = (
                    f"https://webapi.legistar.com/v1/{slug}/events"
                    f"?$top=20&$orderby=EventDate+desc"
                ) if slug else None
                all20: list = []
                if expanded_url:
                    _, exp_body = await safe_fetch(http, expanded_url, timeout=15.0)
                    try:
                        all20 = json.loads(exp_body) if exp_body else []
                    except (json.JSONDecodeError, TypeError):
                        all20 = []

                council_in_20 = [e for e in all20 if _is_council_body(e.get("EventBodyName", ""))]
                non_council_names = sorted(
                    {e.get("EventBodyName", "") for e in events if e.get("EventBodyName")}
                )[:3]

                if council_in_20:
                    dates_events = council_in_20
                    council_note = (
                        f" (top-3 non-council: {non_council_names};"
                        f" {len(council_in_20)} council events found in top-20)"
                    )
                else:
                    # No council body in top-20 — flag it, but keep all-event dates
                    council_note = (
                        f" WARNING:no_council_body_top20"
                        f" (bodies found: {non_council_names})"
                    )

            dates = []
            for e in dates_events:
                ed = e.get("EventDate", "")
                if ed:
                    try:
                        d = datetime.fromisoformat(ed.replace("Z", "").split("T")[0]).date()
                        dates.append(d)
                    except ValueError:
                        pass
            if dates:
                most_recent = max(dates)
                candidate["most_recent_date"] = most_recent.isoformat()
                candidate["days_since_update"] = (TODAY - most_recent).days
                candidate["date_source"] = "api_response"
                candidate["freshness"] = classify_freshness(most_recent)
                candidate["notes"] = f"{len(events)} events, most recent {most_recent}{council_note}"
                if slug:
                    candidate["config"]["legistar_slug"] = slug
                    candidate["display_url"] = f"https://{slug}.legistar.com"
            else:
                candidate["freshness"] = "unknown"
                candidate["notes"] = (
                    "Legistar API returned events but no parseable dates" + council_note
                )
        except (json.JSONDecodeError, TypeError, AttributeError):
            candidate["freshness"] = "unknown"
            candidate["notes"] = "Legistar API response unparseable"
        return candidate

    # ── SPA platforms (CivicClerk, PrimeGov) ──
    if platform in ("civicclerk", "primegov"):
        body = cached_body
        if not body:
            fetch_status, body = await safe_fetch(http, url, timeout=15.0)
            candidate["http_status"] = fetch_status
        if candidate.get("http_status", 0) in (401, 403):
            candidate["freshness"] = "blocked"
        elif candidate.get("http_status", 0) == 200 or status == 200:
            # If Tavily snippet already gave us a date, use it rather than
            # unknown_spa — this is the core gap vs. LLM agents who read snippets.
            if snippet_date_str:
                snippet_date = date.fromisoformat(snippet_date_str)
                candidate["most_recent_date"] = snippet_date_str
                candidate["days_since_update"] = (TODAY - snippet_date).days
                candidate["date_source"] = "tavily_snippet"
                candidate["freshness"] = classify_freshness(snippet_date)
                candidate["notes"] = (
                    (candidate.get("notes") or "").strip()
                    + " (date from Tavily snippet — JS SPA)"
                )
            else:
                candidate["freshness"] = "unknown_spa"
                candidate["notes"] = (candidate.get("notes") or "") + " JS SPA — needs browser"
        else:
            candidate["freshness"] = "blocked"
        return candidate

    # ── All other platforms: fetch HTML and parse dates ──
    body = cached_body
    if not body:
        fetch_status, body = await safe_fetch(http, url, timeout=15.0)
        candidate["http_status"] = fetch_status
        if fetch_status in (401, 403):
            if platform == "escribe":
                candidate["freshness"] = "unknown_spa"
                candidate["notes"] = (candidate.get("notes") or "") + " eSCRIBE portal (POST API required, GET=403)"
            else:
                candidate["freshness"] = "blocked"
                candidate["notes"] = (candidate.get("notes") or "") + " HTTP 403"
            return candidate
        if fetch_status == -5:
            if platform != "unknown":
                candidate["freshness"] = "unknown_spa"
                candidate["notes"] = (candidate.get("notes") or "") + " SSL cert error — portal likely active"
            else:
                candidate["freshness"] = "blocked"
                candidate["notes"] = (candidate.get("notes") or "") + " SSL cert error"
            return candidate
        if fetch_status == 404:
            candidate["freshness"] = "empty"
            candidate["notes"] = (candidate.get("notes") or "") + " 404"
            return candidate
        if fetch_status < 0:
            candidate["freshness"] = "blocked"
            candidate["notes"] = f"fetch error {fetch_status}"
            return candidate

    # Novus: body must not contain Application_Error
    if platform == "novus":
        if "Application_Error" in body:
            candidate["freshness"] = "stale"
            candidate["notes"] = "Novus 200 but Application_Error — invalid slug"
            return candidate

    # Municode code library (not a meeting portal)
    if platform == "municode" and "library.municode.com" in url:
        candidate["freshness"] = "unknown"
        candidate["notes"] = "Municode code library — not a meeting/agenda portal. Need meetings URL."
        return candidate

    # CivicPlus: check for empty AgendaCenter
    if platform == "civicplus":
        has_content = bool(
            re.search(r"(AgendaItemFile|\.pdf|agendacenter|category|archive)", body, re.IGNORECASE)
        )
        if not has_content or len(body) < 300:
            candidate["freshness"] = "empty"
            candidate["notes"] = (candidate.get("notes") or "") + " AgendaCenter has no categories/data"
            return candidate

        # Try to find a City Council–specific category to avoid false-fresh from
        # other committees (e.g. zoning board, arts commission) that are still active.
        # CivicPlus category pages follow the pattern /AgendaCenter/{Category-Name}-{id}
        if "/AgendaCenter" in url and re.search(r"/AgendaCenter/?$|/AgendaCenter\?", url):
            cat_match = re.search(
                r'href="(/AgendaCenter/[^"\']*(?:city[- ]?council|city[- ]?commission|council[- ]?meeting)[^"\']{0,40})"',
                body, re.IGNORECASE
            )
            if cat_match:
                parsed_url = urlparse(url)
                cat_url = f"{parsed_url.scheme}://{parsed_url.netloc}{cat_match.group(1)}"
                cat_status, cat_body = await safe_fetch(http, cat_url, timeout=12.0)
                if cat_status == 200 and cat_body:
                    cat_dates = extract_dates(cat_body)
                    if cat_dates:
                        most_recent = cat_dates[0]
                        candidate["most_recent_date"] = most_recent.isoformat()
                        candidate["days_since_update"] = (TODAY - most_recent).days
                        candidate["date_source"] = "civicplus_council_category"
                        candidate["freshness"] = classify_freshness(most_recent)
                        candidate["notes"] = (candidate.get("notes") or "") + f" council_cat:{cat_url}"
                        return candidate
            else:
                # No City Council category link found — date will be from all categories.
                # Mark so we can downgrade if the all-category dates look "fresh"
                # (advisory boards may be active while city council is stale).
                candidate["notes"] = (candidate.get("notes") or "") + " (no_council_category — dates may be advisory-only)"
                candidate["_no_council_category"] = True

    # BoardDocs: minimal HTML shell is normal — mark unknown_spa if no dates
    if platform == "boarddocs" and (len(body) < 800 or "boarddocs" not in body.lower()):
        candidate["freshness"] = "unknown_spa"
        candidate["notes"] = "BoardDocs SPA — dates not in initial HTML"
        return candidate

    # Parse dates from body
    dates = extract_dates(body)
    if dates:
        most_recent = dates[0]
        candidate["most_recent_date"] = most_recent.isoformat()
        candidate["days_since_update"] = (TODAY - most_recent).days
        candidate["date_source"] = "html_date_parse"
        candidate["freshness"] = classify_freshness(most_recent)
        # CivicPlus: if no council category was found and all-category dates look fresh,
        # downgrade to stale_warning — advisory boards may be active while council is stale.
        if candidate.pop("_no_council_category", False) and candidate["freshness"] == "fresh":
            candidate["freshness"] = "stale_warning"
            candidate["notes"] = (
                (candidate.get("notes") or "").strip()
                + " [downgraded_fresh→stale_warning: no council category confirmed]"
            )
    else:
        # Platform-specific fallback
        if platform in ("boarddocs",):
            candidate["freshness"] = "unknown_spa"
            candidate["notes"] = (candidate.get("notes") or "") + " No dates in HTML — JS-rendered"
        elif platform == "unknown":
            has_agenda_kw = bool(
                re.search(r"\b(agenda|minutes|meeting|council|motion|vote)\b", body, re.IGNORECASE)
            )
            if not has_agenda_kw:
                candidate["freshness"] = "unknown"
                candidate["notes"] = (candidate.get("notes") or "") + " No agenda keywords"
            else:
                candidate["freshness"] = "unknown"
                candidate["notes"] = (candidate.get("notes") or "") + " Agenda keywords found but no dates"
        else:
            candidate["freshness"] = "unknown"
            candidate["notes"] = (candidate.get("notes") or "") + " No parseable dates in HTML"

    # ── Tavily snippet fallback ──
    # If HTML parsing found nothing but Tavily gave us a snippet date, use it.
    # This handles JS-rendered pages, bot-blocked URLs, and other cases where
    # we can't scrape dates but the search engine already indexed the content.
    if snippet_date_str and candidate.get("freshness") in ("unknown", "unknown_spa", "blocked"):
        snippet_date = date.fromisoformat(snippet_date_str)
        candidate["most_recent_date"] = snippet_date_str
        candidate["days_since_update"] = (TODAY - snippet_date).days
        candidate["date_source"] = "tavily_snippet"
        candidate["freshness"] = classify_freshness(snippet_date)
        candidate["notes"] = (
            (candidate.get("notes") or "").strip()
            + " (date from Tavily snippet)"
        )

    return candidate


# ── Phase 3: Rank and Select ───────────────────────────────────────────────────

# Scoring weights
FRESHNESS_SCORE = {
    "fresh": 100, "unknown_spa": 55, "stale_warning": 35,
    "stale": 15, "unknown": 5, "empty": 0, "blocked": 0,
}
# empty and blocked score 0 — they should never win over any real candidate,
# and a Legistar "unknown" (5 + 20 = 25) beats a CivicClerk "empty" (0 + 12 = 12).
PLATFORM_TIER = {
    "legistar": 20, "civicplus": 16, "granicus": 16, "escribe": 14,
    "boarddocs": 14, "municode": 12, "civicclerk": 12, "primegov": 12,
    "novus": 10, "diligent": 10, "unknown": 4,
}
SOURCE_BONUS = {"known": 10, "known_probe": 2, "tavily": 3, "probe": 0}


def candidate_score(c: dict) -> int:
    f = FRESHNESS_SCORE.get(c.get("freshness") or "", 0)
    p = PLATFORM_TIER.get(c.get("platform") or "unknown", 4)
    s = SOURCE_BONUS.get(c.get("source") or "probe", 0)
    return f + p + s


def rank_candidates(candidates: list[dict]) -> list[dict]:
    ranked = sorted(candidates, key=candidate_score, reverse=True)
    for i, c in enumerate(ranked):
        c["rank"] = i + 1
    return ranked


# ── Phase 4: Deep Platform API Probes ─────────────────────────────────────────
#
# These run after the retry loop when best is still unknown_spa or stale_warning.
# Each probe attempts the platform's actual backend API — not the JS-rendered
# frontend. Success upgrades freshness from unknown_spa → fresh/stale/stale_warning
# with a real date. Failure adds "api_probed: failed" to notes so we know we tried.

async def probe_civicclerk_api(url: str, http: httpx.AsyncClient) -> dict:
    """
    CivicClerk portals expose a REST JSON API (OData) that bypasses their SPA.
    The real API lives at {tenant}.api.civicclerk.com/v1/Events/
    where tenant = the subdomain before .portal in {tenant}.portal.civicclerk.com.
    Falls back to legacy /api/v1/event paths and RSS feeds.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc  # e.g. "shermantx.portal.civicclerk.com"

    # Derive tenant: strip ".portal.civicclerk.com" or ".civicclerk.com" suffix
    tenant = re.sub(r"\.portal\.civicclerk\.com$", "", netloc)
    tenant = re.sub(r"\.civicclerk\.com$", "", tenant)

    def _parse_odata_events(body: str, api_url: str) -> dict | None:
        """Parse OData JSON response from CivicClerk Events API."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        items = data.get("value", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return None
        dates = []
        for e in items:
            for field in ("eventDate", "startDate", "start", "EventDate", "date", "EventStartDate"):
                val = e.get(field, "")
                if val:
                    try:
                        d = datetime.fromisoformat(str(val)[:10]).date()
                        if date(2020, 1, 1) <= d <= date(2030, 12, 31):
                            dates.append(d)
                            break
                    except ValueError:
                        pass
        if not dates:
            return None
        most_recent = max(dates)
        return {
            "success": True,
            "most_recent_date": most_recent.isoformat(),
            "days_since_update": (TODAY - most_recent).days,
            "freshness": classify_freshness(most_recent),
            "date_source": "civicclerk_api",
            "api_url": api_url,
            "events_count": len(items),
        }

    # Pattern 1: Real OData API — {tenant}.api.civicclerk.com/v1/Events/
    odata_base = f"https://{tenant}.api.civicclerk.com"
    for odata_path in (
        "/v1/Events?$top=20&$orderby=EventDate+desc",
        "/v1/Events/",
        "/v1/Events",
    ):
        status, body = await safe_fetch(http, odata_base + odata_path, timeout=15.0)
        if status == 200 and body.strip().startswith("{"):
            result = _parse_odata_events(body, odata_base + odata_path)
            if result:
                return result

    # Pattern 2: Legacy portal-side endpoints (older CivicClerk deployments)
    portal_base = f"{parsed.scheme}://{netloc}"
    for api_path in (
        "/api/v1/event?categoryId=&committeeId=&getType=1&startDate=&endDate=",
        "/api/v1/event?getType=1",
        "/api/v1/event",
    ):
        status, body = await safe_fetch(http, portal_base + api_path, timeout=15.0)
        if status == 200 and (body.strip().startswith("[") or body.strip().startswith("{")):
            result = _parse_odata_events(body, portal_base + api_path)
            if result:
                return result

    # Pattern 3: RSS/iCal feed
    for feed_path in ("/RSSFeed.ashx?type=Meetings", "/Feed.ashx"):
        status, body = await safe_fetch(http, portal_base + feed_path, timeout=15.0)
        if status == 200 and ("<pubDate>" in body or "BEGIN:VCALENDAR" in body):
            dates = extract_dates(body)
            if dates:
                most_recent = dates[0]
                return {
                    "success": True,
                    "most_recent_date": most_recent.isoformat(),
                    "days_since_update": (TODAY - most_recent).days,
                    "freshness": classify_freshness(most_recent),
                    "date_source": "civicclerk_rss",
                    "api_url": portal_base + feed_path,
                }

    return {"success": False, "error": "all CivicClerk API endpoints failed"}


async def probe_boarddocs_api(url: str, http: httpx.AsyncClient) -> dict:
    """
    BoardDocs has an undocumented POST endpoint that returns a board meeting list.
    The SPA frontend calls this, and we can too.
    """
    # Derive the POST endpoint from the Public URL
    # e.g. https://go.boarddocs.com/oh/dublin/Board.nsf/Public
    #   → https://go.boarddocs.com/oh/dublin/Board.nsf/BD-GetBoardList-Public
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/Public"):
        post_path = path.replace("/Public", "/BD-GetBoardList-Public")
    elif "Board.nsf" in path:
        base_path = re.sub(r"/[^/]*$", "", path)
        post_path = base_path + "/BD-GetBoardList-Public"
    else:
        return {"success": False, "error": "can't derive BoardDocs POST URL"}

    post_url = f"{parsed.scheme}://{parsed.netloc}{post_path}"

    try:
        resp = await asyncio.wait_for(
            http.post(
                post_url,
                content=f"current_page_url={url}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            timeout=15.0,
        )
        body = resp.text[:80_000]
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}"}

        # BoardDocs returns a JSON array of meeting objects
        # Each has a "unique_key" like "20260115" (YYYYMMDD) or embedded date
        dates = []

        # Try JSON parse first
        try:
            data = json.loads(body)
            if isinstance(data, list):
                for item in data:
                    uk = str(item.get("unique_key", "") or item.get("key", ""))
                    # unique_key format: YYYYMMDD or YYMMDD
                    if re.match(r"^\d{8}$", uk):
                        try:
                            d = date(int(uk[:4]), int(uk[4:6]), int(uk[6:8]))
                            if date(2020, 1, 1) <= d <= date(2030, 12, 31):
                                dates.append(d)
                        except ValueError:
                            pass
                    # Also try title/date fields
                    for field in ("title", "date", "Date"):
                        val = str(item.get(field, ""))
                        if val:
                            found = extract_dates(val)
                            dates.extend(found)
        except json.JSONDecodeError:
            pass

        # Fallback: parse dates from raw HTML/JSON body
        if not dates:
            dates = extract_dates(body)

        if dates:
            most_recent = max(dates)
            return {
                "success": True,
                "most_recent_date": most_recent.isoformat(),
                "days_since_update": (TODAY - most_recent).days,
                "freshness": classify_freshness(most_recent),
                "date_source": "boarddocs_post_api",
                "api_url": post_url,
            }
        return {"success": False, "error": "POST returned data but no parseable dates"}

    except asyncio.TimeoutError:
        return {"success": False, "error": "timeout on POST request"}
    except Exception as e:
        return {"success": False, "error": str(e)[:100]}


async def probe_escribe_api(url: str, http: httpx.AsyncClient) -> dict:
    """
    eSCRIBE portals return SSL errors or 403 to GET, but work via alternate
    endpoints. We try with SSL verification disabled (the cert is valid —
    just not in Python's default bundle) and probe several URL patterns.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    endpoints_to_try = [
        base + "/MeetingsCalendarView.aspx",
        base + "/PublicMeetings.aspx",
        base + "/",
        base + "/en/web",
    ]

    # Use a separate client with SSL verification off for eSCRIBE
    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=True,
        timeout=15.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        },
    ) as unsafe_http:
        for endpoint in endpoints_to_try:
            try:
                resp = await asyncio.wait_for(
                    unsafe_http.get(endpoint), timeout=12.0
                )
                if resp.status_code == 200 and resp.text:
                    body = resp.text[:150_000]
                    dates = extract_dates(body)
                    has_meeting_kw = bool(
                        re.search(r"\b(agenda|council|meeting|minutes)\b", body, re.IGNORECASE)
                    )
                    if dates and has_meeting_kw:
                        most_recent = dates[0]
                        return {
                            "success": True,
                            "most_recent_date": most_recent.isoformat(),
                            "days_since_update": (TODAY - most_recent).days,
                            "freshness": classify_freshness(most_recent),
                            "date_source": "escribe_html",
                            "api_url": endpoint,
                        }
                    elif has_meeting_kw:
                        # Page loaded with meeting content but no dates
                        return {
                            "success": True,
                            "most_recent_date": None,
                            "days_since_update": None,
                            "freshness": "unknown_spa",
                            "date_source": None,
                            "api_url": endpoint,
                            "note": "eSCRIBE loaded (SSL bypass) but no dates in HTML — JS-rendered",
                        }
            except Exception:
                continue

    return {"success": False, "error": "all eSCRIBE endpoints failed (SSL bypass attempted)"}


async def probe_civicplus_variations(url: str, http: httpx.AsyncClient) -> dict:
    """
    CivicPlus AgendaCenter can be queried with year filters or archive paths.
    Useful when the base page has stale dates but current-year agendas exist
    under a year-filtered URL.
    """
    base_url = url.split("?")[0]  # strip existing params
    tried = []

    for year in (TODAY.year, TODAY.year - 1):
        year_url = f"{base_url}?Year={year}"
        tried.append(year_url)
        status, body = await safe_fetch(http, year_url, timeout=12.0)
        if status == 200 and body:
            has_content = bool(
                re.search(r"(AgendaItemFile|\.pdf|\.PDF|agenda|minutes)", body, re.IGNORECASE)
            )
            if has_content:
                dates = extract_dates(body)
                if dates:
                    most_recent = dates[0]
                    freshness = classify_freshness(most_recent)
                    if freshness in ("fresh", "stale_warning"):
                        return {
                            "success": True,
                            "most_recent_date": most_recent.isoformat(),
                            "days_since_update": (TODAY - most_recent).days,
                            "freshness": freshness,
                            "date_source": "civicplus_year_filter",
                            "api_url": year_url,
                        }

    return {"success": False, "error": f"CivicPlus year filters tried: {tried}"}


async def probe_unknown_stale(
    url: str,
    city: str,
    state: str,
    http: httpx.AsyncClient,
) -> dict:
    """
    For unknown-platform stale_warning sources: try fetching a few alternate
    URL patterns — year-based paths, /agendas pages, etc.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    city_slug = city_to_slug(city)

    candidates_to_try = [
        f"{base}/agendas",
        f"{base}/government/agendas-minutes",
        f"{base}/city-council/agendas",
        f"{base}/departments/city-clerk/agendas",
        f"{base}/city-government/city-council/agendas-minutes",
        url + f"/{TODAY.year}",
        url + f"?year={TODAY.year}",
    ]

    for try_url in candidates_to_try:
        if try_url == url:
            continue
        status, body = await safe_fetch(http, try_url, timeout=10.0)
        if status == 200 and body:
            dates = extract_dates(body)
            has_agenda = bool(
                re.search(r"\b(agenda|minutes|meeting|council)\b", body, re.IGNORECASE)
            )
            if dates and has_agenda:
                most_recent = dates[0]
                freshness = classify_freshness(most_recent)
                if freshness in ("fresh", "stale_warning"):
                    return {
                        "success": True,
                        "most_recent_date": most_recent.isoformat(),
                        "days_since_update": (TODAY - most_recent).days,
                        "freshness": freshness,
                        "date_source": "html_date_parse",
                        "api_url": try_url,
                    }

    return {"success": False, "error": "all alternate URL patterns failed"}


async def probe_with_playwright(
    url: str,
    city: str,
    state: str,
) -> dict:
    """
    Phase 5: Use Playwright headless browser to render JS-heavy pages.

    Handles:
    - CivicClerk / PrimeGov JS SPAs where the direct OData API is unavailable
    - Bot-blocked pages (403) where a real browser bypasses basic protection
    - Unknown platforms that serve agenda content only after JS execution

    Requires: pip install playwright && playwright install chromium

    Returns the same result dict shape as probe_civicclerk_api et al.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        return {
            "success": False,
            "error": "playwright not installed (pip install playwright && playwright install chromium)",
        }

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()

                # Navigate — try networkidle first (SPAs complete API calls),
                # fall back to domcontentloaded + extra wait if networkidle times out.
                nav_ok = False
                try:
                    await page.goto(url, wait_until="networkidle", timeout=25000)
                    nav_ok = True
                except Exception:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        await page.wait_for_timeout(4000)  # let JS render
                        nav_ok = True
                    except Exception as nav_err:
                        return {"success": False, "error": f"navigation failed: {str(nav_err)[:80]}"}

                if not nav_ok:
                    return {"success": False, "error": "navigation failed"}

                # Extract rendered text (inner_text strips tags; fall back to HTML)
                try:
                    body_text = await page.inner_text("body")
                except Exception:
                    body_text = ""
                if not body_text:
                    try:
                        raw_html = await page.content()
                        body_text = re.sub(r"<[^>]+>", " ", raw_html)
                    except Exception:
                        return {"success": False, "error": "could not extract rendered content"}

                # Normalize year-header table dates before extraction
                body_text = _normalize_table_dates(body_text)

                has_meeting_kw = bool(
                    re.search(
                        r"\b(agenda|minutes|meeting|council|vote|ordinance)\b",
                        body_text,
                        re.IGNORECASE,
                    )
                )
                dates = extract_dates(body_text)

                if dates and has_meeting_kw:
                    most_recent = dates[0]
                    return {
                        "success": True,
                        "most_recent_date": most_recent.isoformat(),
                        "days_since_update": (TODAY - most_recent).days,
                        "freshness": classify_freshness(most_recent),
                        "date_source": "playwright_render",
                    }
                elif has_meeting_kw:
                    return {
                        "success": True,
                        "most_recent_date": None,
                        "days_since_update": None,
                        "freshness": "unknown_spa",
                        "date_source": None,
                        "note": "Playwright rendered — meeting content found but no parseable dates",
                    }
                else:
                    return {
                        "success": False,
                        "error": "rendered page has no meeting/agenda content",
                    }
            finally:
                await browser.close()
    except Exception as e:
        return {"success": False, "error": f"playwright: {str(e)[:100]}"}


async def deep_probe_candidate(
    candidate: dict,
    city: str,
    state: str,
    http: httpx.AsyncClient,
) -> bool:
    """
    Dispatcher: run the right deep probe for the candidate's platform.
    Mutates the candidate dict in place if a better result is found.
    Returns True if freshness was upgraded (i.e., now has a real date).
    """
    platform = candidate.get("platform", "unknown")
    url = candidate.get("url", "")
    current_freshness = candidate.get("freshness")

    result: dict = {}

    if platform == "civicclerk":
        result = await probe_civicclerk_api(url, http)
    elif platform == "boarddocs":
        result = await probe_boarddocs_api(url, http)
    elif platform == "escribe":
        result = await probe_escribe_api(url, http)
    elif platform == "civicplus" and current_freshness in ("stale_warning", "stale", "unknown", "empty"):
        result = await probe_civicplus_variations(url, http)
    elif platform == "unknown" and current_freshness == "stale_warning":
        result = await probe_unknown_stale(url, city, state, http)
    elif platform == "granicus" and current_freshness in ("stale", "stale_warning", "unknown"):
        # Known Granicus URL is stale — the council may be on a different view_id.
        # Extract subdomain and enumerate all views to find the council feed.
        parsed = urlparse(url)
        subdomain = parsed.netloc.replace(".granicus.com", "")
        gran = await probe_granicus_views(subdomain, http)
        if gran.get("view_id"):
            gran_body = gran.pop("_body", "")
            result = {
                "success": True,
                "most_recent_date": gran.get("most_recent_date"),
                "days_since_update": (
                    (TODAY - date.fromisoformat(gran["most_recent_date"])).days
                    if gran.get("most_recent_date") else None
                ),
                "freshness": gran["freshness"],
                "date_source": "rss_feed",
                "api_url": gran["rss_url"],
                "new_url": gran["rss_url"],
                "new_display_url": gran["display_url"],
                "new_config": {"subdomain": subdomain, "view_id": gran["view_id"]},
            }
        else:
            result = {"success": False, "error": gran.get("error", "no council view found")}
    else:
        return False

    if result.get("success"):
        candidate["freshness"] = result["freshness"]
        candidate["most_recent_date"] = result.get("most_recent_date")
        candidate["days_since_update"] = result.get("days_since_update")
        candidate["date_source"] = result.get("date_source")
        # Granicus: update URL/config to the newly-found council view
        if result.get("new_url"):
            candidate["url"] = result["new_url"]
            candidate["display_url"] = result.get("new_display_url", result["new_url"])
            candidate["config"] = result.get("new_config", candidate.get("config", {}))
        existing_notes = (candidate.get("notes") or "").strip()
        probe_note = f"api_probed:{result.get('api_url','')}"
        if result.get("note"):
            probe_note += f" ({result['note']})"
        candidate["notes"] = f"{existing_notes} {probe_note}".strip()
        # Upgrade collection_method if we confirmed a real API
        if result.get("date_source") in ("civicclerk_api", "civicclerk_rss"):
            candidate["collection_method"] = "rest_api"
        elif result.get("date_source") in ("boarddocs_post_api",):
            candidate["collection_method"] = "post_api_html"
        return True
    else:
        # Record that we tried and failed
        existing_notes = (candidate.get("notes") or "").strip()
        error_msg = result.get("error", "")
        candidate["notes"] = f"{existing_notes} api_probed:failed({error_msg})".strip()
        # CivicClerk false-positive guard: portal.civicclerk.com returns HTTP 200
        # with a generic 1110-byte SPA shell for ANY subdomain — including cities
        # that don't use CivicClerk. If this candidate came from our automated probe
        # (not the registry) and all OData API paths failed, it's almost certainly
        # a false positive. Downgrade to empty so it doesn't block a real source.
        if platform == "civicclerk" and candidate.get("source") == "probe":
            candidate["freshness"] = "empty"
            candidate["notes"] += " [civicclerk-probe-false-positive: generic shell, no events API]"
        return False


# ── Core skill: run_source_discover ───────────────────────────────────────────

async def run_source_discover(
    city: str,
    state: str,
    known_sources: dict,
    tavily: TavilyClient,
    http: httpx.AsyncClient,
) -> dict:
    start = time.monotonic()
    verified: list[dict] = []
    tavily_queries: list[str] = []
    retries_used: list[str] = []
    retry_attempts = 0

    # ── Phase 1: Discover candidates ──────────────────────────────────────────

    # Strategy A: Check known sources
    known_cands = await discover_from_known_sources(known_sources, http)
    seen_urls: set[str] = {c["url"] for c in known_cands}

    # Strategy B: Tavily basic search (always — catches migrations)
    tavily_cands, query_used = await discover_from_tavily(city, state, tavily, "basic")
    tavily_queries.append(query_used)
    for c in tavily_cands:
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            known_cands.append(c)

    all_phase1 = known_cands

    # Strategy C: Probe common URL patterns if no recognized platform with 200 yet
    has_recognized = any(
        c["platform"] != "unknown" and c["http_status"] == 200
        for c in all_phase1
    )
    if not has_recognized:
        probe_cands = await discover_from_probes(city, state, known_sources, http)
        for c in probe_cands:
            if c["url"] not in seen_urls:
                seen_urls.add(c["url"])
                all_phase1.append(c)

    # ── Phase 2: Verify freshness ──────────────────────────────────────────────
    for c in all_phase1:
        try:
            vc = await verify_freshness(c, http)
        except Exception as e:
            c["freshness"] = "unknown"
            c["notes"] = (c.get("notes") or "") + f" verify_error: {str(e)[:100]}"
            c.pop("_body", None)
            vc = c
        verified.append(vc)

    # ── Phase 3: Rank ─────────────────────────────────────────────────────────
    ranked = rank_candidates(verified)
    best = ranked[0] if ranked else None

    # ── Retry loop ────────────────────────────────────────────────────────────
    while retry_attempts < 2 and (not best or best.get("freshness") not in ("fresh",)):
        retry_attempts += 1

        if retry_attempts == 1:
            # Retry 1: Advanced Tavily with alternate queries
            retries_used.append("alternate_queries")
            domain = known_sources.get("domain", "")
            alt_queries = [
                f"{city} {state} city council agenda {TODAY.year}",
                f"{city} {state} council meeting minutes recent",
            ]
            if domain:
                alt_queries.append(f"site:{domain} agenda OR minutes OR meeting")

            for q in alt_queries[:3]:
                new_cands, _ = await discover_from_tavily(
                    city, state, tavily, "advanced", query=q
                )
                tavily_queries.append(q)
                for c in new_cands:
                    if c["url"] not in seen_urls:
                        seen_urls.add(c["url"])
                        try:
                            vc = await verify_freshness(c, http)
                        except Exception:
                            vc = c
                        verified.append(vc)
                        if vc.get("freshness") == "fresh":
                            break
                else:
                    continue
                break  # inner break propagated

        elif retry_attempts == 2:
            # Retry 2: Official domain discovery → common path probe → homepage crawl
            #
            # Replicates the manual process: find the official city website, then look
            # for agendas there. This works for small cities where agenda-specific
            # searches return 0 results but the city homepage is always indexed.
            retries_used.append("domain_discovery")
            domain = known_sources.get("domain", "")

            # Step 2a: Discover domain if not already known
            if not domain:
                domain = await discover_official_domain(city, state, tavily) or ""
                tavily_queries.append(f"[domain_discovery] {city} {state} official city government website")

            # Step 2a-fallback: Tavily found nothing — try Claude web_search
            # Handles small townships and obscure cities not in Tavily's index
            if not domain:
                domain = await discover_official_domain_via_claude(city, state) or ""
                if domain:
                    tavily_queries.append(f"[claude_websearch] {city} {state} official government website")

            if not domain:
                break

            # Step 2b: Probe common agenda paths on the discovered domain
            domain_cands = await probe_domain_for_agendas(domain, http)
            for c in domain_cands:
                if c["url"] not in seen_urls:
                    seen_urls.add(c["url"])
                    try:
                        vc = await verify_freshness(c, http)
                    except Exception:
                        vc = c
                    verified.append(vc)
                    if vc.get("freshness") == "fresh":
                        break

            # Step 2c: Fallback — scan homepage HTML for agenda keyword links
            # (catches custom CMS layouts where agendas are at non-standard paths)
            if not any(v.get("freshness") == "fresh" for v in verified):
                homepage_body = ""
                for base in [f"https://{domain}", f"https://www.{domain}"]:
                    hs, hb = await safe_fetch(http, base, timeout=15.0)
                    if hs == 200 and hb:
                        homepage_body = hb
                        break
                if homepage_body:
                    kw_re = re.compile(r"agenda|minute|meeting|council|clerk|legislative", re.IGNORECASE)
                    link_re = re.compile(r'href=["\']([^"\'<>\s]+)["\']', re.IGNORECASE)
                    deep_count = 0
                    for m in link_re.finditer(homepage_body):
                        link = m.group(1)
                        if not kw_re.search(link):
                            continue
                        if link.startswith("/"):
                            link = f"https://{domain}{link}"
                        elif not link.startswith("http"):
                            continue
                        if link in seen_urls:
                            continue
                        seen_urls.add(link)
                        platform = detect_platform(link)
                        c = make_candidate(url=link, platform=platform, source="domain_probe")
                        try:
                            vc = await verify_freshness(c, http)
                        except Exception:
                            vc = c
                        verified.append(vc)
                        deep_count += 1
                        if vc.get("freshness") == "fresh":
                            break
                        if deep_count >= 5:
                            break

        ranked = rank_candidates(verified)
        best = ranked[0] if ranked else None
        if best and best.get("freshness") == "fresh":
            break

    # ── Phase 4: Deep Platform API Probes ─────────────────────────────────────
    # Runs when no fresh source found, OR when best is fresh-from-tavily but a
    # known structured platform (CivicClerk/BoardDocs/eSCRIBE) candidate is still
    # unknown_spa — the platform API is more reliable than a scraped HTML page.
    # Also runs when best is platform=unknown (scraped HTML / social) — we must
    # verify structured platforms (Legistar, CivicClerk) before accepting an
    # unstructured source as the winner.
    has_unprobed_platform = any(
        c.get("freshness") in ("unknown_spa", "stale_warning", "stale", "unknown", "empty")
        and c.get("platform") in ("civicclerk", "boarddocs", "escribe", "legistar")
        and c.get("source") in ("known", "known_probe", "probe")
        for c in ranked
    )
    best_is_unstructured = (
        best is not None
        and best.get("platform") == "unknown"
        and best.get("freshness") in ("fresh", "stale_warning")
    )
    if not best or best.get("freshness") not in ("fresh",) or has_unprobed_platform or best_is_unstructured:
        # Probe all unknown_spa and stale_warning candidates, ranked order
        probe_targets = [
            c for c in ranked
            if c.get("freshness") in ("unknown_spa", "stale_warning", "stale", "unknown", "empty")
            and c.get("platform") in ("civicclerk", "boarddocs", "escribe", "civicplus", "unknown", "legistar", "granicus")
        ]
        any_changed = False
        for target in probe_targets[:6]:  # cap to avoid runaway cost
            upgraded = await deep_probe_candidate(target, city, state, http)
            if upgraded:
                any_changed = True
                ranked = rank_candidates(verified)  # re-rank after upgrade
                best = ranked[0]
                if best.get("freshness") == "fresh" and best.get("platform") != "unknown":
                    break
        # Always re-rank at end of Phase 4 — some probes mutate freshness without
        # returning True (e.g. CivicClerk false-positive downgrade to empty).
        ranked = rank_candidates(verified)
        best = ranked[0] if ranked else None

    # ── Phase 5: Playwright Browser Rendering ─────────────────────────────────
    # Last resort: render JS-heavy pages with a real headless browser.
    # Targets cities still stuck (not fresh) after all other phases.
    # Primary use cases:
    #   - CivicClerk / PrimeGov SPAs where the OData API is unavailable
    #   - Bot-blocked city websites (403) where a real browser bypasses protection
    #   - Unknown platforms that only serve content after JS execution
    playwright_urls_tried: list[str] = []
    if best and best.get("freshness") not in ("fresh",):
        play_targets = [
            c for c in ranked[:4]
            if c.get("freshness") in ("unknown_spa", "stale", "stale_warning", "unknown", "blocked")
        ]
        for target in play_targets:
            playwright_urls_tried.append(target["url"])
            play_result = await probe_with_playwright(target["url"], city, state)
            existing_notes = (target.get("notes") or "").strip()
            if play_result.get("success"):
                if play_result.get("most_recent_date"):
                    target["freshness"] = play_result["freshness"]
                    target["most_recent_date"] = play_result.get("most_recent_date")
                    target["days_since_update"] = play_result.get("days_since_update")
                    target["date_source"] = play_result.get("date_source")
                    note = play_result.get("note", "playwright_rendered")
                    target["notes"] = f"{existing_notes} {note}".strip()
                else:
                    # Rendered with meeting content but no parseable dates
                    if target.get("freshness") == "blocked":
                        target["freshness"] = "unknown_spa"
                    note = play_result.get("note", "Playwright rendered — no dates")
                    target["notes"] = f"{existing_notes} {note}".strip()
                ranked = rank_candidates(verified)
                best = ranked[0]
                if play_result.get("freshness") == "fresh":
                    break  # fresh source found — stop trying
            else:
                error = play_result.get("error", "unknown_error")
                target["notes"] = f"{existing_notes} playwright:failed({error[:60]})".strip()
        ranked = rank_candidates(verified)
        best = ranked[0] if ranked else None

    # ── Phase 5b: City domain crawl with Playwright ───────────────────────────
    # If Phase 5a tried the known candidates but none yielded a fresh date,
    # attempt common agenda URL patterns on the city's own domain.
    # This catches cases where the known platform (e.g. CivicClerk) is dead but
    # the city hosts agendas directly as HTML/PDFs (e.g. /agendas-and-minutes).
    # Limit: 4 paths max, www.domain only, stop after 2 consecutive no-content failures.
    if best and best.get("freshness") not in ("fresh",) and playwright_urls_tried:
        domain = (
            known_sources.get("domain")
            or known_sources.get("civicplus_domain")
        )
        if domain:
            common_agenda_paths = [
                "/agendas-and-minutes",
                "/government/agendas-minutes",
                "/agendas",
                "/city-council/agendas",
            ]
            consecutive_no_content = 0
            for path in common_agenda_paths:
                try_url = f"https://www.{domain}{path}"
                if try_url in playwright_urls_tried or try_url in seen_urls:
                    continue
                playwright_urls_tried.append(try_url)
                play_result = await probe_with_playwright(try_url, city, state)
                if play_result.get("success") and play_result.get("most_recent_date"):
                    new_cand = make_candidate(
                        url=try_url, platform="unknown", source="playwright_probe",
                    )
                    new_cand["freshness"] = play_result["freshness"]
                    new_cand["most_recent_date"] = play_result.get("most_recent_date")
                    new_cand["days_since_update"] = play_result.get("days_since_update")
                    new_cand["date_source"] = play_result.get("date_source")
                    new_cand["notes"] = play_result.get("note", "playwright_crawl")
                    verified.append(new_cand)
                    ranked = rank_candidates(verified)
                    best = ranked[0]
                    if play_result.get("freshness") == "fresh":
                        break
                    consecutive_no_content = 0
                else:
                    # No dates or no meeting content — count failures
                    consecutive_no_content += 1
                    if consecutive_no_content >= 2:
                        break  # domain not serving accessible agenda pages

    # ── Build output ──────────────────────────────────────────────────────────
    elapsed = round(time.monotonic() - start, 1)

    # Detect migration: known platform stale but a different platform is fresh
    migration_detected = False
    known_platform = None
    if "legistar_slug" in known_sources:
        known_platform = "legistar"
    elif "civicplus_domain" in known_sources:
        known_platform = "civicplus"

    if known_platform and best and best.get("platform") != known_platform:
        known_cand = next(
            (c for c in ranked if c.get("source") == "known" and c.get("platform") == known_platform),
            None,
        )
        if known_cand and known_cand.get("freshness") in ("stale", "empty") and best.get("freshness") in ("fresh", "unknown_spa"):
            migration_detected = True

    # Warnings
    warnings = []
    if not best:
        warnings.append("no_source_found")
    elif best.get("freshness") not in ("fresh", "unknown_spa"):
        warnings.append("no_fresh_source_found")
    if migration_detected:
        warnings.append("known_source_stale_migration_likely")
    if best and best.get("freshness") == "blocked":
        warnings.append("blocked_by_bot_protection")

    # Best source record
    best_source = None
    if best:
        best_source = {
            "platform": best["platform"],
            "url": best["url"],
            "display_url": best.get("display_url") or best["url"],
            "freshness": best.get("freshness"),
            "most_recent_date": best.get("most_recent_date"),
            "days_since_update": best.get("days_since_update"),
            "date_source": best.get("date_source"),
            "collection_method": COLLECTION_METHODS.get(best["platform"], "fetch_and_parse"),
            "config": best.get("config") or {},
            "notes": best.get("notes") or "",
        }

    # All candidates output (top 10, _body already stripped by verify_freshness)
    all_candidates_out = []
    for c in ranked[:10]:
        all_candidates_out.append({
            "platform": c.get("platform"),
            "url": c.get("url"),
            "source": c.get("source"),
            "freshness": c.get("freshness"),
            "most_recent_date": c.get("most_recent_date"),
            "rank": c.get("rank"),
            "notes": (c.get("notes") or "").strip(),
        })

    return {
        "city": city,
        "state": state,
        "discovered_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "best_source": best_source,
        "all_candidates": all_candidates_out,
        "migration_detected": migration_detected,
        "warnings": warnings,
        "search_metadata": {
            "tavily_queries": tavily_queries,
            "tavily_results_count": sum(1 for c in ranked if c.get("source") == "tavily"),
            "candidates_checked": len(verified),
            "retry_attempts": retry_attempts,
            "retries_used": retries_used,
            "playwright_urls_tried": playwright_urls_tried,
            "elapsed_sec": elapsed,
        },
    }


# ── Batch runner ───────────────────────────────────────────────────────────────

async def process_city(
    city_info: dict,
    registry: dict,
    tavily: TavilyClient,
    semaphore: asyncio.Semaphore,
    resume: bool = False,
    output_dir: Optional[Path] = None,
) -> dict:
    city = city_info["city"]
    state = city_info["state"]
    slug = city_to_slug(city)
    base_dir = output_dir if output_dir else SOURCES_DIR
    output_path = base_dir / f"{slug}-{state}" / "source.json"

    if resume and output_path.exists():
        try:
            with open(output_path) as f:
                existing = json.load(f)
            bs = existing.get("best_source") or {}
            freshness = bs.get("freshness", "")
            # Re-run cities whose best source is empty — it means we found a
            # platform URL (e.g. a guessed CivicClerk portal) but the API behind
            # it is dead. Don't treat that as "done"; keep searching.
            if freshness == "empty":
                pass  # fall through to re-run
            else:
                print(
                    f"  [skip] {city:<20s} {state}  (existing: {bs.get('platform','?')}  {freshness})"
                )
                return existing
        except (json.JSONDecodeError, OSError):
            pass  # re-run if file is corrupt

    async with semaphore:
        key = f"{city}, {state}"
        known_sources = {
            k: v for k, v in registry.get(key, {}).items()
            if not k.startswith("_")  # skip _description, _usage, notes
        }
        # Remove notes key from known_sources (it's a human note, not a hint)
        known_sources.pop("notes", None)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(headers=headers, timeout=20.0) as http:
            result = await run_source_discover(city, state, known_sources, tavily, http)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(result, f, indent=2)

        bs = result.get("best_source") or {}
        freshness = bs.get("freshness") or "no_source"
        platform = bs.get("platform") or "none"
        elapsed = result["search_metadata"]["elapsed_sec"]
        marker = (
            "+" if freshness == "fresh"
            else "~" if freshness in ("unknown_spa", "stale_warning")
            else "-"
        )
        mrd = bs.get("most_recent_date") or ""
        print(
            f"  [{marker}] {city:<20s} {state}  {elapsed:5.1f}s  {platform:<12s}  "
            f"{freshness:<14s}  {mrd}"
        )
        return result


async def run_batch(
    cities: list[dict],
    registry: dict,
    tavily: TavilyClient,
    resume: bool = False,
    output_dir: Optional[Path] = None,
) -> None:
    semaphore = asyncio.Semaphore(5)  # max 5 concurrent Tavily searches
    total_start = time.monotonic()

    print(f"{'='*78}")
    print(f"Source Discover — {len(cities)} cities  (today: {TODAY})")
    print(f"{'='*78}")
    print(f"  {'[+]'} fresh   [~] unknown_spa/stale_warning   [-] stale/empty/blocked/unknown\n")

    tasks = [
        process_city(c, registry, tavily, semaphore, resume=resume, output_dir=output_dir)
        for c in cities
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = round(time.monotonic() - total_start, 1)

    # Build summary
    summary: dict = {
        "total_cities": len(cities),
        "fresh_sources": 0,
        "unknown_spa": 0,
        "stale_warning": 0,
        "stale": 0,
        "empty": 0,
        "blocked": 0,
        "unknown": 0,
        "no_source": 0,
        "migrations_detected": 0,
        "elapsed_total_sec": total_elapsed,
        "cities": [],
    }

    freshness_key_map = {
        "fresh": "fresh_sources",
        "unknown_spa": "unknown_spa",
        "stale_warning": "stale_warning",
        "stale": "stale",
        "empty": "empty",
        "blocked": "blocked",
        "unknown": "unknown",
        "no_source": "no_source",
    }

    for r in results:
        if isinstance(r, Exception):
            summary["no_source"] += 1
            continue
        bs = r.get("best_source") or {}
        freshness = bs.get("freshness") or "no_source"
        key = freshness_key_map.get(freshness, "unknown")
        summary[key] = summary.get(key, 0) + 1
        if r.get("migration_detected"):
            summary["migrations_detected"] += 1
        summary["cities"].append({
            "city": r["city"],
            "state": r["state"],
            "platform": bs.get("platform"),
            "freshness": freshness,
            "most_recent_date": bs.get("most_recent_date"),
            "warnings": r.get("warnings", []),
        })

    out_dir = output_dir if output_dir else SOURCES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "discovery-summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*78}")
    print(f"SUMMARY — {total_elapsed}s total ({total_elapsed/max(len(cities),1):.1f}s avg/city)")
    print(f"{'='*78}")
    print(f"  Fresh:          {summary['fresh_sources']}")
    print(f"  Unknown SPA:    {summary['unknown_spa']}")
    print(f"  Stale warning:  {summary['stale_warning']}")
    print(f"  Stale:          {summary['stale']}")
    print(f"  Empty:          {summary['empty']}")
    print(f"  Blocked:        {summary['blocked']}")
    print(f"  Unknown:        {summary['unknown']}")
    print(f"  No source:      {summary['no_source']}")
    print(f"  Migrations:     {summary['migrations_detected']}")
    print(f"\n  Per-city JSON → {SOURCES_DIR}/")
    print(f"  Summary        → {summary_path}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Source-discover skill — find freshest agenda source for each city"
    )
    parser.add_argument("--city", help="Run for a single city (e.g. 'Chapel Hill')")
    parser.add_argument("--state", help="Filter by state (NC, OH, or TX)")
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip cities that already have a source.json output file"
    )
    parser.add_argument(
        "--output-dir",
        help="Write results to this directory instead of the default sources/ dir (useful for benchmarking)"
    )
    args = parser.parse_args()

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        print("ERROR: TAVILY_API_KEY not set in environment / .env")
        sys.exit(1)

    from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    registry = storage.read_json(REGISTRY_S3_KEY)
    tavily = TavilyClient(api_key=api_key)

    cities = PILOT_CITIES
    if args.city:
        cities = [c for c in cities if c["city"].lower() == args.city.lower()]
        if not cities:
            print(f"ERROR: '{args.city}' not found in pilot city list")
            sys.exit(1)
    if args.state:
        cities = [c for c in cities if c["state"].upper() == args.state.upper()]
        if not cities:
            print(f"ERROR: No cities found for state '{args.state}'")
            sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else None
    asyncio.run(run_batch(cities, registry, tavily, resume=args.resume, output_dir=output_dir))


if __name__ == "__main__":
    main()
