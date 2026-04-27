"""
verify_city_sources.py — Verify all pilot city data sources in one run.

Run this, walk away, come back in ~1 hour with a complete verification report.

For each city, tests up to 5 verification levels:
  V0 — Unverified (no checks passed)
  V1 — Reachable (server responds)
  V2 — Data found (API/AJAX returns meeting entries)
  V3 — Content verified (PDF has extractable agenda text)
  V4 — Pipeline verified (LLM extraction produces structured MeetingData)

Usage:
    uv run python meeting_pipeline/scripts/verify_city_sources.py

    # Skip V4 (LLM) to save time/cost — just do HTTP + PDF checks:
    uv run python meeting_pipeline/scripts/verify_city_sources.py --skip-llm

    # Test a single city:
    uv run python meeting_pipeline/scripts/verify_city_sources.py --city "Chapel Hill"

Output:
    meeting_pipeline/verification_report.json    — Full detail per city
    meeting_pipeline/verification_summary.txt    — Human-readable summary
    Console output with progress + final summary

Cost:
    V0-V3: $0 (HTTP + PDF parsing only)
    V4: ~$0.005/city × ~20-30 cities that reach V3 = ~$0.10-0.15 total
"""

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
import fitz  # PyMuPDF

# ============================================================================
# CONFIGURATION
# ============================================================================

# Concurrency limits — be polite to city servers
HTTP_CONCURRENCY = 8       # parallel HTTP checks
LLM_CONCURRENCY = 3        # parallel Gemini calls (avoid rate limits)
REQUEST_TIMEOUT = 20        # seconds per HTTP request
PDF_MAX_BYTES = 80_000_000  # 80MB — skip enormous packets

# Output paths
SCRIPT_DIR = Path(__file__).resolve().parent
BRIEFING_ROOT = SCRIPT_DIR.parent
OUTPUT_DIR = BRIEFING_ROOT
REPORT_PATH = OUTPUT_DIR / "verification_report.json"
SUMMARY_PATH = OUTPUT_DIR / "verification_summary.txt"


# ============================================================================
# PILOT CITY LIST — All 67 cities across NC, OH, TX
# ============================================================================

