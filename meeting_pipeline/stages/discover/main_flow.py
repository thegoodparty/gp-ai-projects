"""
main_flow.py — Core discovery implementation.

Contains the main discovery flow (run_source_discover), platform probes,
freshness verification, and supporting functions. Called by
stages/discover/process.py.
"""

import asyncio
import csv
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from tavily import TavilyClient

from meeting_pipeline.shared.body_validation import validate_body_for_city, VALIDATABLE_PLATFORMS
from meeting_pipeline.shared.constants import (
    STATE_NAMES as _STATE_NAMES, STATE_ABBREVS,
    PLATFORM_PATTERNS, COLLECTION_METHODS,
    FRESH_THRESHOLD, STALE_WARNING_THRESHOLD,
    WRONG_CITY_PATTERNS, WRONG_ENTITY_PATTERNS, WRONG_DOMAIN_PATTERNS,
    BOARDDOCS_WRONG_ENTITY_KEYWORDS, COUNCIL_BODY_KEYWORDS, GRANICUS_COUNCIL_KEYWORDS,
    REJECT_URL_PATTERNS, FETCH_BLOCKLIST, CITY_NAME_PREFIXES, CITY_NAME_SUFFIXES,
    PDF_PLATFORM_SIGNALS,
)
from meeting_pipeline.shared.discovery_helpers import make_candidate, safe_fetch
from meeting_pipeline.shared.url_utils import (
    detect_platform, normalize_platform_url, is_wrong_city, is_wrong_entity,
    is_non_agenda_url, city_to_slug,
)
from meeting_pipeline.shared.date_utils import (
    extract_dates, classify_freshness, normalize_table_dates as _normalize_table_dates,
)
from meeting_pipeline.stages.discover.scoring import (
    candidate_score, rank_candidates, classify_domain_trust, agenda_authority_score,
    FRESHNESS_SCORE, PLATFORM_TIER, SOURCE_BONUS,
)
from meeting_pipeline.stages.discover.search import (
    serper_search,
    search_results_to_candidates as _search_results_to_candidates,
    discover_from_duckduckgo, discover_from_exa, discover_from_tavily,
    discover_from_firecrawl, discover_from_pdf_search,
)
from meeting_pipeline.stages.discover.crawl import (
    validate_domain_for_city, firecrawl_map_agenda, firecrawl_crawl_for_agenda,
)

# ── DotGov index (CISA .gov domain registry) ────────────────────────────────
_DOTGOV_CSV_PATH = Path(__file__).resolve().parent.parent / "config" / "dotgov.csv"
_DOTGOV_INDEX: dict | None = None
_DEPT_REJECT_KEYWORDS = [
    "police", "fire", "sheriff", "court", "library", "school",
    "water", "sewer", "utility", "transit", "housing", "airport",
]


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
        # Extract the governing body name from the Role column.
        # Role is like "Fultondale City Council - Place 1" or "Bennington Town Select Board".
        # Strip the seat/ward/district suffix to get the body name.
        role = (row.get("Role") or row.get("role") or row.get("Office") or "").strip()
        if role:
            # Remove seat/ward/district suffixes: "City Council - Ward 1" → "City Council"
            body = re.sub(r'\s*[-–]\s*(Ward|District|Place|Seat|At Large|Position|Post).*$', '', role, flags=re.IGNORECASE).strip()
            # Remove city name prefix if present: "Fultondale City Council" → "City Council"
            # But keep it if removing it leaves nothing meaningful
            body_no_city = re.sub(r'^' + re.escape(city) + r'\s+', '', body, flags=re.IGNORECASE).strip()
            if body_no_city and len(body_no_city) > 3:
                body = body_no_city
            if body:
                entry["expected_body"] = body
        cities.append(entry)
    return cities


# ── Utilities ──────────────────────────────────────────────────────────────────

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


