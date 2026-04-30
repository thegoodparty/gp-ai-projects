"""probes.py — Platform-specific API probes and deep candidate verification."""

import asyncio
import json
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from meeting_pipeline.shared.constants import (
    BOARDDOCS_WRONG_ENTITY_KEYWORDS,
    GRANICUS_COUNCIL_KEYWORDS,
)
from meeting_pipeline.shared.constants import (
    STATE_NAMES as _STATE_NAMES,
)
from meeting_pipeline.shared.date_utils import (
    classify_freshness,
    extract_dates,
)
from meeting_pipeline.shared.discovery_helpers import make_candidate, safe_fetch
from meeting_pipeline.shared.url_utils import (
    is_wrong_city,
)

# ── Module-level constants ───────────────────────────────────────────────────
TODAY = date.today()


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
            body.lower()
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

    except TimeoutError:
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
                    if has_meeting_kw:
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
            gran.pop("_body", "")
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