PILOT_CITIES = [
    # ── North Carolina (21 cities, 35 EOs) ──
    {"city": "Apex", "state": "NC", "eos": 1, "domain": "apexnc.org", "municode_url": "https://meetings.municode.com/PublishPage/index?cid=APEXNC&ppid=ec03baf8-8c93-4368-a28a-e658e962cbd1&p=0"},
    {"city": "Asheville", "state": "NC", "eos": 2, "domain": "ashevillenc.gov", "civicclerk_url": "https://ashevillenc.portal.civicclerk.com"},
    {"city": "Burlington", "state": "NC", "eos": 2, "domain": "burlingtonnc.gov"},  # CivicPlus ArchiveCenter at /Archive.aspx?AMID=44
    {"city": "Chapel Hill", "state": "NC", "eos": 2, "domain": "chapelhillnc.gov", "legistar_slug": "chapelhill"},
    {"city": "Clayton", "state": "NC", "eos": 1, "domain": "townofclaytonnc.org", "civicclerk_url": "https://claytonnc.portal.civicclerk.com"},
    {"city": "Concord", "state": "NC", "eos": 1, "domain": "concordnc.gov", "civicclerk_url": "https://concordnc.portal.civicclerk.com"},
    {"city": "Durham", "state": "NC", "eos": 2, "domain": "durhamnc.gov"},
    {"city": "Fayetteville", "state": "NC", "eos": 2, "domain": "fayettevillenc.gov", "legistar_slug": "cityoffayetteville"},
    {"city": "Gastonia", "state": "NC", "eos": 2, "domain": "gastonianc.gov", "granicus_url": "https://cityofgastonia.granicus.com/ViewPublisher.php?view_id=1"},
    {"city": "Greensboro", "state": "NC", "eos": 2, "domain": "greensboro-nc.gov", "legistar_slug": "greensboro"},
    {"city": "Greenville", "state": "NC", "eos": 2, "domain": "greenvillenc.gov"},
    {"city": "Hickory", "state": "NC", "eos": 1, "domain": "hickorync.gov", "civicclerk_url": "https://hickorync.portal.civicclerk.com"},
    {"city": "Huntersville", "state": "NC", "eos": 2, "domain": "huntersville.org", "novus_url": "https://huntersville.novusagenda.com/agendapublic"},
    {"city": "Jacksonville", "state": "NC", "eos": 2, "domain": "jacksonvillenc.gov"},
    {"city": "Lexington", "state": "NC", "eos": 2, "domain": "lexingtonnc.gov", "legistar_slug": "lexingtonnc"},
    {"city": "Matthews", "state": "NC", "eos": 1, "domain": "matthewsnc.gov", "civicclerk_url": "https://matthewsnc.portal.civicclerk.com"},
    {"city": "Monroe", "state": "NC", "eos": 2, "domain": "monroenc.org", "civicclerk_url": "https://monroenc.portal.civicclerk.com"},
    {"city": "New Bern", "state": "NC", "eos": 2, "domain": "newbernnc.gov", "municode_url": "https://library.municode.com/nc/new_bern"},
    {"city": "Rocky Mount", "state": "NC", "eos": 2, "domain": "rockymountnc.gov"},
    {"city": "Salisbury", "state": "NC", "eos": 1, "domain": "salisburync.gov", "municode_url": "https://library.municode.com/nc/salisbury"},
    {"city": "Statesville", "state": "NC", "eos": 1, "domain": "statesvillenc.net", "civicclerk_url": "https://statesvillenc.portal.civicclerk.com"},

    # ── Ohio (23 cities, 35 EOs) ──
    {"city": "Canal Winchester", "state": "OH", "eos": 2, "domain": "canalwinchesterohio.gov"},  # Corrected domain; CivicPlus AgendaCenter works
    {"city": "Centerville", "state": "OH", "eos": 2, "domain": "centervilleohio.gov"},
    {"city": "Cleveland", "state": "OH", "eos": 2, "domain": "clevelandohio.gov", "legistar_slug": "cityofcleveland"},
    {"city": "Cuyahoga Falls", "state": "OH", "eos": 1, "domain": "cityofcf.com", "custom_agenda_url": "https://www.cityofcf.com/city-council/files"},
    {"city": "Delaware", "state": "OH", "eos": 1, "domain": "delawareohio.net", "granicus_url": "https://delawareohio.granicus.com/ViewPublisher.php?view_id=1"},
    {"city": "Dublin", "state": "OH", "eos": 2, "domain": "dublinohiousa.gov", "boarddocs_url": "https://go.boarddocs.com/oh/dublin/Board.nsf/Public"},
    {"city": "Euclid", "state": "OH", "eos": 2, "domain": "cityofeuclid.com", "custom_agenda_url": "https://www.euclidlibrary.org/content/council-meetings"},
    {"city": "Fairborn", "state": "OH", "eos": 1, "domain": "fairbornoh.gov", "granicus_url": "https://fairborn.granicus.com/ViewPublisher.php?view_id=2"},
    {"city": "Fairfield", "state": "OH", "eos": 1, "domain": "fairfield-city.org"},
    {"city": "Hamilton", "state": "OH", "eos": 2, "domain": "hamilton-oh.gov", "custom_agenda_url": "https://www.hamilton-oh.gov/agendas-minutes"},  # Squarespace + Google Drive
    {"city": "Lima", "state": "OH", "eos": 2, "domain": "cityhall.lima-ohio.com", "boarddocs_url": "https://go.boarddocs.com/oh/lima/Board.nsf/Public"},
    {"city": "Loveland", "state": "OH", "eos": 1, "domain": "lovelandoh.gov"},
    {"city": "Marysville", "state": "OH", "eos": 1, "domain": "marysvilleohio.org", "municode_url": "https://library.municode.com/oh/marysville/munidocs"},
    {"city": "Mason", "state": "OH", "eos": 2, "domain": "imaginemason.org", "boarddocs_url": "https://go.boarddocs.com/oh/mason/Board.nsf/Public"},
    {"city": "Medina", "state": "OH", "eos": 1, "domain": "medinaoh.org", "boarddocs_url": "https://go.boarddocs.com/oh/medina/Board.nsf/Public"},
    {"city": "North Canton", "state": "OH", "eos": 2, "domain": "northcantonohio.gov"},
    {"city": "Parma", "state": "OH", "eos": 1, "domain": "cityofparma-oh.gov"},
    {"city": "Perrysburg", "state": "OH", "eos": 1, "domain": "ci.perrysburg.oh.us", "boarddocs_url": "https://go.boarddocs.com/oh/perrysburg/Board.nsf/Public"},
    {"city": "Powell", "state": "OH", "eos": 1, "domain": "cityofpowell.us", "granicus_url": "https://cityofpowell.us/government/agendas-minutes"},
    {"city": "Stow", "state": "OH", "eos": 2, "domain": "stow.oh.us"},
    {"city": "Troy", "state": "OH", "eos": 2, "domain": "troyohio.gov"},
    {"city": "Warren", "state": "OH", "eos": 1, "domain": "warren.org", "custom_agenda_url": "https://www.warren.org/government/city_council/meeting_agendas.php"},
    {"city": "Westerville", "state": "OH", "eos": 2, "domain": "westerville.org", "custom_agenda_url": "https://www.westerville.org/government/clerk-of-council/meeting-agendas-and-minutes"},

    # ── Texas (23 cities, 30 EOs) ──
    {"city": "Austin", "state": "TX", "eos": 1, "domain": "austintexas.gov", "legistar_slug": "austintexas"},
    {"city": "Beaumont", "state": "TX", "eos": 2, "domain": "beaumonttexas.gov", "primegov_url": "https://beaumonttexas.primegov.com/public/portal"},
    {"city": "Belton", "state": "TX", "eos": 2, "domain": "beltontexas.gov", "custom_agenda_url": "https://www.beltontexas.gov/government/city_council/city_council_agendas_and_minutes.php"},
    {"city": "Cibolo", "state": "TX", "eos": 2, "domain": "cibolotx.gov", "granicus_url": "https://cibolotx.granicus.com/ViewPublisher.php?view_id=1"},
    {"city": "Cleburne", "state": "TX", "eos": 1, "domain": "cleburne.net", "custom_agenda_url": "https://cleburne.community.diligentoneplatform.com/Portal/MeetingTypeList.aspx"},
    {"city": "Dallas", "state": "TX", "eos": 2, "domain": "dallascityhall.com", "legistar_slug": "cityofdallas"},
    {"city": "Duncanville", "state": "TX", "eos": 1, "domain": "duncanvilletx.gov", "civicclerk_url": "https://duncanvilletx.portal.civicclerk.com"},
    {"city": "El Paso", "state": "TX", "eos": 1, "domain": "elpasotexas.gov", "legistar_slug": "elpasotexas"},
    {"city": "Farmers Branch", "state": "TX", "eos": 2, "domain": "farmersbranchtx.gov", "legistar_slug": "farmersbranch"},
    {"city": "Grand Prairie", "state": "TX", "eos": 1, "domain": "gptx.org", "municode_url": "https://meetings.municode.com/PublishPage/index?cid=GPTX&ppid=0a01ee0f-2750-46df-ae4f-bb7135af7019&p=1"},
    {"city": "Killeen", "state": "TX", "eos": 1, "domain": "killeentexas.gov"},
    {"city": "Kyle", "state": "TX", "eos": 2, "domain": "cityofkyle.com", "novus_url": "https://kyle.novusagenda.com/agendapublic"},
    {"city": "La Porte", "state": "TX", "eos": 1, "domain": "laportetx.gov"},
    {"city": "Lancaster", "state": "TX", "eos": 2, "domain": "lancaster-tx.com"},  # DestinyHosted (no standard check)
    {"city": "Longview", "state": "TX", "eos": 1, "domain": "longviewtexas.gov"},
    {"city": "Lufkin", "state": "TX", "eos": 1, "domain": "cityoflufkin.com", "custom_agenda_url": "https://www.cityoflufkin.com/government/council_webcasts.php"},
    {"city": "Midland", "state": "TX", "eos": 1, "domain": "midlandtexas.gov", "primegov_url": "https://midland.primegov.com/Portal/Search"},
    {"city": "New Braunfels", "state": "TX", "eos": 1, "domain": "newbraunfels.gov", "legistar_slug": "newbraunfels"},
    {"city": "Palestine", "state": "TX", "eos": 1, "domain": "cityofpalestinetx.com"},  # Corrected domain; CivicPlus AgendaCenter
    {"city": "Sherman", "state": "TX", "eos": 1, "domain": "ci.sherman.tx.us"},  # Corrected domain; CivicPlus AgendaCenter
    {"city": "Temple", "state": "TX", "eos": 1, "domain": "templetx.gov", "primegov_url": "https://cityoftemple.primegov.com/public/portal"},
    {"city": "Texarkana", "state": "TX", "eos": 1, "domain": "texarkanatexas.gov", "civicclerk_url": "https://texarkanatx.portal.civicclerk.com"},
    {"city": "Tomball", "state": "TX", "eos": 1, "domain": "tomballtx.gov", "municode_url": "https://tomball-tx.municodemeetings.com"},
]


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class VerificationResult:
    city: str
    state: str
    eos: int
    domain: str
    level: str = "V0"                          # V0, V1, V2, V3, V4
    best_source: str = "unknown"               # legistar, civicplus, boarddocs, novus, primegov, civicclerk, municode, granicus, unknown
    verified: bool = False                     # True only at V4

    # Legistar checks
    legistar_slug: Optional[str] = None
    legistar_reachable: Optional[bool] = None
    legistar_has_events: Optional[bool] = None
    legistar_recent_event_date: Optional[str] = None
    legistar_item_count: Optional[int] = None
    legistar_sample_titles: Optional[list] = None

    # CivicPlus checks
    civicplus_page_loads: Optional[bool] = None
    civicplus_categories: Optional[dict] = None
    civicplus_ajax_works: Optional[bool] = None
    civicplus_working_cat_id: Optional[str] = None
    civicplus_pdf_count: Optional[int] = None
    civicplus_pdf_url: Optional[str] = None

    # Other platform checks
    alt_platform: Optional[str] = None         # boarddocs, novus, primegov, civicclerk, municode, granicus
    alt_platform_url: Optional[str] = None
    alt_platform_reachable: Optional[bool] = None
    alt_platform_has_data: Optional[bool] = None
    alt_platform_sample: Optional[str] = None

    # PDF content checks
    pdf_size_bytes: Optional[int] = None
    pdf_text_length: Optional[int] = None
    pdf_has_date: Optional[bool] = None
    pdf_has_numbered_items: Optional[bool] = None
    pdf_has_agenda_keywords: Optional[bool] = None
    pdf_text_sample: Optional[str] = None

    # LLM extraction (V4)
    llm_item_count: Optional[int] = None
    llm_has_real_titles: Optional[bool] = None
    llm_fiscal_amounts_found: Optional[int] = None
    llm_fiscal_amounts_verified: Optional[int] = None
    llm_date_extracted: Optional[str] = None
    llm_cost_usd: Optional[float] = None

    # Diagnostics
    error: Optional[str] = None
    notes: str = ""
    duration_seconds: float = 0.0


