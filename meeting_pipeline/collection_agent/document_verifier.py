"""
document_verifier.py — Validate downloaded documents are real agendas.

Accepts raw bytes (not a file path) so it works with S3 objects too.
All checks are heuristic — returns (is_valid, reason) tuple.

Checks:
  1. Size > 5 KB
  2. PDF header (%PDF-) or HTML document
  3. Agenda keyword in first-page text
  4. Date within lookback window (if date provided)
  5. Body name hint present in text (if multi-word hint provided)

Also provides verify_url() for lightweight URL probing (used by reason.py
to validate LLM-identified links before saving them as events).

Special handling:
  - Google Drive / Docs / Dropbox / OneDrive URLs: accepted on date check alone
    (document content not directly fetchable without auth)
  - Known platform agenda viewers (Legistar, CivicClerk, eSCRIBE, Diligent,
    BoardDocs, Granicus): accepted if they return 200 and have a valid date
"""

import asyncio
import re
from datetime import datetime, timedelta


def _is_valid_date(date_str: str) -> bool:
    """Return True if date_str is a parseable YYYY-MM-DD date."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


AGENDA_KEYWORDS = [
    "agenda",
    "city council",
    "town council",
    "meeting",
    "board of aldermen",
    "village council",
    "city commission",
    "board of trustees",
    "village council",
    "trustees",
]

MIN_SIZE_BYTES = 5120  # 5 KB

# Document hosting platforms where content isn't directly fetchable but the URL
# is a valid reference to an agenda document. Accept on date check alone.
_DOCUMENT_HOSTING_DOMAINS = {
    "drive.google.com",
    "docs.google.com",
    "dropbox.com",
    "onedrive.live.com",
    "sharepoint.com",
    "1drv.ms",
    "box.com",
}

# Known agenda platform viewer URLs — these are HTML pages that display agenda
# content but may not contain agenda keywords in the first 8KB fetch. Accept
# if they return 200 and have a valid date.
_KNOWN_PLATFORM_PATTERNS = [
    r"legistar\.com",
    r"civicclerk\.com",
    r"civicplus\.com",
    r"escribemeetings\.com",
    r"diligent\.community",
    r"boarddocs\.com",
    r"granicus\.com",
    r"swagit\.com",
    r"novusagenda\.com",
    r"municode\.com",
    r"primegov\.com",
    r"destinyhosted\.com",
    r"haystaq\.com",
    r"civicweb\.net",
]

_KNOWN_PLATFORM_RE = re.compile("|".join(_KNOWN_PLATFORM_PATTERNS), re.IGNORECASE)


def _is_document_hosting_url(url: str) -> bool:
    """Return True if URL is a known document hosting platform (Drive, Dropbox, etc)."""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return any(d in domain for d in _DOCUMENT_HOSTING_DOMAINS)
    except Exception:
        return False


def _is_known_platform_url(url: str) -> bool:
    """Return True if URL is a known civic agenda platform viewer."""
    return bool(_KNOWN_PLATFORM_RE.search(url))


def verify_document(
    content: bytes,
    expected_date: str | None = None,
    lookback_days: int = 90,
    body_name_hint: str | None = None,
) -> tuple[bool, str]:
    """
    Validate that content is a real agenda document.

    Args:
        content:        Raw bytes of the downloaded file
        expected_date:  YYYY-MM-DD string, used for date-range check
        lookback_days:  How many days back (and 180 forward) to accept
        body_name_hint: Optional governing body name (e.g. "City Council")

    Returns:
        (True, "OK") if valid, (False, reason_string) if not.
    """
    # 1. Size check
    if len(content) < MIN_SIZE_BYTES:
        return False, f"Too small ({len(content)} bytes, minimum {MIN_SIZE_BYTES})"

    # 2. Document type check
    is_pdf = content[:5].startswith(b"%PDF-")
    is_html = (
        b"<html" in content[:500].lower()
        or b"<!doctype" in content[:500].lower()
    )

    if not is_pdf and not is_html:
        return False, "Not a PDF or HTML document"

    # 3. Extract text for keyword checks
    text = _extract_text(content, is_pdf)

    # 4. Keyword check (skip if text extraction failed — don't reject on uncertainty)
    if text:
        text_lower = text.lower()
        has_keyword = any(kw in text_lower for kw in AGENDA_KEYWORDS)
        if not has_keyword:
            return False, "No agenda keywords found in document text"
    # else: text extraction failed silently — accept document

    # 5. Date range check
    if expected_date and expected_date != "unknown":
        try:
            dt = datetime.strptime(expected_date, "%Y-%m-%d")
            cutoff = datetime.now() - timedelta(days=lookback_days)
            future_limit = datetime.now() + timedelta(days=180)
            if dt < cutoff:
                return False, f"Date {expected_date} is before lookback cutoff"
            if dt > future_limit:
                return False, f"Date {expected_date} is more than 180 days in the future"
        except ValueError:
            pass  # Unparseable date — don't reject

    # 6. Body name hint check (only for multi-word hints)
    if body_name_hint and text:
        words = [w for w in body_name_hint.lower().split() if len(w) > 3]
        if words and not any(w in text.lower() for w in words):
            return False, f"Body hint '{body_name_hint}' not found in document"

    return True, "OK"


def _extract_text(content: bytes, is_pdf: bool) -> str:
    """
    Extract text for keyword analysis.

    PDF: uses PyMuPDF (first page only — fast).
    HTML: decode as UTF-8.
    Returns empty string on any error.
    """
    try:
        if is_pdf:
            return _pdf_first_page_text(content)
        else:
            return content.decode("utf-8", errors="ignore")[:8000]
    except Exception:
        return ""


def _pdf_first_page_text(content: bytes) -> str:
    """Extract text from the first page of a PDF using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ""

    try:
        doc = fitz.open(stream=content, filetype="pdf")
        if len(doc) == 0:
            return ""
        text = doc[0].get_text()
        doc.close()
        return text
    except Exception:
        return ""


