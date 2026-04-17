"""
scan_meeting_schedule.py — Lightweight scan for upcoming meeting dates.

For each city with a valid source.json, makes a minimal API/scraper call to
discover upcoming meeting dates and whether an agenda has been posted. Writes
per-city upcoming_meetings.json without downloading any PDFs.

This is the first stage of a two-stage pipeline:
  1. scan_meeting_schedule.py (daily, cheap) — discover dates + agenda status
  2. Full collection scripts (triggered only when agenda_posted=true)

Output: sources/{city}/upcoming_meetings.json
  {
    "city_slug": "chapel-hill-NC",
    "city": "Chapel Hill",
    "state": "NC",
    "body": "Town Council",
    "platform": "legistar",
    "scanned_at": "2026-04-15T...",
    "upcoming": [
      {
        "date": "2026-04-22",
        "title": "Town Council Regular Meeting",
        "agenda_posted": true,
        "agenda_url": "https://...",
        "event_id": "12345"
      }
    ]
  }

Usage:
    # Scan all cities with a known source
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py

    # Scan a single city
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --city chapel-hill-NC

    # Dry-run: list what would be scanned
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --dry-run

    # Only show cities where agenda_posted changed from false → true since last scan
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --report-new
"""

import argparse
import asyncio
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.body_validation import (
    REJECT_KEYWORDS,
    GOVERNING_KEYWORDS,
    score_body_match,
    best_body_match,
    validate_legistar_body,
    validate_civicplus_body,
    validate_civicclerk_body,
    validate_boarddocs_body,
    apply_body_validation as _apply_body_validation,
    validate_body_for_city,
)

LOOKAHEAD_DAYS = 90   # How many days ahead to look for meetings
LOOKBACK_DAYS = 60    # How many days back to include (for last meeting date)
SUPPORTED_PLATFORMS = {"legistar", "civicplus", "boarddocs", "civicclerk", "escribe", "granicus"}

# ============================================================================
# PER-PLATFORM LIGHTWEIGHT SCANNERS
# Each returns list of upcoming meeting dicts (no PDFs downloaded).
# ============================================================================

