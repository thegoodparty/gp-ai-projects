"""
generic_html_scraper.py — Scrape agenda PDFs from custom municipal HTML pages.

Handles 20+ Tier 3 cities that each have a unique HTML layout but share common
patterns: PDF links near "agenda" keywords, dates in link text or table cells.

Strategies:
  - direct_pdf:       Find <a href="...pdf"> links on the page
  - document_center:  CivicPlus /DocumentCenter/View/{id} links (serve PDFs)
  - archive_aspx:     CivicPlus Archive.aspx?ADID={id} links (serve PDFs)
  - link_click:       DNN /LinkClick.aspx?fileticket={encoded} links
  - rss_feed:         Parse RSS feed for PDF links (Salisbury NC)

Usage:
    from collectors.generic_html_scraper import GenericScraperConfig, collect_generic

    config = GenericScraperConfig(
        url="https://cityofdublin.org/government/council-meetings-and-agendas",
        city_name="Dublin",
        strategy="direct_pdf",
        output_prefix="meeting_pipeline/sources/dublin-OH/data/generic",
        storage=storage_backend,
    )
    result = await collect_generic(config)
"""

import asyncio
import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from meeting_pipeline.collection_agent.storage import StorageBackend


# ============================================================================
# CONFIG AND RESULT
# ============================================================================

@dataclass
class GenericScraperConfig:
    """Configuration for generic HTML scraping."""
    url: str                   # Main page URL to scrape
    city_name: str
    output_prefix: str         # Storage key prefix
    storage: StorageBackend
    strategy: str = "direct_pdf"   # direct_pdf | document_center | archive_aspx | link_click | rss_feed
    selector: str | None = None    # Optional CSS selector override
    keyword_filter: str = "agenda" # Keyword to filter links (agenda, packet, etc.)
    lookback_days: int = 90
    download_pdfs: bool = True
    request_timeout: int = 30
    rate_limit_delay: float = 0.5
    follow_url: str | None = None  # Optional second URL to follow (e.g. Agenda Center sub-page)
    verify_ssl: bool = True        # Set False for cities with self-signed/expired SSL certs


@dataclass
class ScrapedMeeting:
    """A single meeting found by the generic scraper."""
    date: str               # YYYY-MM-DD
    title: str
    pdf_url: str
    source_url: str
    pdf_filename: str | None = None


@dataclass
class GenericScraperResult:
    """Summary of scraping results."""
    meetings_found: int = 0
    pdfs_downloaded: int = 0
    output_prefix: str = ""


# ============================================================================
# DATE PARSING
# ============================================================================

