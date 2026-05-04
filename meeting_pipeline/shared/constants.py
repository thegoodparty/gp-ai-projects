"""
constants.py — Shared constants for the meeting data pipeline.

Single source of truth for platform patterns, scoring, state names,
reject/blocklist keywords, and configuration thresholds.

Used by: discovery, scan, collection, extraction, briefing.
"""

import re as _re

# ── State Names & Abbreviations ───────────────────────────────────────────────

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
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}

# Reverse lookup: abbreviation → full name
STATE_NAMES = {v: k for k, v in STATE_ABBREVS.items()}


# ── Platform Detection ────────────────────────────────────────────────────────

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
    "generic_html": "fetch_and_parse",
}

# Platforms the scan module can handle
SUPPORTED_PLATFORMS = frozenset({
    "legistar", "civicplus", "boarddocs", "civicclerk",
    "escribe", "granicus", "unknown", "generic_html",
})

# Known platform domains embedded via iframes on city pages
IFRAME_PLATFORM_DOMAINS = frozenset([
    "swagit.com", "granicus.com", "legistar.com", "civicclerk.com",
    "civicweb.net", "primegov.com", "municode.com", "boarddocs.com",
    "novusagenda.com",
])

# Platform signals found in Google-indexed PDF URLs
PDF_PLATFORM_SIGNALS = {
    "legistar": ["legistar.com", "legistar1.com"],
    "civicclerk": ["civicclerk.com", "civicclerk.blob"],
    "granicus": ["granicus.com", "swagit.com", "swagit-attachments"],
    "municode": ["municode.com", "municodemeetings.com"],
    "boarddocs": ["boarddocs.com"],
    "novus": ["novusagenda.com"],
    "escribe": ["escribemeetings.com"],
    "primegov": ["primegov.com"],
}


# ── Candidate Scoring ─────────────────────────────────────────────────────────

FRESHNESS_SCORE = {
    "fresh": 100,
    "unknown_spa": 55,
    "stale_warning": 35,
    "stale": 15,
    "unknown": 5,
    "empty": -300,      # must never beat any source with content
    "blocked": -300,
    "wrong_entity": -500,
}

# Scanning capability tier — higher = more reliable collection
PLATFORM_TIER = {
    "legistar": 22,     # REST API
    "civicclerk": 20,   # OData API
    "civicplus": 20,    # AJAX scraper
    "escribe": 18,      # POST API
    "boarddocs": 18,    # POST API
    "granicus": 8,      # HTML scrape
    "municode": 8,      # HTML scrape
    "primegov": 8,      # SPA
    "novus": 6,         # HTML scrape
    "diligent": 6,      # HTML scrape
    "unknown": 4,
    "generic_html": 4,
}

SOURCE_BONUS = {
    "known": 10, "known_probe": 2, "tavily": 3, "ddg": 0, "probe": 0,
    "serper_search": 3, "pdf_search": 2, "firecrawl": 1, "exa": 2,
}


# ── Freshness Thresholds ──────────────────────────────────────────────────────

FRESH_THRESHOLD = 90            # days — meetings within this are "fresh"
STALE_WARNING_THRESHOLD = 365   # days — between this and FRESH is "stale_warning"


# ── Scan Thresholds ───────────────────────────────────────────────────────────

LOOKBACK_DAYS = 60      # how many days back to include past meetings
LOOKAHEAD_DAYS = 90     # how many days ahead to look for future meetings


# ── Domain Validation ─────────────────────────────────────────────────────────

# Domains that should never be accepted as a city government source
FETCH_BLOCKLIST = frozenset({
    "en.wikipedia.org", "wikipedia.org", "youtube.com", "facebook.com",
    "twitter.com", "x.com", "linkedin.com", "nextdoor.com", "yelp.com",
    "google.com",
})