async def discover_from_serper(
    city: str,
    state: str,
    expected_body: str = "",
) -> tuple[list[dict], str]:
    """
    Use Serper.dev (real Google Search results) to find the official city council agenda URL.

    Strategy:
    1. Search Serper for "{city} {state_full} city council agenda"
    2. For each result: validate domain (right city/state), scan page for meetings
    3. If page has no meetings: crawl sub-pages matching expected_body
    4. If still nothing: try next Serper result (same domain preferred)
    5. Fallback query with site:.gov/.org if primary query fails

    Returns (candidates, query_used). Candidates have source="serper_search".
    Only runs when SERPER_API_KEY is set.
    """
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        return [], ""

    # Helper functions are defined at module level above:
    #   serper_search(), validate_domain_for_city(),
    #   firecrawl_map_agenda(), firecrawl_crawl_for_agenda()

    # (Nested helper functions were extracted to module level — see above)

    # ── Primary query ─────────────────────────────────────────────────────────
    state_full = _STATE_NAMES.get(state.upper(), state)
    primary_q = f"{city} {state_full} city council agenda"
    try:
        results = await asyncio.to_thread(serper_search, primary_q, api_key)
    except RuntimeError as e:
        print(f"  [serper] {city}, {state}: {e}")
        raise
    query_used = primary_q
    rejection_log: list[str] = []  # track all rejected results for diagnostics

    validated_domain: str | None = None
    serper_url: str | None = None  # full URL from Serper (may already point to specific page)
    valid_serper_urls: list[tuple[str, str]] = []  # (url, domain) — all valid results, not just first

    for r in results:
        url = r["url"]
        if not url:
            continue
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        if not domain:
            continue
        if is_non_agenda_url(url):
            rejection_log.append(f"q1:{domain}→non_agenda_url")
            continue
        valid, reason = await asyncio.to_thread(validate_domain_for_city, domain, city, state)
        if valid:
            if not validated_domain:
                validated_domain = domain
                serper_url = url
            valid_serper_urls.append((url, domain))
            rejection_log.append(f"q1:{domain}→{reason}")
        else:
            rejection_log.append(f"q1:{domain}→{reason}")

    # ── Fallback query if primary validation failed ───────────────────────────
    if not validated_domain:
        fallback_q = f"{city} {state_full} city council agenda site:.gov OR site:.org"
        try:
            results2 = await asyncio.to_thread(serper_search, fallback_q, api_key)
        except RuntimeError as e:
            print(f"  [serper] {city}, {state} fallback: {e}")
            raise
        query_used = f"{primary_q} | {fallback_q}"
        for r in results2:
            url = r["url"]
            if not url:
                continue
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            if not domain:
                continue
            if is_non_agenda_url(url):
                rejection_log.append(f"q2:{domain}→non_agenda_url")
                continue
            valid, reason = await asyncio.to_thread(validate_domain_for_city, domain, city, state)
            if valid:
                validated_domain = domain
                serper_url = url
                rejection_log.append(f"q2:{domain}→{reason}")
                break
            else:
                rejection_log.append(f"q2:{domain}→{reason}")

    if not validated_domain:
        rejection_summary = " | ".join(rejection_log) if rejection_log else "no_results_returned"
        print(f"  [serper] {city}, {state}: all rejected — {rejection_summary}")
        return [], query_used

    # ── Find the best URL that actually produces meeting data ──────────────
    # For each valid Serper result (in order):
    #   1. Try the URL directly (scan it for meetings)
    #   2. If no meetings: crawl from that URL to find a deeper page
    #   3. If still no meetings: try the next Serper result
    # This mirrors how a human would search: click result #1, look around,
    # if nothing useful, go back to Google and try result #2.
    #
    # For known platform URLs (Legistar, CivicPlus, etc), skip this —
    # the platform-specific scanner handles them.
    fc_key = os.environ.get("FIRECRAWL_API_KEY")
    drill_note = ""
    agenda_url = None  # set if Firecrawl map found a sub-page
    final_url = serper_url

    if fc_key and detect_platform(serper_url) in ("unknown", "generic_html", None, ""):
        found = False
        from meeting_pipeline.shared.generic_agenda_scanner import scan_generic as scan_generic_firecrawl

        tried_domains: set[str] = set()  # skip same-domain results after crawl
        first_domain = valid_serper_urls[0][1] if valid_serper_urls else ""

        for idx, (candidate_url, candidate_domain) in enumerate(valid_serper_urls):
            # ── Pre-filter: skip URLs that are obviously not going to work ────
            # Skip same domain if we already crawled it (crawl covers sub-pages)
            if candidate_domain in tried_domains:
                print(f"  [discover] Result #{idx+1}: skip (already crawled {candidate_domain})")
                continue

            # For results #2+, if the domain is different from #1, verify the
            # actual page mentions both city AND state to avoid wrong-city matches
            # (e.g. Norfolk VA vs Norfolk NE, Ocean Township vs Ocean City)
            if idx > 0 and candidate_domain != first_domain:
                import httpx as _httpx
                try:
                    with _httpx.Client(follow_redirects=True, timeout=8,
                                       headers={"User-Agent": "Mozilla/5.0"}) as _hc:
                        _r = _hc.get(candidate_url)
                        if _r.status_code in (403, 404, 500):
                            print(f"  [discover] Result #{idx+1}: skip (HTTP {_r.status_code})")
                            continue
                        if len(_r.text) < 500:
                            print(f"  [discover] Result #{idx+1}: skip (empty page)")
                            continue
                        # Verify city AND state on the actual page
                        _page_lower = _r.text.lower()
                        _city_lower = city.lower()
                        _state_full = _STATE_NAMES.get(state.upper(), state).lower()
                        import re as _re2
                        _city_found = all(w in _page_lower for w in _city_lower.split())
                        _state_found = (bool(_re2.search(r'\b' + _re2.escape(state.lower()) + r'\b', _page_lower))
                                       or _state_full in _page_lower)
                        if not (_city_found and _state_found):
                            print(f"  [discover] Result #{idx+1}: skip (different domain, city/state not confirmed on page)")
                            continue
                except Exception:
                    print(f"  [discover] Result #{idx+1}: skip (unreachable)")
                    continue
            else:
                # Same domain as #1 or first result — quick HTTP check only
                try:
                    import httpx as _httpx
                    with _httpx.Client(follow_redirects=True, timeout=8,
                                       headers={"User-Agent": "Mozilla/5.0"}) as _hc:
                        _r = _hc.get(candidate_url)
                        if _r.status_code in (403, 404, 500):
                            print(f"  [discover] Result #{idx+1}: skip (HTTP {_r.status_code})")
                            continue
                        if len(_r.text) < 500:
                            print(f"  [discover] Result #{idx+1}: skip (empty page)")
                            continue
                except Exception:
                    print(f"  [discover] Result #{idx+1}: skip (unreachable)")
                    continue

            # Step 1: Try Firecrawl map on the domain to find a specific agenda sub-page
            candidate_path = urlparse(candidate_url).path.lower() if candidate_url else ""
            has_agenda_path = "agenda" in candidate_path

            if has_agenda_path:
                test_url = candidate_url
            else:
                base = f"https://{candidate_domain}"
                mapped = await asyncio.to_thread(firecrawl_map_agenda, base)
                test_url = mapped or candidate_url
                if mapped:
                    agenda_url = mapped

            # Step 2: Scan the URL — does it produce meetings?
            try:
                test_meetings = await scan_generic_firecrawl(test_url, city, state)
                if test_meetings:
                    # For cross-domain results, verify the page is for the right city.
                    # Check: city name in meeting titles OR in the domain itself.
                    # Also verify state via the domain (e.g. springville.org is UT not AL).
                    if idx > 0 and candidate_domain != first_domain:
                        city_lower = city.lower()
                        state_lower = state.lower()
                        state_full_lower = _STATE_NAMES.get(state.upper(), state).lower()
                        titles = " ".join(m.get("title", "") for m in test_meetings).lower()
                        city_in_domain = city_lower.replace(" ", "") in candidate_domain.replace("-", "")
                        city_in_titles = city_lower in titles
                        # State check: domain should encode the state or state shouldn't conflict
                        state_in_domain = state_lower in candidate_domain or state_full_lower.replace(" ", "") in candidate_domain
                        if not (city_in_domain or city_in_titles):
                            print(f"  [discover] Result #{idx+1}: meetings found but city '{city}' not confirmed — skip")
                            continue
                        if not state_in_domain and not city_in_domain:
                            # Different domain, city only in titles but state not in domain — risky
                            print(f"  [discover] Result #{idx+1}: city in titles but state not in domain — skip")
                            continue

                    final_url = test_url
                    validated_domain = candidate_domain
                    if idx > 0:
                        drill_note = f"→serper_result#{idx+1}:{test_url}"
                    found = True
                    print(f"  [discover] Result #{idx+1} produced {len(test_meetings)} meetings: {test_url[:60]}")
                    break
            except Exception:
                pass

            # Step 3: Crawl from this URL to find a deeper page
            if not found:
                tried_domains.add(candidate_domain)
                try:
                    deeper = await asyncio.to_thread(
                        firecrawl_crawl_for_agenda,test_url, city, state, expected_body
                    )
                    if deeper:
                        # Validate the deeper page also produces meetings
                        try:
                            deep_meetings = await scan_generic_firecrawl(deeper, city, state)
                            if deep_meetings:
                                # Cross-domain: verify city in meeting titles
                                if idx > 0 and candidate_domain != first_domain:
                                    city_lower = city.lower()
                                    titles = " ".join(m.get("title", "") for m in deep_meetings).lower()
                                    if city_lower not in titles and city_lower.replace(" ", "") not in candidate_domain:
                                        print(f"  [discover] Crawl from #{idx+1}: meetings found but city '{city}' not in titles — skip")
                                        continue

                                final_url = deeper
                                validated_domain = candidate_domain
                                drill_note = f"→crawl:{deeper}"
                                found = True
                                print(f"  [discover] Crawl from #{idx+1} found {len(deep_meetings)} meetings: {deeper[:60]}")
                                break
                        except Exception:
                            pass  # Don't accept optimistically for cross-domain
                except Exception:
                    pass

            if not found and idx < len(valid_serper_urls) - 1:
                print(f"  [discover] Result #{idx+1} produced 0 meetings, trying next")

        if not found:
            # None of the Serper results worked — use the first URL as best guess
            final_url = serper_url
            print(f"  [discover] No Serper results produced meetings — using #{1} as fallback")
    else:
        # Known platform — use Firecrawl map for sub-page but skip scan validation
        serper_path = urlparse(serper_url).path.lower() if serper_url else ""
        has_agenda_path = "agenda" in serper_path
        if not has_agenda_path:
            base_url = f"https://{validated_domain}"
            agenda_url = await asyncio.to_thread(firecrawl_map_agenda, base_url)
            final_url = agenda_url or serper_url or base_url

    platform = detect_platform(final_url) or "generic_html"
    rejection_summary = " | ".join(rejection_log) if rejection_log else ""
    cand = make_candidate(
        url=final_url,
        platform=platform,
        source="serper_search",
        notes=(
            f"serper→{validated_domain}"
            + (f"→map:{agenda_url}" if agenda_url else "")
            + drill_note
            + (f" | validations:[{rejection_summary}]" if rejection_summary else "")
        ),
    )
    return [cand], query_used


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
        _COST["tavily_searches"] += 1
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
    preferred_view_id: int | None = None,
) -> dict:
    """
    Enumerate Granicus RSS feeds (view_id=1..max_view_id) to find the City Council view.

    Granicus portals have multiple publisher views — view_id=1 is often a stale archive
    or a different body (e.g. Planning Board). The City Council view may be at any ID.

    If preferred_view_id is provided (from the source URL query params), probe that view
    first and accept it if it has fresh data — small cities often have one view that
    serves all their meeting content under a generic title ("City WA Content").

    Returns a result dict with keys:
      view_id, rss_url, display_url, freshness, most_recent_date, title
    Or {"view_id": None, "error": ...} if no council view found.
    """
    best_result: dict | None = None

    def _probe_result(view_id: int, body: str, title: str) -> dict:
        dates = extract_dates(body)
        most_recent = dates[0] if dates else None
        freshness = classify_freshness(most_recent) if most_recent else "unknown"
        rss_url = (
            f"https://{subdomain}.granicus.com/ViewPublisherRSS.php"
            f"?view_id={view_id}&mode=agendas"
        )
        return {
            "view_id": view_id,
            "rss_url": rss_url,
            "display_url": f"https://{subdomain}.granicus.com/ViewPublisher.php?view_id={view_id}",
            "freshness": freshness,
            "most_recent_date": most_recent.isoformat() if most_recent else None,
            "title": title,
            "_body": body,
        }

    # ── Step 1: Probe the preferred view_id first (if provided) ──────────────
    # Cities often have one Granicus view with a generic title (e.g. "City WA Content")
    # that doesn't contain council keywords but IS the correct view. Accept it directly
    # if it has fresh data, since the source URL already validated this view_id.
    if preferred_view_id:
        rss_url = (
            f"https://{subdomain}.granicus.com/ViewPublisherRSS.php"
            f"?view_id={preferred_view_id}&mode=agendas"
        )
        status, body = await safe_fetch(http, rss_url, timeout=8.0)
        if status == 200 and "<channel>" in body:
            title = ""
            m = re.search(r"<title><!\[CDATA\[([^\]]+)\]\]>", body)
            if m:
                title = m.group(1).strip()
            else:
                m = re.search(r"<title>([^<]+)</title>", body)
                if m:
                    title = m.group(1).strip()
            result = _probe_result(preferred_view_id, body, title)
            if result["freshness"] in ("fresh", "stale_warning"):
                return result
            # Keep as fallback — still enumerate to find a council-titled view
            best_result = result

    # ── Step 2: Enumerate views looking for council-titled one ────────────────
    for view_id in range(1, max_view_id + 1):
        if view_id == preferred_view_id:
            continue  # already probed above
        rss_url = (
            f"https://{subdomain}.granicus.com/ViewPublisherRSS.php"
            f"?view_id={view_id}&mode=agendas"
        )
        status, body = await safe_fetch(http, rss_url, timeout=8.0)
        if status != 200 or "<channel>" not in body:
            continue

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

        result = _probe_result(view_id, body, title)

        # Fresh council view — ideal, return immediately
        if result["freshness"] == "fresh":
            return result

        # Keep as best candidate but continue looking (fresher view may exist)
        if best_result is None or (
            result["freshness"] == "stale_warning" and best_result.get("freshness") == "unknown"
        ):
            best_result = result

    if best_result:
        return best_result
    return {"view_id": None, "error": f"no council view found in view_id 1-{max_view_id}"}


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

    # Legistar slug candidates: try multiple patterns since cities register
    # under different conventions (e.g. "sandyutah", "pompano", "cityofrochester").
    city_first_word = city.split()[0].lower().replace(".", "")
    state_full_lower = _STATE_NAMES.get(state.upper(), state).lower().replace(" ", "")
    legistar_slug_candidates = [
        city_nospace,                          # sandyut, rogersar
        f"{city_nospace}{state_full_lower}",   # sandyutah, rogersarkansas
        f"cityof{city_nospace}",               # cityofsandy, cityofrogers
    ]
    if city_first_word != city_nospace and len(city.split()) > 1:
        legistar_slug_candidates.append(city_first_word)  # sandy, rogers
    # Deduplicate while preserving order
    legistar_slug_candidates = list(dict.fromkeys(legistar_slug_candidates))

    probe_specs: list[tuple[str, str, dict]] = []
    for lg_slug in legistar_slug_candidates:
        probe_specs.append((
            f"https://webapi.legistar.com/v1/{lg_slug}/events?$top=3&$orderby=EventDate+desc",
            "legistar",
            {"legistar_slug": lg_slug, "display_url": f"https://{lg_slug}.legistar.com"},
        ))
    probe_specs += [
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
    # Try multiple subdomain patterns: citystate (acworthga), city-state (acworth-ga),
    # cityofcity (cityofacworth).
    gran_result = {}
    granicus_sub = city_nospace
    for gran_slug in [city_nospace, f"{city_nospace.rstrip(state_lower)}-{state_lower}" if city_nospace.endswith(state_lower) else f"{city_nospace}-{state_lower}", f"cityof{city_nospace}"]:
        gran_result = await probe_granicus_views(gran_slug, http)
        if gran_result.get("view_id"):
            granicus_sub = gran_slug
            break
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

    # Platform upgrade: unknown/generic_html CivicPlus CMS pages
    # CivicPlus sites often have a CMS page (e.g. /273/Agendas-Minutes) that links TO
    # the AgendaCenter rather than being the AgendaCenter itself. If the fetched body
    # contains an /AgendaCenter href, upgrade the candidate to civicplus + correct URL.
    # Also catches CivicPlus Archive Center pages (/Archive.aspx?AMID=N) that are used
    # for document archives instead of the live AgendaCenter.
    if platform in ("unknown", "generic_html"):
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        upgrade_url = None
        if "/AgendaCenter" in body:
            upgrade_url = f"{base}/AgendaCenter"
        elif "/Archive.aspx" in body or "ArchiveCenter" in body:
            # CivicPlus Archive Center — try to find the AMID from the body
            amid_m = re.search(r'/Archive\.aspx\?AMID=(\d+)', body)
            upgrade_url = f"{base}/Archive.aspx?AMID={amid_m.group(1)}" if amid_m else f"{base}/Archive.aspx"
        if upgrade_url:
            ac_status, ac_body = await safe_fetch(http, upgrade_url, timeout=12.0)
            if ac_status == 200 and ac_body and len(ac_body) > 300:
                candidate["url"] = upgrade_url
                candidate["platform"] = "civicplus"
                candidate["notes"] = (candidate.get("notes") or "") + " upgraded:civicplus_cms_link"
                platform = "civicplus"
                body = ac_body

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
        # Pass preferred_view_id from the URL so small cities with generic feed titles
        # (e.g. "City WA Content") are accepted if they have fresh data.
        parsed = urlparse(url)
        subdomain = parsed.netloc.replace(".granicus.com", "")
        qs = parse_qs(parsed.query)
        preferred_vid_str = (qs.get("view_id") or qs.get("view", [None]))[0]
        preferred_vid = int(preferred_vid_str) if preferred_vid_str and preferred_vid_str.isdigit() else None
        gran = await probe_granicus_views(subdomain, http, preferred_view_id=preferred_vid)
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

    # Strategy B: Search — Serper.dev (primary) → DDG → Exa → Tavily (fallbacks).
    # Serper runs first when SERPER_API_KEY is set: returns real Google Search results,
    # validates the domain via HTTP, and uses the Serper URL directly when it already
    # points to an agenda page (skipping Firecrawl). Falls back to DDG/Exa/Tavily.
    body_term = expected_body or "city council"
    state_name = _STATE_NAMES.get(state.upper(), state)
    search_query = f"{city} {state_name} {body_term} agenda minutes"

    search_cands: list[dict] = []

    if os.environ.get("SERPER_API_KEY"):
        try:
            grounding_cands, grounding_query = await discover_from_serper(city, state, expected_body=expected_body)
            tavily_queries.append(f"[serper] {grounding_query}")
            search_cands = grounding_cands
        except RuntimeError as e:
            # Rate limited — fall through to DDG/Tavily, log it
            tavily_queries.append(f"[serper_skipped] {e}")
            search_cands = []

    if not search_cands:
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

    # Strategy D: PDF search — find platforms via Google-indexed agenda PDFs
    has_recognized_after_probes = any(
        c["platform"] != "unknown" and c.get("http_status") == 200
        for c in all_phase1
    )
    if not has_recognized_after_probes and os.environ.get("SERPER_API_KEY"):
        try:
            pdf_cands = await asyncio.to_thread(
                discover_from_pdf_search, city, state, body_term
            )
            for cand in pdf_cands:
                if cand["url"] not in seen_urls:
                    seen_urls.add(cand["url"])
                    all_phase1.append(cand)
                    tavily_queries.append(f"[pdf_search] {cand['platform']}:{cand['url'][:60]}")
        except Exception as e:
            tavily_queries.append(f"[pdf_search_err] {str(e)[:40]}")

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

    # Firecrawl body validation: if best has body_match=False, use cheap Firecrawl
    # scrape (1 credit) to confirm or reject it before spending Playwright budget
    # on the wrong entity. Plain scrape is sufficient — we just need agenda keywords.
    if (
        best
        and not best.get("body_match")
        and best.get("freshness") in ("fresh", "stale_warning", "unknown_spa")
        and os.environ.get("FIRECRAWL_API_KEY")
    ):
        try:
            from meeting_pipeline.shared.firecrawl_client import validate_agenda_page
            _COST["firecrawl_scrape_basic"] += 1
            fc = validate_agenda_page(best["url"], city, state)
            if fc.get("valid"):
                best["body_match"] = True
                best["body_match_source"] = "firecrawl_scrape"
            else:
                best["freshness"] = "wrong_entity"
                best["wrong_entity_reason"] = "firecrawl_scrape found no city council agenda signals"
                ranked = rank_candidates(verified, city=city, state=state)
                best = ranked[0] if ranked else None
        except Exception:
            pass  # Firecrawl validation failed — accept candidate as-is

    # ── Phase 4b: Firecrawl rescue for high-trust blocked candidates ──────────
    # When a .gov domain or city-name-in-domain candidate has freshness=unknown/
    # blocked (often because Playwright or httpx hit a captcha), try Firecrawl
    # to validate it actually hosts city council meeting content with PDFs.
    # On success, upgrades freshness to fresh so it beats aggregator sites that
    # rank higher purely because they have parseable publication dates.
    #
    # Only runs when: (a) FIRECRAWL_API_KEY is set, (b) current best is not fresh
    # OR there are high-trust blocked candidates that could beat the current best.
    _has_high_trust_blocked = any(
        c.get("freshness") in ("unknown", "blocked", "unknown_spa")
        and classify_domain_trust(c.get("url") or "", city, state) >= 0.7
        for c in verified
    )
    if os.environ.get("FIRECRAWL_API_KEY") and (
        not best
        or best.get("freshness") not in ("fresh",)
        or _has_high_trust_blocked
    ):
        rescue_targets = [
            c for c in verified
            if c.get("freshness") in ("unknown", "blocked", "unknown_spa")
            and classify_domain_trust(c.get("url") or "", city, state) >= 0.7
            and not (c.get("url") or "").lower().endswith(".pdf")  # PDF docs are not agenda index pages
        ]
        # Probe highest-trust / highest-score candidates first
        rescue_targets.sort(
            key=lambda c: candidate_score(c, city, state), reverse=True
        )
        for target in rescue_targets[:3]:  # cap Firecrawl API calls
            try:
                # Use cheap scrape (1 credit) not LLM extract (~15-30 credits) —
                # we only need to confirm this is a real agenda page, not extract data.
                from meeting_pipeline.shared.firecrawl_client import validate_agenda_page
                _COST["firecrawl_scrapes"] += 1
                fc = validate_agenda_page(target["url"], city, state)
                if fc.get("valid"):
                    # Use the page's own modification date if available
                    date_str = fc.get("most_recent_date")
                    if date_str:
                        try:
                            most_recent = datetime.fromisoformat(date_str[:10]).date()
                            if date(2020, 1, 1) <= most_recent <= date(2030, 12, 31):
                                target["most_recent_date"] = most_recent.isoformat()
                                target["days_since_update"] = (TODAY - most_recent).days
                                target["freshness"] = classify_freshness(most_recent)
                            else:
                                target["freshness"] = "fresh"
                        except Exception:
                            target["freshness"] = "fresh"
                    else:
                        target["freshness"] = "fresh"
                    pdf_count = len(fc.get("pdf_urls") or [])
                    existing_notes = (target.get("notes") or "").strip()
                    target["notes"] = f"{existing_notes} firecrawl_rescue:valid({pdf_count}pdfs)".strip()
                    ranked = rank_candidates(verified, city=city, state=state)
                    best = ranked[0]
                    if best.get("freshness") == "fresh" and classify_domain_trust(best.get("url") or "", city, state) >= 0.7:
                        break  # high-trust fresh source found — stop
            except Exception:
                pass  # Firecrawl rescue failed — continue to Playwright

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

    # ── Phase 6: Firecrawl search + extract ───────────────────────────────────
    # Last resort for cities with no fresh source after all Playwright attempts.
    if (not best or best.get("freshness") not in ("fresh",)) and os.environ.get("FIRECRAWL_API_KEY"):
        fc_cands = await discover_from_firecrawl(city, state)
        for c in fc_cands:
            if c["url"] not in seen_urls:
                seen_urls.add(c["url"])
                try:
                    vc = await verify_freshness(c, http, city=city)
                except Exception:
                    vc = c
                verified.append(vc)
        if fc_cands:
            ranked = rank_candidates(verified, city=city, state=state)
            best = ranked[0] if ranked else best

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
            "source": best.get("source") or "",
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

    # Extract public_agenda_url: the human-facing agenda page URL from Serper
    # (the page a resident would find via Google). Distinct from best_source.url
    # which may be a platform API endpoint or portal subdomain.
    public_agenda_url = ""
    for c in all_candidates_out:
        if c.get("source") == "serper_search" and c.get("url"):
            public_agenda_url = c["url"]
            break

    return {
        "city": city,
        "state": state,
        "discovered_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "public_agenda_url": public_agenda_url,
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

