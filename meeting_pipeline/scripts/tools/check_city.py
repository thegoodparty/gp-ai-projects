"""
check_city.py — Test whether a city can be fully supported by the pipeline.

Runs three checks in sequence:
  1. Source discovery — does the city have a fresh source on a supported platform?
  2. Collection — does the collection agent return at least one meeting?
  3. PDF check — is the downloaded PDF > 50KB? (rules out stubs and viewer-only links)

Prints a clear PASS / FAIL with reason at each step.

Usage:
    uv run python meeting_pipeline/scripts/check_city.py --city "Johnstown" --state OH
    uv run python meeting_pipeline/scripts/check_city.py --city "Kyle" --state TX
    uv run python meeting_pipeline/scripts/check_city.py --city "Mason" --state OH

Output:
    meeting_pipeline/sources/{city-slug}/  — discovery + collection data written here
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent

SOURCES_DIR = _ROOT / "sources"

SUPPORTED_PLATFORMS = {"civicclerk", "civicplus", "granicus", "legistar", "escribemeetings"}

MIN_PDF_BYTES = 50_000  # 50KB — anything smaller is a stub or viewer redirect


# ── Step 1: Discovery ────────────────────────────────────────────────────────

def run_discovery(city: str, state: str) -> dict:
    """Run source_discover.py for this city and return the source.json result."""
    print(f"\n{'─'*60}")
    print(f"STEP 1 — Source Discovery: {city}, {state}")
    print(f"{'─'*60}")

    result = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "source_discover.py"),
         "--city", city, "--state", state],
        capture_output=True, text=True,
        cwd=str(_PROJECT_ROOT),
    )
    if result.returncode != 0:
        print(f"  Discovery script error:\n{result.stderr[-500:]}")
        return {}

    city_slug = _make_slug(city, state)
    source_file = SOURCES_DIR / city_slug / "source.json"
    if not source_file.exists():
        for d in SOURCES_DIR.iterdir():
            sf = d / "source.json"
            if sf.exists():
                try:
                    data = json.loads(sf.read_text())
                    if data.get("city", "").lower() == city.lower() and data.get("state", "") == state:
                        source_file = sf
                        city_slug = d.name
                        break
                except Exception:
                    continue

    if not source_file.exists():
        print(f"  ✗ No source.json written — city may not be discoverable")
        return {}

    source = json.loads(source_file.read_text())
    platform = source.get("best_source", {}).get("platform", "unknown")
    freshness = source.get("best_source", {}).get("freshness", "unknown")
    url = source.get("best_source", {}).get("url", "")
    most_recent = source.get("best_source", {}).get("most_recent_date", "")

    print(f"  Platform:    {platform}")
    print(f"  Freshness:   {freshness}")
    print(f"  URL:         {url}")
    print(f"  Most recent: {most_recent or 'unknown'}")
    print(f"  City slug:   {city_slug}")

    if platform not in SUPPORTED_PLATFORMS:
        print(f"\n  ✗ FAIL — Platform '{platform}' is not supported.")
        print(f"    Supported: {', '.join(sorted(SUPPORTED_PLATFORMS))}")
        return {"fail": f"unsupported platform: {platform}", "platform": platform, "city_slug": city_slug, "source": source}

    if freshness in ("stale", "empty", "no_source", "blocked"):
        print(f"\n  ✗ FAIL — Freshness is '{freshness}' — no usable data on this platform.")
        return {"fail": f"freshness={freshness}", "platform": platform, "city_slug": city_slug, "source": source}

    if freshness == "unknown_spa":
        print(f"\n  ✗ FAIL — Platform is a JS SPA — our collector cannot reach the API.")
        return {"fail": "JS SPA — API not accessible without Playwright", "platform": platform, "city_slug": city_slug, "source": source}

    print(f"\n  ✓ PASS — {platform} / {freshness}")
    return {"platform": platform, "city_slug": city_slug, "source": source}


# ── Step 2: Collection ───────────────────────────────────────────────────────

async def run_collection(platform: str, city_slug: str, city: str, state: str) -> dict:
    """Run the collection agent and check for meetings with agenda files."""
    print(f"\n{'─'*60}")
    print(f"STEP 2 — Collection ({platform})")
    print(f"{'─'*60}")

    from meeting_pipeline.shared.config import AgentConfig, get_storage
    from meeting_pipeline.stages.collect.router import route_city

    cfg = AgentConfig(download_pdfs=True)
    storage = get_storage(cfg)
    event = {"city": city, "state": state}

    try:
        result = await route_city(event, storage, cfg)
    except Exception as e:
        print(f"  ✗ FAIL — Collection agent raised exception: {e}")
        return {"fail": f"collector exception: {e}"}

    if result.error:
        print(f"  ✗ FAIL — {result.error}")
        return {"fail": result.error}

    print(f"  Events found: {result.events_found}")
    print(f"  PDFs downloaded: {result.pdfs_downloaded}")

    if result.events_found == 0:
        print(f"\n  ✗ FAIL — Collector returned 0 events.")
        return {"fail": "no events returned"}

    print(f"\n  ✓ PASS — {result.events_found} events collected")
    return {"events_found": result.events_found, "pdfs_downloaded": result.pdfs_downloaded}


# ── Step 3: PDF check ────────────────────────────────────────────────────────

def check_pdfs(city_slug: str, platform: str) -> dict:
    print(f"\n{'─'*60}")
    print(f"STEP 3 — PDF Quality Check")
    print(f"{'─'*60}")

    # Scan all platform data dirs for PDFs (same logic as find_best_pdf)
    data_dir = SOURCES_DIR / city_slug / "data"
    pdfs: list[Path] = []
    if data_dir.exists():
        platform_order = [platform] + [
            d.name for d in sorted(data_dir.iterdir())
            if d.is_dir() and d.name != platform
        ]
        for plat in platform_order:
            for subdir in ["pdfs", "attachments"]:
                d = data_dir / plat / subdir
                if d.exists():
                    pdfs.extend(p for p in d.glob("*.pdf") if p.stat().st_size >= MIN_PDF_BYTES)

    if not pdfs:
        print(f"  No PDFs > {MIN_PDF_BYTES // 1024}KB found.")
        print(f"\n  ✗ FAIL — No downloadable PDFs found (viewer-only or auth required).")
        return {"fail": "no downloadable PDFs"}

    largest = max(pdfs, key=lambda p: p.stat().st_size)
    size_kb = largest.stat().st_size // 1024
    print(f"  PDFs on disk: {len(pdfs)}")
    print(f"  Largest PDF:  {largest.name} ({size_kb}KB)")
    print(f"\n  ✓ PASS — {size_kb}KB PDF available")
    return {"largest_pdf": largest, "size_kb": size_kb}


# ── Main ─────────────────────────────────────────────────────────────────────

def _make_slug(city: str, state: str) -> str:
    return f"{city.lower().replace(' ', '-')}-{state}"


async def main_async(args: argparse.Namespace) -> None:
    city, state = args.city.strip(), args.state.strip().upper()

    print(f"\n{'='*60}")
    print(f"CITY CHECK: {city}, {state}")
    print(f"{'='*60}")

    # Step 1 — Discovery
    step1 = run_discovery(city, state)
    if "fail" in step1:
        _print_verdict(False, step1["fail"])
        return

    # Step 2 — Collection (via agent)
    step2 = await run_collection(step1["platform"], step1["city_slug"], city, state)
    if "fail" in step2:
        _print_verdict(False, step2["fail"])
        return

    # Step 3 — PDF check
    step3 = check_pdfs(step1["city_slug"], step1["platform"])
    if "fail" in step3:
        _print_verdict(False, step3["fail"])
        return

    _print_verdict(True, (
        f"Platform={step1['platform']} | "
        f"{step2['events_found']} events | "
        f"Largest PDF={step3['size_kb']}KB"
    ))


def _print_verdict(passed: bool, detail: str) -> None:
    print(f"\n{'='*60}")
    if passed:
        print(f"  ✅ PASS — This city is supported by the pipeline.")
    else:
        print(f"  ❌ FAIL — This city cannot be collected.")
    print(f"  {detail}")
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check if a city can be supported by the pipeline")
    parser.add_argument("--city", required=True, help="City name (e.g. 'Johnstown')")
    parser.add_argument("--state", required=True, help="State abbreviation (e.g. OH)")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