# City name prefixes/suffixes that indicate a different municipality
# e.g. "North Logan" ≠ "Logan", "Loganville" ≠ "Logan"
CITY_NAME_PREFIXES = (
    "north", "south", "east", "west", "new", "old", "upper",
    "lower", "mount", "fort", "port", "lake", "grand",
)
CITY_NAME_SUFFIXES = (
    "ville", "town", "burg", "field", "dale", "view", "wood",
)


# ── Body Validation Keywords ──────────────────────────────────────────────────

# Generic meeting titles — kept even when score_body_match returns 0
GENERIC_MEETING_TITLES = [
    "regular meeting", "work session", "special meeting",
    "budget", "public hearing", "goal setting", "retreat",
    "workshop", "agenda", "commission meeting", "council",
    "governing board",
]

# Keywords for Granicus view title matching
GRANICUS_COUNCIL_KEYWORDS = [
    "city council", "council meeting", "regular meeting",
    "town council", "village board", "board of trustees",
    "common council", "board of aldermen", "city commission",
    "select board", "board of commissioners",
]


# ── Wrong Entity Detection ────────────────────────────────────────────────────

# Known wrong-city URL fragments for ambiguous city names
WRONG_CITY_PATTERNS: dict[str, list[str]] = {
    "El Paso": ["elpasoil", "el-paso-il", "el paso, il", "el paso illinois"],
    "Burlington": ["burlington.ca", "burlington.on", "burlington ontario",
                    "burlington, vt", "burlington vermont"],
    "Medina": ["cityofmedinatn", "medina, tn", "medina, tennessee", "medinatn",
               "planning-zoning", "planning_zoning", "boards-and-commission"],
    "Loveland": ["lovelandco", "cilovelandco", "loveland, co",
                  "loveland colorado", "loveland co.gov"],
    "Belton": ["belton.org", "belton, mo", "belton missouri",
                "belton, sc", "beltonedc.org"],
    "Hamilton": ["hamilton.ca", "hamilton, ontario", "hamilton ontario"],
}

# Title/URL patterns that indicate a wrong entity (school board, etc.)
WRONG_ENTITY_PATTERNS = [
    "school district", "school board", "independent school",
    "isd board", "board of education", "school committee",
    "county commission", "county council", "county board",
    "county legislature", "county supervisors",
    "chamber of commerce",
]

# Domain patterns indicating wrong entity
WRONG_DOMAIN_PATTERNS = [
    "schools.org", "school.org", "k12.", ".edu",
    "countyoh.gov", "countytx.gov", "countyfl.gov",
]

# BoardDocs entity keywords that indicate school district (not city government)
BOARDDOCS_WRONG_ENTITY_KEYWORDS = [
    "school", "district", "isd", "usd", "unified",
    "education", "academy", "learning", "collegiate",
    "charter", "preparatory",
]


# ── URL Reject Patterns ──────────────────────────────────────────────────────
# URL fragments that indicate non-agenda sites. Checked by is_non_agenda_url().

