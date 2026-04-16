"""
source_discover.py — Source-discover skill for all 67 pilot cities.

Finds the freshest, most active agenda source for each city and outputs a
structured JSON record the briefing-collect skill can consume.

Usage:
    uv run python meeting_pipeline/scripts/source_discover.py                       # all cities
    uv run python meeting_pipeline/scripts/source_discover.py --city "Chapel Hill"  # single city
    uv run python meeting_pipeline/scripts/source_discover.py --state NC            # one state
    uv run python meeting_pipeline/scripts/source_discover.py --resume              # skip existing
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv            # use serve_users.csv
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv --skip-existing  # CSV, skip done
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv --city "Tuscaloosa"  # single CSV city

Output:
    meeting_pipeline/sources/{city-slug}-{state}/source.json   per-city records
    meeting_pipeline/sources/discovery-summary.json            batch summary

Algorithm (3-phase with retry loop):
  Phase 1 — Discover candidates (known sources registry + Exa/Tavily + URL probing)
  Phase 2 — Verify freshness by platform
  Phase 3 — Rank and select best source
  Retry loop — up to 2 retries with escalating strategies
  Phase 4 — Deep platform API probes (CivicClerk REST, BoardDocs POST, eSCRIBE,
             CivicPlus year-filter) — only runs if still no fresh source after retries
  Phase 5 — Playwright browser rendering — last resort for JS SPAs (CivicClerk,
             PrimeGov) and bot-blocked pages where httpx cannot extract dates.
             Requires: pip install playwright && playwright install chromium

Search backends:
  Exa   — primary when EXA_API_KEY is set (exa-py package required)
  Tavily — fallback when EXA_API_KEY is not set (always required for domain discovery)
"""

import argparse
import asyncio
import csv
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
from meeting_pipeline.body_validation import validate_body_for_city, VALIDATABLE_PLATFORMS

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Paths (module-level) ───────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_PIPELINE_DIR = _SCRIPT_DIR.parent
SERVE_CSV = _PIPELINE_DIR / "serve_users_unified.csv"
if not SERVE_CSV.exists():
    SERVE_CSV = _PIPELINE_DIR / "serve_users.csv"
_DOTGOV_CSV_PATH = _PIPELINE_DIR / "config" / "dotgov.csv"

# ── State name → abbreviation (mirrors collect_haystaq_batch.py) ──────────────
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
}

# ── Paths ──────────────────────────────────────────────────────────────────────
SOURCES_DIR = _PIPELINE_DIR / "sources"
REGISTRY_S3_KEY = "meeting_pipeline/config/known-sources-registry.json"

# ── Constants ──────────────────────────────────────────────────────────────────
TODAY = date.today()

# ── DotGov domain index ────────────────────────────────────────────────────────
#
# CISA publishes the authoritative list of .gov registrants at:
#   https://github.com/cisagov/dotgov-data/blob/main/current-full.csv
#
# We download it once to config/dotgov.csv and use it as Layer 0 discovery:
# look up the official .gov domain before any web search so that the domain
# crawl (probe_domain_for_agendas) always runs against the right site.

# Org-name fragments that identify department sub-sites, not the city hall
_DEPT_REJECT_KEYWORDS = [
    "court", "police department", "police dept", "sheriff", "fire department",
    "fire district", "library", "city marshal", "marshal's", "jail",
    "district attorney", "prosecutor", "ems ", "utilities", "water district",
    "sewer", "transit", "parking authority", "permit office", "airport",
]

# Org-name prefixes that confirm this is the main municipal government
_CITY_GOV_PREFIXES = (
    "city of ", "town of ", "village of ", "borough of ",
    "township of ", "city and county of ",
)

_DOTGOV_INDEX: dict[tuple[str, str], list[dict]] | None = None


def _load_dotgov_index() -> dict[tuple[str, str], list[dict]]:
    """Load CISA DotGov CSV → (city_lower, state_upper) → [{domain, org}, ...] index.

    Cached in module-level variable after first call. Returns empty dict if
    the CSV file is missing (discovery continues without DotGov data).
    """
    global _DOTGOV_INDEX
    if _DOTGOV_INDEX is not None:
        return _DOTGOV_INDEX

    index: dict[tuple[str, str], list[dict]] = {}
    if not _DOTGOV_CSV_PATH.exists():
        _DOTGOV_INDEX = index
        return index

    import csv as _csv
    with _DOTGOV_CSV_PATH.open() as f:
        for row in _csv.DictReader(f):
            if row.get("Domain type", "") not in ("City", "City - Election"):
                continue
            domain = row.get("Domain name", "").strip().lower()
            org = row.get("Organization name", "").strip()
            city = row.get("City", "").strip()
            state = row.get("State", "").strip().upper()
            if not domain or not city or not state:
                continue
            # Filter out department sub-domains
            org_lower = org.lower()
            if any(kw in org_lower for kw in _DEPT_REJECT_KEYWORDS):
                continue
            key = (city.lower(), state)
            index.setdefault(key, []).append({"domain": domain, "org": org})

    _DOTGOV_INDEX = index
    return index


def lookup_gov_domain(city: str, state: str) -> str | None:
    """Return the official .gov domain for a city, or None if not in CISA DotGov.

    Prefers the main city-hall domain when multiple .gov registrants exist for
    the same city (e.g. police dept, fire dept, and city hall all registered).

    Cities not in the DotGov database — common for IL, IA, KS, MI, and others
    that use .org/.com/.net — return None; the pipeline falls back to web search.
    """
    index = _load_dotgov_index()
    hits = index.get((city.lower(), state.upper()), [])
    if not hits:
        return None

    city_slug = city.lower().replace(" ", "").replace(".", "").replace("'", "")
    state_lower = state.lower()

    def _score(entry: dict) -> int:
        d = entry["domain"]
        org_lower = entry["org"].lower()
        score = 0
        # Strongly prefer "City of X" / "Town of X" named orgs
        if any(org_lower.startswith(pfx) for pfx in _CITY_GOV_PREFIXES):
            score += 100
        # Prefer domain containing city+state slug (e.g. austintx.gov)
        d_clean = d.replace("-", "").replace(".", "")
        if (city_slug + state_lower) in d_clean:
            score += 20
        elif city_slug in d_clean:
            score += 10
        # Prefer shorter domains (main site, not a sub-dept like falmouthpolicema.gov)
        score -= len(d)
        return score

    return max(hits, key=_score)["domain"]


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
    # School boards / ISDs — extremely common false positives from Tavily/probes
    " isd public",        # "Duncanville ISD Public View", "Killeen ISD Public"
    "isd board",
    "independent school district",
    "school district board",
    "school board meeting",
    "school district",    # catches BoardDocs pages titled "Lima City School District"
    "unified school district",
    "community school district",
    # Other non-city-council entities (also caught on BoardDocs page titles)
    "public library board",
    "library board of trustees",
    "library advisory board",  # e.g. Duncanville Library Advisory Board
    "fire district",
    "water district",
    "park district",
    "health district",
    # Economic development / municipal corporations — active but not city council
    "economic development corporation",
    "economic development commission",
    "community development authority",
]

