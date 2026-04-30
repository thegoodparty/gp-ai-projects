"""freshness.py — Candidate freshness verification via live HTTP/API probing."""

import json
import re
from datetime import date, datetime
from urllib.parse import urlparse

import httpx

from meeting_pipeline.shared.constants import (
    BOARDDOCS_WRONG_ENTITY_KEYWORDS,
    WRONG_CITY_PATTERNS,
)
from meeting_pipeline.shared.date_utils import (
    classify_freshness,
    extract_dates,
)
from meeting_pipeline.shared.discovery_helpers import safe_fetch

TODAY = date.today()


def _is_council_body(name: str) -> bool:
    """Check if an event body name is a city council (not advisory/planning/etc)."""
    from meeting_pipeline.shared.body_validation import GOVERNING_KEYWORDS
    name_lower = name.lower()
    return any(kw in name_lower for kw in GOVERNING_KEYWORDS)


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
    if platform == "novus" and "Application_Error" in body:
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
