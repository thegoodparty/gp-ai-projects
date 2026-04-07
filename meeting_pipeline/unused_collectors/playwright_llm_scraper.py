"""
playwright_llm_scraper.py — Multimodal LLM-assisted web scraper for ad-hoc city sites.

Hybrid approach:
  1. Playwright renders the JS-heavy page and extracts all links from the DOM
  2. Gemini Flash vision analyzes a screenshot to identify which links are
     city council meeting agenda PDFs
  3. Downloads the identified PDFs

This handles sites that:
  - Require JavaScript to render (SPAs, Telerik, ASP.NET)
  - Have non-standard HTML structure
  - Need visual understanding to distinguish agendas from other docs

Cost: ~$0.01-0.05 per city page (1 screenshot + structured output via Gemini Flash).

Usage:
    from collectors.playwright_llm_scraper import collect_with_llm_vision
    result = await collect_with_llm_vision(config)
"""

import asyncio
import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, unquote

import httpx
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pydantic schema for LLM structured output
# ---------------------------------------------------------------------------

class IdentifiedAgendaLink(BaseModel):
    """A link the LLM identified as a city council meeting agenda."""
    link_index: int = Field(description="Index into the links list (0-based)")
    date: Optional[str] = Field(None, description="Meeting date in YYYY-MM-DD format if visible")
    body: str = Field(description="Governing body name, e.g. 'City Council', 'Town Council'")
    description: str = Field(description="Brief description of why this link is an agenda")


class PageAnalysis(BaseModel):
    """LLM analysis of a city meeting page."""
    agenda_links: list[IdentifiedAgendaLink] = Field(
        description="Links identified as city council meeting agendas"
    )
    page_description: str = Field(description="One-sentence description of what this page shows")
    has_pagination: bool = Field(False, description="True if the page has pagination / 'next' links")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class PlaywrightLLMConfig:
    url: str
    city_name: str
    output_dir: Path
    lookback_days: int = 90
    download_pdfs: bool = True
    max_links: int = 200  # cap links sent to LLM to control token usage
    screenshot_width: int = 1280
    screenshot_height: int = 2400  # tall to capture more content
    wait_seconds: int = 5  # extra wait after page load for JS rendering
    extra_keywords: list[str] = field(default_factory=list)
    _depth: int = 0  # internal: recursion depth (0=top-level, 1=sub-page)


@dataclass
class PlaywrightLLMResult:
    city: str
    url: str
    total_links: int
    agenda_links: int
    pdfs_downloaded: int
    events: list[dict]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