# Wrong-entity keywords specifically for BoardDocs page-title validation.
# These appear in the <title> or main header of pages that belong to school
# districts or other non-city entities that share the same city-name slug.
BOARDDOCS_WRONG_ENTITY_KEYWORDS = [
    "school", "school district", "isd", "library", "fire district",
    "water district", "park district", "health district",
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


# State abbreviation → full name (used in search queries for disambiguation)
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


# URL patterns that are never official municipal agenda sources
REJECT_URL_PATTERNS = [
    "facebook.com/",
    "youtube.com/watch",
    "youtube.com/@",
    "youtube.com/channel",
    "youtube.com/user",
    "youtu.be/",
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
    "politico.com/",
    "journaltimes.com/",
    "jsonline.com/",
    "jsonline.com/",
    "mlive.com/",
    "nytimes.com/",
    "washingtonpost.com/",
    "foxnews.com/",
    "nbcnews.com/",
    "abcnews.go.com/",
    "cbsnews.com/",
    "usatoday.com/",
    "apnews.com/",
    "reuters.com/",
    "racinecountyeye.com/",  # local news blog
    "thebuckeyeflame.com/",  # Ohio news blog
    "cantonrep.com/",  # Ohio newspaper
    "mchenrytimes.com/",  # local news — not official source
    "shawlocal.com/",  # Shaw Media local news network
    "dailyherald.com/",  # Chicago suburb news
    "triblocal.com/",  # Tribune local news blog
    "suburbanchicagoland.com/",  # local news aggregator
    # Local TV news stations — common DDG false positives
    "wmtv15news.com/",
    "wtmj.com/",
    "fox6now.com/",
    "wisn.com/",
    "witi.com/",
    "tmj4.com/",
    "wkow.com/",
    "channel3000.com/",
    "wbay.com/",
    "weau.com/",
    "waow.com/",
    "wfrv.com/",
    "wgba.com/",
    "wearegreenbay.com/",
    "wsaw.com/",
    "wkbt.com/",
    "wqow.com/",
    "wxow.com/",
    "wlax.com/",
    "nbc15.com/",
    "abc27.com/",
    "local3news.com/",
    "local10.com/",
    "kxan.com/",
    "kvue.com/",
    "khou.com/",
    "click2houston.com/",
    "abc13.com/",
    "cbsaustin.com/",
    "wfaa.com/",
    "nbcdfw.com/",
    "cbslocal.com/",
    "myfoxny.com/",
    "wral.com/",
    "abc11.com/",
    "wcnc.com/",
    "wsoc-tv.com/",
    "wbtv.com/",
    "wccb.com/",
    "wtvd.com/",
    "wbns10tv.com/",
    "nbc4i.com/",
    "10tv.com/",
    "local12.com/",
    "fox19.com/",
    "wkrc.com/",
    "wcpo.com/",
    "wlwt.com/",
    "whio.com/",
    "daytondailynews.com/",
    "cincinnaticitybeat.com/",
    "dispatch.com/",
    "cleveland.com/",
    "familydestinationsguide.com/",  # travel guide — not gov source
    "towleroad.com/",  # news blog
    # Archive / academic sites without current meeting data
    "archive.org/details/",
    # Travel / lifestyle sites
    "tripadvisor.com/",
    "travelchannel.com/",
    # ZIP code / demographic lookup sites — not government sources
    "zip-codes.com/",
    "unitedstateszipcodes.org/",
    "zipcodestogo.com/",
    "zipdatamaps.com/",
    "city-data.com/",
    "bestplaces.net/",
    "niche.com/",
    "areavibes.com/",
    "neighborhoodscout.com/",
    "homefacts.com/",
    "point2homes.com/",
    "datausa.io/",
    "census.gov/",
    # Real estate sites — often have city data but not gov sources
    "zillow.com/",
    "realtor.com/",
    "trulia.com/",
    "redfin.com/",
    "movoto.com/",
    # Hotel / travel booking sites — not government sources
    "agoda.com/",
    "booking.com/",
    "hotels.com/",
    "expedia.com/",
    "airbnb.com/",
    "travelocity.com/",
    "orbitz.com/",
    "kayak.com/",
    # Tech / corporate support sites — common DDG noise
    "microsoft.com/",
    "support.microsoft.com/",
    "learn.microsoft.com/",
    "apple.com/",
    "support.apple.com/",
    "google.com/",
    "support.google.com/",
    "amazon.com/",
    "yelp.com/",
    "yellowpages.com/",
    "whitepages.com/",
    "mapquest.com/",
    "tripadvisor.com/",
    "indeed.com/",
    "glassdoor.com/",
    "reddit.com/",
    "quora.com/",
    "stackoverflow.com/",
    "github.com/",
    "wikipedia.org/",
    # Document / presentation sharing sites — not gov sources
    "slideshare.net/",
    "scribd.com/",
    "issuu.com/",
    "docsend.com/",
    # Generic news aggregators / scrapers
    "readkong.com/",
    "yahoo.com/news",
    "yahoo.com/local",
    "msn.com/",
    "aol.com/",
    # Petition / crowdfunding / user-generated content
    "change.org/",
    "gofundme.com/",
    "kickstarter.com/",
    # AI-generated encyclopedias / content farms
    "grokipedia.com/",
    "dbpedia.org/",
    # Generic city-data / directory sites — not official sources
    "city2map.com/",
    "citydata.us/",
    "onlyinyourstate.com/",
    # Apartment / lifestyle / travel blogs
    "wellnesscoachingforlife.com/",
    "familydestinationsguide.com/",
    "roadtrippers.com/",
    "theroamingsole.com/",
    # Sports / entertainment
    "espn.com/",
    "nbcsports.com/",
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


# ── City list helpers ──────────────────────────────────────────────────────────

def get_serve_csv_cities() -> list[dict]:
    """Return deduplicated cities from serve_users.csv.

    Mirrors the same logic as collect_haystaq_batch.py so both scripts
    cover the same population.  State/Region column has full state names
    (e.g. "Ohio"), which are converted to 2-letter abbreviations.

    Also extracts the "Meeting URL" column as a seed URL for discovery.
    When present, this URL is added to known_sources as custom_agenda_url,
    which ensures the discovery probes the city's own page even when search
    engines return poor results for that city.
    """
    if not SERVE_CSV.exists():
        print(f"ERROR: {SERVE_CSV} not found")
        sys.exit(1)
    seen: set[tuple[str, str]] = set()
    cities: list[dict] = []
    for row in csv.DictReader(SERVE_CSV.open()):
        # Support unified CSV (lowercase columns) and legacy formats
        city = (row.get("city") or row.get("City") or "").strip()
        state_raw = (row.get("state") or row.get("State") or row.get("State/Region") or "").strip()
        if not city or not state_raw:
            continue
        state = STATE_ABBREVS.get(
            state_raw,
            state_raw[:2].upper() if len(state_raw) > 2 else state_raw.upper(),
        )
        key = (city, state)
        if key in seen:
            continue
        seen.add(key)
        entry: dict = {"city": city, "state": state}
        # Carry the CSV meeting URL as a discovery seed (used as custom_agenda_url)
        meeting_url = row.get("Meeting URL", row.get("meeting_url", "")).strip()
        if meeting_url and meeting_url.startswith("http"):
            entry["csv_meeting_url"] = meeting_url
        cities.append(entry)
    return cities


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


def is_wrong_city(url: str, title: str, city: str, state: str = "") -> bool:
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
    # If state is provided, reject snippets that explicitly name a different state.
    # e.g. searching for Louisville OH — reject "City of Greenland, Arkansas"
    if state:
        expected_state_name = _STATE_NAMES.get(state.upper(), "").lower()
        state_abbrev = state.upper()
        # Check if a different state name appears explicitly in the text
        for abbrev, name in _STATE_NAMES.items():
            if abbrev == state_abbrev:
                continue
            name_lower = name.lower()
            # Only trigger if the other state name appears as a standalone word
            # to avoid false positives (e.g. "New" in "New York" vs "New Britain, CT")
            import re as _re
            if _re.search(rf'\b{_re.escape(name_lower)}\b', combined):
                return True

        # For .gov domains, check if the domain embeds a different state abbreviation.
        # Pattern: {city}{state_abbrev}.gov — e.g. westmorelandtn.gov when searching WI.
        # Only fires when city name is NOT in the domain (avoids false positives).
        _parsed_url = urlparse(url)
        _netloc = _parsed_url.netloc.lower().replace("www.", "")
        if _netloc.endswith(".gov") and "." not in _netloc[:-4]:
            _domain_base = _netloc[:-4]  # strip .gov
            if len(_domain_base) >= 3:
                _embedded = _domain_base[-2:].upper()
                if _embedded in _STATE_NAMES and _embedded != state_abbrev:
                    # Don't reject if the target city name is in the domain
                    _city_in_dom = city.lower().replace(" ", "") in _domain_base
                    if not _city_in_dom:
                        return True  # .gov domain is in a different state

        # Check if a different state name is embedded in the URL domain (no word boundary).
        # Catches e.g. louisvillenebraska.com (Nebraska embedded) when searching Louisville OH.
        # Only applies to non-.gov domains since .gov is already checked above.
        _dom_check = urlparse(url).netloc.lower().replace("www.", "").replace("-", "").replace(".", "")
        if _dom_check and not _dom_check.endswith("gov"):
            for abbrev, name in _STATE_NAMES.items():
                if abbrev == state_abbrev:
                    continue
                state_slug = name.lower().replace(" ", "")  # e.g. "nebraska", "newmexico"
                if len(state_slug) >= 5 and state_slug in _dom_check:
                    return True  # wrong state embedded in domain

        # For Municode shared-platform URLs, validate the cid parameter's embedded
        # state abbreviation matches the target state, AND that the city name prefix
        # in the cid roughly matches the target city (catches same-state wrong-city:
        # e.g. cid=MANORTX when searching Palestine TX → "manor" ≠ "palestine").
        if "meetings.municode.com" in url or "municodemeetings.com" in url:
            try:
                from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
                _qs = _parse_qs(_urlparse(url).query)
                _cid = (_qs.get("cid") or [""])[0].upper()
                if _cid and len(_cid) >= 3:
                    _cid_state = _cid[-2:]
                    if _cid_state in _STATE_NAMES and _cid_state != state_abbrev:
                        return True  # Municode client is in a different state
                    # City name check: strip trailing state abbrev (if present), then see if
                    # the target city name appears in the remaining cid prefix.
                    _cid_city = (_cid[:-2] if _cid_state in _STATE_NAMES else _cid).lower()
                    # Also strip "cityof" / "city" prefix from cid for matching
                    _cid_city_stripped = _cid_city.replace("cityof", "").replace("city", "").replace("townof", "").replace("villageof", "")
                    _city_slug = city.lower().replace(" ", "").replace("-", "")
                    if _cid_city and _city_slug and len(_city_slug) >= 4:
                        if not (_city_slug in _cid_city or _city_slug in _cid_city_stripped or
                                _cid_city in _city_slug or _cid_city_stripped in _city_slug):
                            return True  # Municode cid city prefix doesn't match target city
            except Exception:
                pass

    return False


def is_wrong_entity(text: str) -> bool:
    """Return True if text matches a global wrong-entity pattern (school, library, etc.).

    Use this to filter candidates from ANY discovery source (probe, known, search).
    Unlike is_wrong_city() this does NOT require a city name — it only checks the
    global WRONG_ENTITY_PATTERNS that indicate a non-city-council governing body.
    """
    lower = text.lower()
    return any(pat.lower() in lower for pat in WRONG_ENTITY_PATTERNS)


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
            # Apply global wrong-entity filter to known-source candidates too.
            # The registry entry URL itself may point to a school board — check
            # the URL against WRONG_ENTITY_PATTERNS before accepting.
            if is_wrong_entity(r["url"]):
                r["freshness"] = "wrong_entity"
                r["wrong_entity_reason"] = "url matches wrong-entity pattern"
                # Still include so it shows in all_candidates output for debugging,
                # but it will score 0 and never become best_source.
            # Also check fetched body for BoardDocs candidates — the URL alone
            # doesn't reveal whether the board is a school district or city council.
            # verify_freshness will run a deeper check, but pre-flagging here avoids
            # wrong-entity pages poisoning the has_recognized guard in run_source_discover.
            elif r.get("platform") == "boarddocs" and r.get("_body"):
                body_text = r["_body"]
                title_m = re.search(r"<title[^>]*>([^<]+)</title>", body_text, re.IGNORECASE)
                page_title = title_m.group(1).strip().lower() if title_m else ""
                if any(kw in page_title for kw in BOARDDOCS_WRONG_ENTITY_KEYWORDS):
                    r["freshness"] = "wrong_entity"
                    r["wrong_entity_reason"] = f"boarddocs page title: '{page_title[:60]}'"
            candidates.append(r)
    return candidates


def _search_results_to_candidates(
    raw_results: list[dict],
    city: str,
    source_label: str,
    state: str = "",
) -> list[dict]:
    """Convert a list of {url, title, content} dicts to verified candidate dicts.

    Shared by both Tavily and Exa backends so entity filtering and snippet
    date extraction happen identically regardless of search provider.
    """
    candidates = []
    for r in raw_results:
        url = r.get("url", "")
        title = r.get("title", "")
        content = r.get("content", "")
        if not url:
            continue
        # Include snippet in wrong-city/entity check — content often names city/state
        if is_wrong_city(url, f"{title} {content}", city, state=state):
            continue
        if is_non_agenda_url(url):
            continue
        platform = detect_platform(url)
        url = normalize_platform_url(url, platform)

        # For path-detected platforms (CivicPlus, Novus) where each city owns its own
        # domain, validate that the target city name appears in the domain.
        # Truly multi-tenant platforms (Legistar, BoardDocs, Granicus, Municode, CivicClerk)
        # are exempt because the city name is embedded in paths/params, not the TLD domain.
        _PATH_DETECTED_PLATFORMS = {"civicplus", "novus", "primegov", "diligent"}
        if platform in _PATH_DETECTED_PLATFORMS and city:
            _netloc = urlparse(url).netloc.lower().replace("www.", "")
            _domain_clean = _netloc.replace("-", "").replace(".", "")
            _city_slug = city.lower().replace(" ", "")
            if _city_slug not in _domain_clean and city.lower() not in _domain_clean:
                continue  # CivicPlus/Novus site belongs to a different city

        # For all other unknown-platform results, require the city name to appear
        # in the domain OR in the page title. Content is too noisy (adjacent cities
        # mention each other), but domain+title is reliable.
        # Multi-tenant platforms (legistar, boarddocs, granicus, etc.) are exempt.
        _MULTITENANT_PLATFORMS = {"legistar", "boarddocs", "granicus", "municode", "civicclerk", "escribe"}
        if platform == "unknown" and city:
            _netloc = urlparse(url).netloc.lower().replace("www.", "")
            _domain_clean = _netloc.replace("-", "").replace(".", "")
            _city_slug = city.lower().replace(" ", "")
            _title_lower = title.lower()
            # Accept if city name is in domain OR in page title
            city_in_domain = _city_slug in _domain_clean or city.lower() in _domain_clean
            city_in_title = _city_slug in _title_lower.replace(" ", "") or city.lower() in _title_lower
            if not city_in_domain and not city_in_title:
                continue  # City absent from domain and title — skip

        # Build note from title + snippet prefix
        note_parts = [title[:80]]
        if content:
            note_parts.append(content[:100])
        c = make_candidate(
            url=url, platform=platform, source=source_label,
            notes=" | ".join(p for p in note_parts if p).strip(" |")[:200],
        )

        # Pre-populate freshness from snippet text.
        # Handles JS SPAs and bot-blocked pages where httpx can't fetch dates directly.
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
    return candidates


async def discover_from_duckduckgo(
    city: str,
    state: str,
    query: Optional[str] = None,
    max_results: int = 8,
) -> tuple[list[dict], str]:
    """Run a DuckDuckGo web search. Return (candidates, query_used).

    DDG requires no API key and often has better coverage of small government
    sites than Exa or Tavily. Used as the first search backend.
    Requires: duckduckgo-search package.
    """
    if query is None:
        query = f"{city} {state} city council agenda minutes"

    try:
        from ddgs import DDGS  # type: ignore

        results = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: list(DDGS().text(query, max_results=max_results))
            ),
            timeout=30.0,
        )
    except ImportError:
        return [], query
    except Exception:
        return [], query

    raw: list[dict] = [
        {
            "url": r.get("href", ""),
            "title": r.get("title", ""),
            "content": r.get("body", "")[:500],
        }
        for r in results
        if r.get("href")
    ]

    candidates = _search_results_to_candidates(raw, city, "ddg", state=state)
    return candidates, query