REJECT_URL_PATTERNS = [
    # Social media
    "facebook.com/", "youtube.com/watch", "youtube.com/@", "youtube.com/channel",
    "youtube.com/user", "youtu.be/", "twitter.com/", "x.com/", "nextdoor.com/",
    "instagram.com/", "linkedin.com/", "reddit.com/", "tiktok.com/",
    # News / journalism
    "patch.com/", "ballotpedia.org/", "govtech.com/", "politico.com/",
    "nytimes.com/", "washingtonpost.com/", "foxnews.com/", "nbcnews.com/",
    "abcnews.go.com/", "cbsnews.com/", "usatoday.com/", "apnews.com/",
    "reuters.com/", "mlive.com/", "cleveland.com/", "dispatch.com/",
    "documenters.org/", "hickoryrecord.com/", "journaltimes.com/",
    "jsonline.com/", "racinecountyeye.com/", "thebuckeyeflame.com/",
    "cantonrep.com/", "mchenrytimes.com/", "shawlocal.com/",
    "dailyherald.com/", "triblocal.com/", "suburbanchicagoland.com/",
    "daytondailynews.com/", "cincinnaticitybeat.com/",
    # Local TV stations
    "wmtv15news.com/", "wtmj.com/", "fox6now.com/", "wisn.com/", "witi.com/",
    "tmj4.com/", "wkow.com/", "channel3000.com/", "wbay.com/", "weau.com/",
    "waow.com/", "wfrv.com/", "wgba.com/", "wearegreenbay.com/", "wsaw.com/",
    "wkbt.com/", "wqow.com/", "wxow.com/", "wlax.com/", "nbc15.com/",
    "abc27.com/", "local3news.com/", "local10.com/", "kxan.com/", "kvue.com/",
    "khou.com/", "click2houston.com/", "abc13.com/", "cbsaustin.com/",
    "wfaa.com/", "nbcdfw.com/", "cbslocal.com/", "myfoxny.com/", "wral.com/",
    "abc11.com/", "wcnc.com/", "wsoc-tv.com/", "wbtv.com/", "wccb.com/",
    "wtvd.com/", "wbns10tv.com/", "nbc4i.com/", "10tv.com/", "local12.com/",
    "fox19.com/", "wkrc.com/", "wcpo.com/", "wlwt.com/", "whio.com/",
    # Travel / lifestyle
    "tripadvisor.com/", "travelchannel.com/", "airbnb.com/", "expedia.com/",
    "booking.com/", "hotels.com/", "agoda.com/", "kayak.com/", "travelocity.com/",
    "orbitz.com/", "familydestinationsguide.com/", "roadtrippers.com/",
    # Real estate
    "zillow.com/", "realtor.com/", "trulia.com/", "redfin.com/", "movoto.com/",
    # Data / directory / lookup
    "zip-codes.com/", "unitedstateszipcodes.org/", "city-data.com/",
    "bestplaces.net/", "niche.com/", "areavibes.com/", "neighborhoodscout.com/",
    "homefacts.com/", "datausa.io/", "census.gov/", "wikipedia.org/",
    "yellowpages.com/", "whitepages.com/", "mapquest.com/", "yelp.com/",
    # Tech / corporate
    "microsoft.com/", "apple.com/", "google.com/", "amazon.com/",
    "indeed.com/", "glassdoor.com/", "quora.com/", "stackoverflow.com/",
    "github.com/", "slideshare.net/", "scribd.com/",
    # Archive / academic
    "archive.org/details/",
    # Aggregators / content farms
    "yahoo.com/news", "yahoo.com/local", "msn.com/", "aol.com/",
    "readkong.com/", "grokipedia.com/",
    # Petition / crowdfunding
    "change.org/", "gofundme.com/",
    # Other
    "citizenportal.ai/", "espn.com/", "nbcsports.com/",
    "onlyinyourstate.com/", "towleroad.com/",
]

# Domain-name suffixes indicating local news/media (.com domains only)
NEWS_DOMAIN_SUFFIXES = {
    "roundtable", "tribune", "chronicle", "courier", "sentinel", "bulletin",
    "advertiser", "examiner", "ledger", "dispatch", "herald", "gazette",
    "register", "observer", "reporter", "journal", "dailynews", "weeklynews",
    "newsroom", "newspost", "newstimes",
    "thread", "reader", "live", "wire", "today", "now", "insider", "indy", "talk",
}

# US broadcast callsign pattern: K/W + 2-3 letters (kiow.com, wkrc.com, etc.)
BROADCAST_CALLSIGN_RE = _re.compile(r"^[kw][a-z]{2,3}\.(com|tv|org)$")


# ── Government Platform Trust Domains ─────────────────────────────────────────

GOV_PLATFORM_DOMAINS = [
    "legistar.com", "civicclerk.com", "granicus.com", "swagit.com",
    "boarddocs.com", "escribemeetings.com", "municode.com",
    "municodemeetings.com", "civicplus.com", "novusagenda.com",
    "primegov.com", "diligentoneplatform.com",
]

# ── LLM / Extraction Defaults ────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-flash"
EXTRACT_MAX_PAGES = 60
LARGE_AGENDA_WORD_THRESHOLD = 8000
