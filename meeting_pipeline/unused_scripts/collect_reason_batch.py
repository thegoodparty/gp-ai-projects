"""
collect_reason_batch.py — Run the agentic browser collector (reason.py) against
cities that have a source URL but no usable platform-specific collector.

Saves PDFs directly to sources/{city}/playwright_llm/pdfs/ so that
extract_meeting_from_pdf.py --batch can pick them up in the standard pipeline.

Usage:
    uv run python meeting_pipeline/scripts/collect_reason_batch.py
    uv run python meeting_pipeline/scripts/collect_reason_batch.py --city gibsonville-NC
    uv run python meeting_pipeline/scripts/collect_reason_batch.py --skip-extract
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent  # gp-ai-projects/meeting_pipeline
_PROJECT_ROOT = _BRIEFING_ROOT.parent                   # gp-ai-projects
# Only add project root — adding meeting_pipeline/ as well causes a phantom
# `meeting_pipeline/meeting_pipeline/` resolution that breaks `from meeting_pipeline.collectors...`
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.collection_agent.misc.reason import collect_with_reason, ReasonFailed

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# Cities to try with the agentic browser.
# These have source URLs but no platform-specific collector.
CITIES = [
    {"city": "Mount Sterling", "state": "OH", "slug": "mount-sterling-OH"},
    {"city": "Vermilion", "state": "OH", "slug": "vermilion-OH"},
    {"city": "Stallings", "state": "NC", "slug": "stallings-NC"},
    {"city": "Marvin", "state": "NC", "slug": "marvin-NC"},
    {"city": "Walton Hills", "state": "OH", "slug": "walton-hills-OH"},
    {"city": "Hillsboro", "state": "OH", "slug": "hillsboro-OH"},
    {"city": "Gibsonville", "state": "NC", "slug": "gibsonville-NC"},
    {"city": "Poland", "state": "OH", "slug": "poland-OH"},
    {"city": "Pembroke", "state": "NC", "slug": "pembroke-NC"},
    {"city": "Maple Heights", "state": "OH", "slug": "maple-heights-OH"},
    {"city": "Coleman", "state": "TX", "slug": "coleman-TX"},
    # New additions from HubSpot candidate list
    {"city": "Lexington", "state": "OH", "slug": "lexington-OH"},
    {"city": "Palestine", "state": "TX", "slug": "palestine-TX"},
    {"city": "Lago Vista", "state": "TX", "slug": "lago-vista-TX"},
    {"city": "Walbridge", "state": "OH", "slug": "walbridge-OH"},
    {"city": "Hartville", "state": "OH", "slug": "hartville-OH"},
    {"city": "Sandy Oaks", "state": "TX", "slug": "sandy-oaks-TX"},
    {"city": "Mount Vernon", "state": "TX", "slug": "mount-vernon-TX"},
    {"city": "Canal Fulton", "state": "OH", "slug": "canal-fulton-OH"},
    {"city": "Clearcreek Township", "state": "OH", "slug": "clearcreek-township-OH"},
    {"city": "Rootstown Township", "state": "OH", "slug": "rootstown-township-OH"},
    {"city": "Chardon Township", "state": "OH", "slug": "chardon-township-OH"},
    {"city": "Beavercreek Township", "state": "OH", "slug": "beavercreek-township-OH"},
    {"city": "Refugio", "state": "TX", "slug": "refugio-TX"},
    {"city": "Etna Township", "state": "OH", "slug": "etna-township-OH"},
    {"city": "Elm City", "state": "NC", "slug": "elm-city-NC"},
    {"city": "Pflugerville", "state": "TX", "slug": "pflugerville-TX"},
    {"city": "Hartville", "state": "OH", "slug": "hartville-OH"},
]


async def collect_city(city_info: dict, cfg: AgentConfig, storage, skip_extract: bool) -> dict:
    slug = city_info["slug"]
    city = city_info["city"]
    state = city_info["state"]

    source_path = SOURCES_DIR / slug / "source.json"
    if not source_path.exists():
        return {"slug": slug, "status": "error", "error": "No source.json"}

    source = json.loads(source_path.read_text())
    best = source.get("best_source") or {}
    url = best.get("url", "")
    if not url:
        return {"slug": slug, "status": "skip", "error": "No URL in source.json"}

    print(f"\n{'='*60}")
    print(f"  {slug}  ({url[:60]})")
    print(f"{'='*60}")

    try:
        result = await collect_with_reason(
            event={"city": city, "state": state},
            source=source,
            storage=storage,
            cfg=cfg,
        )
        print(f"  -> {result.events_found} events, {result.pdfs_downloaded} PDFs via {result.platform}")

        # Move PDFs from output/ to sources/ so the extractor finds them
        output_pdf_dir = _PROJECT_ROOT / cfg.output_prefix / slug / "playwright_llm" / "pdfs"
        target_pdf_dir = SOURCES_DIR / slug / "playwright_llm" / "pdfs"
        if output_pdf_dir.exists() and output_pdf_dir != target_pdf_dir:
            target_pdf_dir.mkdir(parents=True, exist_ok=True)
            moved = 0
            for pdf in output_pdf_dir.glob("*.pdf"):
                dest = target_pdf_dir / pdf.name
                if not dest.exists():
                    pdf.rename(dest)
                    moved += 1
            if moved:
                print(f"  -> Moved {moved} PDFs to sources/{slug}/playwright_llm/pdfs/")

        # Run PDF extraction for this city
        if not skip_extract and result.pdfs_downloaded > 0:
            print(f"  -> Running PDF extraction for {slug}...")
            subprocess.run(
                [
                    "uv", "run", "python",
                    str(_BRIEFING_ROOT / "scripts" / "extract_meeting_from_pdf.py"),
                    "--city-dir", str(SOURCES_DIR / slug),
                ],
                cwd=str(_PROJECT_ROOT),
                check=False,
            )
            # Normalize
            subprocess.run(
                [
                    "uv", "run", "python",
                    str(_BRIEFING_ROOT / "scripts" / "normalize_all_meetings.py"),
                    "--city", slug,
                ],
                cwd=str(_PROJECT_ROOT),
                check=False,
            )

        return {
            "slug": slug, "status": "success",
            "events": result.events_found, "pdfs": result.pdfs_downloaded,
            "platform": result.platform,
        }

    except ReasonFailed as e:
        print(f"  -> REASON FAILED: {e}")
        return {"slug": slug, "status": "reason_failed", "error": str(e)}
    except Exception as e:
        import traceback
        print(f"  -> ERROR: {e}")
        traceback.print_exc()
        return {"slug": slug, "status": "error", "error": str(e)}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", help="Run only this slug (e.g. gibsonville-NC)")
    parser.add_argument("--skip-extract", action="store_true", help="Skip PDF extraction step")
    parser.add_argument("--lookback", type=int, default=90)
    args = parser.parse_args()

    cities = CITIES
    if args.city:
        cities = [c for c in CITIES if c["slug"] == args.city]
        if not cities:
            print(f"City '{args.city}' not in CITIES list.")
            return

    # Save to sources/ directly so extractors can find PDFs
    cfg = AgentConfig(
        output_prefix="meeting_pipeline/sources",
        lookback_days=args.lookback,
        download_pdfs=True,
    )
    storage = get_storage(cfg)

    print(f"Running agentic browser collector for {len(cities)} cities...")
    results = []
    for city_info in cities:
        result = await collect_city(city_info, cfg, storage, args.skip_extract)
        results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print("REASON BATCH SUMMARY")
    print(f"{'='*60}")
    successes = [r for r in results if r["status"] == "success"]
    failures = [r for r in results if r["status"] != "success"]
    print(f"Succeeded: {len(successes)}/{len(results)}")
    for r in successes:
        print(f"  {r['slug']}: {r['events']} events, {r['pdfs']} PDFs ({r['platform']})")
    if failures:
        print(f"Failed: {len(failures)}")
        for r in failures:
            print(f"  {r['slug']}: {r['status']} — {r.get('error', '')[:80]}")

    results_path = SOURCES_DIR / "reason_batch_results.json"
    results_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults saved: {results_path}")


if __name__ == "__main__":
    asyncio.run(main())