async def discover_from_exa(
    city: str,
    state: str,
    query: Optional[str] = None,
) -> tuple[list[dict], str]:
    """Run one Exa search. Return (candidates, query_used).

    Used as primary search backend when EXA_API_KEY is set.
    Falls back to empty list (not an error) when Exa is unavailable or key missing.
    Requires: exa-py package (already in pyproject.toml optional[discovery]).
    """
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return [], ""

    if query is None:
        query = (
            f"{city} {state} city council agenda minutes "
            "site:gov OR site:granicus.com OR site:legistar.com"
        )

    try:
        from exa_py import Exa  # type: ignore
        exa = Exa(api_key=api_key)
        result = await asyncio.wait_for(
            asyncio.to_thread(
                exa.search_and_contents,
                query,
                num_results=5,
                use_autoprompt=False,
                text=True,
            ),
            timeout=30.0,
        )
        # Map Exa result objects to the uniform {url, title, content} dict format
        raw: list[dict] = [
            {
                "url": r.url,
                "title": r.title or "",
                "content": (r.text or "")[:500],
            }
            for r in (result.results or [])
        ]
    except ImportError:
        return [], query
    except Exception:
        return [], query

    candidates = _search_results_to_candidates(raw, city, "exa", state=state)
    return candidates, query


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

    raw: list[dict] = [
        {
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "content": r.get("content", ""),
        }
        for r in result.get("results", [])
    ]
    candidates = _search_results_to_candidates(raw, city, "tavily", state=state)
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

    # Pass 1: prefer .gov domains containing the city name, or city-named domains
    for r in candidates_found:
        url = r.get("url", "")
        if not url:
            continue
        if is_wrong_city(url, (r.get("title") or "") + " " + (r.get("content") or ""), city, state=state):
            continue
        if is_non_agenda_url(url):
            continue
        domain = urlparse(url).netloc.replace("www.", "")
        domain_clean = domain.lower().replace("-", "").replace(".", "")
        # Require city name in domain OR the domain must be city-named (.gov with city)
        if city_lower in domain_clean:
            return domain

    # Pass 2: .gov domains that pass wrong-city check (city name already in domain filter above)
    for r in candidates_found:
        url = r.get("url", "")
        if not url:
            continue
        if is_wrong_city(url, (r.get("title") or "") + " " + (r.get("content") or ""), city, state=state):
            continue
        if is_non_agenda_url(url):
            continue
        domain = urlparse(url).netloc.replace("www.", "")
        if domain.endswith(".gov"):
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