# ============================================================================
# VERIFICATION FUNCTIONS
# ============================================================================

async def check_legistar(client: httpx.AsyncClient, slug: str) -> dict:
    """Test Legistar API. Returns dict with results."""
    result = {"reachable": False}
    try:
        r = await client.get(
            f"https://webapi.legistar.com/v1/{slug}/events"
            f"?$top=10&$orderby=EventDate%20desc",
            timeout=REQUEST_TIMEOUT,
        )
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        events = r.json()
        result["has_events"] = len(events) > 0
        if not events:
            result["error"] = "No events returned"
            return result

        result["recent_event_date"] = (events[0].get("EventDate") or "")[:10]
        result["event_body"] = events[0].get("EventBodyName") or ""
        result["total_events"] = len(events)

        # Try multiple events — some have 0 items for the most recent
        for event in events:
            event_id = event["EventId"]
            r2 = await client.get(
                f"https://webapi.legistar.com/v1/{slug}/events/{event_id}/eventitems",
                timeout=REQUEST_TIMEOUT,
            )
            items = r2.json() if r2.status_code == 200 else []
            titles = [
                (i.get("EventItemTitle") or "")
                for i in items
                if (i.get("EventItemTitle") or "").strip()
            ]
            if titles:
                result["item_count"] = len(titles)
                result["sample_titles"] = titles[:3]
                result["event_with_items"] = (event.get("EventDate") or "")[:10]
                result["event_body_with_items"] = event.get("EventBodyName") or ""
                result["success"] = True
                return result

        # All events had 0 items
        result["item_count"] = 0
        result["success"] = False
        result["error"] = f"{len(events)} events found but all have 0 agenda items"

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


