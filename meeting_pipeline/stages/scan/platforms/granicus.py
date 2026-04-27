"""
Granicus scanner — lightweight scan for upcoming meetings.

Returns list of meeting dicts with date, title, agenda_posted, agenda_url.
No PDFs downloaded — that is the collection stage.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.constants import LOOKBACK_DAYS, LOOKAHEAD_DAYS


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



