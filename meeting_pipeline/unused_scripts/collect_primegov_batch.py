"""
collect_primegov_batch.py — Collect meeting data from PrimeGov portals.

PrimeGov provides a REST API at primegov.com/api/v2/ and stores agenda PDFs
in Azure Blob Storage at pgwest.blob.core.windows.net/{container}/.

Currently only Beaumont TX uses PrimeGov in our pilot.

Usage:
    uv run python meeting_pipeline/scripts/collect_primegov_batch.py
    uv run python meeting_pipeline/scripts/collect_primegov_batch.py --city beaumont-TX
    uv run python meeting_pipeline/scripts/collect_primegov_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# PrimeGov cities. API base is https://beaumonttexas.primegov.com/api/v2/
# PDF storage: pgwest.blob.core.windows.net/{container}/Meetings/{id}/Agenda_*.pdf
KNOWN_CITIES: list[dict] = [
    {
        "city_slug": "beaumont-TX",
        "city": "Beaumont",
        "state": "TX",
        "api_base": "https://beaumonttexas.primegov.com/api/v2",
        "container": "beaumonttexas",
        "council_keywords": ["city council"],
    },
    {
        "city_slug": "temple-TX",
        "city": "Temple",
        "state": "TX",
        "api_base": "https://cityoftemple.primegov.com/api/v2",
        "container": "cityoftemple",
        "council_keywords": ["city council"],
    },
]


async def collect_primegov_city(
    city_info: dict,
    output_dir: Path,
    lookback_days: int = 90,
    download_pdfs: bool = True,
) -> dict:
    """Collect meeting data from a PrimeGov API."""
    city = city_info["city"]
    api_base = city_info["api_base"]
    council_keywords = city_info.get("council_keywords", ["city council"])

    cutoff = datetime.now() - timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    print(f"  Collecting {city} from PrimeGov API...")

    events = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # PrimeGov calendar API
        try:
            resp = await client.get(
                f"{api_base}/PublicPortal/ListUpcomingMeetings",
                params={"StartDate": cutoff_str},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            # Fall back to existing events.json if API fails
            existing = output_dir / "events.json"
            if existing.exists():
                print(f"  API failed ({e}), using existing events.json")
                data = json.load(open(existing))
                if isinstance(data, list):
                    events = data
                    data = None
            else:
                print(f"  ERROR: {e}")
                return {"city": city, "events": 0, "error": str(e)}

        if data is not None:
            meetings = data if isinstance(data, list) else data.get("meetings", data.get("data", []))
            for m in meetings:
                title = m.get("title", m.get("Title", ""))
                body = m.get("body", m.get("Body", ""))
                date = m.get("date", m.get("Date", ""))

                # Filter to council meetings
                combined = f"{title} {body}".lower()
                if not any(kw in combined for kw in council_keywords):
                    continue

                meeting_id = m.get("id", m.get("Id", ""))
                agenda_url = m.get("agendaUrl", m.get("AgendaUrl", ""))

                events.append({
                    "id": meeting_id,
                    "date": date[:10] if date else "",
                    "title": title,
                    "body": body or "City Council",
                    "pdf_url": agenda_url,
                })

        # If no events from API, check existing
        if not events:
            existing = output_dir / "events.json"
            if existing.exists():
                events = json.load(open(existing))
                print(f"  Using existing events.json ({len(events)} events)")

        # Save events
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "events.json", "w") as f:
            json.dump(events, f, indent=2)
        print(f"  Found {len(events)} events")

        # Download PDFs
        pdfs_dir = output_dir / "pdfs"
        pdfs_dir.mkdir(exist_ok=True)
        downloaded = 0

        if download_pdfs:
            for event in events:
                pdf_url = event.get("pdf_url")
                if not pdf_url:
                    continue

                date = event.get("date", "unknown")
                mid = event.get("id", "noID")
                filename = f"{date}_agenda_{mid}.pdf"
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
    parser = argparse.ArgumentParser(description="Collect PrimeGov meeting data")
    parser.add_argument("--city", help="Single city slug")
    parser.add_argument("--no-pdfs", action="store_true")
    parser.add_argument("--lookback", type=int, default=90)
    args = parser.parse_args()

    cities = KNOWN_CITIES
    if args.city:
        cities = [c for c in cities if c["city_slug"] == args.city]

    results = {}
    for city_info in cities:
        slug = city_info["city_slug"]
        output_dir = SOURCES_DIR / slug / "data" / "primegov"

        print(f"\n{'='*60}")
        print(f"Collecting: {slug}")
        print(f"{'='*60}")

        result = await collect_primegov_city(
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