# Common US city government domain patterns — in rough order of likelihood.
# These let us find the official city website WITHOUT depending on search engine indexing.
# Many small cities don't rank in Exa/Tavily for "city council agenda" but their
# homepage IS reachable at one of these predictable domains.
_CITY_DOMAIN_PATTERNS = [
    "{city_hyphen}-{state_lower}.gov",         # janesville-wi.gov  ← most common
    "{city_slug}{state_lower}.gov",             # janesviellewi.gov
    "ci.{city_hyphen}.{state_lower}.us",        # ci.janesville.wi.us
    "ci.{city_slug}.{state_lower}.us",          # ci.janesville.wi.us
    "cityof{city_slug}.gov",                    # cityofjanesville.gov
    "cityof{city_slug}.{state_lower}.gov",      # cityofjanesville.wi.gov
    "{city_slug}.gov",                          # janesville.gov
    "{city_hyphen}.gov",                        # janesville.gov  (same when no spaces)
    "{city_slug}city.gov",                      # janesvielleecity.gov
    "city.{city_slug}.{state_lower}.us",        # city.janesville.wi.us
    "{city_hyphen}-city.{state_lower}.gov",     # janesville-city.wi.gov
]


async def probe_city_domain_patterns(
    city: str,
    state: str,
    http: httpx.AsyncClient,
) -> Optional[str]:
    """
    Probe common US city government domain patterns directly.

    Bypasses search engine coverage gaps for small cities — their homepage is
    reachable at a predictable domain even when their agenda pages aren't indexed.
    Runs all patterns in parallel with a short timeout; returns the first domain
    that responds with a valid city website (HTTP 200 + city name in body).

    Returns the domain string (e.g. "janesville-wi.gov"), or None.
    """
    state_lower = state.lower()
    city_slug = city.lower().replace(" ", "").replace(".", "").replace("'", "")
    city_hyphen = city.lower().replace(" ", "-").replace(".", "").replace("'", "")
    city_lower = city.lower()

    seen: set[str] = set()
    domains: list[str] = []
    for pattern in _CITY_DOMAIN_PATTERNS:
        domain = pattern.format(
            city_slug=city_slug, city_hyphen=city_hyphen, state_lower=state_lower,
        )
        if domain not in seen:
            seen.add(domain)
            domains.append(domain)

    state_full = _STATE_NAMES.get(state, state).lower()

    async def check_domain(domain: str) -> Optional[str]:
        for base_url in [f"https://www.{domain}", f"https://{domain}"]:
            status, body = await safe_fetch(http, base_url, timeout=6.0)
            body_lower = body.lower() if body else ""
            # Require city name AND state name/abbrev in body to avoid wrong-city matches
            # (e.g. "Lexington, KY" domain showing up for "Lexington, NC")
            city_in_body = city_lower in body_lower
            state_in_body = state_lower in body_lower or state_full in body_lower
            if status == 200 and len(body) > 1500 and city_in_body and state_in_body:
                return domain
            # 403 = site exists and blocks bots — accept if city+state confirmed
            if status == 403 and city_in_body and state_in_body:
                return domain
        return None

    results = await asyncio.gather(*(check_domain(d) for d in domains), return_exceptions=True)
    return next((r for r in results if isinstance(r, str)), None)