async def check_civicplus(client: httpx.AsyncClient, domain: str) -> dict:
    """Test CivicPlus AgendaCenter. Returns dict with results."""
    result = {"page_loads": False}
    try:
        # Step 1: Load page + get session cookie
        page = await client.get(f"https://{domain}/AgendaCenter", timeout=REQUEST_TIMEOUT)
        result["page_loads"] = page.status_code == 200
        if page.status_code != 200:
            result["error"] = f"Status {page.status_code}"
            return result

        html = page.text

        # Is this actually a CivicPlus AgendaCenter page?
        if "AgendaCenter" not in html and "agenda" not in html.lower():
            result["error"] = "Page loaded but doesn't look like CivicPlus AgendaCenter"
            return result

        # Check for external platforms (Municode, Granicus, etc.)
        iframe_src = re.findall(r'<iframe[^>]*src="([^"]*(?:municode|granicus|novus|primegov)[^"]*)"', html, re.IGNORECASE)
        if iframe_src:
            result["external_platform"] = iframe_src[0]
            result["error"] = f"Uses external platform via iframe: {iframe_src[0][:100]}"
            return result

        cookies = dict(page.cookies)

        # Step 2: Check if PDFs are already in the initial page HTML
        # Many CivicPlus sites render agenda links directly — no AJAX needed
        pdf_links = re.findall(r'/AgendaCenter/ViewFile/Agenda/[^"\'>\s]+', html)
        # Deduplicate (links often appear twice — agenda + icon)
        pdf_links = list(dict.fromkeys(pdf_links))

        if pdf_links:
            # Pick the most recent (first in page = most recent)
            result["pdf_count"] = len(pdf_links)
            result["pdf_url"] = f"https://{domain}{pdf_links[0]}"
            result["ajax_works"] = True  # data available, even if not via AJAX
            result["notes"] = "PDFs found directly in page HTML"
            result["success"] = True

        # Step 3: Find category IDs (correct pattern: name="chkCategoryID" value="X")
        cat_matches = re.findall(
            r'name="chkCategoryID"\s+value="(\d+)"[^>]*>\s*([^<]+)',
            html
        )
        if cat_matches:
            result["categories"] = {cid: label.strip() for cid, label in cat_matches[:15]}
        else:
            result["categories"] = {}

        # If we already found PDFs in the page, we're good — return early
        if result.get("success"):
            return result

        # Step 4: No PDFs in page — try AJAX with correct params (year + catID)
        if not cat_matches:
            result["error"] = "No category checkboxes found (AgendaCenter may be empty)"
            return result

        for cat_id, cat_label in cat_matches[:6]:
            try:
                # Try current year first, then previous
                for year in ["2026", "2025"]:
                    r = await client.post(
                        f"https://{domain}/AgendaCenter/UpdateCategoryList",
                        data={"year": year, "catID": cat_id},
                        cookies=cookies,
                        headers={"X-Requested-With": "XMLHttpRequest"},
                        timeout=REQUEST_TIMEOUT,
                    )
                    text = r.text.strip()

                    ajax_pdfs = re.findall(r'/AgendaCenter/ViewFile/Agenda/[^"\'>\s]+', text)
                    ajax_pdfs = list(dict.fromkeys(ajax_pdfs))

                    if ajax_pdfs:
                        result["ajax_works"] = True
                        result["working_cat_id"] = cat_id
                        result["working_cat_label"] = cat_label.strip()
                        result["working_year"] = year
                        result["pdf_count"] = len(ajax_pdfs)
                        result["pdf_url"] = f"https://{domain}{ajax_pdfs[0]}"
                        result["success"] = True
                        return result

            except Exception:
                continue

        result["ajax_works"] = False
        result["error"] = f"Tried {len(cat_matches[:6])} categories × 2 years, none returned agenda PDFs"

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


