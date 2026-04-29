"""
source_discover.py — Source-discover skill for all 67 pilot cities.

Finds the freshest, most active agenda source for each city and outputs a
structured JSON record the briefing-collect skill can consume.

Usage:
    uv run python meeting_pipeline/scripts/source_discover.py                       # all cities
    uv run python meeting_pipeline/scripts/source_discover.py --city "Chapel Hill"  # single city
    uv run python meeting_pipeline/scripts/source_discover.py --state NC            # one state
    uv run python meeting_pipeline/scripts/source_discover.py --resume              # skip existing
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv            # use serve_users.csv
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv --skip-existing  # CSV, skip done
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv --city "Tuscaloosa"  # single CSV city

Output:
    meeting_pipeline/sources/{city-slug}-{state}/source.json   per-city records
    meeting_pipeline/sources/discovery-summary.json            batch summary

Algorithm (3-phase with retry loop):
  Phase 1 — Discover candidates (known sources registry + Exa/Tavily + URL probing)
  Phase 2 — Verify freshness by platform
  Phase 3 — Rank and select best source
  Retry loop — up to 2 retries with escalating strategies
  Phase 4 — Deep platform API probes (CivicClerk REST, BoardDocs POST, eSCRIBE,
             CivicPlus year-filter) — only runs if still no fresh source after retries
  Phase 5 — Playwright browser rendering — last resort for JS SPAs (CivicClerk,
             PrimeGov) and bot-blocked pages where httpx cannot extract dates.
             Requires: pip install playwright && playwright install chromium

Search backends:
  Exa   — primary when EXA_API_KEY is set (exa-py package required)
  Tavily — fallback when EXA_API_KEY is not set (always required for domain discovery)
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv
from tavily import TavilyClient
from meeting_pipeline.shared.body_validation import validate_body_for_city, VALIDATABLE_PLATFORMS

# ── Imports from extracted modules ────────────────────────────────────────────
from meeting_pipeline.shared.constants import (
    STATE_ABBREVS,
    STATE_NAMES as _STATE_NAMES,
    PLATFORM_PATTERNS, COLLECTION_METHODS,
    FRESH_THRESHOLD, STALE_WARNING_THRESHOLD,
    WRONG_CITY_PATTERNS, WRONG_ENTITY_PATTERNS, WRONG_DOMAIN_PATTERNS,
    BOARDDOCS_WRONG_ENTITY_KEYWORDS, COUNCIL_BODY_KEYWORDS, GRANICUS_COUNCIL_KEYWORDS,
    REJECT_URL_PATTERNS, FETCH_BLOCKLIST, CITY_NAME_PREFIXES, CITY_NAME_SUFFIXES,
    PDF_PLATFORM_SIGNALS,
)
from meeting_pipeline.shared.discovery_helpers import make_candidate, safe_fetch
from meeting_pipeline.shared.url_utils import (
    detect_platform, normalize_platform_url, is_wrong_city, is_wrong_entity,
    is_non_agenda_url, city_to_slug,
)
from meeting_pipeline.shared.date_utils import (
    extract_dates, classify_freshness, normalize_table_dates as _normalize_table_dates,
)
from meeting_pipeline.stages.discover.scoring import (
    candidate_score, rank_candidates, classify_domain_trust, agenda_authority_score,
    FRESHNESS_SCORE, PLATFORM_TIER, SOURCE_BONUS,
)
from meeting_pipeline.stages.discover.search import (
    serper_search,
    search_results_to_candidates as _search_results_to_candidates,
    discover_from_duckduckgo, discover_from_exa, discover_from_tavily,
    discover_from_firecrawl, discover_from_pdf_search,
)
from meeting_pipeline.stages.discover.crawl import (
    validate_domain_for_city, firecrawl_map_agenda, firecrawl_crawl_for_agenda,
)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Cost tracking ─────────────────────────────────────────────────────────────
# Counters are module-level so async tasks can increment them safely
# (single-threaded event loop — no locking needed).
_COST = {
    "exa_searches": 0,
    "tavily_searches": 0,
    # Firecrawl: tracked by call type because credit cost varies significantly:
    #   validate_agenda_page() → scrape(formats=["markdown","links"]) → ~1 credit
    #   scrape_civicclerk_event_files() → scrape(actions=[...]) → likely 5+ credits (rendered)
    # Check https://firecrawl.dev/app for actual credit consumption.
    "firecrawl_scrape_basic": 0,    # validate_agenda_page calls
    "firecrawl_scrape_actions": 0,  # calls with browser actions (civicclerk event pages)
    "serper_searches": 0,            # Serper.dev search calls ($1/1k; 2500 free credits on signup)
}
# Per-call costs (USD).
# Exa search_and_contents: $7/1k (search) + $1/1k × 5 pages (contents) = $0.012/call
# Tavily: $0.01/search (basic plan — unconfirmed, update with actual plan pricing)
# Firecrawl: credit cost per call type is uncertain — see dashboard for actuals
_COST_PER_CALL = {
    "exa_searches": 0.012,          # $7/1k search + $1/1k × 5 pages contents
    "tavily_searches": 0.01,        # basic search (unconfirmed)
    "firecrawl_scrape_basic": None, # unknown — check Firecrawl dashboard
    "firecrawl_scrape_actions": None,
}

# ── Paths (module-level) ───────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_PIPELINE_DIR = _SCRIPT_DIR.parent
SERVE_CSV = _PIPELINE_DIR / "serve_users_unified.csv"
if not SERVE_CSV.exists():
    SERVE_CSV = _PIPELINE_DIR / "serve_users.csv"
if not SERVE_CSV.exists():
    SERVE_CSV = _PIPELINE_DIR / "Terry Users2.csv"
# ── Paths ──────────────────────────────────────────────────────────────────────
SOURCES_DIR = _PIPELINE_DIR / "sources"
REGISTRY_S3_KEY = "meeting_pipeline/config/known-sources-registry.json"

# ── Constants ──────────────────────────────────────────────────────────────────
TODAY = date.today()

# ── DotGov domain index ────────────────────────────────────────────────────────
#
# CISA publishes the authoritative list of .gov registrants at:
#   https://github.com/cisagov/dotgov-data/blob/main/current-full.csv
#
# We download it once to config/dotgov.csv and use it as Layer 0 discovery:
# look up the official .gov domain before any web search so that the domain
# crawl (probe_domain_for_agendas) always runs against the right site.

# Org-name fragments that identify department sub-sites, not the city hall
_DEPT_REJECT_KEYWORDS = [
    "court", "police department", "police dept", "sheriff", "fire department",
    "fire district", "library", "city marshal", "marshal's", "jail",
    "district attorney", "prosecutor", "ems ", "utilities", "water district",
    "sewer", "transit", "parking authority", "permit office", "airport",
]

# Org-name prefixes that confirm this is the main municipal government
_CITY_GOV_PREFIXES = (
    "city of ", "town of ", "village of ", "borough of ",
    "township of ", "city and county of ",
)

_DOTGOV_INDEX: dict[tuple[str, str], list[dict]] | None = None


# ── Implementation moved to stages/discover/ ─────────────────────────────────
# The core discovery logic (probes, freshness, main flow) is in:
#   stages/discover/main_flow.py
# Search, scoring, crawl functions are in their respective modules.
from meeting_pipeline.stages.discover.main_flow import (
    run_source_discover,
    discover_from_known_sources,
    discover_from_probes,
    verify_freshness,
    probe_granicus_views,
    deep_probe_candidate,
    lookup_gov_domain,
)


async def process_city(
    city_info: dict,
    registry: dict,
    tavily: TavilyClient,
    semaphore: asyncio.Semaphore,
    resume: bool = False,
    skip_existing: bool = False,
    output_dir: Optional[Path] = None,
    storage=None,
    sources_prefix: str = "meeting_pipeline/sources",
) -> dict:
    city = city_info["city"]
    state = city_info["state"]
    slug = city_to_slug(city)

    # --resume / --skip-existing: read existing result from S3 (no local files)
    if (resume or skip_existing) and storage is not None:
        s3_key = f"{sources_prefix}/{slug}-{state}/source.json"
        try:
            if storage.exists(s3_key):
                existing = storage.read_json(s3_key)
                bs = existing.get("best_source") or {}
                freshness = bs.get("freshness", "")
                rerun_freshnesses = {"empty", "wrong_entity", "stale"} if skip_existing else {"empty"}
                # Always skip manually-set sources — never overwrite with automated discovery.
                if bs.get("source") == "manual":
                    print(
                        f"  [skip] {city:<20s} {state}  (manual source — protected)"
                    )
                    return existing
                # Also re-run if platform is unknown/generic — source was found but
                # not on a supported platform. Re-running may discover a better URL.
                platform_is_unsupported = bs.get("platform", "") in ("unknown", "generic_html", "")
                if freshness not in rerun_freshnesses and not platform_is_unsupported:
                    print(
                        f"  [skip] {city:<20s} {state}  (existing: {bs.get('platform','?')}  {freshness})"
                    )
                    return existing
        except Exception:
            pass  # re-run if S3 read fails

    async with semaphore:
        key = f"{city}, {state}"
        known_sources = {
            k: v for k, v in registry.get(key, {}).items()
            if not k.startswith("_")  # skip _description, _usage, notes
        }
        # Remove notes key from known_sources (it's a human note, not a hint)
        known_sources.pop("notes", None)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        # Load manifest to get expected_body for targeted search queries
        expected_body = ""
        if storage is not None:
            manifest_key = f"{sources_prefix}/{slug}-{state}/manifest.json"
            try:
                if storage.exists(manifest_key):
                    manifest = storage.read_json(manifest_key)
                    expected_body = (manifest or {}).get("expected_body", "")
            except Exception:
                pass

        async with httpx.AsyncClient(headers=headers, timeout=20.0) as http:
            result = await run_source_discover(
                city, state, known_sources, tavily, http, expected_body=expected_body
            )

            # Write to S3 and run body validation while the HTTP client is still open.
            # Body validation makes live API calls (Legistar /bodies, CivicPlus AJAX, etc.)
            # and MUST run inside the async with block — not after the client closes.
            if storage is not None:
                s3_key = f"{sources_prefix}/{slug}-{state}/source.json"
                try:
                    # Source stability guard: never downgrade from a higher-tier platform,
                    # and never replace a fresh/working source with an empty/blocked one.
                    _NON_WORKING = {"empty", "blocked", "wrong_entity"}
                    new_bs = result.get("best_source") or {}
                    new_platform = new_bs.get("platform", "unknown")
                    new_tier = PLATFORM_TIER.get(new_platform, 4)
                    new_freshness = new_bs.get("freshness", "unknown")
                    if storage.exists(s3_key):
                        try:
                            existing = storage.read_json(s3_key)
                            old_bs = existing.get("best_source") or {}
                            old_platform = old_bs.get("platform", "unknown")
                            old_tier = PLATFORM_TIER.get(old_platform, 4)
                            old_freshness = old_bs.get("freshness", "unknown")
                            keep_existing = False
                            reason = ""
                            # Only protect working sources (not empty/blocked/wrong_entity).
                            # If existing is already broken, let any new result replace it.
                            old_is_working = old_freshness not in _NON_WORKING
                            if new_freshness in _NON_WORKING and old_is_working:
                                keep_existing = True
                                reason = f"new={new_freshness} would replace working {old_freshness} {old_platform}"
                            elif new_tier < old_tier and old_is_working:
                                keep_existing = True
                                reason = f"tier downgrade ({old_tier}→{new_tier})"
                            if keep_existing:
                                print(
                                    f"  [guard] {city}, {state}: keeping existing {old_platform} "
                                    f"(tier={old_tier}) over new {new_platform} (tier={new_tier}) — {reason}"
                                )
                                result = existing
                        except Exception:
                            pass
                    storage.write_json(s3_key, result)
                except Exception as e:
                    print(f"  [warn] Could not upload source.json to S3 for {city}, {state}: {e}")

                # Body validation: auto-correct category/committee IDs and expected_body.
                # If the winner fails, automatically try next-ranked supported candidate.
                bs_platform = (result.get("best_source") or {}).get("platform", "")
                if bs_platform in VALIDATABLE_PLATFORMS:
                    try:
                        bv = await validate_body_for_city(
                            f"{slug}-{state}", result, s3_key, http, storage
                        )
                        bv_status = bv.get("status", "skip")
                        if bv_status == "corrected":
                            print(f"  [body] corrected → {bv.get('correction_note', '')}")
                        elif bv_status == "unresolved":
                            print(f"  [body] UNRESOLVED for {bs_platform} — {bv.get('reason', '')}")
                            for alt in (result.get("all_candidates") or [])[1:]:
                                alt_platform = alt.get("platform", "")
                                alt_freshness = alt.get("freshness", "")
                                alt_source = {
                                    **result,
                                    "best_source": {
                                        "platform": alt_platform,
                                        "url": alt.get("url", ""),
                                        "display_url": alt.get("url", ""),
                                        "freshness": alt.get("freshness"),
                                        "most_recent_date": alt.get("most_recent_date"),
                                        "days_since_update": alt.get("days_since_update"),
                                        "date_source": alt.get("date_source"),
                                        "collection_method": COLLECTION_METHODS.get(alt_platform, "fetch_and_parse"),
                                        "config": alt.get("config") or {},
                                        "notes": alt.get("notes") or "",
                                    },
                                }
                                if alt_platform not in VALIDATABLE_PLATFORMS:
                                    # Non-validatable platforms (unknown, generic_html) can't be
                                    # body-checked, but if they're fresh/stale accept them as
                                    # fallback — better than staying on a broken validated source.
                                    if alt_freshness in ("fresh", "stale"):
                                        print(
                                            f"  [body] fallback OK: switched to non-validatable "
                                            f"{alt_platform} ({alt_freshness}) ({alt.get('url', '')})"
                                        )
                                        result = alt_source
                                        try:
                                            storage.write_json(s3_key, result)
                                        except Exception:
                                            pass
                                        break
                                    continue
                                try:
                                    alt_bv = await validate_body_for_city(
                                        f"{slug}-{state}", alt_source, s3_key, http, storage
                                    )
                                except Exception:
                                    continue
                                if alt_bv.get("status") in ("ok", "corrected"):
                                    print(
                                        f"  [body] fallback OK: switched to {alt_platform} "
                                        f"({alt.get('url', '')})"
                                    )
                                    result = alt_source
                                    try:
                                        storage.write_json(s3_key, result)
                                    except Exception:
                                        pass
                                    break
                            else:
                                print(f"  [body] no fallback candidate resolved — body unresolved")
                                # Downgrade best_source to wrong_entity so the city
                                # is not treated as fresh and will be re-discovered
                                # next time instead of silently producing bad briefings.
                                if result.get("best_source"):
                                    result["best_source"]["freshness"] = "wrong_entity"
                                    result["best_source"]["wrong_entity_reason"] = (
                                        bv.get("reason", "body unresolved — no matching governing body found")
                                    )
                                    try:
                                        storage.write_json(s3_key, result)
                                    except Exception:
                                        pass
                        elif bv_status not in ("skip", "ok"):
                            print(f"  [body] {bv_status}: {bv.get('reason', '')}")
                    except Exception as e:
                        print(f"  [body] validation error: {e}")

        bs = result.get("best_source") or {}
        freshness = bs.get("freshness") or "no_source"
        platform = bs.get("platform") or "none"
        elapsed = result["search_metadata"]["elapsed_sec"]
        marker = (
            "+" if freshness == "fresh"
            else "~" if freshness in ("unknown_spa", "stale_warning")
            else "-"
        )
        mrd = bs.get("most_recent_date") or ""
        print(
            f"  [{marker}] {city:<20s} {state}  {elapsed:5.1f}s  {platform:<12s}  "
            f"{freshness:<14s}  {mrd}"
        )
        return result


async def run_batch(
    cities: list[dict],
    registry: dict,
    tavily: TavilyClient,
    resume: bool = False,
    skip_existing: bool = False,
    output_dir: Optional[Path] = None,
    storage=None,
    sources_prefix: str = "meeting_pipeline/sources",
) -> None:
    semaphore = asyncio.Semaphore(5)  # max 5 concurrent Tavily/Exa searches
    total_start = time.monotonic()

    backend = "Exa+Tavily" if os.environ.get("EXA_API_KEY") else "Tavily"
    print(f"{'='*78}")
    print(f"Source Discover — {len(cities)} cities  (today: {TODAY})  [search: {backend}]")
    print(f"{'='*78}")
    print(f"  {'[+]'} fresh   [~] unknown_spa/stale_warning   [-] stale/empty/blocked/unknown\n")

    tasks = [
        process_city(
            c, registry, tavily, semaphore,
            resume=resume, skip_existing=skip_existing, output_dir=output_dir,
            storage=storage, sources_prefix=sources_prefix,
        )
        for c in cities
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = round(time.monotonic() - total_start, 1)

    # Build summary
    summary: dict = {
        "total_cities": len(cities),
        "fresh_sources": 0,
        "unknown_spa": 0,
        "stale_warning": 0,
        "stale": 0,
        "empty": 0,
        "blocked": 0,
        "unknown": 0,
        "no_source": 0,
        "migrations_detected": 0,
        "elapsed_total_sec": total_elapsed,
        "cities": [],
    }

    freshness_key_map = {
        "fresh": "fresh_sources",
        "unknown_spa": "unknown_spa",
        "stale_warning": "stale_warning",
        "stale": "stale",
        "empty": "empty",
        "blocked": "blocked",
        "unknown": "unknown",
        "no_source": "no_source",
    }

    for r in results:
        if isinstance(r, Exception):
            summary["no_source"] += 1
            continue
        bs = r.get("best_source") or {}
        freshness = bs.get("freshness") or "no_source"
        key = freshness_key_map.get(freshness, "unknown")
        summary[key] = summary.get(key, 0) + 1
        if r.get("migration_detected"):
            summary["migrations_detected"] += 1
        summary["cities"].append({
            "city": r["city"],
            "state": r["state"],
            "platform": bs.get("platform"),
            "freshness": freshness,
            "most_recent_date": bs.get("most_recent_date"),
            "warnings": r.get("warnings", []),
        })

    print(f"\n{'='*78}")
    print(f"SUMMARY — {total_elapsed}s total ({total_elapsed/max(len(cities),1):.1f}s avg/city)")
    print(f"{'='*78}")
    print(f"  Fresh:          {summary['fresh_sources']}")
    print(f"  Unknown SPA:    {summary['unknown_spa']}")
    print(f"  Stale warning:  {summary['stale_warning']}")
    print(f"  Stale:          {summary['stale']}")
    print(f"  Empty:          {summary['empty']}")
    print(f"  Blocked:        {summary['blocked']}")
    print(f"  Unknown:        {summary['unknown']}")
    print(f"  No source:      {summary['no_source']}")
    print(f"  Migrations:     {summary['migrations_detected']}")

    # Write summary to S3
    if storage is not None:
        summary_key = f"{sources_prefix}/discovery-summary.json"
        try:
            storage.write_json(summary_key, summary)
            print(f"\n  Summary → s3://{summary_key}")
        except Exception as e:
            print(f"\n  [warn] Could not upload summary to S3: {e}")
    else:
        print(f"\n  [warn] No storage backend — summary not persisted")

    # Cost report
    exa_cost = _COST["exa_searches"] * _COST_PER_CALL["exa_searches"]
    tav_cost = _COST["tavily_searches"] * _COST_PER_CALL["tavily_searches"]
    known_total = exa_cost + tav_cost
    fc_basic = _COST["firecrawl_scrape_basic"]
    fc_actions = _COST["firecrawl_scrape_actions"]

    serper_cost = _COST["serper_searches"] * 0.001  # $1/1k
    print(f"\n  DISCOVERY COST:")
    print(f"    Serper.dev:             {_COST['serper_searches']:3d} searches   × $0.0010 = ${serper_cost:.4f}  ($1/1k; 2500 free credits)")
    print(f"    Exa:                    {_COST['exa_searches']:3d} searches   × $0.0120 = ${exa_cost:.4f}  (search+contents, 5 pages)")
    print(f"    Tavily:                 {_COST['tavily_searches']:3d} searches   × $0.0100 = ${tav_cost:.4f}  (unconfirmed)")
    print(f"    Firecrawl:              {fc_basic:3d} basic + {fc_actions} action scrapes — see firecrawl.dev/app for credits")
    print(f"    Subtotal (Exa+Tavily):  ${known_total:.4f}")

    if storage is not None:
        cost_report = {
            "phase": "discovery",
            "exa_searches": _COST["exa_searches"],
            "tavily_searches": _COST["tavily_searches"],
            "firecrawl_scrape_basic": fc_basic,
            "firecrawl_scrape_actions": fc_actions,
            "estimated_usd": round(known_total, 6),  # Exa+Tavily only; Firecrawl credits tracked separately
            "cities_processed": len(cities),
        }
        output_prefix = sources_prefix.replace("/sources", "/output")
        try:
            storage.write_json(f"{output_prefix}/cost_reports/discovery.json", cost_report)
        except Exception:
            pass


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Source-discover skill — find freshest agenda source for each city"
    )
    parser.add_argument("--city", help="Run for a single city (e.g. 'Chapel Hill')")
    parser.add_argument("--state", help="Filter by state abbreviation (e.g. NC, OH, TX)")
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip cities that already have a non-empty source.json (re-runs 'empty' results)"
    )
    parser.add_argument(
        "--from-csv", action="store_true",
        help=(
            "Use serve_users.csv as the city list instead of the hardcoded PILOT_CITIES. "
            "CSV State/Region column uses full state names (e.g. 'Ohio')."
        ),
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        help=(
            "Path to an alternate CSV file. Must have 'City' and 'State' columns "
            "(2-letter state abbreviations). Implies --from-csv behavior."
        ),
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help=(
            "Skip cities that already have a valid source.json "
            "(freshness not in wrong_entity/stale/empty). "
            "Use with --from-csv to only process new cities."
        ),
    )
    parser.add_argument(
        "--output-dir",
        help="Write results to this directory instead of the default sources/ dir (useful for benchmarking)"
    )
    args = parser.parse_args()

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        print("ERROR: TAVILY_API_KEY not set in environment / .env")
        sys.exit(1)

    exa_key = os.environ.get("EXA_API_KEY")
    serper_key = os.environ.get("SERPER_API_KEY")
    if serper_key:
        print("  [search backend] Serper.dev (primary) → DDG → Exa → Tavily (fallbacks)")
    elif exa_key:
        print("  [search backend] DDG (primary) → Exa → Tavily (fallbacks)")
    else:
        print("  [search backend] DDG (primary) → Tavily (fallback; set EXA_API_KEY to also enable Exa)")

    from meeting_pipeline.shared.config import AgentConfig, get_storage
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    registry = storage.read_json(REGISTRY_S3_KEY)
    tavily = TavilyClient(api_key=api_key)

    # City list: --csv or --from-csv uses a CSV file; otherwise use hardcoded PILOT_CITIES
    if args.csv:
        import pathlib
        alt_csv = pathlib.Path(args.csv)
        if not alt_csv.exists():
            print(f"ERROR: {alt_csv} not found")
            sys.exit(1)
        seen: set[tuple[str, str]] = set()
        all_cities = []
        for row in csv.DictReader(alt_csv.open()):
            city = row.get("City", "").strip()
            state = row.get("State", "").strip().upper()
            if not city or not state or (city, state) in seen:
                continue
            seen.add((city, state))
            all_cities.append({"city": city, "state": state})
        city_source = f"{alt_csv.name} ({len(all_cities)} cities)"
    elif args.from_csv:
        all_cities = get_serve_csv_cities()
        city_source = f"serve_users.csv ({len(all_cities)} cities)"
    else:
        all_cities = list(PILOT_CITIES)
        city_source = f"PILOT_CITIES ({len(all_cities)} cities)"

    # --city filter (works for both sources)
    if args.city:
        filtered = [c for c in all_cities if c["city"].lower() == args.city.lower()]
        if not filtered:
            if args.state:
                # Ad-hoc city not in registry — create entry on the fly so phase_verify
                # can re-discover any city without requiring it in PILOT_CITIES.
                print(f"  [discover] '{args.city}' not in {city_source} — running ad-hoc discovery for {args.city}, {args.state.upper()}")
                filtered = [{"city": args.city, "state": args.state.upper()}]
            else:
                print(f"ERROR: '{args.city}' not found in {city_source}")
                sys.exit(1)
        cities = filtered
    elif args.state:
        filtered = [c for c in all_cities if c["state"].upper() == args.state.upper()]
        if not filtered:
            print(f"ERROR: No cities found for state '{args.state}' in {city_source}")
            sys.exit(1)
        cities = filtered
    else:
        cities = all_cities

    output_dir = Path(args.output_dir) if args.output_dir else None
    asyncio.run(
        run_batch(
            cities, registry, tavily,
            resume=args.resume,
            skip_existing=args.skip_existing,
            output_dir=output_dir,
            storage=storage,
            sources_prefix=cfg.sources_prefix,
        )
    )


if __name__ == "__main__":
    main()
