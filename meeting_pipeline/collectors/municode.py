"""
municode.py — Collect meeting data from Municode Meetings portals.

Municode Meetings (municodemeetings.com) is a Drupal 7-based portal that lists
city council agendas and minutes. PDFs are hosted on Azure Government Blob
Storage at mccmeetings.blob.core.usgovcloudapi.net/{container}-pubu/.

HTML structure (server-rendered, no JS required with proper User-Agent):
  <tr class="odd|even">
    <td data-th="Date">
      <span class="date-display-single" content="{ISO datetime}">04/06/2026 - 7:00pm</span>
    </td>
    <td data-th="Meeting">Regular Council Meeting</td>
    <td data-th="Agenda">
      <a href="https://mccmeetings.blob.core.usgovcloudapi.net/{container}/MEET-Agenda-{uuid}.pdf">
    </td>
    <td data-th="Minutes">
      <a href="https://mccmeetings.blob.core.usgovcloudapi.net/{container}/MEET-Minutes-{uuid}.pdf">
    </td>
  </tr>

The page URL is just the base domain (e.g. https://austell-ga.municodemeetings.com)
and lists all meetings chronologically (most recent first), filtered by the
"Agendas/Minutes" view which includes city council meetings.

Usage:
    from collectors.municode import MunicodeConfig, collect_municode

    config = MunicodeConfig(
        portal_url="https://austell-ga.municodemeetings.com",
        city_name="Austell",
        output_prefix="meeting_pipeline/sources/austell-GA/data/municode",
        storage=storage_backend,
        lookback_days=180,
    )
    result = await collect_municode(config)
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from meeting_pipeline.shared.storage import StorageBackend

# ============================================================================
# CONFIG AND RESULT
# ============================================================================

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class MunicodeConfig:
    """Configuration for Municode Meetings scraping."""
    portal_url: str        # e.g. "https://austell-ga.municodemeetings.com"
    city_name: str
    output_prefix: str     # Storage key prefix, e.g. ".../data/municode"
    storage: StorageBackend
    lookback_days: int = 365
    download_pdfs: bool = True
    request_timeout: int = 30


@dataclass
class MunicodeResult:
    """Summary of collected Municode data."""
    meetings_found: int = 0
    pdfs_downloaded: int = 0
    output_prefix: str = ""


# ============================================================================
# MAIN COLLECTOR
# ============================================================================

async def collect_municode(config: MunicodeConfig) -> MunicodeResult:
    """Collect meeting data from a Municode Meetings portal.

    Outputs:
      {output_prefix}/meetings.json — list of meeting dicts with date, title,
                                       agendaUrl, minutesUrl, allPdfs
      {output_prefix}/pdfs/{date}_{type}.pdf — downloaded PDF files
    """
    cutoff = datetime.now() - timedelta(days=config.lookback_days)
    result = MunicodeResult(output_prefix=config.output_prefix)

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(
        timeout=config.request_timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        url = config.portal_url.rstrip("/")
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR fetching {url}: {e}")
            return result

        html = resp.text
        meetings = _parse_meetings(html, cutoff, config.portal_url)

        # Save meetings.json
        config.storage.write_json(f"{config.output_prefix}/meetings.json", meetings)
        result.meetings_found = len(meetings)

        # Download PDFs
        if config.download_pdfs:
            pdf_count = await _download_pdfs(client, config, meetings)
            result.pdfs_downloaded = pdf_count

    return result


# ============================================================================
# HTML PARSING
# ============================================================================

def _parse_meetings(html: str, cutoff: datetime, portal_url: str) -> list[dict]:
    """Parse meeting rows from the Municode Meetings HTML page.

    Returns a list of dicts with:
      date       — YYYY-MM-DD
      time       — "7:00pm" or ""
      title      — "Regular Council Meeting"
      agendaUrl  — blob PDF URL or ""
      minutesUrl — blob PDF URL or ""
      allPdfs    — list of all PDF URLs in this row
      sourceUrl  — portal URL
    """
    meetings = []

    # Each meeting is in a <tr class="odd"> or <tr class="even">
    # The date is in a <span class="date-display-single" content="{ISO datetime}">
    # The title is in <td data-th="Meeting">
    # Agenda PDF in <td data-th="Agenda"> <a href="...">
    # Minutes PDF in <td data-th="Minutes"> <a href="...">
    row_pattern = re.compile(
        r'<tr\s+class="(?:odd|even)"[^>]*>(.*?)</tr>',
        re.S | re.I,
    )

    for row_m in row_pattern.finditer(html):
        row = row_m.group(1)

        # Extract ISO datetime from content attribute of date-display-single span
        date_m = re.search(
            r'date-display-single[^>]*content="([^"]+)"',
            row, re.I
        )
        if not date_m:
            # Fallback: try to parse from text like "04/06/2026 - 7:00pm"
            date_text_m = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', row)
            if not date_text_m:
                continue
            try:
                dt = datetime.strptime(date_text_m.group(1), "%m/%d/%Y")
            except ValueError:
                continue
            iso_date = dt.strftime("%Y-%m-%d")
            time_str = ""
        else:
            iso_str = date_m.group(1)  # e.g. "2026-04-06T19:00:00-04:00"
            try:
                # Parse just the date part
                dt = datetime.fromisoformat(iso_str[:19])
            except ValueError:
                try:
                    dt = datetime.fromisoformat(iso_str[:10])
                except ValueError:
                    continue
            iso_date = dt.strftime("%Y-%m-%d")
            # Extract time if present
            time_m = re.search(r'T(\d{2}:\d{2})', iso_str)
            time_str = time_m.group(1) if time_m else ""

        if dt < cutoff:
            continue

        # Extract meeting title from data-th="Meeting" cell
        title_m = re.search(
            r'data-th="Meeting"[^>]*>(.*?)</td>',
            row, re.S | re.I
        )
        title = ""
        if title_m:
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()

        # Collect all blob/PDF links in this row
        all_pdfs = re.findall(
            r'href="(https://mccmeetings\.blob\.core\.usgovcloudapi\.net/[^"]+\.pdf)"',
            row, re.I
        )

        # Also catch non-blob PDF links that might appear
        other_pdfs = re.findall(
            r'href="([^"]+\.pdf)"',
            row, re.I
        )
        for pdf in other_pdfs:
            if pdf not in all_pdfs:
                all_pdfs.append(pdf)

        # Categorize by type (Agenda vs Minutes vs Packet)
        agenda_url = ""
        minutes_url = ""

        # Check the Agenda column specifically
        agenda_cell_m = re.search(
            r'data-th="Agenda"[^>]*>(.*?)</td>',
            row, re.S | re.I
        )
        if agenda_cell_m:
            agenda_links = re.findall(r'href="([^"]+)"', agenda_cell_m.group(1))
            if agenda_links:
                agenda_url = agenda_links[0]

        # Check the Minutes column
        minutes_cell_m = re.search(
            r'data-th="Minutes"[^>]*>(.*?)</td>',
            row, re.S | re.I
        )
        if minutes_cell_m:
            minutes_links = re.findall(r'href="([^"]+)"', minutes_cell_m.group(1))
            if minutes_links:
                minutes_url = minutes_links[0]

        # If no agenda found via cell, try by filename pattern
        if not agenda_url:
            for pdf in all_pdfs:
                if "MEET-Agenda" in pdf or "Agenda" in pdf:
                    agenda_url = pdf
                    break

        if not minutes_url:
            for pdf in all_pdfs:
                if "MEET-Minutes" in pdf or "Minutes" in pdf:
                    minutes_url = pdf
                    break

        meetings.append({
            "date": iso_date,
            "time": time_str,
            "title": title,
            "agendaUrl": agenda_url,
            "minutesUrl": minutes_url,
            "allPdfs": all_pdfs,
            "sourceUrl": portal_url,
        })

    # Sort by date descending (most recent first)
    meetings.sort(key=lambda m: m["date"], reverse=True)
    return meetings


# ============================================================================
# PDF DOWNLOAD
# ============================================================================

async def _download_pdfs(
    client: httpx.AsyncClient,
    config: MunicodeConfig,
    meetings: list[dict],
) -> int:
    """Download agenda PDFs for all meetings. Returns count of PDFs saved."""
    downloaded = 0
    for meeting in meetings:
        date = meeting.get("date", "unknown")
        agenda_url = meeting.get("agendaUrl", "")
        minutes_url = meeting.get("minutesUrl", "")
        all_pdfs = meeting.get("allPdfs", [])

        # Build a list of (filename_type, url) for everything to download
        to_download: list[tuple[str, str]] = []
        for pdf_url in all_pdfs:
            if not pdf_url:
                continue
            fn = pdf_url.split("/")[-1].split("?")[0]
            if pdf_url == minutes_url or "minutes" in fn.lower() or "MEET-Minutes" in fn:
                pdf_type = "minutes"
            elif "packet" in fn.lower() or "MEET-Packet" in fn:
                pdf_type = "packet"
            else:
                pdf_type = "agenda"
            to_download.append((pdf_type, pdf_url))

        # Fall back to agendaUrl/minutesUrl if allPdfs was empty
        if not to_download:
            if agenda_url:
                to_download.append(("agenda", agenda_url))
            if minutes_url:
                to_download.append(("minutes", minutes_url))

        for pdf_type, pdf_url in to_download:
            key = f"{config.output_prefix}/pdfs/{date}_{pdf_type}.pdf"

            if config.storage.exists(key):
                downloaded += 1
                continue

            try:
                resp = await client.get(pdf_url)
                if resp.status_code == 200 and len(resp.content) > 5000 and (resp.content[:4] == b"%PDF" or "pdf" in resp.headers.get("content-type", "").lower()):
                        config.storage.write_bytes(key, resp.content)
                        downloaded += 1
            except Exception as e:
                print(f"    WARN: PDF download failed for {pdf_url}: {e}")

    return downloaded


# ============================================================================
# CLI
# ============================================================================

async def _main_cli():
    import argparse

    from meeting_pipeline.shared.config import AgentConfig, get_storage

    parser = argparse.ArgumentParser(description="Collect Municode meeting data")
    parser.add_argument("--city-slug", required=True, help="e.g. austell-GA")
    parser.add_argument("--url", required=True, help="Portal URL, e.g. https://austell-ga.municodemeetings.com")
    parser.add_argument("--city-name", default="", help="City name for display")
    parser.add_argument("--no-pdfs", action="store_true")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    output_prefix = f"{cfg.sources_prefix}/{args.city_slug}/data/municode"
    mc_cfg = MunicodeConfig(
        portal_url=args.url,
        city_name=args.city_name or args.city_slug,
        output_prefix=output_prefix,
        storage=storage,
        download_pdfs=not args.no_pdfs,
    )
    result = await collect_municode(mc_cfg)


if __name__ == "__main__":
    asyncio.run(_main_cli())
