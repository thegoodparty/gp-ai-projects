"""
collect_playwright_llm_batch.py — Collect meeting data using Playwright + LLM vision.

Fallback collector for cities where traditional HTML scraping fails:
  - JS-rendered SPAs (CivicClerk, Diligent, Novus)
  - Complex ASP.NET forms (MunicipalOne)
  - Custom WordPress calendars
  - Bot-protected pages

Uses Playwright to render the page and Gemini Flash vision to identify agenda links.
Cost: ~$0.01-0.05 per city.

Usage:
    uv run python meeting_pipeline/scripts/collect_playwright_llm_batch.py
    uv run python meeting_pipeline/scripts/collect_playwright_llm_batch.py --city mason-OH
    uv run python meeting_pipeline/scripts/collect_playwright_llm_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from collectors.playwright_llm_scraper import PlaywrightLLMConfig, collect_with_llm_vision

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# Cities that need the Playwright + LLM approach.
# These failed with traditional HTML scrapers or have no collector.
KNOWN_CITIES: list[dict] = [
    # Ad-hoc cities — previously required manual PDF download
    {
        "city_slug": "mason-OH",
        "city": "Mason",
        "state": "OH",
        "url": "https://www.imaginemason.org/city-government/city-council/agendas-and-minutes/",
        "notes": "WordPress calendar — JS-rendered detail pages",
    },
    {
        "city_slug": "matthews-NC",
        "city": "Matthews",
        "state": "NC",
        "url": "https://matthewsnc.municipalone.com/agendalist.aspx?categoryid=9947",
        "notes": "MunicipalOne ASP.NET — docview.aspx PDF links",
    },
    # Cities with NO data yet — all need JS rendering
    {
        "city_slug": "cibolo-TX",
        "city": "Cibolo",
        "state": "TX",
        "url": "https://cibolotx.granicus.com/ViewPublisher.php?view_id=1",
        "notes": "Granicus Classic — RSS returned 0 events, try rendered page",
    },
    {
        "city_slug": "delaware-OH",
        "city": "Delaware",
        "state": "OH",
        "url": "https://www.delawareohio.net/AgendaCenter",
        "notes": "CivicPlus — scraper didn't reach it",
    },
    {
        "city_slug": "kyle-TX",
        "city": "Kyle",
        "state": "TX",
        "url": "https://kyletx.new.swagit.com/views/78",
        "notes": "Swagit — no data collected yet",
    },
    {
        "city_slug": "loveland-OH",
        "city": "Loveland",
        "state": "OH",
        "url": "https://loveland.community.diligentoneplatform.com/meetings",
        "notes": "Diligent One Platform — fully JS-rendered SPA",
    },
    {
        "city_slug": "westerville-OH",
        "city": "Westerville",
        "state": "OH",
        "url": "https://westerville-oh.portal.civicclerk.com",
        "notes": "CivicClerk SPA — needs browser rendering",
    },
]


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect meeting data using Playwright + LLM vision"
    )
    parser.add_argument("--city", help="Single city slug (e.g. mason-OH)")
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    parser.add_argument("--lookback", type=int, default=90, help="Days to look back")
    args = parser.parse_args()

    cities = KNOWN_CITIES
    if args.city:
        cities = [c for c in cities if c["city_slug"] == args.city]
        if not cities:
            print(f"City {args.city} not found in registry")
            print("Available cities:")
            for c in KNOWN_CITIES:
                print(f"  {c['city_slug']:20s} — {c['notes']}")
            return

    results = []
    for city_info in cities:
        slug = city_info["city_slug"]
        output_dir = SOURCES_DIR / slug / "data" / "playwright_llm"

        print(f"\n{'=' * 60}")
        print(f"Collecting: {slug}")
        print(f"  URL: {city_info['url']}")
        print(f"  Notes: {city_info['notes']}")
        print(f"{'=' * 60}")

        config = PlaywrightLLMConfig(
            url=city_info["url"],
            city_name=city_info["city"],
            output_dir=output_dir,
            lookback_days=args.lookback,
            download_pdfs=not args.no_pdfs,
        )

        result = await collect_with_llm_vision(config)
        results.append({
            "city": slug,
            "total_links": result.total_links,
            "agenda_links": result.agenda_links,
            "pdfs": result.pdfs_downloaded,
            "error": result.error,
        })

    # Summary
    print(f"\n{'=' * 60}")
    print("PLAYWRIGHT + LLM BATCH COLLECTION SUMMARY")
    print(f"{'=' * 60}")
    for r in results:
        status = "ERROR" if r["error"] else "OK"
        print(
            f"  {r['city']:20s}: {r['agenda_links']} agendas, "
            f"{r['pdfs']} PDFs [{status}]"
        )
        if r["error"]:
            print(f"    Error: {r['error']}")

    # Save results
    summary_path = SOURCES_DIR / "playwright_llm_collection_results.json"
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
