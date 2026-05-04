"""
granicus_scraper.py — Granicus / Swagit meeting data collector.

Two platform variants:
  1. Classic Granicus: RSS feed at {subdomain}.granicus.com
  2. New Swagit:       JSON API at {subdomain}.new.swagit.com

Classic Granicus RSS endpoint:
    https://{subdomain}.granicus.com/ViewPublisherRSS.php?view_id={view_id}&mode=agendas

New Swagit JSON endpoint:
    https://{subdomain}.new.swagit.com/city-council.json?page={n}   (10 items/page)
    Fallback: /events.json?page={n}

Usage:
    from collectors.granicus_scraper import (
        GranicusConfig, CLASSIC_GRANICUS, NEW_SWAGIT, collect_granicus
    )

    config = GranicusConfig(
        platform=CLASSIC_GRANICUS,
        subdomain="cibolotx",
        city_name="Cibolo",
        view_id=1,
        output_prefix="data/granicus",
        storage=storage_backend,
    )
    result = await collect_granicus(config)
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urljoin

import httpx

from meeting_pipeline.shared.storage import StorageBackend

# Granicus XML namespace used in RSS feeds
GRAN_NS = "http://granicus.com/ns/1.0"

# Platform constants
CLASSIC_GRANICUS = "classic_granicus"
NEW_SWAGIT = "new_swagit"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ============================================================================
# CONFIG AND RESULT DATACLASSES
# ============================================================================

@dataclass
class GranicusConfig:
    """Configuration for Granicus/Swagit data collection."""
    platform: str                  # CLASSIC_GRANICUS or NEW_SWAGIT
    subdomain: str                 # e.g. "cibolotx", "beaumonttx"
    city_name: str
    output_prefix: str
    storage: StorageBackend
    view_id: int = 1               # Classic Granicus only — publisher view ID
    lookback_days: int = 90
    council_keywords: list[str] = field(default_factory=lambda: [
        "city council", "town council", "board of aldermen",
        "village council", "city commission",
    ])
    download_pdfs: bool = True
    request_timeout: int = 30
    rate_limit_delay: float = 0.5


@dataclass
class GranicusResult:
    """Summary of collected Granicus/Swagit data."""
    platform: str = ""
    total_events: int = 0
    council_events: int = 0
    pdfs_downloaded: int = 0
    output_prefix: str = ""


# ============================================================================
# SHARED HELPERS
# ============================================================================

def _is_council_event(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _parse_month(s: str) -> int | None:
    return _MONTHS.get(s[:3].lower())


def _extract_pdf_url_from_html(html: str, base_url: str) -> str | None:
    """Find a .pdf URL in an HTML page (Granicus agenda viewer fallback)."""
    m = re.search(r'(?:href|src)=["\']([^"\']*\.pdf[^"\']*)["\']', html, re.IGNORECASE)
    if m:
        url = m.group(1)
        return url if url.startswith("http") else urljoin(base_url, url)
    return None


async def _download_pdf(
    client: httpx.AsyncClient,
    url: str,
    dest_key: str,
    storage: StorageBackend,
    label: str = "",
) -> bool:
    """Download a PDF to dest_key via storage. Returns True on success."""
    if storage.exists(dest_key):
        return True
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        # If we got HTML instead of a PDF, try to find the embedded PDF URL
        if "pdf" not in content_type and not str(resp.url).lower().endswith(".pdf"):
            pdf_url = _extract_pdf_url_from_html(resp.text, str(resp.url))
            if not pdf_url:
                print(f"     WARNING: No PDF content for {label}")
                return False
            resp = await client.get(pdf_url)
            resp.raise_for_status()
        storage.write_bytes(dest_key, resp.content)
        return True
    except Exception as e:
        # Granicus S3 bucket has cert hostname mismatch — retry without SSL verification.
        # SECURITY NOTE: This only triggers after a specific SSL cert error, not by default.
        # The Granicus CDN sometimes serves PDFs from S3 buckets with mismatched certs.
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSL" in str(e):
            print(f"     WARNING: SSL cert error for {label}, retrying without verification")
            try:
                async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=30) as insecure:
                    resp = await insecure.get(url)
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if "pdf" not in content_type and not str(resp.url).lower().endswith(".pdf"):
                        pdf_url = _extract_pdf_url_from_html(resp.text, str(resp.url))
                        if not pdf_url:
                            print(f"     WARNING: No PDF content for {label}")
                            return False
                        resp = await insecure.get(pdf_url)
                        resp.raise_for_status()
                    storage.write_bytes(dest_key, resp.content)
                    return True
            except Exception as e2:
                print(f"     WARNING: Failed to download {label} (even with SSL bypass): {e2}")
                return False
        print(f"     WARNING: Failed to download {label}: {e}")
        return False


# ============================================================================
# CLASSIC GRANICUS — RSS
# ============================================================================

def _parse_granicus_title(title: str) -> tuple[str, datetime | None]:
    """
    Parse Classic Granicus RSS title: "{Body} - {Mon DD, YYYY}".
    Returns (body_name, date).  Date may be None if parsing fails.
    """
    m = re.match(r"^(.+?)\s*-\s*(\w+)\s+(\d{1,2}),?\s+(\d{4})\s*$", title.strip())
    if not m:
        return title.strip(), None
    body = m.group(1).strip()
    month = _parse_month(m.group(2))
    if not month:
        return body, None
    try:
        return body, datetime(int(m.group(4)), month, int(m.group(3)))
    except ValueError:
        return body, None


def _parse_rfc2822(date_str: str) -> datetime | None:
    """Parse RFC 2822 pubDate: 'Tue, 24 Mar 2026 19:00:00 -0500'."""
    try:
        return datetime.strptime(date_str[:25].strip(), "%a, %d %b %Y %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(date_str[:16].strip(), "%d %b %Y %H:%M")
    except ValueError:
        return None


async def _collect_classic_granicus(
    config: GranicusConfig,
    client: httpx.AsyncClient,
) -> tuple[list[dict], int, int]:
    """
    Collect from Classic Granicus RSS feed.
    Returns (meetings, pdfs_downloaded, total_rss_items).
    """
    base_url = f"https://{config.subdomain}.granicus.com"
    rss_url = (
        f"{base_url}/ViewPublisherRSS.php"
        f"?view_id={config.view_id}&mode=agendas"
    )
    cutoff = datetime.now() - timedelta(days=config.lookback_days)

    resp = await client.get(rss_url)
    resp.raise_for_status()

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        print(f"  ERROR: Failed to parse RSS XML: {e}")
        return [], 0, 0

    items = root.findall(".//item")
    total = len(items)

    meetings: list[dict] = []
    pdf_count = 0

    for item in items:
        title_el = item.find("title")
        link_el = item.find("link")
        pub_date_el = item.find("pubDate")
        clip_id_el = item.find(f"{{{GRAN_NS}}}clipID")
        enclosure_el = item.find("enclosure")

        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        pub_date_str = (pub_date_el.text or "").strip() if pub_date_el is not None else ""
        clip_id = (clip_id_el.text or "").strip() if clip_id_el is not None else ""

        body_name, event_date = _parse_granicus_title(title)

        # Fallback: parse from pubDate if title date failed
        if event_date is None and pub_date_str:
            event_date = _parse_rfc2822(pub_date_str)

        # Skip items with no parseable date, or older than the lookback window
        if event_date is None or event_date < cutoff:
            continue

        # Filter to council-type body
        if not _is_council_event(body_name or title, config.council_keywords):
            continue

        date_str = event_date.strftime("%Y-%m-%d") if event_date else "unknown"

        # Prefer enclosure URL (direct PDF), then AgendaViewer, then link
        if enclosure_el is not None:
            agenda_url = enclosure_el.get("url", "")
        elif clip_id:
            agenda_url = (
                f"{base_url}/AgendaViewer.php"
                f"?view_id={config.view_id}&clip_id={clip_id}"
            )
        else:
            agenda_url = link

        meeting = {
            "date": date_str,
            "title": title,
            "body": body_name,
            "clipId": clip_id,
            "agendaUrl": agenda_url,
            "pubDate": pub_date_str,
        }
        meetings.append(meeting)

        # Download agenda PDF
        if config.download_pdfs and agenda_url:
            filename = f"{date_str}_agenda_{clip_id or 'noID'}.pdf"
            pdf_key = f"{config.output_prefix}/pdfs/{filename}"
            success = await _download_pdf(client, agenda_url, pdf_key, config.storage, label=filename)
            if success:
                pdf_count += 1
            await asyncio.sleep(config.rate_limit_delay)

    return meetings, pdf_count, total


# ============================================================================
# NEW SWAGIT — JSON API
# ============================================================================

def _parse_swagit_date(date_str: str) -> datetime | None:
    """Parse Swagit date strings: ISO, '{Mon DD, YYYY}', or '{YYYY-MM-DD}'."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str[:len(fmt)], fmt)
        except ValueError:
            pass
    m = re.match(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", date_str)
    if m:
        month = _parse_month(m.group(1))
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass
    return None


def _extract_swagit_date(item: dict) -> datetime | None:
    for key in ("date", "eventDate", "event_date", "startDate", "start_date"):
        val = item.get(key)
        if val:
            dt = _parse_swagit_date(str(val))
            if dt:
                return dt
    return None


def _extract_swagit_title(item: dict) -> str:
    for key in ("title", "name", "body_name", "eventName", "event_name"):
        val = item.get(key)
        if val:
            return str(val).strip()
    return ""


def _extract_swagit_agenda_url(item: dict) -> str:
    for key in ("agenda_url", "agendaUrl", "agenda_pdf_url", "agenda_pdf", "agenda"):
        val = item.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
    # Check nested files/attachments
    for key in ("attachments", "files", "documents"):
        for attach in item.get(key, []):
            if not isinstance(attach, dict):
                continue
            for ukey in ("url", "file_url", "download_url"):
                url = attach.get(ukey, "")
                if url and ".pdf" in url.lower():
                    return url
    return ""


async def _get_swagit_pdf_url(
    client: httpx.AsyncClient,
    subdomain: str,
    event_id,
) -> str:
    """
    Attempt to discover the agenda PDF URL for a Swagit event.

    Strategy:
      1. Check v2.swagit.com REST API for the meeting record.
      2. Scrape the event page on {subdomain}.new.swagit.com/videos/{id}.
    """
    # 1. v2.swagit.com REST API
    try:
        api_url = f"https://v2.swagit.com/api/v1/meetings/{event_id}"
        resp = await client.get(api_url)
        if resp.status_code == 200:
            data = resp.json()
            url = _extract_swagit_agenda_url(data)
            if url:
                return url
    except Exception:
        pass

    # 2. Scrape the event viewer page
    try:
        page_url = f"https://{subdomain}.new.swagit.com/videos/{event_id}"
        resp = await client.get(page_url)
        if resp.status_code == 200:
            url = _extract_pdf_url_from_html(resp.text, page_url)
            if url:
                return url
    except Exception:
        pass

    return ""


async def _collect_new_swagit(
    config: GranicusConfig,
    client: httpx.AsyncClient,
) -> tuple[list[dict], int, int]:
    """
    Collect from New Swagit JSON API.
    Returns (meetings, pdfs_downloaded, total_raw_events).
    """
    base_url = f"https://{config.subdomain}.new.swagit.com"
    cutoff = datetime.now() - timedelta(days=config.lookback_days)

    events_raw: list[dict] = []

    # Try city-council.json first, fall back to events.json
    for endpoint in ("city-council.json", "events.json"):
        page = 1
        endpoint_found = False
        stop_paging = False

        while not stop_paging:
            url = f"{base_url}/{endpoint}?page={page}"
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPStatusError, Exception):
                break

            # Normalize to a list of items
            if isinstance(data, list):
                page_items = data
            elif isinstance(data, dict):
                page_items = data.get("data") or data.get("results") or data.get("items") or []
                if not isinstance(page_items, list):
                    page_items = []
            else:
                break

            if not page_items:
                break

            endpoint_found = True

            for item in page_items:
                dt = _extract_swagit_date(item)
                if dt is not None and dt >= cutoff:
                    events_raw.append(item)
                elif dt is not None and dt < cutoff:
                    # Items are typically newest-first; hint to stop paging if past cutoff
                    stop_paging = True

            # Single-page list response — no further pages
            if isinstance(data, list):
                break

            # Check pagination metadata
            total = data.get("total") or data.get("total_count") or 0
            per_page = data.get("per_page") or data.get("perPage") or 10
            if total and page * per_page >= total:
                break
            page += 1
            await asyncio.sleep(config.rate_limit_delay)

        if endpoint_found:
            break

    total_raw = len(events_raw)

    # Filter to council-type body
    council_raw = [
        item for item in events_raw
        if _is_council_event(_extract_swagit_title(item), config.council_keywords)
    ]

    meetings: list[dict] = []
    pdf_count = 0

    for item in council_raw:
        event_id = item.get("id") or item.get("event_id") or ""
        title = _extract_swagit_title(item)
        dt = _extract_swagit_date(item)
        date_str = dt.strftime("%Y-%m-%d") if dt else str(item.get("date", ""))[:10]

        agenda_url = _extract_swagit_agenda_url(item)
        if not agenda_url and event_id and config.download_pdfs:
            agenda_url = await _get_swagit_pdf_url(client, config.subdomain, event_id)
            await asyncio.sleep(config.rate_limit_delay)

        meeting = {
            "date": date_str,
            "title": title,
            "eventId": str(event_id),
            "agendaUrl": agenda_url,
        }
        meetings.append(meeting)

        if config.download_pdfs and agenda_url:
            filename = f"{date_str}_agenda_{event_id}.pdf"
            pdf_key = f"{config.output_prefix}/pdfs/{filename}"
            success = await _download_pdf(client, agenda_url, pdf_key, config.storage, label=filename)
            if success:
                pdf_count += 1
            await asyncio.sleep(config.rate_limit_delay)

    return meetings, pdf_count, total_raw


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

async def collect_granicus(config: GranicusConfig) -> GranicusResult:
    """
    Collect meeting data from a Granicus or Swagit portal.

    Dispatches to the platform-specific collector based on config.platform.

    Output structure:
        {output_prefix}/
            events.json   — council meeting metadata list
            pdfs/         — downloaded agenda PDFs
    """

    async with httpx.AsyncClient(
        timeout=config.request_timeout,
        follow_redirects=True,
    ) as client:
        if config.platform == CLASSIC_GRANICUS:
            meetings, pdf_count, total_events = await _collect_classic_granicus(config, client)
        elif config.platform == NEW_SWAGIT:
            meetings, pdf_count, total_events = await _collect_new_swagit(config, client)
        else:
            raise ValueError(f"Unknown platform: {config.platform!r}")

    config.storage.write_json(f"{config.output_prefix}/events.json", meetings)


    return GranicusResult(
        platform=config.platform,
        total_events=total_events,
        council_events=len(meetings),
        pdfs_downloaded=pdf_count,
        output_prefix=config.output_prefix,
    )