async def collect_with_llm_vision(config: PlaywrightLLMConfig) -> PlaywrightLLMResult:
    """
    Render a page with Playwright, analyze with Gemini Flash, download agenda PDFs.
    """
    from shared.llm_gemini import GeminiClient, GeminiModelType

    print(f"  [playwright] Navigating to {config.url}")

    # --- Step 1: Render page and extract links + screenshot ---
    try:
        links, screenshot_path = await _render_and_extract(config)
    except Exception as e:
        print(f"  [playwright] ERROR rendering: {e}")
        return PlaywrightLLMResult(
            city=config.city_name, url=config.url,
            total_links=0, agenda_links=0, pdfs_downloaded=0,
            events=[], error=str(e),
        )

    print(f"  [playwright] Extracted {len(links)} links, screenshot saved")

    if not links:
        return PlaywrightLLMResult(
            city=config.city_name, url=config.url,
            total_links=0, agenda_links=0, pdfs_downloaded=0,
            events=[], error="No links found on page",
        )

    # --- Step 1b: Follow current-year sub-archive pages (depth-2 scrape) ---
    # Sites like Squarespace/Wix organize agendas as:
    #   /agendas-minutes → /2026-agendas-minutes (year sub-page with actual PDFs)
    # Detect year sub-pages and merge their links before the LLM sees anything.
    if config._depth == 0:
        current_year = str(datetime.now().year)
        year_subpages = _find_year_subpages(links, config.url, current_year)
        if year_subpages:
            print(f"  [playwright] Found {len(year_subpages)} year sub-page(s), following for depth-2 scrape")
            for sub_url in year_subpages[:2]:  # follow at most 2 year pages
                try:
                    sub_cfg = PlaywrightLLMConfig(
                        url=sub_url, city_name=config.city_name,
                        output_dir=config.output_dir,
                        lookback_days=config.lookback_days,
                        download_pdfs=False,  # don't download yet — LLM decides
                        wait_seconds=config.wait_seconds,
                        _depth=1,
                    )
                    sub_links, _ = await _render_and_extract(sub_cfg)
                    print(f"  [playwright] Sub-page {sub_url}: {len(sub_links)} links")
                    # Filter sub-page links: keep PDFs/docs + links with date-like text
                    # This avoids flooding the LLM with nav boilerplate from the sub-page
                    useful_sub = _filter_subpage_links(sub_links, sub_url)
                    print(f"  [playwright] Sub-page kept {len(useful_sub)}/{len(sub_links)} useful links")
                    for lnk in useful_sub:
                        lnk["_from_subpage"] = sub_url
                    links = links + useful_sub
                except Exception as e:
                    print(f"  [playwright] Warning: couldn't follow sub-page {sub_url}: {e}")

    # --- Step 2: Send screenshot + link list to Gemini Flash ---
    # Build the link list for the LLM (capped to max_links)
    link_list_text = _format_links_for_llm(links[:config.max_links])

    prompt = f"""You are analyzing a city government meeting/agenda webpage for {config.city_name}.

Below is a numbered list of ALL links found on this page (including links pre-fetched from sub-pages).
Your task: identify links that are individual city council meeting agenda documents.

PRIORITY ORDER — always prefer higher-priority matches:
1. **PDF files** (.pdf) whose link text contains a date (e.g. "4.6.2026 | Regular Meeting") → HIGHEST PRIORITY
2. **Individual meeting agenda pages** (HTML) for a specific dated meeting
3. **Year-archive pages** (e.g. /2026-agendas-minutes) — only use these if NO PDFs or individual pages exist

IMPORTANT RULES:
- Only include links for the PRIMARY legislative body (City Council, Town Council, Town Board, Board of Aldermen, Village Council, City Commission).
- Do NOT include links for advisory boards, planning commissions, zoning boards, school boards, or committees.
- Do NOT include minutes — only agendas or agenda packets.
- Do NOT include year-archive index pages if individual PDFs for that year are already in the list.
- If link text contains a date like "4.6.2026" or "March 2, 2026", extract it as YYYY-MM-DD.

LINK LIST:
{link_list_text}

Analyze the link list (and screenshot for context) to identify the best individual city council agenda links."""

    try:
        gemini = GeminiClient(default_model=GeminiModelType.FLASH)

        # Use the raw multimodal approach with structured output
        from PIL import Image as PILImage
        from google.genai import types

        image = PILImage.open(screenshot_path)

        gemini_config = gemini._get_base_config(temperature=0.1, thinking_budget=0)
        gemini_config.response_mime_type = "application/json"
        gemini_config.response_schema = PageAnalysis

        contents = [image, prompt]
        model_name = GeminiModelType.FLASH.value

        response = gemini.client.models.generate_content(
            model=model_name,
            contents=contents,
            config=gemini_config,
        )
        gemini._track_usage(response, model_name)

        # Parse response
        if hasattr(response, "parsed") and response.parsed:
            analysis = response.parsed
            if isinstance(analysis, dict):
                analysis = PageAnalysis(**analysis)
        else:
            data = json.loads(response.text.strip())
            analysis = PageAnalysis(**data)

    except Exception as e:
        print(f"  [llm] ERROR analyzing page: {e}")
        return PlaywrightLLMResult(
            city=config.city_name, url=config.url,
            total_links=len(links), agenda_links=0, pdfs_downloaded=0,
            events=[], error=f"LLM analysis failed: {e}",
        )
    finally:
        # Clean up screenshot
        Path(screenshot_path).unlink(missing_ok=True)

    print(f"  [llm] Page: {analysis.page_description}")
    print(f"  [llm] Identified {len(analysis.agenda_links)} agenda links")

    # --- Step 3: Build events from identified links ---
    cutoff = datetime.now() - timedelta(days=config.lookback_days)
    events = []

    for item in analysis.agenda_links:
        if item.link_index < 0 or item.link_index >= len(links):
            continue

        link = links[item.link_index]
        url = link["url"]
        date = item.date

        # Apply lookback filter
        if date:
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                if dt < cutoff:
                    continue
            except ValueError:
                pass

        events.append({
            "date": date or "unknown",
            "title": f"{item.body} Meeting",
            "body": item.body,
            "agendaUrl": url,
            "linkText": link["text"],
            "llmReason": item.description,
        })

    # Deduplicate by URL
    seen = set()
    unique_events = []
    for e in events:
        if e["agendaUrl"] not in seen:
            seen.add(e["agendaUrl"])
            unique_events.append(e)
    events = unique_events

    # --- Step 4: Save events and download PDFs ---
    config.output_dir.mkdir(parents=True, exist_ok=True)
    events_file = config.output_dir / "events.json"
    with open(events_file, "w") as f:
        json.dump(events, f, indent=2)
    print(f"  [save] {len(events)} events -> {events_file}")

    downloaded = 0
    if config.download_pdfs and events:
        downloaded = await _download_agendas(events, config.output_dir)

    return PlaywrightLLMResult(
        city=config.city_name, url=config.url,
        total_links=len(links), agenda_links=len(events),
        pdfs_downloaded=downloaded, events=events,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _render_and_extract(config: PlaywrightLLMConfig) -> tuple[list[dict], str]:
    """Use Playwright to render page, extract links, and take a screenshot."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": config.screenshot_width, "height": config.screenshot_height},
        )
        page = await context.new_page()

        # Navigate — try networkidle first, fall back to domcontentloaded
        try:
            await page.goto(config.url, wait_until="networkidle", timeout=25000)
        except Exception:
            await page.goto(config.url, wait_until="domcontentloaded", timeout=20000)

        # Extra wait for JS rendering
        await page.wait_for_timeout(config.wait_seconds * 1000)

        # Extract all links from the rendered DOM
        links = await page.evaluate("""() => {
            const links = [];
            document.querySelectorAll('a[href]').forEach((a, i) => {
                const href = a.href;
                const text = (a.textContent || '').trim().substring(0, 200);
                const parentText = (a.parentElement?.textContent || '').trim().substring(0, 300);
                links.push({ url: href, text, parentText, index: i });
            });
            return links;
        }""")

        # Take full-page screenshot
        screenshot_path = tempfile.mktemp(suffix=".png")
        await page.screenshot(path=screenshot_path, full_page=True)

        await browser.close()

    return links, screenshot_path


def _format_links_for_llm(links: list[dict]) -> str:
    """Format the link list for the LLM prompt."""
    lines = []
    for i, link in enumerate(links):
        url_decoded = unquote(link["url"])
        # Truncate long URLs
        if len(url_decoded) > 150:
            url_decoded = url_decoded[:147] + "..."
        text = link["text"][:80] if link["text"] else "(no text)"
        lines.append(f"[{i}] text=\"{text}\"  url={url_decoded}")
    return "\n".join(lines)


def _find_year_subpages(links: list[dict], base_url: str, current_year: str) -> list[str]:
    """
    Detect year-archive sub-pages from a link list.

    These are same-domain HTML links whose URL or text contains the current year
    and look like archive pages (e.g. /2026-agendas-minutes, /agendas/2026).
    Returns URLs sorted descending by year (most recent first).
    """
    from urllib.parse import urlparse

    base_domain = urlparse(base_url).netloc
    candidates: list[tuple[int, str]] = []

    for link in links:
        url = link.get("url", "")
        text = link.get("text", "")
        if not url:
            continue
        # Same domain only
        if urlparse(url).netloc != base_domain:
            continue
        # Must not be a file (PDF, doc, etc.)
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in [".pdf", ".docx", ".doc", ".xlsx"]):
            continue
        # Skip anchor-only links
        if url == base_url or url.rstrip("/") == base_url.rstrip("/"):
            continue
        if "#" in url and urlparse(url).path == urlparse(base_url).path:
            continue

        # Look for a 4-digit year in the URL path or link text
        year_match = re.search(r"(20\d{2})", path + " " + text)
        if year_match:
            year = int(year_match.group(1))
            # Only current and previous year (don't go back too far)
            if year >= int(current_year) - 1:
                candidates.append((year, url))

    # Deduplicate and sort descending
    seen_urls: set[str] = set()
    result = []
    for year, url in sorted(candidates, reverse=True):
        if url not in seen_urls:
            seen_urls.add(url)
            result.append(url)

    return result


def _filter_subpage_links(links: list[dict], subpage_url: str) -> list[dict]:
    """
    Filter a sub-page's link list down to the ones that are likely agenda files.

    Keeps:
      - PDF/doc files (.pdf, .docx)
      - Links whose text contains a date pattern (M.D.YYYY, MM/DD/YYYY, Month YYYY)
      - Links whose text or URL contains agenda-related keywords

    Strips nav boilerplate to avoid wasting LLM token budget.
    """
    date_pattern = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{4}\b|\b\d{4}\b")
    agenda_kw = re.compile(r"agenda|packet|meeting|regular|special|workshop|retreat", re.I)
    result = []
    for lnk in links:
        url = lnk.get("url", "")
        text = lnk.get("text", "")
        path = url.lower()
        # Always keep PDF/doc files
        if any(path.endswith(ext) for ext in [".pdf", ".docx", ".doc"]):
            result.append(lnk)
            continue
        # Keep if text has a date pattern AND an agenda keyword
        if date_pattern.search(text) and agenda_kw.search(text):
            result.append(lnk)
            continue
        # Keep if URL contains agenda keyword and a year
        if agenda_kw.search(path) and re.search(r"20\d{2}", path):
            result.append(lnk)
    return result


async def _download_agendas(events: list[dict], output_dir: Path) -> int:
    """Download agenda documents (PDFs or HTML) from identified events."""
    pdfs_dir = output_dir / "pdfs"
    html_dir = output_dir / "html"
    pdfs_dir.mkdir(exist_ok=True)

    downloaded = 0
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False, headers=headers) as client:
        for event in events:
            agenda_url = event.get("agendaUrl")
            if not agenda_url:
                continue

            date = event.get("date", "unknown")
            safe_text = re.sub(r"[^\w\-.]", "_", event.get("body", "agenda"))[:40]

            # Check if already downloaded
            pdf_path = pdfs_dir / f"{date}_{safe_text}.pdf"
            html_path = html_dir / f"{date}_{safe_text}.html"
            if (pdf_path.exists() and pdf_path.stat().st_size > 5000) or (
                html_path.exists() and html_path.stat().st_size > 1000
            ):
                downloaded += 1
                continue

            try:
                resp = await client.get(agenda_url)
                resp.raise_for_status()
                content = resp.content

                if content[:4] == b"%PDF":
                    # Actual PDF
                    pdf_path.write_bytes(content)
                    downloaded += 1
                    print(f"    PDF: {pdf_path.name} ({len(content):,} bytes)")
                elif b"<!doctype" in content[:100].lower() or b"<html" in content[:100].lower():
                    # HTML agenda viewer page — save the HTML for text extraction
                    html_dir.mkdir(exist_ok=True)
                    html_path.write_bytes(content)
                    event["format"] = "html"
                    event["localPath"] = str(html_path)
                    downloaded += 1
                    print(f"    HTML: {html_path.name} ({len(content):,} bytes)")
                elif len(content) > 5000:
                    # Unknown format but substantial content — save as-is
                    pdf_path.write_bytes(content)
                    downloaded += 1
                    print(f"    RAW: {pdf_path.name} ({len(content):,} bytes)")
                else:
                    print(f"    SKIP (too small / not recognized): {date}")
            except Exception as e:
                print(f"    ERROR downloading {date}: {e}")

    return downloaded