async def playwright_crawl_city_site(
    domain: str,
    city: str,
    state: str,
    expected_body: str = "",
) -> list[dict]:
    """
    Use Playwright to crawl a city's official website and find agenda/meeting pages.

    Handles JS-rendered city CMSes and non-standard path structures where
    httpx probing of common paths (/AgendaCenter, /meetings) returns nothing.

    Strategy:
    1. Load the city homepage
    2. Find navigation links containing government/council/meeting keywords
    3. Follow up to MAX_NAV_LINKS links (one level deep)
    4. On each page, check for dates + meeting content + entity validation
    5. Return any pages that look like valid agenda sources

    Returns a list of candidate dicts (same shape as make_candidate).
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        return []

    MAX_NAV_LINKS = 8
    NAV_KEYWORDS = re.compile(
        r"\b(agenda|minute|meeting|council|commission|clerk|legislative|government|city hall)\b",
        re.IGNORECASE,
    )
    # Pre-filter links whose text explicitly names a non-council governing body.
    # Prevents following "Civil Service Commission", "Planning Commission", etc.
    # which may have fresh dates but are the wrong entity.
    WRONG_BODY_LINK_KEYWORDS = re.compile(
        r"\b(civil service|planning commission|zoning board|historic preservation|"
        r"ethics board|library board|park board|fire district|water board|"
        r"advisory board|school board|board of education|port authority)\b",
        re.IGNORECASE,
    )
    candidates: list[dict] = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )
                page = await ctx.new_page()
                home_url = f"https://www.{domain}"
                try:
                    await page.goto(home_url, wait_until="networkidle", timeout=20000)
                except Exception:
                    try:
                        home_url = f"https://{domain}"
                        await page.goto(home_url, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        return []

                # Extract all links from the homepage
                links = await page.evaluate("""() =>
                    Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({text: a.textContent.trim().slice(0, 80), href: a.href}))
                    .filter(l => l.href.startsWith('http'))
                """)

                # Filter to links that look like council/meeting pages
                nav_links = [
                    l for l in links
                    if NAV_KEYWORDS.search(l["text"]) or NAV_KEYWORDS.search(l["href"])
                ][:MAX_NAV_LINKS]

                for link in nav_links:
                    link_url = link["href"]
                    # Skip external domains, PDFs, and already-known platforms
                    parsed = urlparse(link_url)
                    if domain not in parsed.netloc:
                        continue
                    if link_url.lower().endswith(".pdf"):
                        continue
                    # Skip links whose text explicitly names a non-council body
                    if WRONG_BODY_LINK_KEYWORDS.search(link.get("text", "")) or \
                            WRONG_BODY_LINK_KEYWORDS.search(link_url):
                        continue

                    try:
                        await page.goto(link_url, wait_until="networkidle", timeout=15000)
                    except Exception:
                        continue

                    try:
                        body_text = await page.inner_text("body")
                    except Exception:
                        continue

                    if not body_text or len(body_text) < 200:
                        continue

                    # Entity validation — skip if this looks like the wrong entity
                    if expected_body:
                        body_text_lower = body_text.lower()
                        wrong_entity_here = any(
                            kw in body_text_lower for kw in WRONG_ENTITY_PATTERNS
                        )
                        expected_here = expected_body.lower() in body_text_lower
                        if wrong_entity_here and not expected_here:
                            continue

                    dates = extract_dates(body_text)
                    has_meeting_kw = bool(re.search(
                        r"\b(agenda|minutes|meeting|council|vote|ordinance)\b",
                        body_text, re.IGNORECASE,
                    ))
                    if not (dates and has_meeting_kw):
                        continue

                    # Check if the page is on a known platform
                    platform = detect_platform(link_url)
                    most_recent = dates[0] if dates else None
                    c = make_candidate(
                        url=link_url, platform=platform, source="playwright_crawl",
                        http_status=200,
                        notes=f"playwright_crawl:{domain} link='{link['text'][:50]}'",
                    )
                    if most_recent:
                        c["most_recent_date"] = most_recent.isoformat()
                        c["days_since_update"] = (TODAY - most_recent).days
                        c["freshness"] = classify_freshness(most_recent)
                        c["date_source"] = "playwright_crawl"
                    candidates.append(c)

            finally:
                await browser.close()
    except Exception:
        pass

    return candidates


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

        # BoardDocs: validate the page belongs to a city council, not a school district.
        # BoardDocs URL slugs are first-come-first-served — lima/oh resolves to Lima City
        # School District, not Lima city government.  Extract the <title> or first <h1>
        # from the fetched HTML and reject if it contains school/district/ISD keywords.
        if platform == "boarddocs" and body:
            body_lower = body.lower()
            # Extract page title
            title_match = re.search(r"<title[^>]*>([^<]+)</title>", body, re.IGNORECASE)
            page_title = title_match.group(1).strip().lower() if title_match else ""
            # Also check first h1
            h1_match = re.search(r"<h1[^>]*>([^<]+)</h1>", body, re.IGNORECASE)
            page_h1 = h1_match.group(1).strip().lower() if h1_match else ""
            entity_text = f"{page_title} {page_h1}"
            wrong_entity_kw = next(
                (kw for kw in BOARDDOCS_WRONG_ENTITY_KEYWORDS if kw in entity_text),
                None,
            )
            if wrong_entity_kw:
                # Don't add as a candidate — this BoardDocs URL belongs to a school
                # district or other non-city entity. Skip silently.
                continue

        # Apply global wrong-entity filter to all probe candidates.
        # Previously this filter only ran on Tavily/search results; now it also
        # covers candidates generated by URL probing.
        if is_wrong_city(url, "", city, state=state):
            continue

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

async def verify_freshness(candidate: dict, http: httpx.AsyncClient, city: str = "") -> dict:
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

                    # Wrong-city check: if body names indicate a different city's
                    # Metropolitan government, mark as wrong_city.
                    # Catches e.g. Louisville OH hitting the Louisville KY Legistar slug
                    # (Louisville KY is "Louisville Metro Government" — top body is
                    # "Metro Council" not "City Council").
                    if city and slug:
                        bodies_url = f"https://webapi.legistar.com/v1/{slug}/bodies"
                        _, bodies_body = await safe_fetch(http, bodies_url, timeout=10.0)
                        try:
                            all_bodies = json.loads(bodies_body) if bodies_body else []
                            all_body_names = [b.get("BodyName", "") for b in all_bodies if b.get("BodyName")]
                            body_names_lower = [n.lower() for n in all_body_names]
                            # Signal 1: top-level body is "Metro Council" (Louisville KY pattern)
                            # — indicates this is a Metropolitan government, not a small city
                            has_metro_council = any("metro council" in n for n in body_names_lower)
                            # Signal 2: no body name matches "city council" (the expected gov body)
                            # without also being a "metro" body
                            has_city_council = any(
                                "city council" in n and "metro" not in n
                                for n in body_names_lower
                            )
                            # Also check city-specific wrong-city patterns in body names
                            wrong_city_patterns = WRONG_CITY_PATTERNS.get(city, [])
                            body_text_combined = " ".join(body_names_lower)
                            wrong_match = next(
                                (p for p in wrong_city_patterns if p.lower() in body_text_combined),
                                None,
                            )
                            if (has_metro_council and not has_city_council) or wrong_match:
                                top_names = all_body_names[:5]
                                candidate["freshness"] = "wrong_city"
                                candidate["notes"] = (
                                    f"Legistar slug '{slug}' appears to be a different city's Metro govt"
                                    f" (top bodies: {top_names})"
                                )
                                return candidate
                        except (json.JSONDecodeError, TypeError):
                            pass  # bodies check is best-effort

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
                # CivicPlus loads categories via AJAX, not static hrefs.
                # Structure: <label for="N"><input type="checkbox" value="N">City Council</label>
                # Extract the category ID from the label wrapping or adjacent to "City Council".
                _COUNCIL_LABEL_RE = re.compile(
                    r'(?:city[- ]?council|city[- ]?commission|council[- ]?meeting)',
                    re.IGNORECASE,
                )
                ajax_cat_id = None
                # Pattern A: <label for="N" ...>...(City Council text)...</label>
                for label_m in re.finditer(r'<label[^>]+for=["\']?(\d+)["\']?[^>]*>(.*?)</label>', body, re.IGNORECASE | re.DOTALL):
                    text = re.sub(r'<[^>]+>', '', label_m.group(2))
                    if _COUNCIL_LABEL_RE.search(text):
                        ajax_cat_id = label_m.group(1)
                        break
                if not ajax_cat_id:
                    # Pattern B: <option value="N">City Council</option>
                    for opt_m in re.finditer(r'<option[^>]+value=["\']?(\d+)["\']?[^>]*>(.*?)</option>', body, re.IGNORECASE | re.DOTALL):
                        if _COUNCIL_LABEL_RE.search(opt_m.group(2)):
                            ajax_cat_id = opt_m.group(1)
                            break

                if ajax_cat_id:
                    parsed_url = urlparse(url)
                    base = f"{parsed_url.scheme}://{parsed_url.netloc}"
                    ajax_url = f"{base}/AgendaCenter/UpdateCategoryList"
                    headers = {
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": url,
                    }
                    for yr in [TODAY.year, TODAY.year - 1]:
                        try:
                            ajax_resp = await http.post(
                                ajax_url,
                                content=f"catID={ajax_cat_id}&year={yr}",
                                headers=headers,
                                timeout=12.0,
                            )
                            if ajax_resp.status_code == 200 and ajax_resp.text:
                                ajax_dates = extract_dates(ajax_resp.text)
                                if ajax_dates:
                                    most_recent = ajax_dates[0]
                                    candidate["most_recent_date"] = most_recent.isoformat()
                                    candidate["days_since_update"] = (TODAY - most_recent).days
                                    candidate["date_source"] = "civicplus_ajax_council"
                                    candidate["freshness"] = classify_freshness(most_recent)
                                    candidate["notes"] = (candidate.get("notes") or "") + f" ajax_council_cat:{ajax_cat_id}"
                                    return candidate
                        except Exception:
                            pass

                # No City Council category link found — date will be from all categories.
                # Mark so we can downgrade if the all-category dates look "fresh"
                # (advisory boards may be active while city council is stale).
                candidate["notes"] = (candidate.get("notes") or "") + " (no_council_category — dates may be advisory-only)"
                candidate["_no_council_category"] = True

    # BoardDocs: validate that the page belongs to a city council, not a school district.
    # BoardDocs URL slugs are first-registered wins — go.boarddocs.com/oh/lima → Lima City
    # School District, not Lima city government.  Check the page <title> and first <h1>
    # for wrong-entity keywords before accepting the candidate.
    if platform == "boarddocs" and body:
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", body, re.IGNORECASE)
        page_title = title_m.group(1).strip().lower() if title_m else ""
        h1_m = re.search(r"<h1[^>]*>([^<]+)</h1>", body, re.IGNORECASE)
        page_h1 = h1_m.group(1).strip().lower() if h1_m else ""
        entity_text = f"{page_title} {page_h1}"
        wrong_kw = next(
            (kw for kw in BOARDDOCS_WRONG_ENTITY_KEYWORDS if kw in entity_text),
            None,
        )
        if wrong_kw:
            candidate["freshness"] = "wrong_entity"
            candidate["wrong_entity_reason"] = f"boarddocs page title contains '{wrong_kw}'"
            candidate["notes"] = (
                (candidate.get("notes") or "").strip()
                + f" [rejected: wrong_entity — '{page_title[:60]}']"
            )
            return candidate

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
    "wrong_entity": 0,  # school district / library / etc — never wins
}
# empty and blocked score 0 — they should never win over any real candidate,
# and a Legistar "unknown" (5 + 20 = 25) beats a CivicClerk "empty" (0 + 12 = 12).
PLATFORM_TIER = {
    "legistar": 20, "civicplus": 16, "granicus": 16, "escribe": 14,
    "boarddocs": 14, "municode": 12, "civicclerk": 12, "primegov": 12,
    "novus": 10, "diligent": 10, "unknown": 4,
}
SOURCE_BONUS = {"known": 10, "known_probe": 2, "tavily": 3, "ddg": 0, "probe": 0}

# Known government meeting platform domains — always receive full trust
_GOV_PLATFORM_DOMAINS = [
    "legistar.com", "civicclerk.com", "granicus.com", "swagit.com",
    "boarddocs.com", "escribemeetings.com", "municode.com",
    "municodemeetings.com", "primegov.com", "novusagenda.com",
    "diligentoneplatform.com", "civicweb.net", "civicengage.com",
    "portal.civicclerk.com", "api.civicclerk.com",
    "destinyhosted.com",  # Destiny Agenda — municipal agenda hosting platform
]

# Domain keyword fragments that structurally indicate content/media sites (not government)
_CONTENT_SITE_DOMAIN_KEYWORDS = [
    "news", "times", "herald", "journal", "tribune", "reporter",
    "chronicle", "gazette", "press", "dispatch", "daily", "weekly",
    "channel", "radio", "media", "broadcast", "network",
    "hotel", "hotels", "agoda", "expedia", "kayak", "booking",
    "apartments", "realty", "homes", "travel", "airbnb", "vrbo",
    "weather", "accuweather",
    "tiktok", "youtube", "facebook", "instagram", "twitter",
    "forum", "patch", "blog",
]

# URL path fragments that structurally indicate content/media pages
_CONTENT_SITE_PATH_PATTERNS = [
    "/article/", "/articles/", "/story/", "/stories/",
    "/news/local/", "/watch?", "/channel/", "/@", "/video/",
]


def classify_domain_trust(url: str, city: str = "", state: str = "") -> float:
    """Return a trust multiplier (0.1–1.0) for how likely this URL is a gov source.

    1.0 = verified government (.gov TLD) or known meeting platform
    0.7 = city name found in domain (likely official city site)
    0.4 = generic unknown domain (no government signal)
    0.1 = structural content-site signals (news, TV, travel, social media)

    This multiplier is applied to the freshness score so that news articles
    with today's publication date (freshness=100) cannot outscore a government
    SPA page with no static date (unknown_spa=55 × 1.0 = 55 vs 100 × 0.1 = 10).
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().replace("www.", "")
    url_lower = url.lower()

    # Known government meeting platforms → full trust
    for plat_domain in _GOV_PLATFORM_DOMAINS:
        if plat_domain in netloc:
            return 1.0

    # .gov TLD → full trust
    if netloc.endswith(".gov") or ".gov/" in url_lower:
        return 1.0

    # Structural content-site domain keywords → content site penalty
    domain_base = netloc.split(".")[0] if "." in netloc else netloc
    for kw in _CONTENT_SITE_DOMAIN_KEYWORDS:
        if kw in domain_base or kw in netloc:
            return 0.1

    # Structural content-site URL path patterns
    for pat in _CONTENT_SITE_PATH_PATTERNS:
        if pat in url_lower:
            return 0.1

    # .tv TLD or "tv" in SLD → TV station (check BEFORE city-name match)
    # Catches both auroratv.org (SLD ends in "tv") and wbtv.com (.tv TLD)
    if netloc.endswith(".tv") or domain_base.endswith("tv") or domain_base.startswith("tv"):
        return 0.1

    # County government domain → reduced trust when searching for a city.
    # Pattern: co.{county}.{state}.us  or  {county}county.gov/  or county.{name}.gov
    # County agendas are for county commissioners, not city council.
    _parts = netloc.split(".")
    if (len(_parts) >= 4 and _parts[0] == "co" and _parts[-1] in ("us", "gov")) or \
       ("county" in _parts[0] and netloc.endswith((".gov", ".us"))) or \
       (len(_parts) >= 2 and _parts[0] == "county"):
        return 0.3  # county gov: below city-name match (0.7) but above generic (0.4)

    # City name in domain → likely official city website
    if city:
        city_slug = city.lower().replace(" ", "").replace("-", "").replace("'", "")
        domain_clean = netloc.replace("-", "").replace(".", "")
        if city_slug in domain_clean:
            return 0.7
        # State-qualified variant (e.g. "austintx" in "austintexas.gov")
        if state:
            state_lower = state.lower()
            state_name_slug = _STATE_NAMES.get(state.upper(), "").lower().replace(" ", "")
            if (city_slug + state_lower) in domain_clean:
                return 0.7
            if state_name_slug and (city_slug + state_name_slug[:4]) in domain_clean:
                return 0.7

    # Generic unknown domain
    return 0.4


