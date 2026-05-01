"""
main_flow.py — Discovery orchestration.

Contains the main discovery flow (run_source_discover) and supporting
search functions. Platform probes, freshness verification, and domain
discovery have been split into dedicated modules:

  - domain.py     — City domain discovery and .gov registry lookup
  - freshness.py  — Candidate freshness verification
  - probes.py     — Platform-specific API probes

Called by stages/discover/process.py.
"""

import asyncio
import os
import re
import time
from datetime import UTC, date, datetime
from urllib.parse import urlparse

import httpx

from meeting_pipeline.shared.constants import (
    BOARDDOCS_WRONG_ENTITY_KEYWORDS,
    COLLECTION_METHODS,
)
from meeting_pipeline.shared.constants import (
    STATE_NAMES as _STATE_NAMES,
)
from meeting_pipeline.shared.date_utils import classify_freshness
from meeting_pipeline.shared.discovery_helpers import make_candidate, safe_fetch
from meeting_pipeline.shared.url_utils import (
    detect_platform,
    is_non_agenda_url,
    is_wrong_entity,
)
from meeting_pipeline.stages.discover.crawl import (
    firecrawl_crawl_for_agenda,
    firecrawl_map_agenda,
    validate_domain_for_city,
)
from meeting_pipeline.stages.discover.domain import (
    probe_city_domain_patterns,
    probe_domain_for_agendas,
)

# Backward-compatible re-export for tests that import _is_council_body from here
from meeting_pipeline.stages.discover.freshness import (
    _is_council_body,  # noqa: F401
    verify_freshness,
)
from meeting_pipeline.stages.discover.probes import (
    deep_probe_candidate,
    discover_from_probes,
)
from meeting_pipeline.stages.discover.scoring import (
    candidate_score,
    classify_domain_trust,
    rank_candidates,
)
from meeting_pipeline.stages.discover.search import (
    discover_from_firecrawl,
    discover_from_pdf_search,
    serper_search,
)

# ── Module-level constants ───────────────────────────────────────────────────
TODAY = date.today()

# Cost tracking — module-level counters (single-threaded event loop)
_COST: dict[str, int] = {
    "firecrawl_scrape_basic": 0,
    "firecrawl_scrape_actions": 0,
    "firecrawl_scrapes": 0,
    "serper_searches": 0,
}


# ── Known Sources ────────────────────────────────────────────────────────────

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


# ── Web Search ───────────────────────────────────────────────────────────────

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
    crawl_url = None   # set if Firecrawl crawl found a page with PDFs
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
            # actual page mentions both city AND state to avoid wrong-city matches.
            # Note: 403 is NOT a skip — many city sites block httpx but work with
            # Firecrawl (real browser). Only skip 404 (page doesn't exist).
            if idx > 0 and candidate_domain != first_domain:
                import httpx as _httpx
                try:
                    with _httpx.Client(follow_redirects=True, timeout=8,
                                       headers={"User-Agent": "Mozilla/5.0"}) as _hc:
                        _r = _hc.get(candidate_url)
                        if _r.status_code == 404:
                            print(f"  [discover] Result #{idx+1}: skip (HTTP 404)")
                            continue
                        if _r.status_code not in (403, 500) and len(_r.text) >= 500:
                            # Page loaded — verify city AND state
                            _page_lower = _r.text.lower()
                            _city_lower = city.lower()
                            _state_full = _STATE_NAMES.get(state.upper(), state).lower()
                            _city_found = all(w in _page_lower for w in _city_lower.split())
                            _state_found = (bool(re.search(r'\b' + re.escape(state.lower()) + r'\b', _page_lower))
                                           or _state_full in _page_lower)
                            if not (_city_found and _state_found):
                                print(f"  [discover] Result #{idx+1}: skip (different domain, city/state not confirmed on page)")
                                continue
                        # 403/500/unreachable on cross-domain: let Firecrawl try it
                except Exception:
                    pass  # unreachable via httpx — let Firecrawl try

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
                        # Track the crawl result as fallback even if scan_generic
                        # can't parse meetings — the page with PDFs is still better
                        # than the raw Serper landing page.
                        if not crawl_url:
                            crawl_url = deeper

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
            # None of the Serper results produced parseable meetings.
            # Prefer the Firecrawl-mapped URL (deeper page) over the raw Serper
            # landing page — the mapped URL is more likely to be the actual agenda
            # page (e.g. /AgendaCenter/City-Council-6), even if scan_generic
            # couldn't extract dates from it.
            final_url = agenda_url or crawl_url or serper_url
            fallback_source = "mapped" if agenda_url else ("crawl" if crawl_url else "#1")
            print(f"  [discover] No Serper results produced meetings — using {fallback_source} as fallback")
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


# ── Orchestration ────────────────────────────────────────────────────────────

async def run_source_discover(
    city: str,
    state: str,
    known_sources: dict,
    http: httpx.AsyncClient,
    expected_body: str = "",
) -> dict:
    start = time.monotonic()
    verified: list[dict] = []
    tavily_queries: list[str] = []
    retries_used: list[str] = []
    retry_attempts = 0

    effective_known = dict(known_sources)

    # ── Phase 1: Discover candidates ──────────────────────────────────────────

    # Strategy A: Check known sources
    known_cands = await discover_from_known_sources(effective_known, http)
    seen_urls: set[str] = {c["url"] for c in known_cands}

    # Strategy B: Search — Serper.dev (real Google Search results).
    # Validates the domain via HTTP, and uses the Serper URL directly when it already
    # points to an agenda page (skipping Firecrawl).
    body_term = expected_body or "city council"

    search_cands: list[dict] = []

    if os.environ.get("SERPER_API_KEY"):
        try:
            grounding_cands, grounding_query = await discover_from_serper(city, state, expected_body=expected_body)
            tavily_queries.append(f"[serper] {grounding_query}")
            search_cands = grounding_cands
        except RuntimeError as e:
            tavily_queries.append(f"[serper_skipped] {e}")
            search_cands = []

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
    while retry_attempts < 1 and (not best or best.get("freshness") not in ("fresh",)):
        retry_attempts += 1

        if retry_attempts == 1:
            # Retry 1: Domain discovery via pattern probing → common path probe → Playwright crawl
            #
            # Replicates the manual process: find the official city website, then look
            # for agendas there. This works for small cities where agenda-specific
            # searches return 0 results but the city homepage is always reachable.
            retries_used.append("domain_discovery")
            domain = effective_known.get("domain", "")

            # Step 1a: Probe predictable domain patterns (no search API needed)
            if not domain:
                domain = await probe_city_domain_patterns(city, state, http) or ""
                if domain:
                    tavily_queries.append(f"[domain_probe] {domain}")

            if not domain:
                break

            # Step 1b: Probe common agenda paths on the discovered domain (fast, httpx)
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

        ranked = rank_candidates(verified, city=city, state=state)
        best = ranked[0] if ranked else None
        if best and best.get("freshness") == "fresh":
            break

    # ── Phase 4: Deep Platform API Probes ─────────────────────────────────────
    # Runs when no fresh source found, OR when best is fresh-from-search but a
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
        for target in probe_targets[:6]:  # cap to avoid runaway cost
            upgraded = await deep_probe_candidate(target, city, state, http)
            if upgraded:
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
                pass  # Firecrawl rescue failed — continue

    # ── Phase 5: Firecrawl search + extract ───────────────────────────────────
    # Last resort for cities with no fresh source.
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
        "discovered_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            "elapsed_sec": elapsed,
        },
    }
