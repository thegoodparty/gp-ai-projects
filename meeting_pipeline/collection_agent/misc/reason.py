"""
misc/reason.py — Reason mode: agentic browser collects agendas from any city page.

Uses browser-use (LLM-driven browser agent) to navigate city government websites
and find city council agenda documents. The agent handles SPAs, JavaScript rendering,
pagination, and arbitrary navigation patterns without platform-specific code.

Falls back to the legacy one-shot Playwright+LLM vision scraper if browser-use fails.

Architecture notes (from collection-agent-scaling.md):
  - Agentic browser eliminates nav_config staleness — no config to go stale
  - Handles SPAs (CivicClerk, BoardDocs) that one-shot screenshot can't navigate
  - Same cost profile as one-shot at pilot scale; preferred long-term at 30K cities
  - Still requires Fargate (headless Chromium) — not Lambda-compatible

On success, still saves a nav_config so replay mode can take over on subsequent
runs — preserving the Architecture A cost advantage while gaining Architecture B
reliability for first-time / SPA cities.
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent.parent.parent

from ..models import CollectionResult, NavConfig
from ..storage import StorageBackend
from ..config import AgentConfig, city_to_slug
from .. import notification_log
from ..document_verifier import verify_events


class ReasonFailed(Exception):
    """Raised when the agentic browser (and fallback) found no agenda links."""


# ── Goal prompt for the browser agent ─────────────────────────────────────────

_AGENT_GOAL = """\
Go to {start_url} and find city council agenda documents for {city_name}.

GOAL: Collect the most current agenda documents available for the PRIMARY governing
body (City Council, Town Council, Village Council, Board of Trustees, etc.).
Prefer UPCOMING meetings (future dates) but accept recent meetings from the last
{lookback_days} days (on or after {cutoff_date}). Always return the most recent/upcoming
items you can find.

━━ CRITICAL RULES ━━
• Every event MUST have a specific meeting date in YYYY-MM-DD format (e.g. 2026-04-15).
  NEVER return "unknown" or a year-only string. If you only see an archive/index page
  listing years or months, you MUST click into it to find individual meeting dates.
• Only collect the PRIMARY governing body meetings: City Council, Town Council, Village
  Council, City Commission, Board of Trustees (for townships), Board of Aldermen.
  Skip planning commissions, zoning boards, school boards, advisory committees.
• Prefer future/upcoming meetings. If none exist, take the most recent past meetings
  dated on or after {cutoff_date}.
• The agendaUrl must be the URL of the actual agenda document (PDF, DOCX, or an
  agenda viewer page for that specific meeting) — NOT a page listing many meetings.

━━ NAVIGATION STRATEGY ━━
1. Navigate to the meetings or agendas section. Look for nav items like "Agendas",
   "Meetings", "City Council", "Minutes & Agendas", "Boards & Commissions".
2. Once on the agendas/meetings page, IMMEDIATELY scroll down and expand ALL
   collapsed sections before searching for links. Government sites frequently hide
   content in accordions — look for year labels ("2026", "2025"), "+" icons, chevrons,
   collapsed rows, or any clickable section headers. CLICK THEM ALL to reveal content.
   Do this before concluding anything about what's on the page.
3. After expanding, look for links to individual meeting documents: text like "Agenda",
   "View Agenda", "Packet", or links ending in .pdf or .docx.
4. If you land on a year-index page, click into the current year to see individual entries.
5. If the list is paginated, start from the most recent page.
6. NEVER leave the city's official website. Do not navigate to YouTube, Facebook,
   Twitter, or any other external/social media site under any circumstances.
   If agendas are only on social media, return [].
7. If you are stuck on a page and not making progress after 2–3 steps, try a
   DIFFERENT navigation path — go back to the homepage, look for a different menu
   item, try searching the site. Only return [] after you have genuinely exhausted
   all reasonable navigation paths on the site.

━━ OUTPUT FORMAT ━━
Return ONLY a JSON array (no markdown fences, no explanation):
[
  {{"date": "YYYY-MM-DD", "body": "City Council", "agendaUrl": "FULL_ABSOLUTE_URL"}},
  ...
]