def agenda_authority_score(c: dict) -> int:
    """Return bonus points for URL/content signals that prove this is an agenda source.

    Added on top of the trust-adjusted freshness score. Max ~45 points.
    These bonuses reward government agenda pages that may have no parseable dates
    (SPAs, JavaScript-rendered calendars) over unrelated pages that happen to be fresh.
    """
    url_lower = (c.get("url") or "").lower()
    notes_lower = (c.get("notes") or "").lower()
    score = 0

    # Strong URL path signals
    if "/agendacenter" in url_lower or "/agenda-center" in url_lower:
        score += 20
    elif "/agendas" in url_lower or "/agenda" in url_lower:
        score += 15
    if "/minutes" in url_lower:
        score += 10
    if "/citycouncil" in url_lower or "/city-council" in url_lower:
        score += 8
    elif "/council" in url_lower:
        score += 5
    if "/meetings" in url_lower or "/meeting" in url_lower:
        score += 5

    # Content signals from search snippet / notes
    if "agenda" in notes_lower:
        score += 5
    if "minutes" in notes_lower:
        score += 3

    return score


def candidate_score(c: dict, city: str = "", state: str = "") -> int:
    f = FRESHNESS_SCORE.get(c.get("freshness") or "", 0)
    p = PLATFORM_TIER.get(c.get("platform") or "unknown", 4)
    s = SOURCE_BONUS.get(c.get("source") or "probe", 0)
    b = 5 if c.get("body_match") else 0  # boost when title/content matches expected_body
    a = agenda_authority_score(c)

    # Trust multiplier: prevents content sites (news, TV, travel) that happen to have
    # fresh publication dates from outscoring government agenda pages.
    trust = classify_domain_trust(c.get("url") or "", city, state)
    return int(f * trust) + p + s + b + a


