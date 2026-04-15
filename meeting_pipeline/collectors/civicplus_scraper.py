"""
civicplus_scraper.py — Scrape CivicPlus AgendaCenter for meeting agendas.

CivicPlus is a SaaS platform used by ~35 pilot cities. All cities use identical
HTML structure, JS codebase, and AJAX endpoints. One scraper covers all of them.

Flow:
  1. GET /AgendaCenter → session cookie + category discovery
  2. POST /AgendaCenter/UpdateCategoryList → meeting list HTML
  3. Parse HTML → extract meeting metadata + PDF URLs
  4. Download agenda PDFs

Usage:
    from collectors.civicplus_scraper import CivicPlusConfig, collect_civicplus

    config = CivicPlusConfig(
        domain="durhamnc.gov",
        city_name="Durham",
        council_category_id=4,
        output_prefix="data/civicplus",
        storage=storage_backend,
    )
    result = await collect_civicplus(config)

See docs/investigation-learnings/civicplus-scraper-findings.md for full research.
"""

import asyncio
import re
from datetime import datetime
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from meeting_pipeline.collection_agent.storage import StorageBackend


# ============================================================================
# CONFIG AND RESULT
# ============================================================================

@dataclass
class CivicPlusConfig:
    """Configuration for CivicPlus AgendaCenter scraping."""
    domain: str            # e.g. "durhamnc.gov" (www. prefix added automatically)
    city_name: str
    output_prefix: str
    storage: StorageBackend
    council_category_id: int | None = None  # If None, auto-discovered
    years: list[int] | None = None  # Default: current year
    download_pdfs: bool = True
    request_timeout: int = 30
    rate_limit_delay: float = 0.5
    expected_body: str = ""  # e.g. "City Council" — used to override category discovery


@dataclass
class CivicPlusMeeting:
    """A single meeting extracted from CivicPlus."""
    date: str              # YYYY-MM-DD
    title: str             # "December 16, 2025 Work Session Agenda"
    category_name: str     # "City Council"
    agenda_pdf_url: str | None = None
    packet_url: str | None = None
    minutes_url: str | None = None
    agenda_id: str | None = None     # e.g. "_12162025-3246"
    posted_date: str | None = None


@dataclass
class CivicPlusResult:
    """Summary of collected CivicPlus data."""
    meetings_found: int = 0
    pdfs_downloaded: int = 0
    categories_found: list[dict] = field(default_factory=list)
    output_prefix: str = ""


# ============================================================================
# HELPERS
# ============================================================================

def _ensure_www(domain: str) -> str:
    """CivicPlus AJAX requires www. prefix — POST body is lost on 301 redirect."""
    if not domain.startswith("www."):
        return f"www.{domain}"
    return domain