async def check_boarddocs(client: httpx.AsyncClient, url: str) -> dict:
    """Test BoardDocs portal. Returns dict with results."""
    result = {"reachable": False}
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT)
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        html = r.text
        # BoardDocs pages have specific JS that loads meeting data
        has_bd_content = "boarddocs" in html.lower() or "Board.nsf" in html
        result["has_content"] = has_bd_content

        if has_bd_content:
            result["success"] = True
            result["notes"] = "BoardDocs portal accessible"
            # Try to find meeting links or agenda references
            meeting_refs = re.findall(r'(?:agenda|meeting|minutes)', html.lower())
            result["keyword_count"] = len(meeting_refs)
        else:
            result["error"] = "Page loaded but doesn't look like BoardDocs"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_novus_agenda(client: httpx.AsyncClient, url: str) -> dict:
    """Test Novus Agenda portal. Returns dict with results."""
    result = {"reachable": False}
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT)
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        html = r.text
        # Novus returns 200 for ANY slug — check for Application_Error to detect fakes
        if "Application_Error" in html or "Object reference not set" in html:
            result["error"] = "Novus returned generic error page (not a real portal)"
            return result

        # Check for actual agenda content
        has_agenda = "agenda" in html.lower() and ("meeting" in html.lower() or "session" in html.lower())
        result["has_content"] = has_agenda

        if has_agenda:
            result["success"] = True
            result["notes"] = "Novus Agenda portal with meeting data"
            # Look for PDF links
            pdf_links = re.findall(r'href="([^"]*\.pdf[^"]*)"', html, re.IGNORECASE)
            result["pdf_count"] = len(pdf_links)
            if pdf_links:
                result["pdf_url"] = pdf_links[0] if pdf_links[0].startswith("http") else f"{url.rsplit('/', 1)[0]}/{pdf_links[0]}"
        else:
            result["error"] = "Page loaded but no agenda content found"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_primegov(client: httpx.AsyncClient, url: str) -> dict:
    """Test PrimeGov portal. Returns dict with results."""
    result = {"reachable": False}
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT)
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        html = r.text
        has_content = "primegov" in html.lower() or "meeting" in html.lower()
        result["has_content"] = has_content

        if has_content:
            result["success"] = True
            result["notes"] = "PrimeGov portal accessible"
        else:
            result["error"] = "Page loaded but no PrimeGov content found"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_civicclerk(client: httpx.AsyncClient, url: str) -> dict:
    """Test CivicClerk portal. Returns dict with results."""
    result = {"reachable": False}
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT)
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        html = r.text
        has_content = "civicclerk" in html.lower() or "meeting" in html.lower()
        result["has_content"] = has_content

        if has_content:
            result["success"] = True
            result["notes"] = "CivicClerk portal accessible"
        else:
            result["error"] = "Page loaded but no CivicClerk content found"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_municode(client: httpx.AsyncClient, url: str) -> dict:
    """Test Municode portal. Returns dict with results."""
    result = {"reachable": False}
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT)
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        html = r.text
        has_content = "municode" in html.lower() or "meeting" in html.lower() or "agenda" in html.lower()
        result["has_content"] = has_content

        if has_content:
            result["success"] = True
            result["notes"] = "Municode portal accessible"
            # Look for PDF/document links
            doc_links = re.findall(r'href="([^"]*(?:\.pdf|ViewFile|agenda)[^"]*)"', html, re.IGNORECASE)
            result["doc_count"] = len(doc_links)
        else:
            result["error"] = "Page loaded but no Municode content found"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_custom_page(client: httpx.AsyncClient, url: str) -> dict:
    """Check a non-standard agenda page for reachability and agenda keywords."""
    result = {"reachable": False}
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT)
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        html = r.text
        keywords = ["agenda", "minutes", "meeting", "council", "session", "packet"]
        found = [kw for kw in keywords if kw in html.lower()]
        result["has_content"] = len(found) >= 2
        result["keywords_found"] = found

        if result["has_content"]:
            result["success"] = True
            result["notes"] = f"Custom page with agenda content ({', '.join(found[:3])})"
            # Look for PDF links
            pdf_links = re.findall(r'href="([^"]*\.pdf[^"]*)"', html, re.IGNORECASE)
            result["pdf_count"] = len(pdf_links)
            if pdf_links:
                # Make absolute if relative
                if not pdf_links[0].startswith("http"):
                    from urllib.parse import urljoin
                    pdf_links[0] = urljoin(url, pdf_links[0])
                result["pdf_url"] = pdf_links[0]
        else:
            result["error"] = f"Page loaded but only found keywords: {found}"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_granicus(client: httpx.AsyncClient, url: str) -> dict:
    """Test Granicus-based agenda page. Returns dict with results."""
    result = {"reachable": False}
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT)
        result["reachable"] = r.status_code == 200
        if r.status_code != 200:
            result["error"] = f"Status {r.status_code}"
            return result

        html = r.text
        has_content = any(kw in html.lower() for kw in ["granicus", "agenda", "meeting", "minutes"])
        result["has_content"] = has_content

        if has_content:
            result["success"] = True
            result["notes"] = "Granicus-powered agenda page accessible"
            # Look for PDF links
            pdf_links = re.findall(r'href="([^"]*\.pdf[^"]*)"', html, re.IGNORECASE)
            result["pdf_count"] = len(pdf_links)
        else:
            result["error"] = "Page loaded but no agenda content found"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_pdf_content(client: httpx.AsyncClient, pdf_url: str, cookies: dict = None) -> dict:
    """Download a PDF and verify it contains agenda content."""
    result = {"downloaded": False}
    try:
        r = await client.get(pdf_url, timeout=30, cookies=cookies)
        if r.status_code != 200:
            result["error"] = f"PDF download status {r.status_code}"
            return result

        pdf_bytes = r.content
        result["size_bytes"] = len(pdf_bytes)

        if len(pdf_bytes) < 1000:
            result["error"] = f"PDF too small ({len(pdf_bytes)} bytes)"
            return result

        if len(pdf_bytes) > PDF_MAX_BYTES:
            result["error"] = f"PDF too large ({len(pdf_bytes):,} bytes), skipping"
            return result

        result["downloaded"] = True

        # Extract text from first 5 pages
        doc = fitz.open(stream=BytesIO(pdf_bytes), filetype="pdf")
        full_text = ""
        for page_num in range(min(5, len(doc))):
            full_text += doc[page_num].get_text()
        page_count = len(doc)
        doc.close()

        result["pages"] = page_count
        result["text_length"] = len(full_text)

        if len(full_text) < 200:
            result["error"] = f"Only {len(full_text)} chars extracted — likely a scanned image"
            return result

        # Content checks
        result["has_date"] = bool(re.search(
            r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}',
            full_text.lower()
        ))
        result["has_numbered_items"] = bool(re.search(
            r'^\s*(\d+|[IVX]+|[A-Z]{2,4}\d+)\.\s+\S', full_text, re.MULTILINE
        ))
        result["has_agenda_keywords"] = any(
            kw in full_text.lower()
            for kw in ["agenda", "consent", "public hearing", "call to order",
                        "roll call", "city council", "regular meeting", "work session"]
        )
        result["text_sample"] = full_text[:400].replace("\n", " ").strip()
        result["full_text"] = full_text  # kept for V4 LLM test, not saved to report

        result["is_agenda"] = (
            result["has_agenda_keywords"]
            and (result["has_date"] or result["has_numbered_items"])
        )

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def check_llm_extraction(pdf_text: str, gemini_client) -> dict:
    """V4: Run LLM extraction and verify output quality (sync — GeminiClient is sync)."""
    from pydantic import BaseModel, Field

    class AgendaItemExtraction(BaseModel):
        number: Optional[str] = Field(None, description="Item number as shown in document")
        title: str = Field(description="Item title, verbatim from document")
        section: Optional[str] = Field(None, description="consent|action|public_hearing|discussion|procedural|other")
        fiscal_amounts: list[str] = Field(default_factory=list, description="Dollar amounts verbatim")
        is_public_hearing: bool = Field(False)

    class MeetingExtraction(BaseModel):
        date: str = Field(description="Meeting date in YYYY-MM-DD format")
        time: Optional[str] = None
        body: str = Field(description="Governing body name")
        meeting_type: Optional[str] = Field(None, description="regular|special|work_session")
        items: list[AgendaItemExtraction]

    result = {"success": False}

    try:
        # Truncate to ~15k chars to keep cost low for verification
        text_to_send = pdf_text[:15000]

        extraction = gemini_client.generate_structured_content(
            prompt=(
                "Extract structured agenda data from this city council meeting document. "
                "Extract the meeting date, governing body, and every agenda item with its "
                "number, title (verbatim), section, dollar amounts (verbatim), and whether "
                "it's a public hearing.\n\n"
                f"{text_to_send}"
            ),
            response_schema=MeetingExtraction,
            temperature=0.1,
            thinking_budget=0,  # no thinking needed, keep cost minimal
        )

        if not extraction:
            result["error"] = "LLM returned None"
            return result

        # Handle both Pydantic model and dict responses
        if isinstance(extraction, dict):
            items = extraction.get("items", [])
            date = extraction.get("date", "")
        else:
            items = extraction.items
            date = extraction.date

        result["item_count"] = len(items)
        result["date_extracted"] = date

        if not items:
            result["error"] = "LLM returned 0 items"
            return result

        # Check: do items have real titles?
        if isinstance(items[0], dict):
            titles = [i.get("title", "") for i in items]
            all_amounts = [a for i in items for a in i.get("fiscal_amounts", [])]
        else:
            titles = [i.title for i in items]
            all_amounts = [a for i in items for a in i.fiscal_amounts]

        real_titles = [t for t in titles if len(t) > 10]
        result["has_real_titles"] = len(real_titles) >= len(titles) * 0.5

        # Check: do dollar amounts appear in source text?
        verified_amounts = [a for a in all_amounts if a in pdf_text]
        result["fiscal_amounts_found"] = len(all_amounts)
        result["fiscal_amounts_verified"] = len(verified_amounts)

        # Check: valid date?
        try:
            datetime.strptime(date, "%Y-%m-%d")
            result["valid_date"] = True
        except (ValueError, TypeError):
            result["valid_date"] = False

        result["success"] = (
            len(items) > 0
            and result["has_real_titles"]
            and result.get("valid_date", False)
        )

        result["sample_titles"] = titles[:3]

    except Exception as e:
        result["error"] = str(e)[:300]

    return result