def rank_candidates(candidates: list[dict], city: str = "", state: str = "") -> list[dict]:
    ranked = sorted(candidates, key=lambda c: candidate_score(c, city, state), reverse=True)
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
    expected_body: str = "",
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

                # If expected_body is known, validate the rendered entity against it.
                # This catches cases like school district BoardDocs pages whose HTML
                # title is a street address (no keyword match in raw HTML) but whose
                # JS-rendered content contains "School District" or "Board of Education".
                if expected_body:
                    body_text_lower = body_text.lower()
                    expected_lower = expected_body.lower()
                    body_name_confirmed = expected_lower in body_text_lower
                    wrong_entity_in_text = any(
                        kw in body_text_lower for kw in WRONG_ENTITY_PATTERNS
                    )
                    if not body_name_confirmed and wrong_entity_in_text:
                        # Playwright rendered successfully but the entity is wrong.
                        # Don't accept this as a valid source — mark wrong_entity so
                        # the pipeline can continue searching for the correct source.
                        wrong_kw = next(
                            kw for kw in WRONG_ENTITY_PATTERNS if kw in body_text_lower
                        )
                        return {
                            "success": True,
                            "wrong_entity": True,
                            "freshness": "wrong_entity",
                            "most_recent_date": None,
                            "date_source": "playwright_render",
                            "note": (
                                f"playwright: entity mismatch — "
                                f"expected '{expected_body}' not found, "
                                f"page contains '{wrong_kw}'"
                            ),
                        }

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
    expected_body: str = "",
) -> dict:
    start = time.monotonic()
    verified: list[dict] = []
    tavily_queries: list[str] = []
    retries_used: list[str] = []
    retry_attempts = 0

    # ── Layer 0: DotGov official domain injection ─────────────────────────────
    # Before any web search, look up the city's official .gov domain from the
    # CISA DotGov database. Injecting it into known_sources["domain"] causes:
    #   • discover_from_known_sources() to probe /AgendaCenter on the .gov domain
    #   • discover_from_probes() to probe the .gov domain for all _AGENDA_PATHS
    #   • Retry 2 to skip its expensive domain-discovery search (domain already known)
    # This is the highest-ROI fix for cities where web search returns noise instead
    # of the official city site (small cities, ambiguous city names, etc.).
    effective_known = dict(known_sources)
    if "domain" not in effective_known:
        gov_domain = lookup_gov_domain(city, state)
        if gov_domain:
            effective_known["domain"] = gov_domain
            tavily_queries.append(f"[dotgov] {gov_domain}")
    else:
        effective_known = known_sources

    # ── Phase 1: Discover candidates ──────────────────────────────────────────

    # Strategy A: Check known sources
    known_cands = await discover_from_known_sources(effective_known, http)
    seen_urls: set[str] = {c["url"] for c in known_cands}

    # Strategy B: Search — DDG (primary) → Exa → Tavily (fallbacks).
    # DDG runs first: no API key needed and has broad government site coverage.
    # Exa runs if DDG returns nothing and EXA_API_KEY is set.
    # Tavily is the final fallback and also handles domain discovery in retry 2.
    # Use expected_body from the manifest for more targeted queries.
    body_term = expected_body or "city council"
    state_name = _STATE_NAMES.get(state.upper(), state)
    search_query = f"{city} {state_name} {body_term} agenda minutes"

    ddg_cands, ddg_query = await discover_from_duckduckgo(city, state, query=search_query)
    tavily_queries.append(f"[ddg] {ddg_query}")
    search_cands = ddg_cands

    if not search_cands and os.environ.get("EXA_API_KEY"):
        exa_cands, exa_query = await discover_from_exa(city, state, query=search_query)
        if exa_query:
            tavily_queries.append(f"[exa] {exa_query}")
        search_cands = exa_cands

    if not search_cands:
        tavily_q = f"{city} {state} {body_term} meeting agendas minutes"
        search_cands, query_used = await discover_from_tavily(city, state, tavily, "basic", query=tavily_q)
        tavily_queries.append(query_used)

    for c in search_cands:
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
        probe_cands = await discover_from_probes(city, state, effective_known, http)
        for c in probe_cands:
            if c["url"] not in seen_urls:
                seen_urls.add(c["url"])
                all_phase1.append(c)

    # Tag candidates whose title/content/notes contain the expected body name.
    # This boosts the score of sources that are explicitly for the right body.
    if expected_body:
        expected_lower = expected_body.lower()
        for c in all_phase1:
            combined = " ".join([
                (c.get("title") or ""),
                (c.get("content") or ""),
                (c.get("notes") or ""),
            ]).lower()
            c["body_match"] = expected_lower in combined

    # ── Phase 2: Verify freshness ──────────────────────────────────────────────
    for c in all_phase1:
        try:
            vc = await verify_freshness(c, http, city=city)
        except Exception as e:
            c["freshness"] = "unknown"
            c["notes"] = (c.get("notes") or "") + f" verify_error: {str(e)[:100]}"
            c.pop("_body", None)
            vc = c
        verified.append(vc)

    # ── Phase 3: Rank ─────────────────────────────────────────────────────────
    ranked = rank_candidates(verified, city=city, state=state)
    best = ranked[0] if ranked else None

    # ── Retry loop ────────────────────────────────────────────────────────────
    while retry_attempts < 2 and (not best or best.get("freshness") not in ("fresh",)):
        retry_attempts += 1

        if retry_attempts == 1:
            # Retry 1: Advanced Tavily with alternate queries
            retries_used.append("alternate_queries")
            domain = effective_known.get("domain", "")
            # Use expected_body in retry queries for more targeted results
            body_term_retry = expected_body or "city council"
            alt_queries = [
                f"{city} {state} {body_term_retry} agenda {TODAY.year}",
                f"{city} {state} {body_term_retry} meeting minutes recent",
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
                        # Tag body_match on retry candidates too
                        if expected_body:
                            combined = " ".join([
                                (c.get("title") or ""),
                                (c.get("content") or ""),
                            ]).lower()
                            c["body_match"] = expected_body.lower() in combined
                        try:
                            vc = await verify_freshness(c, http, city=city)
                        except Exception:
                            vc = c
                        verified.append(vc)
                        if vc.get("freshness") == "fresh":
                            break
                else:
                    continue
                break  # inner break propagated

        elif retry_attempts == 2:
            # Retry 2: Official domain discovery → common path probe → Playwright crawl
            #
            # Replicates the manual process: find the official city website, then look
            # for agendas there. This works for small cities where agenda-specific
            # searches return 0 results but the city homepage is always reachable.
            retries_used.append("domain_discovery")
            domain = effective_known.get("domain", "")

            # Step 2a: Discover domain via Tavily search
            if not domain:
                domain = await discover_official_domain(city, state, tavily) or ""
                tavily_queries.append(f"[domain_discovery] {city} {state} official city government website")

            # Step 2a-fallback-1: Tavily found nothing — try Claude web_search
            if not domain:
                domain = await discover_official_domain_via_claude(city, state) or ""
                if domain:
                    tavily_queries.append(f"[claude_websearch] {city} {state} official government website")

            # Step 2a-fallback-2: Tavily and Claude both failed — try DDG for domain
            if not domain:
                ddg_domain_cands, ddg_dom_q = await discover_from_duckduckgo(
                    city, state,
                    query=f"{city} {state} official city government website",
                    max_results=5,
                )
                tavily_queries.append(f"[ddg_domain] {ddg_dom_q}")
                for c in ddg_domain_cands:
                    parsed_d = urlparse(c["url"]).netloc.replace("www.", "")
                    if parsed_d.endswith(".gov") or city.lower().replace(" ", "") in parsed_d.replace("-", "").replace(".", ""):
                        domain = parsed_d
                        break

            if not domain:
                break

            # Step 2b: Probe common agenda paths on the discovered domain (fast, httpx)
            domain_cands = await probe_domain_for_agendas(domain, http)
            for c in domain_cands:
                if c["url"] not in seen_urls:
                    seen_urls.add(c["url"])
                    try:
                        vc = await verify_freshness(c, http, city=city)
                    except Exception:
                        vc = c
                    verified.append(vc)
                    if vc.get("freshness") == "fresh":
                        break

            # Step 2c: Playwright site crawl — when standard paths find nothing.
            # Handles JS-rendered city CMSes and non-standard URL structures.
            # Follows homepage navigation links to find the council/meetings section,
            # with entity validation to avoid accepting wrong-body pages.
            if not any(v.get("freshness") == "fresh" for v in verified):
                crawl_cands = await playwright_crawl_city_site(
                    domain, city, state, expected_body=expected_body
                )
                for c in crawl_cands:
                    if c["url"] not in seen_urls:
                        seen_urls.add(c["url"])
                        if expected_body:
                            c["body_match"] = expected_body.lower() in (
                                c.get("notes") or ""
                            ).lower()
                        verified.append(c)
                        if c.get("freshness") == "fresh":
                            break

        ranked = rank_candidates(verified, city=city, state=state)
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
                ranked = rank_candidates(verified, city=city, state=state)  # re-rank after upgrade
                best = ranked[0]
                if best.get("freshness") == "fresh" and best.get("platform") != "unknown":
                    break
        # Always re-rank at end of Phase 4 — some probes mutate freshness without
        # returning True (e.g. CivicClerk false-positive downgrade to empty).
        ranked = rank_candidates(verified, city=city, state=state)
        best = ranked[0] if ranked else None

    # ── Phase 5: Playwright Browser Rendering ─────────────────────────────────
    # Last resort: render JS-heavy pages with a real headless browser.
    # Targets cities still stuck (not fresh) after all other phases.
    # Primary use cases:
    #   - CivicClerk / PrimeGov SPAs where the OData API is unavailable
    #   - Bot-blocked city websites (403) where a real browser bypasses protection
    #   - Unknown platforms that only serve content after JS execution
    playwright_urls_tried: list[str] = []
    # Run Playwright if best is not fresh, OR if best has no body_match but there
    # are body_match=True candidates that are blocked/unknown (e.g. real city site
    # returning 403 while a wrong-entity site ranked higher due to a fresh date).
    _has_unprobed_body_matches = any(
        c.get("body_match") and c.get("freshness") in ("unknown_spa", "stale", "stale_warning", "unknown", "blocked")
        for c in ranked[:6]
    )
    if best and (best.get("freshness") not in ("fresh",) or (not best.get("body_match") and _has_unprobed_body_matches)):
        play_targets = [
            c for c in ranked[:6]
            if c.get("freshness") in ("unknown_spa", "stale", "stale_warning", "unknown", "blocked")
        ]
        # Prioritize body_match=True candidates so we probe the right entity first
        play_targets.sort(key=lambda c: (0 if c.get("body_match") else 1))
        for target in play_targets:
            playwright_urls_tried.append(target["url"])
            play_result = await probe_with_playwright(
                target["url"], city, state, expected_body=expected_body
            )
            existing_notes = (target.get("notes") or "").strip()
            if play_result.get("success"):
                if play_result.get("wrong_entity"):
                    # Playwright confirmed this is the wrong entity — downgrade it so
                    # the pipeline doesn't accept a school district / library / etc.
                    target["freshness"] = "wrong_entity"
                    target["wrong_entity_reason"] = play_result.get("note", "playwright entity validation")
                    note = play_result.get("note", "playwright: wrong_entity detected")
                    target["notes"] = f"{existing_notes} {note}".strip()
                    # Don't break — keep trying other candidates
                elif play_result.get("most_recent_date"):
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
                ranked = rank_candidates(verified, city=city, state=state)
                best = ranked[0]
                if play_result.get("freshness") == "fresh" and not play_result.get("wrong_entity"):
                    break  # fresh source found — stop trying
            else:
                error = play_result.get("error", "unknown_error")
                target["notes"] = f"{existing_notes} playwright:failed({error[:60]})".strip()
        ranked = rank_candidates(verified, city=city, state=state)
        best = ranked[0] if ranked else None

    # ── Phase 5b: City domain crawl with Playwright ───────────────────────────
    # If Phase 5a tried the known candidates but none yielded a fresh date,
    # attempt common agenda URL patterns on the city's own domain.
    # This catches cases where the known platform (e.g. CivicClerk) is dead but
    # the city hosts agendas directly as HTML/PDFs (e.g. /agendas-and-minutes).
    # Limit: 4 paths max, www.domain only, stop after 2 consecutive no-content failures.
    if best and best.get("freshness") not in ("fresh",) and playwright_urls_tried:
        domain = (
            effective_known.get("domain")
            or effective_known.get("civicplus_domain")
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
                    ranked = rank_candidates(verified, city=city, state=state)
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
        entry = {
            "platform": c.get("platform"),
            "url": c.get("url"),
            "source": c.get("source"),
            "freshness": c.get("freshness"),
            "most_recent_date": c.get("most_recent_date"),
            "rank": c.get("rank"),
            "notes": (c.get("notes") or "").strip(),
        }
        if c.get("body_match") is not None:
            entry["body_match"] = c["body_match"]
        all_candidates_out.append(entry)

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
    skip_existing: bool = False,
    output_dir: Optional[Path] = None,
    storage=None,
    sources_prefix: str = "meeting_pipeline/sources",
) -> dict:
    city = city_info["city"]
    state = city_info["state"]
    slug = city_to_slug(city)

    # --resume / --skip-existing: read existing result from S3 (no local files)
    if (resume or skip_existing) and storage is not None:
        s3_key = f"{sources_prefix}/{slug}-{state}/source.json"
        try:
            if storage.exists(s3_key):
                existing = storage.read_json(s3_key)
                bs = existing.get("best_source") or {}
                freshness = bs.get("freshness", "")
                rerun_freshnesses = {"empty", "wrong_entity", "stale"} if skip_existing else {"empty"}
                if freshness not in rerun_freshnesses:
                    print(
                        f"  [skip] {city:<20s} {state}  (existing: {bs.get('platform','?')}  {freshness})"
                    )
                    return existing
        except Exception:
            pass  # re-run if S3 read fails

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
        # Load manifest to get expected_body for targeted search queries
        expected_body = ""
        if storage is not None:
            manifest_key = f"{sources_prefix}/{slug}-{state}/manifest.json"
            try:
                if storage.exists(manifest_key):
                    manifest = storage.read_json(manifest_key)
                    expected_body = (manifest or {}).get("expected_body", "")
            except Exception:
                pass

        async with httpx.AsyncClient(headers=headers, timeout=20.0) as http:
            result = await run_source_discover(
                city, state, known_sources, tavily, http, expected_body=expected_body
            )

        # Write to S3 only — no local files
        if storage is not None:
            s3_key = f"{sources_prefix}/{slug}-{state}/source.json"
            try:
                storage.write_json(s3_key, result)
            except Exception as e:
                print(f"  [warn] Could not upload source.json to S3 for {city}, {state}: {e}")

            # Body validation: auto-correct council_category_id / committee_id for
            # known platforms so collection never runs against the wrong body.
            bs_platform = (result.get("best_source") or {}).get("platform", "")
            if bs_platform in VALIDATABLE_PLATFORMS:
                try:
                    bv = await validate_body_for_city(
                        f"{slug}-{state}", result, s3_key, http, storage
                    )
                    bv_status = bv.get("status", "skip")
                    if bv_status == "corrected":
                        print(f"  [body] corrected → {bv.get('correction_note', '')}")
                    elif bv_status == "unresolved":
                        print(f"  [body] UNRESOLVED — {bv.get('reason', '')}")
                    elif bv_status not in ("skip", "ok"):
                        print(f"  [body] {bv_status}: {bv.get('reason', '')}")
                except Exception as e:
                    print(f"  [body] validation error: {e}")

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
    skip_existing: bool = False,
    output_dir: Optional[Path] = None,
    storage=None,
    sources_prefix: str = "meeting_pipeline/sources",
) -> None:
    semaphore = asyncio.Semaphore(5)  # max 5 concurrent Tavily/Exa searches
    total_start = time.monotonic()

    backend = "Exa+Tavily" if os.environ.get("EXA_API_KEY") else "Tavily"
    print(f"{'='*78}")
    print(f"Source Discover — {len(cities)} cities  (today: {TODAY})  [search: {backend}]")
    print(f"{'='*78}")
    print(f"  {'[+]'} fresh   [~] unknown_spa/stale_warning   [-] stale/empty/blocked/unknown\n")

    tasks = [
        process_city(
            c, registry, tavily, semaphore,
            resume=resume, skip_existing=skip_existing, output_dir=output_dir,
            storage=storage, sources_prefix=sources_prefix,
        )
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

    # Write summary to S3
    if storage is not None:
        summary_key = f"{sources_prefix}/discovery-summary.json"
        try:
            storage.write_json(summary_key, summary)
            print(f"\n  Summary → s3://{summary_key}")
        except Exception as e:
            print(f"\n  [warn] Could not upload summary to S3: {e}")
    else:
        print(f"\n  [warn] No storage backend — summary not persisted")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Source-discover skill — find freshest agenda source for each city"
    )
    parser.add_argument("--city", help="Run for a single city (e.g. 'Chapel Hill')")
    parser.add_argument("--state", help="Filter by state abbreviation (e.g. NC, OH, TX)")
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip cities that already have a non-empty source.json (re-runs 'empty' results)"
    )
    parser.add_argument(
        "--from-csv", action="store_true",
        help=(
            "Use serve_users.csv as the city list instead of the hardcoded PILOT_CITIES. "
            "CSV State/Region column uses full state names (e.g. 'Ohio')."
        ),
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        help=(
            "Path to an alternate CSV file. Must have 'City' and 'State' columns "
            "(2-letter state abbreviations). Implies --from-csv behavior."
        ),
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help=(
            "Skip cities that already have a valid source.json "
            "(freshness not in wrong_entity/stale/empty). "
            "Use with --from-csv to only process new cities."
        ),
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

    exa_key = os.environ.get("EXA_API_KEY")
    if exa_key:
        print("  [search backend] DDG (primary) → Exa → Tavily (fallbacks)")
    else:
        print("  [search backend] DDG (primary) → Tavily (fallback; set EXA_API_KEY to also enable Exa)")

    from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    registry = storage.read_json(REGISTRY_S3_KEY)
    tavily = TavilyClient(api_key=api_key)

    # City list: --csv or --from-csv uses a CSV file; otherwise use hardcoded PILOT_CITIES
    if args.csv:
        import pathlib
        alt_csv = pathlib.Path(args.csv)
        if not alt_csv.exists():
            print(f"ERROR: {alt_csv} not found")
            sys.exit(1)
        seen: set[tuple[str, str]] = set()
        all_cities = []
        for row in csv.DictReader(alt_csv.open()):
            city = row.get("City", "").strip()
            state = row.get("State", "").strip().upper()
            if not city or not state or (city, state) in seen:
                continue
            seen.add((city, state))
            all_cities.append({"city": city, "state": state})
        city_source = f"{alt_csv.name} ({len(all_cities)} cities)"
    elif args.from_csv:
        all_cities = get_serve_csv_cities()
        city_source = f"serve_users.csv ({len(all_cities)} cities)"
    else:
        all_cities = list(PILOT_CITIES)
        city_source = f"PILOT_CITIES ({len(all_cities)} cities)"

    # --city filter (works for both sources)
    if args.city:
        filtered = [c for c in all_cities if c["city"].lower() == args.city.lower()]
        if not filtered:
            print(f"ERROR: '{args.city}' not found in {city_source}")
            sys.exit(1)
        cities = filtered
    elif args.state:
        filtered = [c for c in all_cities if c["state"].upper() == args.state.upper()]
        if not filtered:
            print(f"ERROR: No cities found for state '{args.state}' in {city_source}")
            sys.exit(1)
        cities = filtered
    else:
        cities = all_cities

    output_dir = Path(args.output_dir) if args.output_dir else None
    asyncio.run(
        run_batch(
            cities, registry, tavily,
            resume=args.resume,
            skip_existing=args.skip_existing,
            output_dir=output_dir,
            storage=storage,
            sources_prefix=cfg.sources_prefix,
        )
    )


if __name__ == "__main__":
    main()
