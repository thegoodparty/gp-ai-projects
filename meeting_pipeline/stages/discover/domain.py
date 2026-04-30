"""domain.py — City domain discovery and common-path probing."""

import asyncio
import re
from datetime import date

import httpx

from meeting_pipeline.shared.constants import (
    FRESH_THRESHOLD,
)
from meeting_pipeline.shared.constants import (
    STATE_NAMES as _STATE_NAMES,
)
from meeting_pipeline.shared.date_utils import (
    extract_dates,
)
from meeting_pipeline.shared.discovery_helpers import make_candidate, safe_fetch
from meeting_pipeline.shared.url_utils import (
    detect_platform,
)

# ── Module-level constants ───────────────────────────────────────────────────
TODAY = date.today()

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
) -> str | None:
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

    async def check_domain(domain: str) -> str | None:
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