# Common date patterns found across Tier 3 city pages
DATE_PATTERNS = [
    # MM/DD/YYYY or M/D/YYYY
    (re.compile(r'(\d{1,2})/(\d{1,2})/(\d{4})'), lambda m: f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"),
    # MM-DD-YYYY or MM-DD-YY
    (re.compile(r'(\d{1,2})-(\d{1,2})-(\d{2,4})'), lambda m: _fix_year(m.group(3)) + f"-{int(m.group(1)):02d}-{int(m.group(2)):02d}"),
    # YYYY-MM-DD
    (re.compile(r'(\d{4})-(\d{2})-(\d{2})'), lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}"),
    # Month DD, YYYY (e.g. "March 17, 2026")
    (re.compile(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s*(\d{4})', re.IGNORECASE),
     lambda m: f"{m.group(3)}-{_month_num(m.group(1)):02d}-{int(m.group(2)):02d}"),
    # Mon DD, YYYY (e.g. "Mar 17, 2026")
    (re.compile(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s*(\d{4})', re.IGNORECASE),
     lambda m: f"{m.group(3)}-{_month_num(m.group(1)):02d}-{int(m.group(2)):02d}"),
    # MM/DD/YY
    (re.compile(r'(\d{1,2})/(\d{1,2})/(\d{2})(?!\d)'), lambda m: f"{_fix_year(m.group(3))}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"),
]

MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _month_num(name: str) -> int:
    return MONTH_MAP.get(name.lower(), 1)


def _fix_year(yr: str) -> str:
    if len(yr) == 2:
        y = int(yr)
        return str(2000 + y) if y < 50 else str(1900 + y)
    return yr


def extract_date(text: str) -> str | None:
    """Extract the first date found in text using common patterns."""
    for pattern, formatter in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return formatter(match)
            except (ValueError, IndexError):
                continue
    return None


def extract_date_from_filename(url: str) -> str | None:
    """Try to extract a date from a PDF filename/URL path."""
    # Get the filename part
    path = urlparse(url).path
    filename = path.split("/")[-1]
    return extract_date(filename)


# ============================================================================
# LINK EXTRACTION STRATEGIES
# ============================================================================

def _is_agenda_link(href: str, text: str, keyword: str) -> bool:
    """Check if a link is likely an agenda (not minutes, not video)."""
    lower_text = text.lower()
    lower_href = href.lower()
    combined = lower_text + " " + lower_href

    # Must contain the keyword
    if keyword and keyword.lower() not in combined:
        # Also accept if it's just a .pdf with a date in the filename
        if not lower_href.endswith(".pdf"):
            return False

    # Exclude minutes-only links (but allow "agenda and minutes" combo links)
    if "minute" in lower_text and keyword.lower() not in lower_text:
        return False

    # Exclude video/audio links
    if any(kw in lower_text for kw in ["video", "audio", "youtube", "stream"]):
        return False

    return True


def _get_surrounding_text(tag, max_chars: int = 500) -> str:
    """Get text from parent/sibling elements for date extraction."""
    texts = []

    # Own text
    texts.append(tag.get_text(strip=True))

    # Parent row/cell text
    parent_tr = tag.find_parent("tr")
    if parent_tr:
        texts.append(parent_tr.get_text(" ", strip=True))

    # Parent list item
    parent_li = tag.find_parent("li")
    if parent_li:
        texts.append(parent_li.get_text(" ", strip=True))

    # Parent div (first level)
    parent_div = tag.find_parent("div")
    if parent_div:
        texts.append(parent_div.get_text(" ", strip=True)[:max_chars])

    # Previous sibling heading
    prev = tag.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"])
    if prev:
        texts.append(prev.get_text(strip=True))

    return " ".join(texts)


async def extract_direct_pdf(
    soup: BeautifulSoup,
    base_url: str,
    keyword: str,
    selector: str | None = None,
) -> list[ScrapedMeeting]:
    """Strategy: find <a href="...pdf"> links directly on the page."""
    meetings = []

    if selector:
        links = soup.select(selector)
    else:
        # Find all links that point to PDFs or known document patterns
        links = soup.find_all("a", href=True)

    for link in links:
        href = link.get("href", "")
        text = link.get_text(strip=True)

        # Check if it's a PDF link (strip query params for extension check)
        href_path = urlparse(href).path.lower()
        is_pdf = (
            href_path.endswith(".pdf")
            or "/documentcenter/view/" in href_path
            or "/viewfile/" in href_path
            or "fileticket=" in href.lower()
        )
        if not is_pdf:
            continue

        if not _is_agenda_link(href, text, keyword):
            continue

        # Build absolute URL (encode spaces for Revize CMS URLs)
        pdf_url = urljoin(base_url, href.replace(" ", "%20"))

        # Extract date from surrounding context
        surrounding = _get_surrounding_text(link)
        date = extract_date(surrounding)

        # Fallback: try the URL/filename
        if not date:
            date = extract_date_from_filename(href)

        # Fallback: try link text
        if not date:
            date = extract_date(text)

        if not date:
            # Can't determine date — still include with unknown date
            date = "unknown"

        title = text or f"Agenda {date}"

        meetings.append(ScrapedMeeting(
            date=date,
            title=title,
            pdf_url=pdf_url,
            source_url=base_url,
        ))

    return meetings


async def extract_document_center(
    soup: BeautifulSoup,
    base_url: str,
    keyword: str,
    selector: str | None = None,
) -> list[ScrapedMeeting]:
    """Strategy: CivicPlus /DocumentCenter/View/{id} links."""
    meetings = []

    links = soup.find_all("a", href=re.compile(r"/DocumentCenter/View/\d+"))

    for link in links:
        href = link.get("href", "")
        text = link.get_text(strip=True)

        if not _is_agenda_link(href, text, keyword):
            continue

        pdf_url = urljoin(base_url, href)

        surrounding = _get_surrounding_text(link)
        date = extract_date(surrounding)
        if not date:
            date = extract_date(text)
        if not date:
            date = "unknown"

        title = text or f"Agenda {date}"

        meetings.append(ScrapedMeeting(
            date=date,
            title=title,
            pdf_url=pdf_url,
            source_url=base_url,
        ))

    return meetings


async def extract_archive_aspx(
    client: httpx.AsyncClient,
    soup: BeautifulSoup,
    base_url: str,
    keyword: str,
    selector: str | None = None,
) -> list[ScrapedMeeting]:
    """Strategy: CivicPlus Archive.aspx?ADID={id} links that serve PDFs."""
    meetings = []

    # Look for links with ADID parameter
    links = soup.find_all("a", href=re.compile(r"Archive\.aspx\?ADID=\d+|ADID=\d+"))

    for link in links:
        href = link.get("href", "")
        text = link.get_text(strip=True)

        if not _is_agenda_link(href, text, keyword):
            continue

        pdf_url = urljoin(base_url, href)

        surrounding = _get_surrounding_text(link)
        date = extract_date(surrounding)
        if not date:
            date = extract_date(text)
        if not date:
            date = "unknown"

        title = text or f"Agenda {date}"

        meetings.append(ScrapedMeeting(
            date=date,
            title=title,
            pdf_url=pdf_url,
            source_url=base_url,
        ))

    return meetings


async def extract_two_hop(
    client: httpx.AsyncClient,
    soup: BeautifulSoup,
    base_url: str,
    keyword: str,
    selector: str | None = None,
    rate_limit_delay: float = 0.5,
) -> list[ScrapedMeeting]:
    """Strategy: index page has links to subpages, subpages have PDF links.

    Used for Drupal sites like Cuyahoga Falls where the index lists
    intermediate pages (e.g. /files/city-council-schedules-agendas-legislation-2026-03-23)
    and each subpage contains the actual PDF links.
    """
    meetings = []

    # Step 1: Find subpage links on the index page
    if selector:
        subpage_links = soup.select(selector)
    else:
        subpage_links = soup.find_all("a", href=True)

    subpage_urls = []
    for link in subpage_links:
        href = link.get("href", "")
        text = link.get_text(strip=True)
        if keyword and keyword.lower() not in (href + " " + text).lower():
            continue
        full_url = urljoin(base_url, href)
        if full_url not in subpage_urls:
            subpage_urls.append(full_url)

    print(f"  Two-hop: found {len(subpage_urls)} subpages to check")

    # Step 2: Visit each subpage and extract PDF links
    for i, sub_url in enumerate(subpage_urls[:30]):  # Cap at 30 subpages
        try:
            await asyncio.sleep(rate_limit_delay)
            resp = await client.get(sub_url, follow_redirects=True)
            resp.raise_for_status()
            sub_soup = BeautifulSoup(resp.text, "html.parser")

            # Find PDF links on subpage
            for pdf_link in sub_soup.find_all("a", href=True):
                pdf_href = pdf_link.get("href", "")
                pdf_path = urlparse(pdf_href).path.lower()
                if not pdf_path.endswith(".pdf"):
                    continue

                pdf_text = pdf_link.get_text(strip=True)
                pdf_url = urljoin(sub_url, pdf_href.replace(" ", "%20"))

                # Extract date from subpage URL or content
                date = extract_date(sub_url)
                if not date:
                    surrounding = _get_surrounding_text(pdf_link)
                    date = extract_date(surrounding)
                if not date:
                    date = extract_date_from_filename(pdf_href)
                if not date:
                    date = "unknown"

                title = pdf_text or f"Agenda {date}"

                meetings.append(ScrapedMeeting(
                    date=date,
                    title=title,
                    pdf_url=pdf_url,
                    source_url=sub_url,
                ))

        except Exception as e:
            print(f"    WARNING: Failed to fetch subpage {sub_url}: {e}")
            continue

        if (i + 1) % 10 == 0:
            print(f"    Processed {i + 1}/{len(subpage_urls)} subpages...")

    return meetings


async def extract_rss_feed(
    client: httpx.AsyncClient,
    url: str,
    keyword: str,
) -> list[ScrapedMeeting]:
    """Strategy: parse an RSS feed for PDF links."""
    import xml.etree.ElementTree as ET

    meetings = []

    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()

    root = ET.fromstring(response.text)

    for item in root.iter("item"):
        title_elem = item.find("title")
        link_elem = item.find("link")
        desc_elem = item.find("description")

        title = title_elem.text if title_elem is not None else ""
        link = link_elem.text if link_elem is not None else ""
        desc = desc_elem.text if desc_elem is not None else ""

        # Look for PDF links in description
        pdf_urls = re.findall(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', desc)
        if not pdf_urls and link and link.lower().endswith(".pdf"):
            pdf_urls = [link]

        if not pdf_urls:
            continue

        date = extract_date(title or "")
        if not date and desc:
            date = extract_date(desc)
        if not date:
            date = "unknown"

        for pdf_url in pdf_urls:
            if keyword and keyword.lower() not in (title + pdf_url).lower():
                continue

            meetings.append(ScrapedMeeting(
                date=date,
                title=title or f"Agenda {date}",
                pdf_url=pdf_url,
                source_url=url,
            ))

    return meetings


# ============================================================================
# PDF DOWNLOAD
# ============================================================================

async def download_pdf(
    client: httpx.AsyncClient,
    url: str,
    dest_key: str,
    storage: StorageBackend,
) -> bool:
    """Download a PDF file and save via storage backend. Returns True if successful."""
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()

        content = response.content

        # Skip tiny stub/redirect PDFs
        if len(content) < 1000:
            return False

        # Verify it looks like a PDF
        if not content[:5].startswith(b"%PDF") and len(content) < 10000:
            # Small non-PDF response — probably an error page
            return False

        storage.write_bytes(dest_key, content)
        return True

    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        print(f"    WARNING: Failed to download {url}: {e}")
        return False


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

async def collect_generic(config: GenericScraperConfig) -> GenericScraperResult:
    """
    Collect meeting data from a generic HTML page.

    1. Fetch the page
    2. Extract PDF links using the configured strategy
    3. Filter by date (last N days)
    4. Download PDFs
    """
    print(f"Collecting {config.city_name} from {config.url}")
    print(f"  Strategy: {config.strategy}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    async with httpx.AsyncClient(
        timeout=config.request_timeout,
        follow_redirects=True,
        headers=headers,
        verify=config.verify_ssl,
    ) as client:

        meetings: list[ScrapedMeeting] = []

        if config.strategy == "rss_feed":
            # RSS feeds don't need HTML parsing
            print("  Fetching RSS feed...")
            meetings = await extract_rss_feed(client, config.url, config.keyword_filter)
        else:
            # Fetch the HTML page
            print("  Fetching page...")
            response = await client.get(config.url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            # Optionally follow a second URL (e.g. Agenda Center sub-page)
            if config.follow_url:
                print(f"  Following link: {config.follow_url}")
                await asyncio.sleep(config.rate_limit_delay)
                response2 = await client.get(config.follow_url)
                response2.raise_for_status()
                soup = BeautifulSoup(response2.text, "html.parser")

            # Extract meetings based on strategy
            if config.strategy == "direct_pdf":
                meetings = await extract_direct_pdf(soup, config.url, config.keyword_filter, config.selector)
            elif config.strategy == "document_center":
                meetings = await extract_document_center(soup, config.url, config.keyword_filter, config.selector)
            elif config.strategy == "archive_aspx":
                meetings = await extract_archive_aspx(client, soup, config.url, config.keyword_filter, config.selector)
            elif config.strategy == "link_click":
                # LinkClick.aspx URLs are direct downloads — treat like direct PDF
                meetings = await extract_direct_pdf(soup, config.url, config.keyword_filter, config.selector)
            elif config.strategy == "two_hop":
                meetings = await extract_two_hop(client, soup, config.url, config.keyword_filter, config.selector, config.rate_limit_delay)
            else:
                print(f"  ERROR: Unknown strategy '{config.strategy}'")
                return GenericScraperResult(output_prefix=config.output_prefix)

        print(f"  Found {len(meetings)} potential agenda links")

        # Deduplicate by PDF URL
        seen_urls = set()
        unique_meetings = []
        for m in meetings:
            if m.pdf_url not in seen_urls:
                seen_urls.add(m.pdf_url)
                unique_meetings.append(m)
        meetings = unique_meetings

        if len(meetings) != len(seen_urls):
            print(f"  After dedup: {len(meetings)} unique meetings")

        # Filter by date
        cutoff = (datetime.now() - timedelta(days=config.lookback_days)).strftime("%Y-%m-%d")
        dated = [m for m in meetings if m.date != "unknown" and m.date >= cutoff]
        undated = [m for m in meetings if m.date == "unknown"]

        # Keep dated meetings within range + undated ones (might be recent)
        meetings = dated + undated
        print(f"  After date filter (>= {cutoff}): {len(dated)} dated + {len(undated)} undated = {len(meetings)}")

        # Save meeting metadata
        meetings_data = [
            {
                "date": m.date,
                "title": m.title,
                "pdfUrl": m.pdf_url,
                "sourceUrl": m.source_url,
            }
            for m in meetings
        ]
        config.storage.write_json(f"{config.output_prefix}/meetings.json", meetings_data)

        # Download PDFs
        pdf_count = 0
        if config.download_pdfs and meetings:
            print(f"  Downloading {len(meetings)} PDFs...")
            for i, m in enumerate(meetings):
                safe_date = m.date.replace("-", "")
                # Create a safe filename from the URL
                url_path = urlparse(m.pdf_url).path
                url_filename = url_path.split("/")[-1]
                if not url_filename or len(url_filename) > 100:
                    url_filename = f"agenda_{i}.pdf"
                if not url_filename.lower().endswith(".pdf"):
                    url_filename += ".pdf"

                filename = f"{safe_date}_{url_filename}"
                # Sanitize filename
                filename = re.sub(r'[^\w\-_\. ]', '_', filename)
                dest_key = f"{config.output_prefix}/pdfs/{filename}"

                if config.storage.exists(dest_key):
                    pdf_count += 1
                    continue

                await asyncio.sleep(config.rate_limit_delay)
                if await download_pdf(client, m.pdf_url, dest_key, config.storage):
                    size = config.storage.get_size(dest_key)
                    print(f"    Downloaded: {filename} ({size // 1024}KB)")
                    pdf_count += 1

    # Summary
    print()
    print(f"Collection complete: {config.city_name}")
    print(f"  Meetings found: {len(meetings)}")
    print(f"  PDFs downloaded: {pdf_count}")
    print(f"  Output: {config.output_prefix}")

    return GenericScraperResult(
        meetings_found=len(meetings),
        pdfs_downloaded=pdf_count,
        output_prefix=config.output_prefix,
    )