async def scan_legistar(city: str, config: dict, client: httpx.AsyncClient, source_url: str = "") -> list[dict]:
    """
    Legistar: query the events API for future meetings only.
    EventAgendaLastPublishedUTC non-null → agenda is posted.
    """
    slug = config.get("legistar_slug", "")
    if not slug and source_url:
        # Derive slug from URL (e.g. "https://hampton.legistar.com/..." → "hampton")
        m = re.search(r"https?://([^.]+)\.legistar\.com", source_url)
        if m:
            slug = m.group(1)
    if not slug:
        return []

    today_dt = datetime.now()
    start = (today_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    today = today_dt.strftime("%Y-%m-%d")
    cutoff = (today_dt + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    base_url = f"https://webapi.legistar.com/v1/{slug}"

    try:
        resp = await client.get(
            f"{base_url}/events",
            params={
                "$filter": f"EventDate ge datetime'{start}' and EventDate le datetime'{cutoff}'",
                "$orderby": "EventDate asc",
                "$top": 50,
            },
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"    Legistar fetch error for {slug}: {e}")
        return []

    upcoming = []
    for ev in events:
        date_raw = ev.get("EventDate", "")
        date = date_raw[:10] if date_raw else None
        if not date:
            continue
        agenda_url = ev.get("EventAgendaFile") or None
        published = ev.get("EventAgendaLastPublishedUTC")
        agenda_posted = bool(published and published != "0001-01-01T00:00:00")

        upcoming.append({
            "date": date,
            "title": ev.get("EventBodyName", city),
            "agenda_posted": agenda_posted,
            "agenda_url": agenda_url,
            "event_id": str(ev.get("EventId", "")),
            "status": "past" if date < today else "upcoming",
        })

    return upcoming


async def scan_civicplus(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    CivicPlus AgendaCenter: reuse the existing scraper's category discovery and
    meeting-list fetch — same logic, no PDF download.
    Presence of agenda_pdf_url on a CivicPlusMeeting → agenda_posted=True.
    """
    from urllib.parse import urlparse
    from meeting_pipeline.collectors.civicplus_scraper import find_council_category, fetch_meeting_list

    # Extract domain the same way router.py does
    domain = config.get("domain", "") or urlparse(source_url).netloc.replace("www.", "")
    if not domain:
        return []

    cat_id = config.get("council_category_id") or config.get("category_id")

    today = datetime.now().date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)
    current_year = today.year

    try:
        if not cat_id:
            cat_id, _ = await find_council_category(client, domain)

        # Fetch current year meetings
        raw = await fetch_meeting_list(client, domain, cat_id, current_year)

        # If the lookback window spans into last year (Jan–Feb), also fetch prev year
        if start.year < current_year:
            try:
                prev_year_meetings = await fetch_meeting_list(client, domain, cat_id, current_year - 1)
                raw = prev_year_meetings + raw
            except Exception:
                pass  # best-effort

    except Exception as e:
        print(f"    CivicPlus fetch error for {domain}: {e}")
        return []

    upcoming = []
    for m in raw:
        try:
            date_obj = datetime.strptime(m.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if date_obj < start or date_obj > cutoff:
            continue
        upcoming.append({
            "date": m.date,
            "title": m.title,
            "agenda_posted": bool(m.agenda_pdf_url),
            "agenda_url": m.agenda_pdf_url,
            "event_id": m.agenda_id,
            "status": "past" if date_obj < today else "upcoming",
        })

    upcoming.sort(key=lambda m: m["date"])
    return upcoming


async def scan_boarddocs(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    BoardDocs: reuse the existing collector's committee discovery and meeting-list
    fetch — same logic, no agenda download.
    """
    from meeting_pipeline.collectors.boarddocs import BoardDocsConfig, _fetch_committees, _fetch_meetings

    match = re.search(r"(https://go\.boarddocs\.com/\w+/\w+/Board\.nsf)", source_url)
    base_url = match.group(1) if match else None
    if not base_url:
        return []

    bd_config = BoardDocsConfig(
        base_url=base_url,
        city_name=city,
        output_prefix="",
        storage=None,  # not used by _fetch_committees or _fetch_meetings
        committee_id=config.get("committee_id", ""),
        expected_body=config.get("expected_body", ""),
    )

    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://go.boarddocs.com",
        "Referer": f"{base_url}/Public",
        "User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)",
    }

    try:
        committees = await _fetch_committees(client, bd_config, headers)
    except Exception as e:
        print(f"    BoardDocs committees error: {e}")
        return []

    council_kw = ["city council", "town council", "village council", "board of aldermen", "municipal council"]
    council_committees = [c for c in committees if any(kw in c["name"].lower() for kw in council_kw)]
    if not council_committees:
        council_committees = committees[:1]

    today = datetime.now().date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)
    start_str = start.strftime("%Y%m%d")
    today_str = today.strftime("%Y%m%d")
    cutoff_str = cutoff.strftime("%Y%m%d")
    upcoming = []

    for committee in council_committees:
        meetings = await _fetch_meetings(client, bd_config, headers, committee["id"])
        for m in meetings:
            num_date = str(m.get("numberdate", ""))
            if not num_date or len(num_date) < 8:
                continue
            if num_date < start_str or num_date > cutoff_str:
                continue
            try:
                date_str = datetime.strptime(num_date[:8], "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                continue
            agenda_url = m.get("EventAgendaFile") or None
            upcoming.append({
                "date": date_str,
                "title": m.get("EventComment", committee["name"]),
                "agenda_posted": bool(agenda_url),
                "agenda_url": agenda_url,
                "event_id": str(m.get("EventId", m.get("unique", ""))),
                "status": "past" if num_date < today_str else "upcoming",
            })

    upcoming.sort(key=lambda m: m["date"])
    return upcoming


async def scan_civicclerk(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    CivicClerk OData API: query for future events.

    Supports two API generations:
    - Legacy ({tenant}.api.civicclerk.com): filter field = MeetingStartDate, datetime format
    - Portal ({tenant}.portal.civicclerk.com): filter field = startDateTime, date-only format
      Both share the same backend at {tenant}.api.civicclerk.com/v1.
    """
    match = re.search(r"https://(\w+)\.(?:api\.|portal\.)?civicclerk\.com", source_url)
    if not match:
        tenant = config.get("tenant", "")
        if not tenant:
            return []
    else:
        tenant = match.group(1)

    is_portal = "portal.civicclerk.com" in source_url

    today_dt = datetime.now()
    today_str = today_dt.strftime("%Y-%m-%d")
    start_date = (today_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    cutoff_date = (today_dt + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    start_dt = (today_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00")
    cutoff_dt = (today_dt + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT00:00:00")

    if is_portal:
        # Portal API uses startDateTime with date-only comparison values
        params = {
            "$filter": f"startDateTime ge {start_date} and startDateTime le {cutoff_date}",
            "$orderby": "startDateTime asc",
            "$top": 100,
        }
    else:
        params = {
            "$filter": f"MeetingStartDate ge {start_dt} and MeetingStartDate le {cutoff_dt}",
            "$orderby": "MeetingStartDate asc",
            "$top": 50,
        }

    try:
        resp = await client.get(
            f"https://{tenant}.api.civicclerk.com/v1/Events/",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data.get("value", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"    CivicClerk fetch error for {tenant}: {e}")
        return []

    upcoming = []
    for ev in events:
        if is_portal:
            date_raw = ev.get("startDateTime", ev.get("eventDate", ""))
            title = ev.get("eventName", city)
            agenda_url = ev.get("agendaFile") or None
            agenda_posted = bool(agenda_url)  # hasAgenda=True without agendaFile means no downloadable document
            event_id = str(ev.get("id", ""))
        else:
            date_raw = ev.get("MeetingStartDate", ev.get("EventDate", ""))
            title = ev.get("Name", ev.get("EventName", city))
            agenda_url = ev.get("AgendaFile") or ev.get("AgendaUrl") or None
            agenda_posted = bool(agenda_url) or bool(ev.get("AgendaPostedDate"))
            event_id = str(ev.get("EventId", ev.get("Id", "")))

        date = date_raw[:10] if date_raw else None
        if not date:
            continue
        upcoming.append({
            "date": date,
            "title": title,
            "agenda_posted": agenda_posted,
            "agenda_url": agenda_url,
            "event_id": event_id,
            "status": "past" if date < today_str else "upcoming",
        })

    return upcoming


async def scan_granicus(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    Granicus/Swagit: fetch ViewPublisherRSS.php?mode=agendas feed for meeting schedule.

    Granicus is a video-archive platform that also publishes agenda PDFs.
    The RSS feed (mode=agendas) returns items for past and upcoming meetings —
    each item has a pubDate for the meeting date and an optional <enclosure>
    pointing to the agenda PDF when one has been uploaded.

    Supports:
      - Classic Granicus: {tenant}.granicus.com/ViewPublisher.php?view_id={id}
      - Swagit:           {tenant}.new.swagit.com/...  (falls back to HTML scrape)
    """
    parsed = urlparse(source_url)
    netloc = parsed.netloc.lower()
    today_str = datetime.now().strftime("%Y-%m-%d")
    start_str = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    cutoff_str = (datetime.now() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")

    # ── Granicus RSS ──────────────────────────────────────────────────────────
    if "granicus.com" in netloc:
        tenant = netloc.replace(".granicus.com", "")
        qs = parse_qs(parsed.query)
        view_id = config.get("view_id") or (qs.get("view_id") or qs.get("view", ["1"]))[0]
        rss_url = (
            f"https://{tenant}.granicus.com/ViewPublisherRSS.php"
            f"?view_id={view_id}&mode=agendas"
        )
        try:
            resp = await client.get(rss_url, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except Exception as e:
            print(f"    Granicus RSS fetch error for {tenant}: {e}")
            return []

        upcoming = []
        ns = {"media": "http://search.yahoo.com/mrss/"}
        for item in root.iter("item"):
            title_el = item.find("title")
            pubdate_el = item.find("pubDate")
            enclosure_el = item.find("enclosure")
            if pubdate_el is None:
                continue
            # Parse RFC-2822 pubDate → YYYY-MM-DD
            try:
                dt = datetime.strptime(pubdate_el.text.strip(), "%a, %d %b %Y %H:%M:%S %z")
                date_str = dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                # Try alternate format without timezone
                try:
                    dt = datetime.strptime(pubdate_el.text.strip()[:25], "%a, %d %b %Y %H:%M:%S")
                    date_str = dt.strftime("%Y-%m-%d")
                except (ValueError, AttributeError):
                    continue
            if date_str < start_str or date_str > cutoff_str:
                continue
            title = (title_el.text or city).strip() if title_el is not None else city
            agenda_url = enclosure_el.get("url") if enclosure_el is not None else None
            upcoming.append({
                "date": date_str,
                "title": title,
                "agenda_posted": bool(agenda_url),
                "agenda_url": agenda_url,
                "event_id": "",
                "status": "past" if date_str < today_str else "upcoming",
            })
        return upcoming

    # ── Swagit fallback: scrape the main page for meeting links ─────────────
    elif "swagit.com" in netloc:
        # Swagit doesn't expose a standard RSS — scrape the publisher index page
        # to find meeting links and dates. Strip any specific video path.
        base_url = f"https://{netloc}"
        try:
            resp = await client.get(base_url, timeout=15)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            print(f"    Swagit fetch error for {netloc}: {e}")
            return []

        upcoming = []
        # Swagit pages list meetings as: <span class="date">April 21, 2026</span>
        # near links like /videos/{id} with optional agenda PDF link
        date_pattern = re.compile(
            r'(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})',
            re.IGNORECASE,
        )
        pdf_pattern = re.compile(r'href=["\']([^"\']*\.pdf)["\']', re.IGNORECASE)
        # Group dates with nearby PDF links (rough approach for Swagit HTML)
        for m in date_pattern.finditer(html):
            raw_date = m.group(1)
            try:
                dt = datetime.strptime(raw_date.replace(",", "").strip(), "%B %d %Y")
                date_str = dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
            if date_str < start_str or date_str > cutoff_str:
                continue
            # Look for a PDF link in the surrounding 500 chars
            snippet = html[max(0, m.start() - 100): m.end() + 400]
            pdf_match = pdf_pattern.search(snippet)
            agenda_url = pdf_match.group(1) if pdf_match else None
            upcoming.append({
                "date": date_str,
                "title": city,
                "agenda_posted": bool(agenda_url),
                "agenda_url": agenda_url,
                "event_id": "",
                "status": "past" if date_str < today_str else "upcoming",
            })
        return upcoming

    return []


async def scan_escribe(city: str, config: dict, source_url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    eSCRIBE: POST to MeetingsCalendarView.aspx/UpcomingMeetings for each meeting type.
    Falls back to PastMeetings if UpcomingMeetings returns nothing.
    Uses verify=False because eSCRIBE portals commonly have self-signed/expired SSL certs.
    """
    base_url = source_url.rstrip("/")
    meeting_view_id = config.get("meeting_view_id", "1")
    meeting_types: list[str] = list(config.get("meeting_types", []))

    # eSCRIBE portals often have expired/self-signed SSL certs — use verify=False
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
        timeout=15,
        verify=False,
    ) as ec:
        # Discover meeting types from HTML if not configured
        if not meeting_types:
            try:
                resp = await ec.get(f"{base_url}/?MeetingviewId={meeting_view_id}")
                resp.raise_for_status()
                found = re.findall(
                    r'(?:meetingType|MeetingType|filterType)["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                    resp.text,
                )
                seen: set[str] = set()
                for t in found:
                    t = t.replace("&amp;", "&")
                    if t and t not in seen:
                        seen.add(t)
                        meeting_types.append(t)
            except Exception as e:
                print(f"    eSCRIBE type discovery failed for {city}: {e}")
                return []

        if not meeting_types:
            return []

        today_str = datetime.now().strftime("%Y-%m-%d")
        cutoff_str = (datetime.now() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
        upcoming: list[dict] = []

        for mt in meeting_types:
            for endpoint in ("UpcomingMeetings", "PastMeetings"):
                try:
                    resp = await ec.post(
                        f"{base_url}/MeetingsCalendarView.aspx/{endpoint}?MeetingviewId={meeting_view_id}",
                        json={"type": mt, "pageNumber": 1},
                        headers={
                            "Content-Type": "application/json; charset=utf-8",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json().get("d", {})
                    meetings = data.get("Meetings", [])

                    for m in meetings:
                        date_str = m.get("DateShort") or m.get("DateLong") or ""
                        try:
                            from dateutil import parser as dateparser
                            date = dateparser.parse(date_str).strftime("%Y-%m-%d") if date_str else None
                        except Exception:
                            date = None
                        if not date or not (today_str <= date <= cutoff_str):
                            continue
                        agenda_posted = bool(m.get("Agenda")) or bool(m.get("HasAgenda"))
                        agenda_url = m.get("AgendaUrl") or m.get("Agenda") or None
                        upcoming.append({
                            "date": date,
                            "title": mt,
                            "agenda_posted": agenda_posted,
                            "agenda_url": agenda_url,
                            "event_id": str(m.get("Id", "")),
                            "status": "past" if date < today_str else "upcoming",
                        })

                    if upcoming:
                        break  # got results from this endpoint, skip PastMeetings fallback
                except Exception as e:
                    print(f"    eSCRIBE {endpoint} failed for {city}/{mt}: {e}")
                    continue

    return upcoming


# ============================================================================
# MAIN SCAN DISPATCHER
# ============================================================================

async def scan_city(
    slug: str,
    source: dict,
    source_key: str,
    client: httpx.AsyncClient,
    storage,
    skip_body_validation: bool = False,
) -> dict | None:
    """Scan one city's upcoming meetings. Returns the upcoming_meetings record."""
    best = source.get("best_source") or {}
    platform = best.get("platform", "")
    config = best.get("config", {})
    source_url = best.get("url", "")
    city = source.get("city", slug)
    state = source.get("state", "")

    # Derive body name from source
    body = best.get("expected_body", config.get("expected_body", ""))

    # ── Stage 1: Body validation (before any PDF downloads) ──────────────
    body_validation: dict = {}
    if not skip_body_validation and platform in SUPPORTED_PLATFORMS:
        body_validation = await validate_body_for_city(slug, source, source_key, client, storage)

        status = body_validation.get("status", "skip")
        validated_body = body_validation.get("validated_body")

        if status == "unresolved":
            # Try next-ranked supported candidate from all_candidates before giving up
            print(f"\n      ⚠ BODY MISMATCH ({platform}): {body_validation.get('reason')}")
            from meeting_pipeline.scripts.source_discover import PLATFORM_TIER, COLLECTION_METHODS
            for alt in (source.get("all_candidates") or [])[1:]:
                alt_platform = alt.get("platform", "")
                if alt_platform not in SUPPORTED_PLATFORMS:
                    continue
                alt_source = {
                    **source,
                    "best_source": {
                        "platform": alt_platform,
                        "url": alt.get("url", ""),
                        "display_url": alt.get("url", ""),
                        "freshness": alt.get("freshness"),
                        "most_recent_date": alt.get("most_recent_date"),
                        "collection_method": COLLECTION_METHODS.get(alt_platform, "fetch_and_parse"),
                        "config": alt.get("config") or {},
                        "notes": alt.get("notes") or "",
                    },
                }
                try:
                    alt_bv = await validate_body_for_city(slug, alt_source, source_key, client, storage)
                except Exception:
                    continue
                if alt_bv.get("status") in ("ok", "corrected"):
                    print(f"\n      ↳ Switched to {alt_platform} ({alt.get('url', '')})")
                    source = alt_source
                    best = alt_source["best_source"]
                    platform = alt_platform
                    config = best.get("config", {})
                    source_url = best.get("url", "")
                    body_validation = alt_bv
                    status = alt_bv.get("status", "ok")
                    # Persist the better source so future scans use it
                    try:
                        storage.write_json(source_key, alt_source)
                    except Exception:
                        pass
                    break
            else:
                print(f"\n      No fallback candidate resolved body — scan may return wrong body")
        elif status == "corrected":
            print(f"\n      ✓ BODY CORRECTED: {body_validation.get('correction_note')}")
            # Re-read updated config (source.json was patched in-place)
            try:
                source = storage.read_json(source_key)
                best = source.get("best_source") or {}
                config = best.get("config") or {}
            except Exception:
                pass
            # Use the validated body name
            if validated_body:
                body = validated_body
        elif status == "ok" and validated_body:
            body = validated_body

    # ── Stage 2: Scan for upcoming meetings ──────────────────────────────
    upcoming: list[dict] = []

    if platform == "legistar":
        upcoming = await scan_legistar(city, config, client, source_url=source_url)
    elif platform == "civicplus":
        upcoming = await scan_civicplus(city, config, source_url, client)
    elif platform == "boarddocs":
        upcoming = await scan_boarddocs(city, config, source_url, client)
    elif platform == "civicclerk":
        upcoming = await scan_civicclerk(city, config, source_url, client)
    elif platform == "escribe":
        upcoming = await scan_escribe(city, config, source_url, client)
    elif platform == "granicus":
        upcoming = await scan_granicus(city, config, source_url, client)
    else:
        # Unsupported platform — record that we know it exists but can't scan
        pass

    # ── Body filter: drop events that don't match the validated governing body ──
    # For platforms that return events from multiple bodies (Legistar, CivicClerk),
    # filter to only events whose title matches the expected body. Events with no
    # title or a score of 0 (no match) are kept only if body is not configured —
    # a score < 0 (hard reject: advisory, planning, zoning, etc.) is always dropped.
    if body and upcoming:
        filtered = []
        dropped = []
        for m in upcoming:
            title = m.get("title", "")
            if not title:
                filtered.append(m)
                continue
            sc = score_body_match(title, body)
            if sc < 0:
                # Hard reject — advisory board, planning commission, etc.
                dropped.append(title)
            elif sc == 0:
                # No match — only keep if title exactly equals the body name
                # (catches cases where expected_body is very generic)
                if title.lower() == body.lower():
                    filtered.append(m)
                else:
                    dropped.append(title)
            else:
                filtered.append(m)
        if dropped:
            unique_dropped = sorted(set(dropped))
            print(f"    Body filter dropped {len(dropped)} events for non-matching bodies: {unique_dropped}")
        upcoming = filtered

    return {
        "city_slug": slug,
        "city": city,
        "state": state,
        "body": body,
        "platform": platform,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "body_validation": body_validation,
        "upcoming": upcoming,
    }


# ============================================================================
# BATCH RUNNER
# ============================================================================

async def run_batch(
    city_slug: str | None,
    dry_run: bool,
    report_new: bool,
    skip_body_validation: bool,
    cfg: AgentConfig,
    storage,
):
    # Load all city source.json files
    all_source_keys = storage.list_keys(cfg.sources_prefix)
    source_keys = [k for k in all_source_keys if k.endswith("/source.json")]

    if city_slug:
        source_keys = [k for k in source_keys if f"/{city_slug}/" in k]

    print(f"Schedule Scanner: {len(source_keys)} cities")
    print()

    if dry_run:
        for k in source_keys:
            slug = k.split("/")[-2]
            try:
                src = storage.read_json(k)
                platform = (src.get("best_source") or {}).get("platform", "?")
            except Exception:
                platform = "?"
            print(f"  {slug:<35} [{platform}]")
        return

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True,
        timeout=20,
    ) as client:

        results = {
            "scanned": 0, "skipped": 0, "errors": 0,
            "new_agendas": [],
            "body_corrections": [], "body_unresolved": [],
        }

        for i, key in enumerate(source_keys, 1):
            slug = key.split("/")[-2]
            try:
                source = storage.read_json(key)
            except Exception:
                results["errors"] += 1
                continue

            if not source:
                results["errors"] += 1
                continue

            platform = (source.get("best_source") or {}).get("platform", "")
            if platform not in SUPPORTED_PLATFORMS:
                print(f"[{i}/{len(source_keys)}] {slug} — skip ({platform} not supported)")
                results["skipped"] += 1
                continue

            # Load previous scan to detect agenda_posted changes
            prev_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
            prev = {}
            if storage.exists(prev_key):
                try:
                    prev = storage.read_json(prev_key)
                except Exception:
                    pass

            prev_posted = {m["date"]: m.get("agenda_posted", False)
                          for m in prev.get("upcoming", [])}

            print(f"[{i}/{len(source_keys)}] {slug} ({platform})...", end=" ", flush=True)
            try:
                record = await scan_city(
                    slug, source, key, client, storage,
                    skip_body_validation=skip_body_validation,
                )
                storage.write_json(prev_key, record)

                all_meetings = record.get("upcoming", [])
                past = [m for m in all_meetings if m.get("status") == "past"]
                upcoming = [m for m in all_meetings if m.get("status") != "past"]
                posted = [m for m in upcoming if m.get("agenda_posted")]
                unposted = [m for m in upcoming if not m.get("agenda_posted")]

                bv = record.get("body_validation", {})
                bv_status = bv.get("status", "")
                bv_note = ""
                if bv_status == "corrected":
                    bv_note = f" [BODY CORRECTED → '{bv.get('validated_body')}']"
                    results["body_corrections"].append({
                        "city": slug, "note": bv.get("correction_note"), "patch": bv.get("config_patch"),
                    })
                elif bv_status == "unresolved":
                    bv_note = f" [BODY UNRESOLVED ⚠]"
                    results["body_unresolved"].append({"city": slug, "reason": bv.get("reason")})

                past_note = f", last={past[-1]['date']}" if past else ""
                print(f"{len(upcoming)} upcoming ({len(posted)} posted, {len(unposted)} pending{past_note}){bv_note}")

                # Detect newly-posted agendas (only future meetings)
                for m in upcoming:
                    if m.get("agenda_posted") and not prev_posted.get(m["date"], False):
                        results["new_agendas"].append({"city": slug, "date": m["date"], "title": m["title"]})

                results["scanned"] += 1

            except Exception as e:
                print(f"ERROR: {e}")
                results["errors"] += 1

        print()
        print("=" * 60)
        print(f"SUMMARY: {results['scanned']} scanned, {results['skipped']} skipped, {results['errors']} errors")

        if results["body_corrections"]:
            print(f"\nBODY CORRECTIONS APPLIED ({len(results['body_corrections'])}):")
            for item in results["body_corrections"]:
                print(f"  {item['city']:<35} {item['note']}")

        if results["body_unresolved"]:
            print(f"\nBODY UNRESOLVED — COLLECTION BLOCKED ({len(results['body_unresolved'])}):")
            for item in results["body_unresolved"]:
                print(f"  {item['city']:<35} {item['reason']}")

        if results["new_agendas"]:
            print(f"\nNEW AGENDAS POSTED ({len(results['new_agendas'])}):")
            for item in results["new_agendas"]:
                print(f"  {item['city']:<35} {item['date']}  {item['title']}")
        elif report_new:
            print("\nNo newly-posted agendas detected.")

        print("=" * 60)


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Scan upcoming meeting schedules for all cities")
    parser.add_argument("--city", help="Scan a single city slug (e.g. chapel-hill-NC)")
    parser.add_argument("--dry-run", action="store_true", help="List cities without making HTTP requests")
    parser.add_argument("--report-new", action="store_true",
                        help="Highlight cities where agenda_posted flipped true since last scan")
    parser.add_argument("--skip-body-validation", action="store_true",
                        help="Skip pre-scan body validation (faster, but may collect wrong body)")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    asyncio.run(run_batch(
        args.city, args.dry_run, args.report_new,
        args.skip_body_validation, cfg, storage,
    ))


if __name__ == "__main__":
    main()