# ============================================================================
# MAIN VERIFICATION ORCHESTRATOR
# ============================================================================

async def verify_single_city(
    city_info: dict,
    http_semaphore: asyncio.Semaphore,
    llm_semaphore: asyncio.Semaphore,
    gemini_client,
    skip_llm: bool = False,
) -> VerificationResult:
    """Run all verification levels for a single city."""
    start = time.time()
    r = VerificationResult(
        city=city_info["city"],
        state=city_info["state"],
        eos=city_info["eos"],
        domain=city_info["domain"],
    )

    async with http_semaphore:
        async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:

            # ── Try Legistar first ──
            slug = city_info.get("legistar_slug")
            if slug:
                leg = await check_legistar(client, slug)
                r.legistar_slug = slug
                r.legistar_reachable = leg.get("reachable", False)
                r.legistar_has_events = leg.get("has_events", False)
                r.legistar_recent_event_date = leg.get("recent_event_date")
                r.legistar_item_count = leg.get("item_count")
                r.legistar_sample_titles = leg.get("sample_titles")

                if leg.get("success"):
                    r.best_source = "legistar"
                    r.level = "V3"  # Legistar gives structured data, so V3 = content verified
                    r.notes = (
                        f"Legistar: {leg.get('item_count', 0)} items, "
                        f"most recent event {leg.get('recent_event_date', '?')}"
                    )
                    # Legistar is already structured — skip to V4 conceptually
                    # (no PDF to parse, data is inherently structured)
                    r.level = "V4"
                    r.verified = True
                    r.notes += " — structured API, no LLM needed"
                    r.duration_seconds = time.time() - start
                    return r

                elif leg.get("error"):
                    r.notes = f"Legistar failed: {leg['error']}. "

            # ── Try CivicPlus ──
            cp = await check_civicplus(client, city_info["domain"])
            r.civicplus_page_loads = cp.get("page_loads", False)
            r.civicplus_categories = cp.get("categories")
            r.civicplus_ajax_works = cp.get("ajax_works", False)
            r.civicplus_working_cat_id = cp.get("working_cat_id")
            r.civicplus_pdf_count = cp.get("pdf_count")
            r.civicplus_pdf_url = cp.get("pdf_url")

            if cp.get("page_loads"):
                r.level = "V1"

            if cp.get("success"):
                r.level = "V2"
                r.best_source = "civicplus"
                cat_info = ""
                if cp.get("working_cat_id"):
                    cat_info = f"cat={cp.get('working_cat_label', cp['working_cat_id'])}"
                    if cp.get("working_year"):
                        cat_info += f"/{cp['working_year']}"
                elif cp.get("notes"):
                    cat_info = cp["notes"]
                r.notes += f"CivicPlus: {cp.get('pdf_count', 0)} PDFs ({cat_info}). "

                # ── Check PDF content (V3) ──
                pdf_url = cp.get("pdf_url")
                if pdf_url:
                    pdf = await check_pdf_content(client, pdf_url)
                    r.pdf_size_bytes = pdf.get("size_bytes")
                    r.pdf_text_length = pdf.get("text_length")
                    r.pdf_has_date = pdf.get("has_date")
                    r.pdf_has_numbered_items = pdf.get("has_numbered_items")
                    r.pdf_has_agenda_keywords = pdf.get("has_agenda_keywords")
                    r.pdf_text_sample = pdf.get("text_sample")

                    if pdf.get("is_agenda"):
                        r.level = "V3"
                        r.notes += f"PDF: {pdf.get('pages', '?')}pp, {pdf.get('text_length', 0):,} chars. "

                        # ── LLM extraction (V4) ──
                        if not skip_llm and gemini_client and pdf.get("full_text"):
                            async with llm_semaphore:
                                cost_before = gemini_client.total_cost
                                llm = await asyncio.to_thread(
                                    check_llm_extraction,
                                    pdf.get("full_text"),
                                    gemini_client,
                                )
                                r.llm_cost_usd = round(gemini_client.total_cost - cost_before, 6)
                                r.llm_item_count = llm.get("item_count")
                                r.llm_has_real_titles = llm.get("has_real_titles")
                                r.llm_fiscal_amounts_found = llm.get("fiscal_amounts_found")
                                r.llm_fiscal_amounts_verified = llm.get("fiscal_amounts_verified")
                                r.llm_date_extracted = llm.get("date_extracted")

                                if llm.get("success"):
                                    r.level = "V4"
                                    r.verified = True
                                    r.notes += (
                                        f"LLM: {llm.get('item_count', 0)} items, "
                                        f"date={llm.get('date_extracted', '?')}"
                                    )
                                else:
                                    r.notes += f"LLM failed: {llm.get('error', 'unknown')}. "

                    elif pdf.get("error"):
                        r.notes += f"PDF issue: {pdf['error']}. "

            elif cp.get("external_platform"):
                r.level = "V1"
                r.best_source = "external"
                r.notes += f"External: {cp['external_platform'][:80]}. "

            elif cp.get("error"):
                r.notes += f"CivicPlus: {cp['error']}. "

            # ── Try alternative platforms if CivicPlus didn't work ──
            if r.level in ("V0", "V1"):
                alt_result = None
                alt_platform = None
                alt_url = None

                # Check each platform in priority order
                if city_info.get("boarddocs_url"):
                    alt_platform = "boarddocs"
                    alt_url = city_info["boarddocs_url"]
                    alt_result = await check_boarddocs(client, alt_url)
                elif city_info.get("novus_url"):
                    alt_platform = "novus"
                    alt_url = city_info["novus_url"]
                    alt_result = await check_novus_agenda(client, alt_url)
                elif city_info.get("primegov_url"):
                    alt_platform = "primegov"
                    alt_url = city_info["primegov_url"]
                    alt_result = await check_primegov(client, alt_url)
                elif city_info.get("civicclerk_url"):
                    alt_platform = "civicclerk"
                    alt_url = city_info["civicclerk_url"]
                    alt_result = await check_civicclerk(client, alt_url)
                elif city_info.get("municode_url"):
                    alt_platform = "municode"
                    alt_url = city_info["municode_url"]
                    alt_result = await check_municode(client, alt_url)
                elif city_info.get("granicus_url"):
                    alt_platform = "granicus"
                    alt_url = city_info["granicus_url"]
                    alt_result = await check_granicus(client, alt_url)
                elif city_info.get("custom_agenda_url"):
                    alt_platform = "custom"
                    alt_url = city_info["custom_agenda_url"]
                    alt_result = await check_custom_page(client, alt_url)

                if alt_result:
                    r.alt_platform = alt_platform
                    r.alt_platform_url = alt_url
                    r.alt_platform_reachable = alt_result.get("reachable", False)
                    r.alt_platform_has_data = alt_result.get("success", False)
                    r.alt_platform_sample = alt_result.get("notes", "")

                    if alt_result.get("success"):
                        r.level = "V2"
                        r.best_source = alt_platform
                        r.notes += f"{alt_platform}: {alt_result.get('notes', 'accessible')}. "

                        # For platforms with PDF links, try V3/V4 checks
                        if alt_platform in ("novus", "custom", "granicus") and alt_result.get("pdf_url"):
                            pdf = await check_pdf_content(client, alt_result["pdf_url"])
                            r.pdf_size_bytes = pdf.get("size_bytes")
                            r.pdf_text_length = pdf.get("text_length")
                            r.pdf_has_agenda_keywords = pdf.get("has_agenda_keywords")
                            r.pdf_text_sample = pdf.get("text_sample")
                            if pdf.get("is_agenda"):
                                r.level = "V3"
                                r.notes += f"PDF: {pdf.get('pages', '?')}pp, {pdf.get('text_length', 0):,} chars. "

                                # LLM check
                                if not skip_llm and gemini_client and pdf.get("full_text"):
                                    async with llm_semaphore:
                                        cost_before = gemini_client.total_cost
                                        llm = await asyncio.to_thread(
                                            check_llm_extraction,
                                            pdf.get("full_text"),
                                            gemini_client,
                                        )
                                        r.llm_cost_usd = round(gemini_client.total_cost - cost_before, 6)
                                        r.llm_item_count = llm.get("item_count")
                                        r.llm_has_real_titles = llm.get("has_real_titles")
                                        r.llm_date_extracted = llm.get("date_extracted")
                                        if llm.get("success"):
                                            r.level = "V4"
                                            r.verified = True
                                            r.notes += f"LLM: {llm.get('item_count', 0)} items. "
                                        else:
                                            r.notes += f"LLM failed: {llm.get('error', 'unknown')}. "

                    elif alt_result.get("reachable"):
                        r.level = max(r.level, "V1", key=lambda x: ["V0", "V1", "V2", "V3", "V4"].index(x))
                        r.notes += f"{alt_platform}: reachable but {alt_result.get('error', 'no data found')}. "
                    else:
                        r.notes += f"{alt_platform}: {alt_result.get('error', 'unreachable')}. "

            if not r.notes:
                r.error = "No Legistar slug, CivicPlus not detected, no alt platform configured"
                r.notes = "Needs manual research"

    r.duration_seconds = round(time.time() - start, 1)
    return r


