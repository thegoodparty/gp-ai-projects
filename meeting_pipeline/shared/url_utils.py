"""
url_utils.py — URL validation and classification utilities.

Used by discovery (to validate Serper results) and scan (platform detection).
"""

import re
from urllib.parse import parse_qs, urlparse

from meeting_pipeline.shared.constants import (
    BROADCAST_CALLSIGN_RE,
    NEWS_DOMAIN_SUFFIXES,
    PLATFORM_PATTERNS,
    REJECT_URL_PATTERNS,
    STATE_NAMES,
    WRONG_CITY_PATTERNS,
    WRONG_DOMAIN_PATTERNS,
    WRONG_ENTITY_PATTERNS,
)


def detect_platform(url: str) -> str:
    """Detect the agenda platform from a URL. Returns platform name or 'unknown'."""
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pat in patterns:
            if pat.lower() in url_lower:
                return platform
    return "unknown"


def normalize_platform_url(url: str, platform: str) -> str:
    """Normalize URLs to canonical portal base for SPA platforms."""
    if platform == "escribe" and ".escribemeetings.com" in url:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    if platform == "granicus" and "granicus.com" in url and "/ViewPublisher" not in url:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}/ViewPublisher.php"
    return url


def is_wrong_entity(text: str) -> bool:
    """Return True if text matches a wrong-entity pattern (school board, library, etc.)."""
    lower = text.lower()
    return any(pat.lower() in lower for pat in WRONG_ENTITY_PATTERNS)


def is_wrong_city(url: str, title: str, city: str, state: str = "") -> bool:
    """Return True if this URL/title is recognizably from the wrong city or entity."""
    combined = (url + " " + title).lower()

    # Global wrong-entity patterns (school boards, ISDs, etc.)
    for pattern in WRONG_ENTITY_PATTERNS:
        if pattern.lower() in combined:
            return True

    # Domain-level wrong-entity patterns
    netloc = urlparse(url).netloc.lower().replace("www.", "")
    for pattern in WRONG_DOMAIN_PATTERNS:
        if pattern in netloc:
            return True

    # City-specific patterns
    for pattern in WRONG_CITY_PATTERNS.get(city, []):
        if pattern.lower() in combined:
            return True

    # State-based validation
    if state:
        state_abbrev = state.upper()
        # Check if a different state name appears explicitly
        for abbrev, name in STATE_NAMES.items():
            if abbrev == state_abbrev:
                continue
            if re.search(rf'\b{re.escape(name.lower())}\b', combined):
                return True

        # .gov domain with different state abbreviation
        if netloc.endswith(".gov") and "." not in netloc[:-4]:
            domain_base = netloc[:-4]
            if len(domain_base) >= 3:
                embedded = domain_base[-2:].upper()
                if embedded in STATE_NAMES and embedded != state_abbrev:
                    return True

        # Full state name embedded in domain
        dom_check = netloc.replace("-", "").replace(".", "")
        for abbrev, name in STATE_NAMES.items():
            if abbrev == state_abbrev:
                continue
            state_slug = name.lower().replace(" ", "")
            if len(state_slug) >= 4 and state_slug in dom_check:
                return True

        # Municode shared-platform URL validation
        if "meetings.municode.com" in url or "municodemeetings.com" in url:
            try:
                qs = parse_qs(urlparse(url).query)
                cid = (qs.get("cid") or [""])[0].upper()
                if cid and len(cid) >= 3:
                    cid_state = cid[-2:]
                    if cid_state in STATE_NAMES and cid_state != state_abbrev:
                        return True
                    cid_city = (cid[:-2] if cid_state in STATE_NAMES else cid).lower()
                    cid_stripped = cid_city.replace("cityof", "").replace("city", "").replace("townof", "").replace("villageof", "")
                    city_slug = city.lower().replace(" ", "").replace("-", "")
                    if cid_city and city_slug and len(city_slug) >= 4 and not (city_slug in cid_city or city_slug in cid_stripped or
                                cid_city in city_slug or cid_stripped in city_slug):
                        return True
            except Exception:
                pass

    return False


def is_non_agenda_url(url: str) -> bool:
    """Return True if the URL is clearly not an official municipal agenda source."""
    url_lower = url.lower()
    for pat in REJECT_URL_PATTERNS:
        if pat in url_lower:
            return True

    parsed = urlparse(url)
    netloc = parsed.netloc.lower().removeprefix("www.")

    # Reject .tv domains (local TV stations)
    if netloc.endswith(".tv") or ".tv/" in url_lower:
        return True

    # Reject US broadcast station call signs
    if BROADCAST_CALLSIGN_RE.match(netloc):
        return True

    # Reject news/media sites by domain suffix (.com only)
    if netloc.endswith(".com"):
        domain_label = netloc[:-len(".com")]
        for suffix in NEWS_DOMAIN_SUFFIXES:
            if domain_label.endswith(suffix):
                return True

    return False
