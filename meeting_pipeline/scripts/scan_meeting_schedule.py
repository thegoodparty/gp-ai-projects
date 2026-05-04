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
import contextlib
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig, get_storage  # noqa: E402
from meeting_pipeline.shared.constants import SUPPORTED_PLATFORMS  # noqa: E402
from meeting_pipeline.stages.scan.process import process_one_city as _scan_impl  # noqa: E402

# ============================================================================
# SCAN DISPATCHER — delegates to stages/scan/process.py
# ============================================================================


async def scan_city(
    slug: str,
    source: dict,
    source_key: str,
    client: httpx.AsyncClient,
    storage,
    skip_body_validation: bool = False,
) -> dict | None:
    """Scan one city. Delegates to stages/scan/process.py."""
    return await _scan_impl(
        slug, source, source_key,
        http_client=client, storage=storage,
        skip_body_validation=skip_body_validation,
    )


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
                with contextlib.suppress(Exception):
                    prev = storage.read_json(prev_key)

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
                    bv_note = " [BODY UNRESOLVED ⚠]"
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
