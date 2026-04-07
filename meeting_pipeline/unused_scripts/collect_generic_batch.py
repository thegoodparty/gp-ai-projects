"""
collect_generic_batch.py — Batch-collect agenda PDFs from Tier 3 custom HTML pages.

Each city has a unique HTML structure, so this script uses a per-city config
registry with the appropriate scraping strategy.

Usage:
    uv run python meeting_pipeline/scripts/collect_generic_batch.py
    uv run python meeting_pipeline/scripts/collect_generic_batch.py --city dublin-OH
    uv run python meeting_pipeline/scripts/collect_generic_batch.py --tier easy
    uv run python meeting_pipeline/scripts/collect_generic_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from collectors.generic_html_scraper import GenericScraperConfig, collect_generic

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# ============================================================================
# PER-CITY CONFIGURATION REGISTRY
# ============================================================================
# Grouped by difficulty tier. Each entry has:
#   url:      Main page to scrape
#   strategy: How to extract PDF links (direct_pdf, document_center, archive_aspx, rss_feed)
#   keyword:  Filter links by keyword (default: "agenda")
#   selector: Optional CSS selector to narrow link search
#   tier:     easy | medium | hard
#   notes:    Human-readable notes about this city's page

CITY_CONFIGS: dict[str, dict] = {
    # ========== EASY — direct PDF links in simple HTML ==========
    "dublin-OH": {
        "city": "Dublin",
        "state": "OH",
        "url": "https://cityofdublin.org/government/council-meetings-and-agendas",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "easy",
        "notes": "Table with Date/Agenda/Minutes columns, direct .pdf hrefs",
    },
    "hamilton-OH": {
        "city": "Hamilton",
        "state": "OH",
        "url": "https://hamilton-oh.gov/city-meetings",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "Squarespace with mixed doc types. Actual council agendas may be on Google Drive. ADRB agendas available.",
    },
    "new-bern-NC": {
        "city": "New Bern",
        "state": "NC",
        "url": "https://www.newbernnc.gov/departments/administration/meeting_agendas_and_minutes.php",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "easy",
        "notes": "Revize CMS, direct .pdf hrefs, year-grouped. Uses 'Board of Aldermen' not 'City Council'.",
    },
    "temple-TX": {
        "city": "Temple",
        "state": "TX",
        "url": "https://www.templetx.gov/departments/administration/city_secretary/recent_agendas___minutes/",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "easy",
        "notes": "Revize CMS, simple page with direct PDF link",
    },
    "warren-OH": {
        "city": "Warren",
        "state": "OH",
        "url": "https://www.cityofwarren.org/government/city-council/",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "warren.org has no agenda PDFs. cityofwarren.org (different domain) has the council page.",
    },
    "cuyahoga-falls-OH": {
        "city": "Cuyahoga Falls",
        "state": "OH",
        "url": "https://cityofcf.com/city-council/files",
        "strategy": "two_hop",
        "selector": "a[href*='/files/city-council-schedules-agendas-legislation-']",
        "keyword": "agenda",
        "tier": "easy",
        "notes": "Drupal two-hop: index page links to subpages, subpages have PDFs. Filter for 'Council Agenda' in filename.",
    },
    "rocky-mount-NC": {
        "city": "Rocky Mount",
        "state": "NC",
        "url": "https://rockymountnc.gov/497/Council-Agendas-Minutes",
        "strategy": "document_center",
        "keyword": "agenda",
        "tier": "easy",
        "notes": "CivicPlus tabbed tables by year, /DocumentCenter/View/{id} links serve PDFs",
    },

    # ========== MEDIUM — one-hop indirection or external portals ==========
    "asheville-NC": {
        "city": "Asheville",
        "state": "NC",
        "url": "https://ashevillenc.gov/government/city-council-agenda/",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "WordPress, links go to Google Docs. May need to extract export URL.",
    },
    "burlington-NC": {
        "city": "Burlington",
        "state": "NC",
        "url": "https://burlingtonnc.gov/Archive.aspx?AMID=44",
        "strategy": "archive_aspx",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "CivicPlus Archive.aspx, ADID links serve PDFs directly when fetched",
    },
    "lima-OH": {
        "city": "Lima",
        "state": "OH",
        "url": "https://limaohio.gov/98/City-Council",
        "strategy": "document_center",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "CivicPlus with /DocumentCenter/View/{id} pattern. Also has PrimeGov portal.",
    },
    "salisbury-NC": {
        "city": "Salisbury",
        "state": "NC",
        "url": "https://salisburync.gov/Government/City-Council/Minutes-and-Agendas",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "DNN with LinkClick.aspx obfuscation. Links may still work as direct downloads.",
    },
    "stow-OH": {
        "city": "Stow",
        "state": "OH",
        "url": "https://stowohio.gov/510/Public-Meeting-Information",
        "strategy": "document_center",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "CivicPlus landing page, agendas in linked Agenda Center sub-page",
    },

    # ========== HARD — ASP.NET ViewState, calendar-based, JS-rendered ==========
    # These are included but may fail — skip with --tier easy or --tier medium
    "jacksonville-NC": {
        "city": "Jacksonville",
        "state": "NC",
        "url": "https://jacksonvillenc.gov/Calendar.aspx",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "hard",
        "notes": "Calendar-based, need to navigate into events. iCal feed at /iCalendar.aspx may help.",
    },
    "lancaster-TX": {
        "city": "Lancaster",
        "state": "TX",
        "url": "https://lancaster-tx.com/Archive.aspx",
        "strategy": "archive_aspx",
        "keyword": "agenda",
        "tier": "hard",
        "notes": "Dropdown-based form submission. Requires POST with AMID/ADID.",
    },
    "mason-OH": {
        "city": "Mason",
        "state": "OH",
        "url": "https://imaginemason.org/calendar/",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "hard",
        "notes": "WordPress calendar plugin. Events link to detail pages with PDFs.",
    },
    "matthews-NC": {
        "city": "Matthews",
        "state": "NC",
        "url": "https://matthewsnc.gov/agendalist.aspx?categoryid=9947",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "hard",
        "notes": "Granicus/Telerik ASP.NET, docview.aspx and agendaview.aspx patterns",
    },
    "monroe-NC": {
        "city": "Monroe",
        "state": "NC",
        "url": "https://monroenc.org/Archive.aspx",
        "strategy": "archive_aspx",
        "keyword": "agenda",
        "tier": "hard",
        "notes": "ASP.NET ViewState + dropdown form, frmArchiveNavigation with AJAX postback",
    },
    "statesville-NC": {
        "city": "Statesville",
        "state": "NC",
        "url": "https://statesvillenc.net/agendas-minutes/",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "hard",
        "notes": "Returns 403 to automated requests. Needs headless browser.",
    },

    # ========== EXTERNAL PORTAL — redirect to other platforms ==========
    # Clayton and Marysville redirect to CivicClerk/MuniCode respectively.
    # Skipped for now — need separate platform-specific scrapers.
    # "clayton-NC": external → claytonnc.portal.civicclerk.com (CivicClerk SPA)
    # "marysville-OH": external → library.municode.com (MuniCode)

    # ========== CIVICCLERK SPA FALLBACK — city websites ==========
    # These cities have CivicClerk SPA portals (no OData API), but their city
    # websites have direct PDF links for council agendas.
    "hickory-NC": {
        "city": "Hickory",
        "state": "NC",
        "url": "https://hickorync.gov/agendas-and-minutes",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "easy",
        "verify_ssl": False,
        "notes": "Drupal 10 site. Direct .pdf hrefs under /sites/default/files/hickoryncgov/Council/Agendas/. CivicClerk portal (hickorync) has no OData API. SSL cert issue — verify disabled.",
    },
    "belton-TX": {
        "city": "Belton",
        "state": "TX",
        "url": "https://beltontexas.gov/",
        "follow_url": "https://beltontexas.gov/government/city_council/city_council_agendas_and_minutes.php",
        "strategy": "direct_pdf",
        "keyword": "agenda",
        "tier": "medium",
        "notes": "Revize CMS. PDF hrefs are relative to site root. Set url=site root so urljoin resolves correctly; follow_url fetches the actual page. CivicClerk portal (beltontx) has no OData API.",
    },
}


def get_cities(
    filter_city: str | None = None,
    filter_tier: str | None = None,
) -> list[tuple[str, dict]]:
    """Return filtered list of (city_slug, config) tuples."""
    results = []
    for slug, cfg in CITY_CONFIGS.items():
        if filter_city and slug != filter_city:
            continue
        if filter_tier:
            tiers = filter_tier.split(",")
            if cfg["tier"] not in tiers:
                continue
        results.append((slug, cfg))
    return results


async def main():
    parser = argparse.ArgumentParser(description="Batch-collect agenda PDFs from Tier 3 HTML pages")
    parser.add_argument("--city", help="Collect only this city slug (e.g. dublin-OH)")
    parser.add_argument("--tier", help="Filter by tier: easy, medium, hard, or comma-separated (e.g. easy,medium)")
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    parser.add_argument("--lookback", type=int, default=180, help="Days to look back (default: 180)")
    args = parser.parse_args()

    cities = get_cities(filter_city=args.city, filter_tier=args.tier)

    if not cities:
        print("No cities matched the filter.")
        print(f"  Available: {', '.join(CITY_CONFIGS.keys())}")
        return

    # Group by tier for display
    by_tier: dict[str, list] = {}
    for slug, cfg in cities:
        by_tier.setdefault(cfg["tier"], []).append(slug)

    print(f"Found {len(cities)} cities to collect:")
    for tier in ["easy", "medium", "hard"]:
        if tier in by_tier:
            print(f"  {tier.upper()}: {', '.join(by_tier[tier])}")
    print()

    results = []

    for slug, cfg in cities:
        output_dir = SOURCES_DIR / slug / "data" / "html_scrape"

        config = GenericScraperConfig(
            url=cfg["url"],
            city_name=cfg["city"],
            output_dir=output_dir,
            strategy=cfg["strategy"],
            selector=cfg.get("selector"),
            keyword_filter=cfg.get("keyword", "agenda"),
            lookback_days=args.lookback,
            download_pdfs=not args.no_pdfs,
            follow_url=cfg.get("follow_url"),
            verify_ssl=cfg.get("verify_ssl", True),
        )

        print(f"\n{'='*60}")
        print(f"Collecting: {slug} ({cfg['tier']})")
        print(f"  {cfg['notes']}")
        print(f"{'='*60}\n")

        try:
            result = await collect_generic(config)
            results.append({
                "city": slug,
                "tier": cfg["tier"],
                "status": "success",
                "meetings": result.meetings_found,
                "pdfs": result.pdfs_downloaded,
            })
        except Exception as e:
            print(f"\nERROR collecting {slug}: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "city": slug,
                "tier": cfg["tier"],
                "status": "error",
                "error": str(e),
            })

    # Summary
    print(f"\n{'='*60}")
    print("GENERIC HTML BATCH COLLECTION SUMMARY")
    print(f"{'='*60}")

    successes = [r for r in results if r["status"] == "success"]
    errors = [r for r in results if r["status"] == "error"]

    total_meetings = sum(r.get("meetings", 0) for r in successes)
    total_pdfs = sum(r.get("pdfs", 0) for r in successes)

    print(f"  Successful: {len(successes)}/{len(results)}")
    print(f"  Total meetings found: {total_meetings}")
    print(f"  Total PDFs downloaded: {total_pdfs}")
    print()

    for tier in ["easy", "medium", "hard"]:
        tier_results = [r for r in successes if r["tier"] == tier]
        if tier_results:
            print(f"  {tier.upper()}:")
            for r in tier_results:
                print(f"    {r['city']}: {r['meetings']} meetings, {r['pdfs']} PDFs")

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for r in errors:
            print(f"    {r['city']} ({r['tier']}): {r['error']}")

    # Save results
    summary_path = SOURCES_DIR / "generic_html_collection_results.json"
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