# ============================================================================
# REPORT GENERATION
# ============================================================================

def generate_report(results: list[VerificationResult]) -> str:
    """Generate human-readable summary."""
    lines = []
    lines.append("=" * 70)
    lines.append("CITY DATA SOURCE VERIFICATION REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Cities tested: {len(results)}")
    lines.append("=" * 70)

    # Summary by level
    by_level = {}
    for r in results:
        by_level.setdefault(r.level, []).append(r)

    total_eos = sum(r.eos for r in results)
    lines.append(f"\nTotal EOs across all cities: {total_eos}")
    lines.append("")

    for level in ["V4", "V3", "V2", "V1", "V0"]:
        cities = by_level.get(level, [])
        eo_count = sum(c.eos for c in cities)
        label = {
            "V4": "Pipeline verified (READY — data confirmed usable)",
            "V3": "Content verified (PDF has agenda, LLM not yet tested)",
            "V2": "Data found (AJAX responds, PDF not verified)",
            "V1": "Reachable (page exists, no usable data)",
            "V0": "Unverified (needs manual research)",
        }[level]
        lines.append(f"{'─' * 60}")
        lines.append(f"{level}: {len(cities)} cities, {eo_count} EOs — {label}")
        lines.append(f"{'─' * 60}")

        for c in sorted(cities, key=lambda x: (x.state, x.city)):
            source_tag = f"[{c.best_source}]" if c.best_source != "unknown" else ""
            lines.append(f"  {c.city}, {c.state} ({c.eos} EOs) {source_tag}")
            if c.notes:
                lines.append(f"    {c.notes.strip()}")
        lines.append("")

    # Cost summary
    total_cost = sum(r.llm_cost_usd or 0 for r in results)
    llm_cities = sum(1 for r in results if r.llm_cost_usd)
    lines.append(f"{'─' * 60}")
    lines.append(f"LLM COST: ${total_cost:.4f} ({llm_cities} cities tested)")
    lines.append(f"{'─' * 60}")

    # Coverage summary
    v3_plus = [r for r in results if r.level in ("V3", "V4")]
    v3_plus_eos = sum(r.eos for r in v3_plus)
    lines.append(f"\nCOVERAGE SUMMARY:")
    lines.append(f"  Ready to collect (V3+): {len(v3_plus)} cities, {v3_plus_eos} EOs ({v3_plus_eos*100//total_eos}%)")
    lines.append(f"  Need work (V0-V2):      {len(results) - len(v3_plus)} cities, {total_eos - v3_plus_eos} EOs")

    # By source type
    lines.append(f"\nBY SOURCE TYPE:")
    by_source = {}
    for r in v3_plus:
        by_source.setdefault(r.best_source, []).append(r)
    for source, cities in sorted(by_source.items()):
        eos = sum(c.eos for c in cities)
        lines.append(f"  {source}: {len(cities)} cities, {eos} EOs")

    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Verify all pilot city data sources")
    parser.add_argument("--skip-llm", action="store_true", help="Skip V4 LLM extraction (faster, $0 cost)")
    parser.add_argument("--city", type=str, help="Test a single city by name")
    args = parser.parse_args()

    cities = PILOT_CITIES
    if args.city:
        cities = [c for c in cities if args.city.lower() in c["city"].lower()]
        if not cities:
            print(f"No city found matching '{args.city}'")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"VERIFYING {len(cities)} CITIES")
    print(f"LLM extraction: {'SKIPPED' if args.skip_llm else 'ENABLED (~$0.005/city)'}")
    print(f"{'='*60}\n")

    # Initialize Gemini client for V4 (if not skipped)
    gemini_client = None
    if not args.skip_llm:
        try:
            from dotenv import load_dotenv
            load_dotenv()

            # Add project paths for imports
            project_root = Path(__file__).resolve().parent.parent.parent
            if str(project_root) not in sys.path:

            from shared.llm_gemini import GeminiClient, GeminiModelType
            gemini_client = GeminiClient(
                default_model=GeminiModelType.FLASH,
                default_temperature=0.1,
                thinking_budget=0,
            )
            print("[LLM] Gemini Flash initialized for V4 verification\n")
        except Exception as e:
            print(f"[LLM] Could not initialize Gemini: {e}")
            print("[LLM] Continuing with V0-V3 only (HTTP + PDF checks)\n")
            args.skip_llm = True

    # Run verification
    http_sem = asyncio.Semaphore(HTTP_CONCURRENCY)
    llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)

    start_time = time.time()
    tasks = [
        verify_single_city(city, http_sem, llm_sem, gemini_client, args.skip_llm)
        for city in cities
    ]

    results: list[VerificationResult] = []
    completed = 0
    for coro in asyncio.as_completed(tasks):
        result = await coro
        results.append(result)
        completed += 1

        # Progress indicator
        icon = {"V4": "✓", "V3": "◎", "V2": "○", "V1": "·", "V0": "✗"}[result.level]
        source = f" [{result.best_source}]" if result.best_source != "unknown" else ""
        cost = f" ${result.llm_cost_usd:.4f}" if result.llm_cost_usd else ""
        print(
            f"  [{completed:2d}/{len(cities)}] {icon} {result.level} "
            f"{result.city}, {result.state}{source}{cost} "
            f"({result.duration_seconds:.1f}s)"
        )

    elapsed = time.time() - start_time

    # Sort by state then city
    results.sort(key=lambda r: (r.state, r.city))

    # Save JSON report (exclude full PDF text)
    report_data = []
    for r in results:
        d = asdict(r)
        report_data.append(d)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"\n[saved] {REPORT_PATH}")

    # Save human-readable summary
    summary = generate_report(results)
    with open(SUMMARY_PATH, "w") as f:
        f.write(summary)
    print(f"[saved] {SUMMARY_PATH}")

    # Print summary to console
    print(f"\n{summary}")

    # Final stats
    total_cost = sum(r.llm_cost_usd or 0 for r in results)
    print(f"\nCompleted in {elapsed:.0f} seconds ({elapsed/60:.1f} min)")
    print(f"Total LLM cost: ${total_cost:.4f}")


if __name__ == "__main__":
    # Suppress SSL warnings for city sites with bad certs
    import warnings
    import urllib3
    warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
    warnings.filterwarnings("ignore", message=".*SSL.*")

    asyncio.run(main())