def _parse_meeting_date(date_text: str) -> str | None:
    """Parse date from CivicPlus aria-label like 'Agenda for December 16, 2025'."""
    # Remove "Agenda for " prefix
    date_text = re.sub(r"^Agenda for\s+", "", date_text, flags=re.IGNORECASE)

    # Try common formats
    for fmt in ["%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"]:
        try:
            return datetime.strptime(date_text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try to find a date in the text
    match = re.search(r"(\w+ \d{1,2},?\s*\d{4})", date_text)
    if match:
        for fmt in ["%B %d, %Y", "%B %d %Y", "%b %d, %Y"]:
            try:
                return datetime.strptime(match.group(1), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

    return None


def _extract_agenda_id(url: str) -> str | None:
    """Extract agenda ID from URL like /AgendaCenter/ViewFile/Agenda/_12162025-3246."""
    match = re.search(r"/ViewFile/\w+/([\w_-]+)", url)
    return match.group(1) if match else None


# ============================================================================
# CATEGORY DISCOVERY
# ============================================================================

async def discover_categories(client: httpx.AsyncClient, domain: str) -> list[dict]:
    """
    Discover available categories from the AgendaCenter main page.

    Returns list of {id: int, name: str} dicts.
    """
    url = f"https://{_ensure_www(domain)}/AgendaCenter"
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    categories = []

    # Category checkboxes: <input type="checkbox" id="4" name="chkCategoryID" value="4">
    for checkbox in soup.find_all("input", {"name": "chkCategoryID"}):
        cat_id = int(checkbox.get("value", 0))
        if cat_id <= 0:
            continue

        # Try to find the label for this checkbox
        label = checkbox.find_next("label")
        name = label.get_text(strip=True) if label else f"Category {cat_id}"

        categories.append({"id": cat_id, "name": name})

    return categories


async def find_council_category(client: httpx.AsyncClient, domain: str) -> tuple[int, str]:
    """
    Auto-discover the City Council category ID.

    Strategy: first try checkbox labels. Then verify against AJAX response aria-labels,
    because some CivicPlus sites have mismatched checkbox labels vs actual category content.
    """
    categories = await discover_categories(client, domain)

    if not categories:
        raise ValueError(f"No categories found on {domain}/AgendaCenter")

    # Priority-ordered exact council name patterns
    exact_matches = ["city council", "town council", "board of aldermen", "village council"]
    www_domain = _ensure_www(domain)

    # Try to verify categories via AJAX response aria-labels
    # This is more reliable than checkbox labels which can be mismatched
    verified_categories = []

    for cat in categories:
        try:
            url = f"https://{www_domain}/AgendaCenter/UpdateCategoryList"
            response = await client.post(
                url,
                data={"year": str(datetime.now().year), "catID": str(cat["id"])},
                headers={"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code == 404:
                break  # AJAX not available, fall back to checkbox labels

            soup = BeautifulSoup(response.text, "html.parser")
            year_link = soup.find("a", attrs={"aria-label": True})
            if year_link:
                label = year_link.get("aria-label", "")
                actual_name = re.sub(r"\s*\d{4}\s*$", "", label).strip()
                verified_categories.append({"id": cat["id"], "name": actual_name})
        except Exception:
            break

    # Use verified names if available, otherwise checkbox labels
    search_cats = verified_categories if verified_categories else categories

    # Pass 1: exact match on common council names
    for pattern in exact_matches:
        for cat in search_cats:
            if cat["name"].lower().strip() == pattern:
                return cat["id"], cat["name"]

    # Pass 2: "city council" anywhere in name
    for cat in search_cats:
        if "city council" in cat["name"].lower():
            return cat["id"], cat["name"]

    # Pass 3: "council" but NOT in advisory/commission/committee
    exclude = ["advisory", "commission", "committee", "youth", "senior", "arts", "animal"]
    for cat in search_cats:
        lower = cat["name"].lower()
        if "council" in lower and not any(kw in lower for kw in exclude):
            return cat["id"], cat["name"]

    # Last resort: return first category
    print(f"  WARNING: No 'council' category found. Available: {[c['name'] for c in search_cats]}")
    print(f"  Using first category: {search_cats[0]['name']} (id={search_cats[0]['id']})")
    return search_cats[0]["id"], search_cats[0]["name"]


# ============================================================================
# YEAR DISCOVERY
# ============================================================================

async def fetch_available_years(
    client: httpx.AsyncClient,
    domain: str,
    category_id: int,
) -> list[int]:
    """
    Discover which years have data for a given category.

    Parses the year links from the AJAX response.
    """
    www_domain = _ensure_www(domain)
    url = f"https://{www_domain}/AgendaCenter/UpdateCategoryList"

    response = await client.post(
        url,
        data={"year": str(datetime.now().year), "catID": str(category_id)},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    years = set()

    for link in soup.find_all("a", attrs={"aria-label": True}):
        label = link.get("aria-label", "")
        match = re.search(r"(\d{4})", label)
        if match:
            years.add(int(match.group(1)))

    current_li = soup.find("li", class_="current")
    if current_li:
        link = current_li.find("a")
        if link:
            label = link.get("aria-label", link.get_text())
            match = re.search(r"(\d{4})", label)
            if match:
                years.add(int(match.group(1)))

    return sorted(years, reverse=True)


# ============================================================================
# MEETING LIST FETCHING
# ============================================================================

async def fetch_meeting_list(
    client: httpx.AsyncClient,
    domain: str,
    category_id: int,
    year: int,
) -> list[CivicPlusMeeting]:
    """
    Fetch the meeting list for a given category and year.

    POSTs to /AgendaCenter/UpdateCategoryList and parses the HTML response.
    """
    www_domain = _ensure_www(domain)
    url = f"https://{www_domain}/AgendaCenter/UpdateCategoryList"

    response = await client.post(
        url,
        data={"year": str(year), "catID": str(category_id)},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Extract category name from aria-label
    category_name = "Unknown"
    year_link = soup.find("a", attrs={"aria-label": True})
    if year_link:
        label = year_link.get("aria-label", "")
        # "City Council 2025" -> "City Council"
        category_name = re.sub(r"\s*\d{4}\s*$", "", label).strip()

    # Parse meeting rows
    meetings = []
    for row in soup.find_all("tr", class_="catAgendaRow"):
        meeting = _parse_meeting_row(row, category_name, domain)
        if meeting:
            meetings.append(meeting)

    return meetings


def _parse_meeting_row(row, category_name: str, domain: str) -> CivicPlusMeeting | None:
    """Parse a single meeting row from the CivicPlus HTML table."""

    # Date from strong > aria-label
    date_elem = row.find("strong", attrs={"aria-label": True})
    if not date_elem:
        # Fallback: look for any strong tag
        date_elem = row.find("strong")

    date_iso = None
    if date_elem:
        date_text = date_elem.get("aria-label", date_elem.get_text(strip=True))
        date_iso = _parse_meeting_date(date_text)

    if not date_iso:
        # Fallback for main-page layout: parse date from first cell text
        # Format: "Feb25, 2026City Council" or "Mar5, 2025Charter Review Commission"
        first_cell = row.find("td")
        if first_cell:
            cell_text = first_cell.get_text(strip=True)
            match = re.match(r"(\w{3,9})\s?(\d{1,2}),?\s*(\d{4})", cell_text)
            if match:
                month_str, day, year = match.group(1), match.group(2), match.group(3)
                for fmt in ["%B", "%b"]:
                    try:
                        month = datetime.strptime(month_str, fmt).month
                        date_iso = f"{year}-{month:02d}-{int(day):02d}"
                        break
                    except ValueError:
                        continue

    if not date_iso:
        return None

    # Title from the agenda link
    title_link = row.find("a", href=re.compile(r"ViewFile"))
    title = title_link.get_text(strip=True) if title_link else f"{category_name} Meeting"

    # PDF URLs from download menu
    agenda_pdf_url = None
    packet_url = None
    minutes_url = None
    agenda_id = None

    # Look for PDF link (not HTML, not packet)
    for link in row.find_all("a", href=re.compile(r"ViewFile/Agenda")):
        href = link.get("href", "")
        if "packet=true" in href:
            packet_url = f"https://{_ensure_www(domain)}{href}" if href.startswith("/") else href
        elif "html=true" in href:
            continue  # Skip HTML version
        else:
            agenda_pdf_url = f"https://{_ensure_www(domain)}{href}" if href.startswith("/") else href
            agenda_id = _extract_agenda_id(href)

    # Minutes link
    minutes_link = row.find("a", href=re.compile(r"ViewFile/Minutes"))
    if minutes_link:
        href = minutes_link.get("href", "")
        minutes_url = f"https://{_ensure_www(domain)}{href}" if href.startswith("/") else href

    # Posted date
    posted_date = None
    posted_match = re.search(r"Posted\s+(.+?)(?:\s*$|<)", row.get_text())
    if posted_match:
        posted_date = posted_match.group(1).strip()

    return CivicPlusMeeting(
        date=date_iso,
        title=title,
        category_name=category_name,
        agenda_pdf_url=agenda_pdf_url,
        packet_url=packet_url,
        minutes_url=minutes_url,
        agenda_id=agenda_id,
        posted_date=posted_date,
    )


# ============================================================================
# MAIN PAGE FALLBACK (older CivicPlus versions that don't have AJAX)
# ============================================================================

async def find_council_category_from_panels(
    client: httpx.AsyncClient,
    domain: str,
) -> tuple[int, str]:
    """
    Find the council category by parsing h2 headers on the main page.

    Older CivicPlus sites have all meetings on the main page grouped under
    h2 headers with aria-controls='category-panel-{id}'. The h2 text contains
    the category name (which may differ from checkbox labels).
    """
    url = f"https://{_ensure_www(domain)}/AgendaCenter"
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    council_keywords = ["city council", "town council", "board of aldermen", "village council", "council"]

    for h2 in soup.select("h2[aria-controls]"):
        panel_id = h2.get("aria-controls", "")
        cat_name = h2.get_text(strip=True)

        # Extract category ID from panel ID (category-panel-{id})
        match = re.search(r"category-panel-(\d+)", panel_id)
        if not match:
            continue
        cat_id = int(match.group(1))

        if any(kw in cat_name.lower() for kw in council_keywords):
            return cat_id, cat_name

    raise ValueError(f"No council category found in main page panels for {domain}")


async def fetch_meetings_from_main_page(
    client: httpx.AsyncClient,
    domain: str,
    category_id: int,
) -> tuple[list[CivicPlusMeeting], str]:
    """
    Fallback: scrape meetings from the main page HTML for older CivicPlus sites.

    Returns (meetings, category_name).
    """
    url = f"https://{_ensure_www(domain)}/AgendaCenter"
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Find the category panel
    panel = soup.select_one(f"#category-panel-{category_id}")
    if not panel:
        return [], "Unknown"

    # Get category name from the h2 that controls this panel
    h2 = soup.select_one(f'h2[aria-controls="category-panel-{category_id}"]')
    category_name = h2.get_text(strip=True) if h2 else "City Council"

    # Parse all meeting rows within this panel
    meetings = []
    for row in panel.select("tr.catAgendaRow"):
        meeting = _parse_meeting_row(row, category_name, domain)
        if meeting:
            meetings.append(meeting)

    return meetings, category_name


# ============================================================================
# PDF DOWNLOAD
# ============================================================================

async def download_pdf(
    client: httpx.AsyncClient,
    url: str,
    key: str,
    storage: StorageBackend,
) -> bool:
    """Download a PDF file. Returns True if successful, False if failed or stub."""
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()

        content = response.content

        # Check for stub PDFs (some cities return a tiny redirect PDF)
        if len(content) < 5000:
            text = content.decode("latin-1", errors="ignore").lower()
            if "leaving" in text or "redirect" in text:
                print(f"    Stub/redirect PDF detected ({len(content)} bytes), skipping")
                return False

        storage.write_bytes(key, content)
        return True

    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        print(f"    WARNING: Failed to download PDF: {e}")
        return False


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

async def collect_civicplus(config: CivicPlusConfig) -> CivicPlusResult:
    """
    Collect meeting data from a CivicPlus AgendaCenter.

    1. Establishes session cookie
    2. Discovers or uses provided category ID
    3. Fetches meeting list for target year(s)
    4. Downloads agenda PDFs
    """
    www_domain = _ensure_www(config.domain)

    print(f"Collecting {config.city_name} CivicPlus data from {www_domain}/AgendaCenter")

    async with httpx.AsyncClient(timeout=config.request_timeout, follow_redirects=True) as client:

        # Step 1: GET main page to establish session cookie
        print("  1. Establishing session cookie...")
        session_response = await client.get(f"https://{www_domain}/AgendaCenter")
        if session_response.status_code != 200:
            print(f"    WARNING: Got status {session_response.status_code} from main page")

        # Step 2: Discover or use provided category ID
        if config.council_category_id is not None:
            cat_id = config.council_category_id
            cat_name = "City Council"
            print(f"  2. Using provided category ID: {cat_id}")
        else:
            print("  2. Auto-discovering council category...")
            # If expected_body is set, try matching it against available categories first
            if config.expected_body:
                raw_categories = await discover_categories(client, config.domain)
                expected_lower = config.expected_body.lower()
                matched = [c for c in raw_categories if expected_lower in c["name"].lower()]
                if matched:
                    cat_id = matched[0]["id"]
                    cat_name = matched[0]["name"]
                    print(f"     [body filter] Matched expected_body={config.expected_body!r} → {cat_name} (id={cat_id})")
                else:
                    print(f"     [body filter] No category matched {config.expected_body!r}, falling back to find_council_category()")
                    cat_id, cat_name = await find_council_category(client, config.domain)
                    print(f"     Found: {cat_name} (id={cat_id})")
            else:
                cat_id, cat_name = await find_council_category(client, config.domain)
                print(f"     Found: {cat_name} (id={cat_id})")

        # Step 3: Discover all categories (for reference)
        categories = await discover_categories(client, config.domain)
        config.storage.write_json(
            f"{config.output_prefix}/categories.json",
            {"categories": categories, "council_category_id": cat_id},
        )
        print(f"  3. Found {len(categories)} categories total")

        # Step 4: Try AJAX endpoint first, fallback to main page parsing
        all_meetings: list[CivicPlusMeeting] = []
        use_fallback = False

        # Test if AJAX endpoint works
        try:
            test_url = f"https://{www_domain}/AgendaCenter/UpdateCategoryList"
            test_response = await client.post(
                test_url,
                data={"year": str(datetime.now().year), "catID": str(cat_id)},
                headers={"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded"},
            )
            if test_response.status_code == 404:
                use_fallback = True
        except Exception:
            use_fallback = True

        if use_fallback:
            print("  4. AJAX endpoint not available — using main page fallback")
            # Re-discover category from h2 headers (checkbox labels may not match)
            try:
                cat_id, cat_name = await find_council_category_from_panels(client, config.domain)
                print(f"     Found panel: {cat_name} (id={cat_id})")
            except ValueError:
                print(f"     WARNING: Could not find council panel, using checkbox-discovered id={cat_id}")

            meetings, cat_name = await fetch_meetings_from_main_page(client, config.domain, cat_id)
            print(f"     Found {len(meetings)} meetings from main page")
            all_meetings.extend(meetings)
        else:
            # Standard AJAX flow
            if config.years:
                years = config.years
            else:
                current_year = datetime.now().year
                available_years = await fetch_available_years(client, config.domain, cat_id)
                if current_year in available_years:
                    years = [current_year]
                elif available_years:
                    years = [available_years[0]]
                    print(f"  4. WARNING: No {current_year} data. Most recent year: {years[0]}")
                else:
                    years = [current_year]
                print(f"  4. Available years: {available_years or 'none found'}, collecting: {years}")

            for year in years:
                print(f"     Fetching meetings for {year}...")
                await asyncio.sleep(config.rate_limit_delay)
                meetings = await fetch_meeting_list(client, config.domain, cat_id, year)
                print(f"     Found {len(meetings)} meetings")
                all_meetings.extend(meetings)

        # Filter: keep upcoming meetings + last 90 days only
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        before_filter = len(all_meetings)
        all_meetings = [m for m in all_meetings if m.date >= cutoff]
        if len(all_meetings) < before_filter:
            print(f"     Filtered to {len(all_meetings)} meetings (keeping >= {cutoff}, removed {before_filter - len(all_meetings)} older)")

        # Save meeting metadata
        meetings_data = [
            {
                "date": m.date,
                "title": m.title,
                "categoryName": m.category_name,
                "agendaPdfUrl": m.agenda_pdf_url,
                "packetUrl": m.packet_url,
                "minutesUrl": m.minutes_url,
                "agendaId": m.agenda_id,
                "postedDate": m.posted_date,
            }
            for m in all_meetings
        ]
        config.storage.write_json(f"{config.output_prefix}/meetings.json", meetings_data)

        # Step 5: Download PDFs — prefer packet over agenda-only
        pdf_count = 0
        if config.download_pdfs:
            print(f"  5. Downloading {len(all_meetings)} agenda PDFs...")
            for m in all_meetings:
                if not m.agenda_pdf_url and not m.packet_url:
                    continue

                safe_date = m.date.replace("-", "")
                agenda_id = m.agenda_id or 'unknown'

                # Download packet if available (has full staff reports)
                if m.packet_url:
                    packet_filename = f"agenda_{safe_date}_{agenda_id}_packet.pdf"
                    packet_key = f"{config.output_prefix}/pdfs/{packet_filename}"
                    if config.storage.exists(packet_key):
                        print(f"    Skipping {packet_filename} (already exists)")
                        pdf_count += 1
                    else:
                        await asyncio.sleep(config.rate_limit_delay)
                        if await download_pdf(client, m.packet_url, packet_key, config.storage):
                            print(f"    Downloaded: {packet_filename}")
                            pdf_count += 1

                # Also download agenda PDF if no packet (or as fallback)
                if m.agenda_pdf_url and not m.packet_url:
                    filename = f"agenda_{safe_date}_{agenda_id}.pdf"
                    pdf_key = f"{config.output_prefix}/pdfs/{filename}"
                    if config.storage.exists(pdf_key):
                        print(f"    Skipping {filename} (already exists)")
                        pdf_count += 1
                        continue
                    await asyncio.sleep(config.rate_limit_delay)
                    if await download_pdf(client, m.agenda_pdf_url, pdf_key, config.storage):
                        print(f"    Downloaded: {filename}")
                        pdf_count += 1

    # Summary
    print()
    print(f"Collection complete: {config.city_name}")
    print(f"  Meetings: {len(all_meetings)}")
    print(f"  PDFs downloaded: {pdf_count}")
    print(f"  Saved to: {config.output_prefix}")

    return CivicPlusResult(
        meetings_found=len(all_meetings),
        pdfs_downloaded=pdf_count,
        categories_found=categories,
        output_prefix=config.output_prefix,
    )
