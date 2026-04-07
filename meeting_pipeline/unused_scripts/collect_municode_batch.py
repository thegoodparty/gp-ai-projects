"""
collect_municode_batch.py — Batch-collect Municode meeting data.

Municode hosts meeting agendas at meetings.municode.com. The portal renders
meeting lists as HTML, and agenda PDFs are stored in Azure Blob Storage at
mccmeetings.blob.core.usgovcloudapi.net/{container}-pubu/.

Usage:
    uv run python meeting_pipeline/scripts/collect_municode_batch.py
    uv run python meeting_pipeline/scripts/collect_municode_batch.py --city apex-NC
    uv run python meeting_pipeline/scripts/collect_municode_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# Known Municode cities — cid and ppid from their portal URLs.
# These come from the source.json discovery and city-collection-notes.md.
KNOWN_CITIES: list[dict] = [
    {
        "city_slug": "apex-NC",
        "city": "Apex",
        "state": "NC",
        "cid": "APEXNC",
        "ppid": "ec03baf8-8c93-4368-a28a-e658e962cbd1",
    },
    {
        "city_slug": "grand-prairie-TX",
        "city": "Grand Prairie",
        "state": "TX",
        "cid": "GPTX",
        "ppid": "0a01ee0f-2750-46df-ae4f-bb7135af7019",
    },
    {
        "city_slug": "tomball-TX",
        "city": "Tomball",
        "state": "TX",
        "cid": "TOMBALLTX",
        "ppid": "",  # Tomball uses a different subdomain format — no ppid needed
        "base_url": "https://tomball-tx.municodemeetings.com",
    },
    {
        "city_slug": "mount-vernon-TX",
        "city": "Mount Vernon",
        "state": "TX",
        "cid": "MTVERNONTX",
        "ppid": "",  # Same subdomain format as Tomball
        "base_url": "https://mountvernon-tx.municodemeetings.com",
    },
]


async def collect_municode_city(
    city_info: dict,
    output_dir: Path,
    lookback_days: int = 90,
    download_pdfs: bool = True,
) -> dict:
    """Collect meeting data from a Municode portal."""
    city = city_info["city"]
    cid = city_info["cid"]
    ppid = city_info.get("ppid", "")
    base_url = city_info.get("base_url", "https://meetings.municode.com")

    print(f"  Collecting {city} (cid={cid})...")

    url = f"{base_url}/PublishPage/index?cid={cid}&ppid={ppid}&p=0"

    cutoff = datetime.now() - timedelta(days=lookback_days)
    events = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Fetch the meeting list page
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR fetching {url}: {e}")
            return {"city": city, "events": 0, "error": str(e)}

        soup = BeautifulSoup(resp.text, "html.parser")

        # Parse meeting entries — Municode uses <div class="meeting-row"> or similar
        # Look for links to agenda PDFs (blob storage URLs)
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "blob.core" in href and href.endswith(".pdf"):
                pdf_links.append(href)
            elif "MEET-Agenda" in href or "MEET-Packet" in href:
                pdf_links.append(href)

        # Also look for meeting dates in the page
        date_pattern = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
        meeting_rows = soup.find_all(class_=re.compile(r"meeting|event|row"))

        # Parse structured meeting data from the page
        for row in meeting_rows:
            text = row.get_text(strip=True)
            date_match = date_pattern.search(text)
            if date_match:
                try:
                    date_str = date_match.group(1)
                    dt = datetime.strptime(date_str, "%m/%d/%Y")
                    if dt < cutoff:
                        continue
                    # Find PDF link in this row
                    row_links = row.find_all("a", href=True)
                    agenda_url = None
                    all_pdfs = []
                    for link in row_links:
                        href = link["href"]
                        if not href.startswith("http"):
                            href = urljoin(base_url, href)
                        if "blob.core" in href or href.endswith(".pdf"):
                            all_pdfs.append(href)
                            if "Agenda" in href and not agenda_url:
                                agenda_url = href

                    if agenda_url or all_pdfs:
                        events.append({
                            "date": dt.strftime("%Y-%m-%d"),
                            "title": text[:100].strip(),
                            "agendaUrl": agenda_url or (all_pdfs[0] if all_pdfs else None),
                            "allPdfs": all_pdfs,
                        })
                except ValueError:
                    pass

        # If structured parsing didn't work, fall back to existing events.json
        if not events:
            existing = output_dir / "events.json"
            if existing.exists():
                print(f"  HTML parsing found 0 events, using existing events.json")
                events = json.load(open(existing))

        # Save events
        output_dir.mkdir(parents=True, exist_ok=True)
        events_file = output_dir / "events.json"
        with open(events_file, "w") as f:
            json.dump(events, f, indent=2)
        print(f"  Found {len(events)} events")

        # Download PDFs
        pdfs_dir = output_dir / "pdfs"
        pdfs_dir.mkdir(exist_ok=True)
        downloaded = 0

        if download_pdfs:
            for event in events:
                pdf_url = event.get("agendaUrl")
                if not pdf_url:
                    continue

                date = event.get("date", "unknown")
                filename = f"{date}_agenda.pdf"
                filepath = pdfs_dir / filename

                if filepath.exists() and filepath.stat().st_size > 5000:
                    downloaded += 1
                    continue

                try:
                    pdf_resp = await client.get(pdf_url)
                    pdf_resp.raise_for_status()
                    filepath.write_bytes(pdf_resp.content)
                    downloaded += 1
                    print(f"    Downloaded: {filename} ({len(pdf_resp.content)} bytes)")
                except Exception as e:
                    print(f"    ERROR downloading {filename}: {e}")

        print(f"  {downloaded} PDFs downloaded")
        return {"city": city, "events": len(events), "pdfs": downloaded}


async def main():
    parser = argparse.ArgumentParser(description="Collect Municode meeting data")
    parser.add_argument("--city", help="Single city slug (e.g. apex-NC)")
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    parser.add_argument("--lookback", type=int, default=90, help="Days to look back")
    args = parser.parse_args()

    cities = KNOWN_CITIES
    if args.city:
        cities = [c for c in cities if c["city_slug"] == args.city]
        if not cities:
            print(f"City {args.city} not found in registry")
            return

    results = {}
    for city_info in cities:
        slug = city_info["city_slug"]
        output_dir = SOURCES_DIR / slug / "data" / "municode"

        print(f"\n{'='*60}")
        print(f"Collecting: {slug}")
        print(f"{'='*60}")

        result = await collect_municode_city(
            city_info, output_dir,
            lookback_days=args.lookback,
            download_pdfs=not args.no_pdfs,
        )
        results[slug] = result

    print(f"\n{'='*60}")
    print("SUMMARY")
    for slug, r in results.items():
        print(f"  {slug}: {r.get('events', 0)} events, {r.get('pdfs', 0)} PDFs")


if __name__ == "__main__":
    asyncio.run(main())