# ── URL-based verification (lightweight, no full download) ────────────────────

async def verify_url(
    url: str,
    expected_date: str | None = None,
    lookback_days: int = 90,
    body_name_hint: str | None = None,
    read_bytes: int = 8192,
    timeout: int = 15,
) -> tuple[bool, str]:
    """
    Verify a URL points to a real agenda document by fetching its first bytes.

    Special cases:
    - Document hosting platforms (Google Drive, Dropbox, etc): accepted on date
      check alone — content not directly fetchable without auth.
    - Known civic platform viewers (Legistar, CivicClerk, etc): accepted if
      server returns 200 and date is valid — keyword check skipped.

    Returns (True, "OK") if valid, (False, reason) if not.
    """
    # ── Special case: document hosting platforms ──────────────────────────────
    if _is_document_hosting_url(url):
        if expected_date and not _is_valid_date(expected_date):
            return False, f"Invalid date '{expected_date}' for document hosting URL"
        if expected_date and expected_date not in ("unknown", "Unknown", "UNKNOWN"):
            try:
                dt = datetime.strptime(expected_date, "%Y-%m-%d")
                cutoff = datetime.now() - timedelta(days=lookback_days)
                future_limit = datetime.now() + timedelta(days=180)
                if dt < cutoff:
                    return False, f"Date {expected_date} is before lookback cutoff"
                if dt > future_limit:
                    return False, f"Date {expected_date} is more than 180 days in the future"
            except ValueError:
                pass
        return True, "OK (document hosting platform — content not directly fetchable)"

    try:
        import httpx
    except ImportError:
        return True, "OK (httpx not available, skipping URL check)"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Range": f"bytes=0-{read_bytes - 1}",  # partial fetch where supported
    }

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, verify=False
        ) as client:
            resp = await client.get(url, headers=headers)

            # Some servers ignore Range and return 200 with full content
            if resp.status_code not in (200, 206):
                return False, f"HTTP {resp.status_code}"

            content_type = resp.headers.get("content-type", "").lower()
            content = resp.content[:read_bytes]

            # ── Special case: known civic platform viewer ─────────────────────
            # These return HTML agenda viewer pages — skip keyword check, just
            # verify date range and that the server responded successfully.
            if _is_known_platform_url(url):
                if expected_date and expected_date not in ("unknown", "Unknown", "UNKNOWN"):
                    try:
                        dt = datetime.strptime(expected_date, "%Y-%m-%d")
                        cutoff = datetime.now() - timedelta(days=lookback_days)
                        future_limit = datetime.now() + timedelta(days=180)
                        if dt < cutoff:
                            return False, f"Date {expected_date} is before lookback cutoff"
                        if dt > future_limit:
                            return False, f"Date {expected_date} is more than 180 days in the future"
                    except ValueError:
                        pass
                return True, "OK (known civic platform)"

            # Check content type — accept PDF, HTML, or octet-stream
            if content_type:
                is_pdf_ct = "pdf" in content_type
                is_html_ct = "html" in content_type or "text" in content_type
                is_unknown_ct = "octet-stream" in content_type or not content_type
                if not (is_pdf_ct or is_html_ct or is_unknown_ct):
                    return False, f"Unexpected Content-Type: {content_type}"

    except Exception as e:
        return False, f"Request failed: {e}"

    # Now run the standard byte-level checks
    return verify_document(
        content=content,
        expected_date=expected_date,
        lookback_days=lookback_days,
        body_name_hint=body_name_hint,
    )


async def verify_events(
    events: list[dict],
    lookback_days: int = 90,
    body_name_hint: str | None = None,
    concurrency: int = 5,
) -> tuple[list[dict], list[dict]]:
    """
    Verify a list of event dicts. Each event must have an 'agendaUrl' field.

    Returns (valid_events, rejected_events).
    rejected_events have an added 'verificationReason' field.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _check(event: dict) -> tuple[dict, bool, str]:
        url = event.get("agendaUrl", "")
        date = event.get("date")
        if not url:
            return event, False, "No agendaUrl"

        # Reject events with no parseable date — these are archive/index pages,
        # not individual meeting documents. The agent must provide a specific date.
        if not date or date in ("unknown", "Unknown", "UNKNOWN") or not _is_valid_date(date):
            return event, False, f"No specific meeting date (got '{date}') — likely an archive index page"

        async with semaphore:
            ok, reason = await verify_url(
                url,
                expected_date=date,
                lookback_days=lookback_days,
                body_name_hint=body_name_hint,
            )
        return event, ok, reason

    results = await asyncio.gather(*[_check(e) for e in events])

    valid = []
    rejected = []
    for event, ok, reason in results:
        if ok:
            valid.append(event)
        else:
            rejected.append({**event, "verificationReason": reason})

    return valid, rejected
