"""
generate_briefing.py — Generate council meeting briefings from normalized meeting data.

Takes normalized meeting JSON from output/normalized/ and produces a full briefing
JSON with executive summary, priority issue cards, detail pages, and constituent data.

If Haystaq constituent data is available (issue_scores.json in sources/{city-slug}/constituent/),
the pipeline uses it to weight priority selection and inject real constituent scores.

Pipeline:
  Pass 1: Categorize all agenda items + identify priority issues
  Pass 2: Generate card content for each priority issue (headline, action, questions)
  Pass 3: Generate detail content for each priority issue (deep analysis)

Storage:
    Reads/writes via STORAGE_BACKEND (local or s3). Set S3_BUCKET + STORAGE_BACKEND=s3 in .env for S3.

Usage:
    # Generate briefing for a specific normalized meeting storage key
    uv run python meeting_pipeline/scripts/generate_briefing.py --file meeting_pipeline/output/normalized/johnstown-OH_2026-04-07.json

    # Generate for a city slug (most recent normalized meeting)
    uv run python meeting_pipeline/scripts/generate_briefing.py --city johnstown-OH

    # Generate for all normalized meetings
    uv run python meeting_pipeline/scripts/generate_briefing.py --batch

    # Dry run
    uv run python meeting_pipeline/scripts/generate_briefing.py --city johnstown-OH --dry-run

Output:
    {output_prefix}/briefings/{city-slug}_{date}_briefing.json
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.prompts.briefing import (
    EDITORIAL_RULES,
    build_pass1_prompt,
    build_pass2a_prompt,
    build_pass2b_prompt,
    build_pass3_prompt,
)


# Implementation moved to stages/briefing/generate.py
from meeting_pipeline.stages.briefing.generate import (
    normalized_to_meeting_dict,
    load_constituent_data, format_constituent_context, format_top_constituent_issues,
    AgendaCategorization, BriefingCards, PriorityIssueDetail,
    pass1_categorize, pass2_generate_cards, pass3_generate_detail,
    check_provenance, check_fiscal_amounts, assemble_briefing,
    generate_briefing_for_meeting,
)


def main():
    parser = argparse.ArgumentParser(description="Generate council meeting briefings")
    parser.add_argument("--file", help="Storage key for a normalized meeting JSON (e.g. meeting_pipeline/output/normalized/johnstown-OH_2026-04-07.json)")
    parser.add_argument("--city", action="append", metavar="SLUG",
                        help="City slug (e.g. johnstown-OH). With --batch, filters to these cities (repeatable). Without --batch, uses most recent normalized file for the first slug.")
    parser.add_argument("--batch", action="store_true", help="Generate for all normalized meetings")
    parser.add_argument("--from-date", help="Only process meetings on or after this date (YYYY-MM-DD). Applies to --batch mode.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be generated, no LLM calls")
    parser.add_argument("--force", action="store_true", help="Regenerate even if a briefing already exists (batch mode)")
    args = parser.parse_args()

    if not args.file and not args.city and not args.batch:
        parser.error("Specify --file, --city, or --batch")

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    normalized_prefix = f"{cfg.output_prefix}/normalized"

    if args.file:
        target_keys = [args.file]
    elif args.city and not args.batch:
        # Single-city mode: most recent normalized file for the first slug
        all_keys = storage.list_keys(normalized_prefix)
        city_slug = args.city[0].lower().replace(" ", "-")
        matches = sorted(
            k for k in all_keys
            if k.split("/")[-1].lower().startswith(city_slug) and k.endswith(".json")
        )
        if not matches:
            print(f"No normalized files found for city: {args.city[0]}")
            print(f"  Looked in: {normalized_prefix}")
            sys.exit(1)
        target_keys = [matches[-1]]
    else:
        all_keys = storage.list_keys(normalized_prefix)
        # Only match {city-slug}_{YYYY-MM-DD}.json — skip combined dumps like normalized_meetings.json
        target_keys = sorted(
            k for k in all_keys
            if re.search(r"[^/]+_\d{4}-\d{2}-\d{2}\.json$", k)
        )
        # Filter by --city slugs if specified in batch mode
        if args.city:
            city_filter = set(s.lower().replace(" ", "-") for s in args.city)
            before = len(target_keys)
            target_keys = [
                k for k in target_keys
                if any(k.split("/")[-1].lower().startswith(slug) for slug in city_filter)
            ]
            print(f"City filter {sorted(city_filter)}: {len(target_keys)} of {before} files")

        # Filter by --from-date if specified (extracts date from filename)
        if args.from_date:
            before_filter = len(target_keys)
            target_keys = [
                k for k in target_keys
                if (m := re.search(r"(\d{4}-\d{2}-\d{2})\.json$", k)) and m.group(1) >= args.from_date
            ]
            print(f"Date filter >= {args.from_date}: {len(target_keys)} of {before_filter} files")
        # Skip files that already have a briefing (batch mode default, bypassed by --force)
        briefing_prefix = f"{cfg.output_prefix}/briefings"
        existing_briefing_keys = set(storage.list_keys(briefing_prefix))
        def _has_briefing(norm_key: str) -> bool:
            fn = norm_key.split("/")[-1]           # e.g. chapel-hill-NC_2026-04-15.json
            stem = fn[:-5]                          # chapel-hill-NC_2026-04-15
            city_date = stem                        # same
            return any(city_date in bk for bk in existing_briefing_keys)
        if not args.force:
            before = len(target_keys)
            target_keys = [k for k in target_keys if not _has_briefing(k)]
            skipped_existing = before - len(target_keys)
            if skipped_existing:
                print(f"Skipping {skipped_existing} files with existing briefings (--force to regenerate all)")
        if not target_keys:
            print(f"No normalized meeting files found in {normalized_prefix}")
            sys.exit(1)

    results = []
    for key in target_keys:
        filename = key.split("/")[-1]
        print(f"\nProcessing: {filename}")
        try:
            result = generate_briefing_for_meeting(key, storage, cfg, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            result = {"status": "error", "error": str(e), "cost": 0}
        result["file"] = filename
        results.append(result)

    if len(results) > 1:
        print(f"\n{'=' * 60}")
        print("BRIEFING GENERATION SUMMARY")
        print(f"{'=' * 60}")
        for r in results:
            status = r.get("status", "?")
            cost = r.get("cost", 0)
            fiscal_warns = len(r.get("fiscal_warnings", []))
            warn_str = f" ⚠ {fiscal_warns} fiscal" if fiscal_warns else ""
            print(f"  {r.get('file', '?'):40s} [{status}]{warn_str} ${cost:.4f}")
        total_cost = sum(r.get("cost", 0) for r in results)
        ok_count = sum(1 for r in results if r.get("status") == "ok")
        error_count = sum(1 for r in results if r.get("status") == "error")
        no_haystaq_count = sum(1 for r in results if r.get("reason") == "no_haystaq")
        skipped_count = sum(1 for r in results if r.get("status") in ("skipped", "dry_run")) - no_haystaq_count
        print(f"\n  Total: {ok_count}/{len(results)} generated, {error_count} errors, {skipped_count} skipped, ${total_cost:.4f}")
        if no_haystaq_count:
            print(f"  Skipped (no Haystaq): {no_haystaq_count} — run: collect_haystaq_batch.py --from-csv --skip-existing")

        # Write structured run log to storage
        run_log = {
            "run_at": datetime.utcnow().isoformat() + "Z",
            "total": len(results),
            "ok": ok_count,
            "errors": error_count,
            "skipped": skipped_count,
            "skipped_no_haystaq": no_haystaq_count,
            "total_cost_usd": round(total_cost, 6),
            "briefings": [
                {
                    "file": r.get("file"),
                    "status": r.get("status"),
                    "cost_usd": round(r.get("cost", 0), 6),
                    "fiscal_warnings": r.get("fiscal_warnings", []),
                    "error": r.get("error"),
                }
                for r in results
            ],
        }
        log_key = f"{cfg.output_prefix}/run_logs/briefing_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        storage.write_json(log_key, run_log)
        print(f"  Run log: {log_key}")

        # Write cost report so the pipeline orchestrator can aggregate it
        try:
            cost_report = {
                "phase": "briefing",
                "estimated_usd": round(total_cost, 6),
                "briefings_generated": ok_count,
            }
            storage.write_json(f"{cfg.output_prefix}/cost_reports/briefing.json", cost_report)
        except Exception:
            pass


if __name__ == "__main__":
    main()
