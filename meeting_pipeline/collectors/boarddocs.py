"""
boarddocs.py — Collect legislative data from BoardDocs.

BoardDocs (go.boarddocs.com) is a meeting management platform used by many
municipalities. Unlike Legistar, it has no public REST API — it uses internal
AJAX endpoints (POST requests to a Lotus Domino NSF database).

This collector reverse-engineers those endpoints to fetch meetings, agenda items,
and attachments, then outputs data in the same JSON schema as the Legistar
collector so all downstream scripts work unchanged.

Tested against: https://go.boarddocs.com/nc/raleigh/Board.nsf

Endpoint reference:
  - BD-GetMeetingsList: POST, returns JSON array of meetings
  - BD-GetAgenda: POST, returns HTML of agenda items
  - BD-GetAgendaItem: POST, returns HTML of item detail
  - BD-GetPublicFiles: POST, returns HTML with file links
  - BD-GetMinutes: POST, returns HTML of meeting minutes
  - /Public: GET, contains committee IDs in HTML

Usage:
    from collectors.boarddocs import BoardDocsConfig, collect_boarddocs

    config = BoardDocsConfig(
        base_url="https://go.boarddocs.com/nc/raleigh/Board.nsf",
        city_name="Raleigh",
        output_prefix="data/legistar",
        storage=storage_backend,
        lookback_days=180,
    )
    result = await collect_boarddocs(config)
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser

import httpx

from meeting_pipeline.collection_agent.storage import StorageBackend


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class BoardDocsConfig:
    """Configuration for BoardDocs data collection."""
    base_url: str           # e.g. "https://go.boarddocs.com/nc/raleigh/Board.nsf"
    city_name: str          # e.g. "Raleigh"
    output_prefix: str      # Storage key prefix
    storage: StorageBackend
    lookback_days: int = 180
    committee_id: str = ""  # Primary committee ID (auto-discovered if empty)
    download_pdfs: bool = True
    request_timeout: int = 30
    rate_limit_delay: float = 0.3


@dataclass
class BoardDocsResult:
    """Summary of collected BoardDocs data."""
    bodies_count: int = 0
    events_count: int = 0
    matters_count: int = 0
    pdf_count: int = 0
    vote_count: int = 0
    persons_count: int = 0
    output_prefix: str = ""


# ============================================================================
# HTML PARSERS (lightweight, no external dependencies)
# ============================================================================

class CommitteeParser(HTMLParser):
    """Parse committee IDs from the /Public page HTML."""

    def __init__(self):
        super().__init__()
        self.committees: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag == "a":
            attr_dict = dict(attrs)
            cid = attr_dict.get("committeeid", "")
            if cid:
                name = (attr_dict.get("aria-label", "")
                        or attr_dict.get("title", "")
                        or attr_dict.get("data-name", "")
                        or "")
                self.committees.append({"id": cid, "name": name})


class AgendaParser(HTMLParser):
    """Parse agenda items from BD-GetAgenda HTML response."""

    def __init__(self):
        super().__init__()
        self.items: list[dict] = []
        self.current_category = ""
        self._in_dt = False
        self._dt_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = dict(attrs)
        if tag == "dt":
            self._in_dt = True
            self._dt_text = ""
        elif tag == "li":
            unique = attr_dict.get("unique", "")
            title = attr_dict.get("xtitle", "") or attr_dict.get("title", "")
            if unique:
                self.items.append({
                    "unique": unique,
                    "title": title,
                    "category": self.current_category,
                    "unid": attr_dict.get("unid", ""),
                })

    def handle_data(self, data: str):
        if self._in_dt:
            self._dt_text += data.strip()

    def handle_endtag(self, tag: str):
        if tag == "dt" and self._in_dt:
            self._in_dt = False
            if self._dt_text:
                self.current_category = self._dt_text


class FileParser(HTMLParser):
    """Parse file links from BD-GetPublicFiles HTML response."""

    def __init__(self):
        super().__init__()
        self.files: list[dict] = []
        self._in_link = False
        self._current_href = ""
        self._current_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag == "a":
            attr_dict = dict(attrs)
            cls = attr_dict.get("class", "")
            href = attr_dict.get("href", "")
            if "public-file" in cls or ("files/" in href and href.endswith(".pdf")):
                self._in_link = True
                self._current_href = href
                self._current_text = ""
            elif href and "$file/" in href:
                self._in_link = True
                self._current_href = href
                self._current_text = ""

    def handle_data(self, data: str):
        if self._in_link:
            self._current_text += data.strip()

    def handle_endtag(self, tag: str):
        if tag == "a" and self._in_link:
            self._in_link = False
            if self._current_href:
                self.files.append({
                    "href": self._current_href,
                    "name": self._current_text or self._current_href.split("/")[-1],
                })


class ItemDetailParser(HTMLParser):
    """Extract text content from BD-GetAgendaItem HTML."""

    def __init__(self):
        super().__init__()
        self.text_parts: list[str] = []
        self._skip_tags = {"script", "style", "button", "nav"}
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag: str):
        if tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.text_parts.append(text)


# ============================================================================
# COLLECTOR
# ============================================================================

_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://go.boarddocs.com",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


async def collect_boarddocs(config: BoardDocsConfig) -> BoardDocsResult:
    """Collect legislative data from a BoardDocs site.

    Outputs the same JSON schema as the Legistar collector:
      bodies.json, events.json, matters.json, event_items/{id}.json,
      attachments/*.pdf, persons.json, votes/ (empty for BoardDocs).
    """
    result = BoardDocsResult(output_prefix=config.output_prefix)

    cutoff = datetime.now() - timedelta(days=config.lookback_days)
    cutoff_str = cutoff.strftime("%Y%m%d")

    headers = {
        **_HEADERS,
        "Referer": f"{config.base_url}/Public",
    }

    async with httpx.AsyncClient(timeout=config.request_timeout, follow_redirects=True) as client:
        # ── Step 1: Discover committees ──────────────────────────────
        print(f"\n{'='*60}")
        print(f"01 — Collect Legislative Data for {config.city_name} (BoardDocs)")
        print(f"{'='*60}")
        print(f"\nSource: {config.base_url}")
        print(f"Lookback: {config.lookback_days} days (since {cutoff.strftime('%Y-%m-%d')})")

        committees = await _fetch_committees(client, config, headers)
        print(f"\nFound {len(committees)} committees/boards")

        # Save as bodies.json
        bodies = []
        for i, comm in enumerate(committees):
            bodies.append({
                "BodyId": i + 1,
                "BodyName": comm["name"],
                "BodyTypeName": "Board" if "board" in comm["name"].lower() else "Committee",
                "BodyActiveFlag": 1,
                "_boarddocs_id": comm["id"],
            })
        config.storage.write_json(f"{config.output_prefix}/bodies.json", bodies)
        result.bodies_count = len(bodies)

        # ── Step 2: Fetch meetings for each committee ────────────────
        print(f"\nFetching meetings...")
        all_meetings: list[dict] = []
        for comm in committees:
            meetings = await _fetch_meetings(client, config, headers, comm["id"])
            for m in meetings:
                if m.get("numberdate", "99999999") >= cutoff_str:
                    m["_committee_id"] = comm["id"]
                    m["_committee_name"] = comm["name"]
                    all_meetings.append(m)
            await asyncio.sleep(config.rate_limit_delay)

        print(f"  {len(all_meetings)} meetings within lookback period")

        # Save as events.json
        events = []
        for i, m in enumerate(all_meetings):
            date_str = m.get("numberdate", "")
            iso_date = ""
            if len(date_str) == 8:
                iso_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T00:00:00"
            events.append({
                "EventId": i + 1,
                "EventDate": iso_date,
                "EventBodyName": m.get("_committee_name", ""),
                "EventComment": m.get("name", ""),
                "_boarddocs_unique": m.get("unique", ""),
                "_boarddocs_committee_id": m.get("_committee_id", ""),
            })
        config.storage.write_json(f"{config.output_prefix}/events.json", events)
        result.events_count = len(events)

        # ── Step 3: Fetch agenda items for each meeting ──────────────
        print(f"\nFetching agenda items for {len(all_meetings)} meetings...")
        all_matters: list[dict] = []
        all_event_items: dict[int, list[dict]] = {}
        matter_id_counter = 1

        for evt_idx, meeting in enumerate(all_meetings):
            event_id = evt_idx + 1
            committee_id = meeting["_committee_id"]
            meeting_unique = meeting.get("unique", "")

            if not meeting_unique:
                continue

            agenda_items = await _fetch_agenda(
                client, config, headers, meeting_unique, committee_id
            )
            await asyncio.sleep(config.rate_limit_delay)

            event_items_list = []
            for item_idx, item in enumerate(agenda_items):
                matter_id = matter_id_counter
                matter_id_counter += 1

                # Fetch item detail for action text
                detail_text = ""
                if item.get("unique"):
                    detail_text = await _fetch_item_detail(
                        client, config, headers, item["unique"], committee_id
                    )
                    await asyncio.sleep(config.rate_limit_delay * 0.5)

                date_str = meeting.get("numberdate", "")
                iso_date = ""
                if len(date_str) == 8:
                    iso_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T00:00:00"

                # Build matter record (Legistar-compatible)
                matter = {
                    "MatterId": matter_id,
                    "MatterTitle": item.get("title", "").strip(),
                    "MatterTypeName": item.get("category", "Agenda Item"),
                    "MatterStatusName": "Passed",  # BoardDocs doesn't expose status granularly
                    "MatterBodyName": meeting.get("_committee_name", ""),
                    "MatterIntroDate": iso_date,
                    "_boarddocs_unique": item.get("unique", ""),
                    "_boarddocs_meeting_unique": meeting_unique,
                }
                all_matters.append(matter)

                # Build event item record
                event_item = {
                    "EventItemId": matter_id,
                    "EventItemTitle": item.get("title", "").strip(),
                    "EventItemActionText": detail_text[:2000] if detail_text else "",
                    "EventItemRollCallFlag": 0,
                    "EventItemMatterId": matter_id,
                    "_boarddocs_unique": item.get("unique", ""),
                }
                event_items_list.append(event_item)

                # Fetch and download attachments for this item.
                if item.get("unique"):
                    pdf_count = await _download_item_attachments(
                        client, config, item["unique"], matter_id,
                    )
                    result.pdf_count += pdf_count
                    await asyncio.sleep(config.rate_limit_delay * 0.5)

            # Save event items for this meeting
            if event_items_list:
                config.storage.write_json(
                    f"{config.output_prefix}/event_items/{event_id}.json",
                    event_items_list,
                )
                all_event_items[event_id] = event_items_list

            # Progress
            if (evt_idx + 1) % 5 == 0 or evt_idx == len(all_meetings) - 1:
                print(f"  Processed {evt_idx + 1}/{len(all_meetings)} meetings, "
                      f"{len(all_matters)} items so far")

        # Save matters.json
        config.storage.write_json(f"{config.output_prefix}/matters.json", all_matters)
        result.matters_count = len(all_matters)

        # ── Step 4: Create empty/stub files for compatibility ────────
        # persons.json — BoardDocs doesn't expose structured member data easily
        config.storage.write_json(f"{config.output_prefix}/persons.json", [])
        result.persons_count = 0
        result.vote_count = 0

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Collection complete for {config.city_name} (BoardDocs)")
    print(f"{'='*60}")
    print(f"  Bodies/committees: {result.bodies_count}")
    print(f"  Meetings (events): {result.events_count}")
    print(f"  Agenda items (matters): {result.matters_count}")
    print(f"  PDFs downloaded: {result.pdf_count}")
    print(f"  Output: {config.output_prefix}")

    return result


# ============================================================================
# INTERNAL FETCHERS
# ============================================================================

async def _download_item_attachments(
    client: httpx.AsyncClient,
    config: BoardDocsConfig,
    item_unique: str,
    matter_id: int,
) -> int:
    """Download PDF attachments for a single agenda item. Returns count of PDFs saved."""
    files = await _fetch_files(client, config, {
        **_HEADERS, "Referer": f"{config.base_url}/Public",
    }, item_unique)
    if not files:
        return 0

    pdf_count = 0
    att_records = []
    for f_idx, f in enumerate(files):
        att_id = f_idx + 1
        href = f["href"]
        download_url = _resolve_boarddocs_url(config.base_url, href)

        filename = f["name"] or f"attachment_{att_id}"
        local_name = f"{matter_id}_{att_id}.pdf"
        att_key = f"{config.output_prefix}/attachments/{local_name}"

        if not config.storage.exists(att_key):
            pdf_count += await _try_download_pdf(
                client, config.base_url, download_url, att_key, config.storage
            )

        att_records.append({
            "MatterAttachmentId": att_id,
            "MatterAttachmentName": filename,
            "MatterAttachmentHyperlink": download_url,
            "_local_file": local_name,
        })

    if att_records:
        config.storage.write_json(
            f"{config.output_prefix}/matter_attachments/{matter_id}.json",
            att_records,
        )

    return pdf_count


def _resolve_boarddocs_url(base_url: str, href: str) -> str:
    """Resolve a BoardDocs file href to a full download URL."""
    if href.startswith("/"):
        return f"https://go.boarddocs.com{href}"
    if not href.startswith("http"):
        return f"{base_url}/{href}"
    return href


async def _try_download_pdf(
    client: httpx.AsyncClient,
    base_url: str,
    url: str,
    key: str,
    storage: StorageBackend,
) -> int:
    """Try to download a PDF from url. Returns 1 if saved, 0 otherwise."""
    try:
        resp = await client.get(url, headers={
            "User-Agent": _HEADERS["User-Agent"],
            "Referer": f"{base_url}/Public",
        })
        if resp.status_code != 200 or len(resp.content) <= 100:
            return 0
        ct = resp.headers.get("content-type", "")
        if resp.content[:5] == b"%PDF-" or "pdf" in ct.lower():
            storage.write_bytes(key, resp.content)
            return 1
    except Exception:
        pass
    return 0


async def _fetch_committees(
    client: httpx.AsyncClient,
    config: BoardDocsConfig,
    headers: dict,
) -> list[dict]:
    """Fetch committee list from the /Public page HTML."""
    # If a specific committee_id is configured, use it directly
    if config.committee_id:
        return [{"id": config.committee_id, "name": f"{config.city_name} City Council"}]

    try:
        resp = await client.get(f"{config.base_url}/Public", headers={
            "User-Agent": headers["User-Agent"],
        })
        resp.raise_for_status()

        parser = CommitteeParser()
        parser.feed(resp.text)

        if parser.committees:
            # Deduplicate by ID
            seen = set()
            unique = []
            for c in parser.committees:
                if c["id"] not in seen:
                    seen.add(c["id"])
                    # Try to extract name from text content if title is empty
                    if not c["name"]:
                        c["name"] = f"Committee {c['id'][:8]}"
                    unique.append(c)
            return unique
    except Exception as e:
        print(f"  Warning: Could not fetch committee list: {e}")

    raise RuntimeError(
        "Could not discover BoardDocs committees. Check the base_url and "
        "committee configuration in city_config.json."
    )


async def _fetch_meetings(
    client: httpx.AsyncClient,
    config: BoardDocsConfig,
    headers: dict,
    committee_id: str,
) -> list[dict]:
    """Fetch meetings for a committee via BD-GetMeetingsList."""
    try:
        resp = await client.post(
            f"{config.base_url}/BD-GetMeetingsList?open",
            data=f"current_committee_id={committee_id}",
            headers=headers,
        )
        resp.raise_for_status()
        meetings = resp.json()
        if isinstance(meetings, list):
            return meetings
    except Exception as e:
        print(f"  Warning: Could not fetch meetings for {committee_id}: {e}")
    return []


async def _fetch_agenda(
    client: httpx.AsyncClient,
    config: BoardDocsConfig,
    headers: dict,
    meeting_unique: str,
    committee_id: str,
) -> list[dict]:
    """Fetch agenda items for a meeting via BD-GetAgenda."""
    try:
        resp = await client.post(
            f"{config.base_url}/BD-GetAgenda?open",
            data=f"id={meeting_unique}&current_committee_id={committee_id}",
            headers=headers,
        )
        resp.raise_for_status()

        parser = AgendaParser()
        parser.feed(resp.text)
        return parser.items
    except Exception as e:
        print(f"  Warning: Could not fetch agenda for meeting {meeting_unique}: {e}")
    return []


async def _fetch_item_detail(
    client: httpx.AsyncClient,
    config: BoardDocsConfig,
    headers: dict,
    item_unique: str,
    committee_id: str,
) -> str:
    """Fetch agenda item detail text via BD-GetAgendaItem."""
    try:
        resp = await client.post(
            f"{config.base_url}/BD-GetAgendaItem?open",
            data=f"id={item_unique}&current_committee_id={committee_id}",
            headers=headers,
        )
        resp.raise_for_status()

        parser = ItemDetailParser()
        parser.feed(resp.text)
        return " ".join(parser.text_parts)
    except Exception:
        return ""


async def _fetch_files(
    client: httpx.AsyncClient,
    config: BoardDocsConfig,
    headers: dict,
    item_unique: str,
) -> list[dict]:
    """Fetch attached files for an agenda item via BD-GetPublicFiles."""
    try:
        resp = await client.post(
            f"{config.base_url}/BD-GetPublicFiles?open",
            data=f"id={item_unique}",
            headers=headers,
        )
        resp.raise_for_status()

        parser = FileParser()
        parser.feed(resp.text)
        return parser.files
    except Exception:
        return []