Return [] if:
- No qualifying meetings found within the date window after thorough navigation
- The site requires login or blocks automated access
- After genuine effort you cannot find individual dated agenda documents
"""


# ── Agentic browser collect ────────────────────────────────────────────────────

async def _collect_with_agent(
    url: str,
    city_name: str,
    lookback_days: int,
) -> list[dict]:
    """
    Run browser-use agent to find agenda links on a city government page.

    Returns a list of event dicts: [{date, body, agendaUrl}, ...]
    Raises RuntimeError if the agent errors out or returns unparseable output.
    """
    import os
    try:
        from browser_use import Agent
        from browser_use.browser import BrowserProfile
        from langchain_anthropic import ChatAnthropic
    except ImportError as e:
        raise RuntimeError(
            f"browser_use or langchain_anthropic not available: {e}"
        )

    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    goal = _AGENT_GOAL.format(
        start_url=url,
        city_name=city_name,
        lookback_days=lookback_days,
        cutoff_date=cutoff,
    )

    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0.0,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )

    profile = BrowserProfile(
        headless=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )

    agent = Agent(
        task=goal,
        llm=llm,
        browser_profile=profile,
        directly_open_url=url,
        max_failures=3,
        max_actions_per_step=8,
    )

    print(f"  [agent] Starting browser agent for {city_name} at {url}")
    history = await agent.run(max_steps=20)
    raw = history.final_result()
    print(f"  [agent] Agent finished. Raw result length: {len(raw) if raw else 0}")

    if not raw:
        raise RuntimeError("Agent returned no result")

    # Parse the JSON array the agent was instructed to return
    raw = raw.strip()
    # Strip markdown code fences if the model wrapped its output
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Try direct parse first; if agent returned prose with embedded JSON, extract it
    try:
        events = json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            events = json.loads(match.group())
        else:
            raise RuntimeError(f"Agent result is not parseable JSON: {raw[:200]}")

    if not isinstance(events, list):
        raise RuntimeError(f"Agent result is not a list: {type(events)}")

    return events


# ── Legacy one-shot fallback ───────────────────────────────────────────────────

async def _collect_with_vision_fallback(
    url: str,
    city_name: str,
    lookback_days: int,
    download_pdfs: bool,
    output_dir: Path,
) -> tuple[list[dict], int]:
    """Vision fallback removed — browser-use agent is the primary path."""
    raise RuntimeError(
        "Vision fallback is not available. Ensure browser-use agent is working."
    )


# ── Main entry point ───────────────────────────────────────────────────────────

async def collect_with_reason(
    event: dict,
    source: dict,
    storage: StorageBackend,
    cfg: AgentConfig,
) -> CollectionResult:
    """
    Use an agentic browser to collect agenda links from any city government page.

    Strategy:
      1. Run browser-use agent (handles SPAs, pagination, dynamic navigation)
      2. If agent fails, fall back to legacy one-shot Playwright+vision scraper
      3. Verify collected events are real agenda documents
      4. Save nav_config to source.json for future replay mode

    Args:
        event:   {"city": "...", "state": "..."}
        source:  Parsed source.json dict
        storage: Storage backend
        cfg:     Agent configuration

    Raises:
        ReasonFailed: if both the agent and fallback found no valid agenda links
    """
    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)

    best = source.get("best_source", {})
    entry_url = best.get("url", "")
    if not entry_url:
        raise ReasonFailed(f"No URL in source.json for {city}, {state}")

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    output_dir = repo_root / cfg.output_prefix / city_slug / "playwright_llm"
    output_dir.mkdir(parents=True, exist_ok=True)

    events: list[dict] = []
    pdfs_downloaded = 0
    used_agent = False

    # ── Step 1: Try agentic browser ───────────────────────────────────────────
    try:
        events = await _collect_with_agent(entry_url, city, cfg.lookback_days)
        used_agent = True
        print(f"  [agent] Found {len(events)} raw events for {city}")
    except Exception as e:
        print(f"  [agent] Agent failed for {city}: {e} — falling back to vision scraper")

    # ── Step 2: Fallback to legacy vision scraper if agent failed/empty ───────
    if not events:
        try:
            events, pdfs_downloaded = await _collect_with_vision_fallback(
                entry_url, city, cfg.lookback_days, cfg.download_pdfs, output_dir
            )
            print(f"  [vision] Found {len(events)} raw events for {city}")
        except Exception as e:
            raise ReasonFailed(f"Both agent and vision fallback failed for {city}, {state}: {e}")

    if not events:
        raise ReasonFailed(f"No agenda links found for {city}, {state}")

    # ── Step 3: Verify events are real agenda documents ───────────────────────
    body_hint = events[0].get("body", "City Council") if events else None
    valid_events, rejected = await verify_events(
        events,
        lookback_days=cfg.lookback_days,
        body_name_hint=body_hint,
    )

    if rejected:
        print(f"  [verify] Rejected {len(rejected)}/{len(events)} events as non-agendas")
        for e in rejected[:5]:
            print(f"    - {e.get('date','?')} {e.get('agendaUrl','?')[:60]}: {e.get('verificationReason')}")

    if not valid_events:
        raise ReasonFailed(
            f"All {len(rejected)} identified links failed verification for {city}, {state}"
        )

    print(f"  [verify] {len(valid_events)}/{len(events)} events passed verification")

    # ── Step 4: Save verified events ─────────────────────────────────────────
    events_file = output_dir / "events.json"
    with open(events_file, "w") as f:
        json.dump(valid_events, f, indent=2)

    if rejected:
        with open(output_dir / "events_rejected.json", "w") as f:
            json.dump(rejected, f, indent=2)

    # Download PDFs if we used the agent
    if used_agent and cfg.download_pdfs and valid_events:
        import httpx
        for ev in valid_events:
            agenda_url = ev.get("agendaUrl", "")
            if not agenda_url or not agenda_url.lower().endswith(".pdf"):
                continue
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as dl:
                    resp = await dl.get(agenda_url)
                    resp.raise_for_status()
                date_str = ev.get("date", "unknown")
                filename = agenda_url.split("/")[-1].split("?")[0] or f"{date_str}_agenda.pdf"
                pdf_key = f"{cfg.sources_prefix}/{city_slug}/agentic_browser/pdfs/{filename}"
                storage.write_bytes(pdf_key, resp.content)
                pdfs_downloaded += 1
                print(f"  [agent] Downloaded: {filename} ({len(resp.content) // 1024}KB)")
            except Exception as e:
                print(f"  [agent] PDF download failed for {agenda_url[:60]}: {e}")

    # ── Step 5: Derive and save nav_config for future replay ──────────────────
    nav = _derive_nav_config(valid_events, entry_url)
    best["nav_config"] = nav.to_dict()
    source["best_source"] = best

    source_key = f"{cfg.sources_prefix}/{city_slug}/source.json"
    storage.write_json(source_key, source)

    notification_log.log_event(
        notification_log.NAV_CONFIG_SAVED,
        city, state,
        storage=storage,
        logs_prefix=cfg.logs_prefix,
        entry_url=entry_url,
        strategy=nav.strategy,
        events_found=len(valid_events),
    )

    return CollectionResult(
        city=city,
        state=state,
        platform="agentic_browser" if used_agent else "playwright_llm",
        events_found=len(valid_events),
        pdfs_downloaded=pdfs_downloaded,
        events=valid_events,
        requires_browser=True,
        nav_config_saved=True,
    )


# ── Nav config derivation ──────────────────────────────────────────────────────

def _derive_nav_config(events: list[dict], source_url: str) -> NavConfig:
    """Derive a replayable NavConfig from verified events."""
    urls = [e.get("agendaUrl", "") for e in events]

    if any(".pdf" in u.lower() for u in urls):
        strategy = "direct_pdf"
    elif any("/documentcenter/" in u.lower() for u in urls):
        strategy = "document_center"
    elif any("rss" in u.lower() or ".xml" in u.lower() for u in urls):
        strategy = "rss_feed"
    else:
        strategy = "direct_pdf"

    body_hint = events[0].get("body", "City Council") if events else "City Council"

    return NavConfig(
        platform_guess="unknown",
        entry_url=source_url,
        strategy=strategy,
        selector=None,
        keyword_filter="agenda",
        follow_url=None,
        verify_ssl=True,
        body_name_hint=body_hint,
        recorded_at=date.today().isoformat(),
    )
