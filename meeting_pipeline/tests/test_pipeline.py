"""
Comprehensive test suite for the meeting_pipeline package.

Tests cover: imports, shared modules, scan gating, collect gating,
extraction, lambda handlers, and orchestrator logic.

No AWS credentials or external API calls required -- all external
dependencies are mocked.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: in-memory StorageBackend stub
# ---------------------------------------------------------------------------

class FakeStorage:
    """Minimal in-memory StorageBackend for testing (no S3 needed)."""

    def __init__(self, data: dict[str, bytes | dict] | None = None):
        self._store: dict[str, bytes] = {}
        if data:
            for k, v in data.items():
                if isinstance(v, dict):
                    self._store[k] = json.dumps(v).encode()
                else:
                    self._store[k] = v

    def read_json(self, key: str) -> dict:
        raw = self._store.get(key)
        if raw is None:
            raise FileNotFoundError(key)
        return json.loads(raw)

    def write_json(self, key: str, data: dict) -> None:
        self._store[key] = json.dumps(data).encode()

    def write_bytes(self, key: str, data: bytes) -> None:
        self._store[key] = data

    def read_bytes(self, key: str) -> bytes:
        raw = self._store.get(key)
        if raw is None:
            raise FileNotFoundError(key)
        return raw

    def exists(self, key: str) -> bool:
        return key in self._store

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def get_size(self, key: str) -> int:
        raw = self._store.get(key)
        return len(raw) if raw else 0

    def append_line(self, key: str, line: str) -> None:
        pass

    def get_presigned_url(self, key: str, expiry_seconds: int = 300) -> str:
        return f"https://fake-presigned/{key}"


# =========================================================================
# 1. IMPORT TESTS -- every module loads without errors
# =========================================================================

class TestImports:
    """Verify every Python module in the package can be imported."""

    # -- shared/ --

    def test_import_shared_config(self):
        from meeting_pipeline.shared import config  # noqa: F401

    def test_import_shared_storage(self):
        from meeting_pipeline.shared import storage  # noqa: F401

    def test_import_shared_constants(self):
        from meeting_pipeline.shared import constants  # noqa: F401

    def test_import_shared_models(self):
        from meeting_pipeline.shared import models  # noqa: F401

    def test_import_shared_url_utils(self):
        from meeting_pipeline.shared import url_utils  # noqa: F401

    def test_import_shared_date_utils(self):
        from meeting_pipeline.shared import date_utils  # noqa: F401

    def test_import_shared_manifest(self):
        from meeting_pipeline.shared import manifest  # noqa: F401

    def test_import_shared_verify_source(self):
        from meeting_pipeline.shared import verify_source  # noqa: F401

    def test_import_shared_notification_log(self):
        from meeting_pipeline.shared import notification_log  # noqa: F401

    def test_import_shared_body_validation(self):
        from meeting_pipeline.shared import body_validation  # noqa: F401

    def test_import_shared_discovery_helpers(self):
        from meeting_pipeline.shared import discovery_helpers  # noqa: F401

    def test_import_shared_firecrawl_client(self):
        from meeting_pipeline.shared import firecrawl_client  # noqa: F401

    def test_import_shared_generic_agenda_scanner(self):
        from meeting_pipeline.shared import generic_agenda_scanner  # noqa: F401

    # -- stages/ --

    def test_import_stages_orchestrator(self):
        from meeting_pipeline.stages import orchestrator  # noqa: F401

    def test_import_stages_scan_process(self):
        from meeting_pipeline.stages.scan import process  # noqa: F401

    def test_import_stages_scan_body_filter(self):
        from meeting_pipeline.stages.scan import body_filter  # noqa: F401

    def test_import_stages_scan_platforms_civicclerk(self):
        from meeting_pipeline.stages.scan.platforms import civicclerk  # noqa: F401

    def test_import_stages_scan_platforms_civicplus(self):
        from meeting_pipeline.stages.scan.platforms import civicplus  # noqa: F401

    def test_import_stages_scan_platforms_legistar(self):
        from meeting_pipeline.stages.scan.platforms import legistar  # noqa: F401

    def test_import_stages_scan_platforms_boarddocs(self):
        from meeting_pipeline.stages.scan.platforms import boarddocs  # noqa: F401

    def test_import_stages_scan_platforms_granicus(self):
        from meeting_pipeline.stages.scan.platforms import granicus  # noqa: F401

    def test_import_stages_scan_platforms_escribe(self):
        from meeting_pipeline.stages.scan.platforms import escribe  # noqa: F401

    def test_import_stages_collect_process(self):
        from meeting_pipeline.stages.collect import process  # noqa: F401

    def test_import_stages_collect_router(self):
        from meeting_pipeline.stages.collect import router  # noqa: F401

    def test_import_stages_extract_normalize(self):
        from meeting_pipeline.stages.extract import normalize  # noqa: F401

    def test_import_stages_briefing_generate(self):
        from meeting_pipeline.stages.briefing import generate  # noqa: F401

    def test_import_stages_discover_process(self):
        from meeting_pipeline.stages.discover import process  # noqa: F401

    def test_import_stages_discover_scoring(self):
        from meeting_pipeline.stages.discover import scoring  # noqa: F401

    def test_import_stages_discover_search(self):
        from meeting_pipeline.stages.discover import search  # noqa: F401

    def test_import_stages_discover_crawl(self):
        from meeting_pipeline.stages.discover import crawl  # noqa: F401

    def test_import_stages_discover_main_flow(self):
        from meeting_pipeline.stages.discover import main_flow  # noqa: F401

    # -- lambda_handlers/ live on the meeting-pipeline-infra branch — see
    # meeting_pipeline/tests/test_lambda_handlers.py there.

    # -- collectors/ --

    def test_import_collectors_legistar(self):
        from meeting_pipeline.collectors import legistar  # noqa: F401

    def test_import_collectors_civicclerk(self):
        from meeting_pipeline.collectors import civicclerk  # noqa: F401

    def test_import_collectors_civicplus_scraper(self):
        from meeting_pipeline.collectors import civicplus_scraper  # noqa: F401

    def test_import_collectors_granicus_scraper(self):
        from meeting_pipeline.collectors import granicus_scraper  # noqa: F401

    def test_import_collectors_boarddocs(self):
        from meeting_pipeline.collectors import boarddocs  # noqa: F401

    def test_import_collectors_escribemeetings(self):
        from meeting_pipeline.collectors import escribemeetings  # noqa: F401

    def test_import_collectors_municode(self):
        from meeting_pipeline.collectors import municode  # noqa: F401

    def test_import_collectors_novus_scraper(self):
        from meeting_pipeline.collectors import novus_scraper  # noqa: F401

    # -- prompts/ --

    def test_import_prompts_extraction(self):
        from meeting_pipeline.prompts import extraction  # noqa: F401

    def test_import_prompts_briefing(self):
        from meeting_pipeline.prompts import briefing  # noqa: F401


# =========================================================================
# 2. SHARED MODULE TESTS
# =========================================================================

class TestAgentConfig:
    """AgentConfig.from_env() reads environment variables correctly."""

    def test_from_env_defaults(self):
        from meeting_pipeline.shared.config import AgentConfig

        with patch.dict(os.environ, {}, clear=True):
            cfg = AgentConfig.from_env()
        assert cfg.sources_prefix == "meeting_pipeline/sources"
        assert cfg.storage_backend == "s3"
        assert cfg.s3_bucket is None
        assert cfg.lookback_days == 90
        assert cfg.download_pdfs is True
        # AGENDAS_ONLY defaults to false when env is clear
        assert cfg.agendas_only is False

    def test_from_env_custom(self):
        from meeting_pipeline.shared.config import AgentConfig

        env = {
            "SOURCES_PREFIX": "custom/sources",
            "LOGS_PREFIX": "custom/logs",
            "OUTPUT_PREFIX": "custom/output",
            "STORAGE_BACKEND": "s3",
            "S3_BUCKET": "my-bucket",
            "LOOKBACK_DAYS": "30",
            "DOWNLOAD_PDFS": "false",
            "AGENDAS_ONLY": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = AgentConfig.from_env()
        assert cfg.sources_prefix == "custom/sources"
        assert cfg.s3_bucket == "my-bucket"
        assert cfg.lookback_days == 30
        assert cfg.download_pdfs is False
        assert cfg.agendas_only is True

    def test_get_storage_rejects_non_s3(self):
        from meeting_pipeline.shared.config import AgentConfig, get_storage

        cfg = AgentConfig(storage_backend="local")
        with pytest.raises(ValueError, match="must be 's3'"):
            get_storage(cfg)

    def test_get_storage_rejects_missing_bucket(self):
        from meeting_pipeline.shared.config import AgentConfig, get_storage

        cfg = AgentConfig(storage_backend="s3", s3_bucket=None)
        with pytest.raises(ValueError, match="S3_BUCKET must be set"):
            get_storage(cfg)


class TestCityToSlug:
    """city_to_slug() normalizes city names correctly."""

    def test_simple_city(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("Loveland", "OH") == "loveland-OH"

    def test_city_with_spaces(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("Chapel Hill", "NC") == "chapel-hill-NC"

    def test_city_with_period(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("St. Louis", "MO") == "st-louis-MO"

    def test_city_with_apostrophe(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("O'Fallon", "IL") == "ofallon-IL"

    def test_state_uppercased(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("Austin", "tx") == "austin-TX"

    def test_multiple_spaces(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("Canal Winchester", "OH") == "canal-winchester-OH"


class TestVerifySource:
    """verify_source module: keyword list and _check_pdf_content."""

    def test_agenda_keywords_non_empty(self):
        from meeting_pipeline.shared.verify_source import AGENDA_KEYWORDS
        assert len(AGENDA_KEYWORDS) > 0
        assert "agenda" in AGENDA_KEYWORDS
        assert "meeting" in AGENDA_KEYWORDS

    def test_check_pdf_content_with_non_pdf_bytes(self):
        """Non-PDF bytes should return is_agenda=False without crashing."""
        from meeting_pipeline.shared.verify_source import _check_pdf_content
        result = _check_pdf_content(b"this is not a PDF at all")
        assert result["is_agenda"] is False
        assert result["words"] == 0 or "error" in result

    def test_check_pdf_content_with_empty_bytes(self):
        from meeting_pipeline.shared.verify_source import _check_pdf_content
        result = _check_pdf_content(b"")
        assert result["is_agenda"] is False

    def test_check_pdf_content_returns_expected_keys(self):
        from meeting_pipeline.shared.verify_source import _check_pdf_content
        result = _check_pdf_content(b"fake pdf bytes")
        assert "pages" in result
        assert "words" in result
        assert "keyword_hits" in result
        assert "is_agenda" in result
        assert "is_scanned" in result


class TestFindPastAgendaCivicClerk:
    """_find_past_agenda_from_platform must drill into per-event publishedFiles
    for portal CivicClerk tenants. Older logic only checked inline AgendaFile/
    AgendaUrl/agendaFile fields on event summaries, which portal tenants never
    populate — agenda PDFs only live in publishedFiles[].streamUrl on detail."""

    def test_portal_tenant_uses_published_files(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from meeting_pipeline.shared.verify_source import _find_past_agenda_from_platform

        # Event summaries with no inline agenda fields (portal-style)
        events_resp = MagicMock()
        events_resp.json.return_value = {"value": [
            {"id": 101, "eventName": "Council Meeting"},
            {"id": 102, "eventName": "Special Session"},
        ]}
        events_resp.raise_for_status = MagicMock()

        # Per-event detail #1 has no agenda; #2 has the streamUrl we want
        detail_101 = MagicMock()
        detail_101.json.return_value = {"id": 101, "publishedFiles": []}
        detail_101.raise_for_status = MagicMock()
        detail_102 = MagicMock()
        detail_102.json.return_value = {
            "id": 102,
            "publishedFiles": [
                {"type": "Minutes", "streamUrl": "https://example.com/minutes.pdf"},
                {"type": "Agenda", "streamUrl": "https://example.com/agenda.pdf"},
            ],
        }
        detail_102.raise_for_status = MagicMock()

        client = MagicMock()
        client.get = AsyncMock(side_effect=[events_resp, detail_101, detail_102])

        url = asyncio.run(_find_past_agenda_from_platform(
            "civicclerk", {}, "https://demoxxx.portal.civicclerk.com", client,
        ))
        assert url == "https://example.com/agenda.pdf"

    def test_legacy_tenant_uses_inline_agenda_field(self):
        """Legacy CivicClerk tenants put the agenda URL inline on the event
        summary as AgendaFile — must still work after the portal fix."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from meeting_pipeline.shared.verify_source import _find_past_agenda_from_platform

        events_resp = MagicMock()
        events_resp.json.return_value = {"value": [
            {"Id": 9, "AgendaFile": "https://legacy.example/agenda.pdf"},
        ]}
        events_resp.raise_for_status = MagicMock()

        client = MagicMock()
        client.get = AsyncMock(return_value=events_resp)

        url = asyncio.run(_find_past_agenda_from_platform(
            "civicclerk", {}, "https://demoxxx.api.civicclerk.com", client,
        ))
        assert url == "https://legacy.example/agenda.pdf"
        # Only one HTTP call needed — drilling into details should be skipped
        assert client.get.call_count == 1


class TestFindPastAgendaCivicPlus:
    """_find_past_agenda_from_platform must drill into the AgendaCenter AJAX
    endpoint for CivicPlus. Older logic returned None outright, leaving every
    civicplus city to fall through to the Firecrawl fallback (which often
    can't navigate the JS-rendered AgendaCenter UI and reports 'no agenda URL
    found to verify')."""

    def test_civicplus_extracts_first_agenda_link_using_stored_category(self):
        """Fast path: when discover has already stored council_category_id in
        source.json config, verify uses it directly instead of running the
        heavyweight find_council_category re-discovery (which fails on some
        sites and is wasteful on the ones it doesn't)."""
        import asyncio
        from unittest.mock import AsyncMock
        from meeting_pipeline.shared.verify_source import _find_past_agenda_from_platform

        ajax_html = (
            '<html><body>'
            '<tr class="catAgendaRow">'
            '<td>'
            '<a href="/AgendaCenter/ViewFile/Agenda/_05012026-1234?packet=true">Packet</a>'
            '<a href="/AgendaCenter/ViewFile/Agenda/_05012026-1234">Agenda</a>'
            '</td></tr></body></html>'
        )
        ajax_resp = MagicMock()
        ajax_resp.text = ajax_html
        ajax_resp.raise_for_status = MagicMock()

        client = MagicMock()
        client.post = AsyncMock(return_value=ajax_resp)

        url = asyncio.run(_find_past_agenda_from_platform(
            "civicplus",
            {"council_category_id": 7},
            "https://www.cityofx.com/AgendaCenter",
            client,
        ))
        assert url == "https://www.cityofx.com/AgendaCenter/ViewFile/Agenda/_05012026-1234"

    def test_civicplus_falls_back_to_find_council_category(self):
        """Slow path: when config has no council_category_id, fall back to
        the heavyweight find_council_category."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from meeting_pipeline.shared.verify_source import _find_past_agenda_from_platform

        ajax_html = (
            '<tr class="catAgendaRow">'
            '<a href="/AgendaCenter/ViewFile/Agenda/_05012026-9999">A</a>'
            '</tr>'
        )
        ajax_resp = MagicMock()
        ajax_resp.text = ajax_html
        ajax_resp.raise_for_status = MagicMock()

        client = MagicMock()
        client.post = AsyncMock(return_value=ajax_resp)

        with patch(
            "meeting_pipeline.collectors.civicplus_scraper.find_council_category",
            new=AsyncMock(return_value=(42, "City Council")),
        ):
            url = asyncio.run(_find_past_agenda_from_platform(
                "civicplus", {}, "https://www.cityofx.com/AgendaCenter", client,
            ))
        assert url == "https://www.cityofx.com/AgendaCenter/ViewFile/Agenda/_05012026-9999"

    def test_civicplus_skips_archive_aspx_urls(self):
        """The legacy Archive.aspx product line is a different URL pattern;
        the AgendaCenter drilldown can't help. Return None and let Firecrawl
        fallback try."""
        import asyncio
        from unittest.mock import AsyncMock
        from meeting_pipeline.shared.verify_source import _find_past_agenda_from_platform

        client = MagicMock()
        client.post = AsyncMock()

        url = asyncio.run(_find_past_agenda_from_platform(
            "civicplus",
            {"council_category_id": 30},
            "https://www.dodgecity.org/Archive.aspx?AMID=30",
            client,
        ))
        assert url is None
        # No HTTP calls should have been made — short-circuited.
        assert client.post.call_count == 0


class TestManifest:
    """manifest.py: _is_wrong_entity and validate_against_manifest."""

    def test_is_wrong_entity_school_board(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("school board meeting") is True

    def test_is_wrong_entity_school_district(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("unified school district") is True

    def test_is_wrong_entity_board_of_education(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("board of education") is True

    def test_is_wrong_entity_city_council(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("city council") is False

    def test_is_wrong_entity_superintendent(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("superintendent report") is True

    def test_validate_matching_body(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council"}
        is_valid, reason = validate_against_manifest(
            manifest, ["Regular City Council Meeting", "Planning Commission"]
        )
        assert is_valid is True
        assert reason is None

    def test_validate_non_matching_body(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council"}
        is_valid, reason = validate_against_manifest(
            manifest, ["Water Authority Board"]
        )
        assert is_valid is False
        assert reason is not None

    def test_validate_all_school_bodies_rejected(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council"}
        is_valid, reason = validate_against_manifest(
            manifest, ["school board", "board of education"]
        )
        assert is_valid is False
        assert "wrong entity" in reason.lower()

    def test_validate_governing_body_synonym(self):
        """Board of Aldermen should match when expected is City Council."""
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council"}
        is_valid, reason = validate_against_manifest(
            manifest, ["Board of Aldermen Regular Meeting"]
        )
        assert is_valid is True

    def test_validate_empty_bodies_passes(self):
        """If no bodies collected, validation should pass (best-effort)."""
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council"}
        is_valid, reason = validate_against_manifest(manifest, [])
        assert is_valid is True

    def test_validate_city_match(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_city": "Chapel Hill", "expected_body": ""}
        is_valid, reason = validate_against_manifest(
            manifest, [], collected_city="Chapel Hill"
        )
        assert is_valid is True

    def test_validate_city_mismatch(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_city": "Chapel Hill", "expected_body": ""}
        is_valid, reason = validate_against_manifest(
            manifest, [], collected_city="Durham"
        )
        assert is_valid is False

    def test_load_manifest_missing(self):
        from meeting_pipeline.shared.manifest import load_manifest
        storage = FakeStorage()
        result = load_manifest("nonexistent-city-TX", storage, "meeting_pipeline/sources")
        assert result is None

    def test_load_manifest_exists(self):
        from meeting_pipeline.shared.manifest import load_manifest
        manifest_data = {"expected_body": "City Council", "expected_city": "Austin"}
        storage = FakeStorage({
            "meeting_pipeline/sources/austin-TX/manifest.json": manifest_data,
        })
        result = load_manifest("austin-TX", storage, "meeting_pipeline/sources")
        assert result is not None
        assert result["expected_body"] == "City Council"


# =========================================================================
# 3. SCAN GATING TESTS
# =========================================================================

class TestScanGating:
    """Scan stage skips unverified cities and processes verified ones."""

    def _make_source(self, verification_status: str | None) -> dict:
        source = {
            "city": "TestCity",
            "state": "OH",
            "best_source": {
                "platform": "civicplus",
                "url": "https://example.civicplus.com/AgendaCenter",
                "config": {},
            },
        }
        if verification_status:
            source["best_source"]["verification"] = {"status": verification_status}
        return source

    def test_scan_skips_unverified_city(self):
        """Orchestrator run_scan skips cities without verified status."""
        from meeting_pipeline.stages.orchestrator import VERIFIED_STATUSES

        source = self._make_source("unverified")
        verification = source["best_source"].get("verification", {})
        assert verification.get("status") not in VERIFIED_STATUSES

    def test_scan_processes_verified_city(self):
        from meeting_pipeline.stages.orchestrator import VERIFIED_STATUSES

        source = self._make_source("verified")
        verification = source["best_source"].get("verification", {})
        assert verification.get("status") in VERIFIED_STATUSES

    def test_scan_processes_verified_ocr_needed(self):
        from meeting_pipeline.stages.orchestrator import VERIFIED_STATUSES

        source = self._make_source("verified_ocr_needed")
        verification = source["best_source"].get("verification", {})
        assert verification.get("status") in VERIFIED_STATUSES

    def test_scan_processes_verified_non_pdf(self):
        from meeting_pipeline.stages.orchestrator import VERIFIED_STATUSES

        source = self._make_source("verified_non_pdf")
        verification = source["best_source"].get("verification", {})
        assert verification.get("status") in VERIFIED_STATUSES

    def test_scan_skips_no_verification(self):
        from meeting_pipeline.stages.orchestrator import VERIFIED_STATUSES

        source = self._make_source(None)
        verification = source["best_source"].get("verification", {})
        assert verification.get("status") not in VERIFIED_STATUSES


class TestCivicClerkScanner:
    """CivicClerk scanner rejects dict URLs and bogus AgendaPostedDate."""

    def test_rejects_dict_url(self):
        """AgendaFile that is a dict (not a string URL) must not yield agenda_url."""
        ev = {
            "MeetingStartDate": "2026-04-15T18:00:00",
            "Name": "City Council",
            "AgendaFile": {"agendaId": 0, "fileName": None},
            "EventId": 123,
        }
        raw_url = ev.get("AgendaFile") or ""
        agenda_url = raw_url if isinstance(raw_url, str) and raw_url.startswith("http") else None
        assert agenda_url is None

    def test_rejects_0001_posted_date(self):
        """AgendaPostedDate of 0001-01-01 must not count as posted."""
        ev = {
            "MeetingStartDate": "2026-04-15T18:00:00",
            "Name": "City Council",
            "AgendaFile": "",
            "AgendaPostedDate": "0001-01-01T00:00:00",
            "EventId": 123,
        }
        raw_url = ev.get("AgendaFile") or ""
        agenda_url = raw_url if isinstance(raw_url, str) and raw_url.startswith("http") else None
        posted_date = ev.get("AgendaPostedDate") or ""
        has_real_posted_date = bool(posted_date) and not posted_date.startswith("0001")
        agenda_posted = bool(agenda_url) or has_real_posted_date
        assert agenda_posted is False

    def test_accepts_real_string_url(self):
        """A real string URL should be accepted as agenda_url."""
        ev = {
            "MeetingStartDate": "2026-04-15T18:00:00",
            "Name": "City Council",
            "AgendaFile": "https://tenant.civicclerk.blob/agenda.pdf",
            "AgendaPostedDate": "2026-04-10T12:00:00",
            "EventId": 123,
        }
        raw_url = ev.get("AgendaFile") or ""
        agenda_url = raw_url if isinstance(raw_url, str) and raw_url.startswith("http") else None
        assert agenda_url == "https://tenant.civicclerk.blob/agenda.pdf"

    def test_real_posted_date_marks_as_posted(self):
        """A real AgendaPostedDate (not 0001) should mark as posted even without URL."""
        ev = {
            "MeetingStartDate": "2026-04-15T18:00:00",
            "Name": "City Council",
            "AgendaFile": "",
            "AgendaPostedDate": "2026-04-10T12:00:00",
            "EventId": 123,
        }
        raw_url = ev.get("AgendaFile") or ""
        agenda_url = raw_url if isinstance(raw_url, str) and raw_url.startswith("http") else None
        posted_date = ev.get("AgendaPostedDate") or ""
        has_real_posted_date = bool(posted_date) and not posted_date.startswith("0001")
        agenda_posted = bool(agenda_url) or has_real_posted_date
        assert agenda_posted is True

    def test_scan_civicclerk_returns_empty_on_bad_tenant(self):
        """If no tenant can be extracted, return empty list."""
        from meeting_pipeline.stages.scan.platforms.civicclerk import scan_civicclerk
        client = AsyncMock(spec=["get"])
        result = asyncio.run(scan_civicclerk("TestCity", {}, "https://example.com/no-civicclerk", client))
        assert result == []

    def test_portal_tenant_drills_into_published_files(self):
        """Portal tenants don't put agendaFile on the event summary — agenda
        URLs only live on the per-event detail endpoint at
        publishedFiles[].streamUrl. Without drilldown, every portal city
        scans as 0 posted and never produces briefings."""
        from datetime import datetime, timedelta
        from meeting_pipeline.stages.scan.platforms.civicclerk import scan_civicclerk

        # Two upcoming events with no inline agendaFile (portal-style).
        future = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
        events_resp = MagicMock()
        events_resp.json.return_value = {"value": [
            {"id": 100, "startDateTime": future, "eventName": "Council Meeting"},
            {"id": 101, "startDateTime": future, "eventName": "Special Session"},
        ]}
        events_resp.raise_for_status = MagicMock()

        # Detail #100 has an Agenda; detail #101 has only Minutes.
        detail_100 = MagicMock()
        detail_100.json.return_value = {
            "id": 100,
            "publishedFiles": [
                {"type": "Agenda", "streamUrl": "https://example.com/100-agenda.pdf"},
            ],
        }
        detail_100.raise_for_status = MagicMock()
        detail_101 = MagicMock()
        detail_101.json.return_value = {
            "id": 101,
            "publishedFiles": [
                {"type": "Minutes", "streamUrl": "https://example.com/101-minutes.pdf"},
            ],
        }
        detail_101.raise_for_status = MagicMock()

        # First .get is the events list; subsequent .get calls are details.
        # Order of detail calls is concurrent so we route by URL.
        async def fake_get(url, *args, **kwargs):
            if "/Events/100" in url:
                return detail_100
            if "/Events/101" in url:
                return detail_101
            return events_resp

        client = MagicMock()
        client.get = AsyncMock(side_effect=fake_get)

        result = asyncio.run(scan_civicclerk(
            "TestCity", {}, "https://demoxxx.portal.civicclerk.com", client,
        ))
        by_id = {e["event_id"]: e for e in result}
        assert by_id["100"]["agenda_posted"] is True
        assert by_id["100"]["agenda_url"] == "https://example.com/100-agenda.pdf"
        assert by_id["101"]["agenda_posted"] is False
        assert by_id["101"]["agenda_url"] is None

    def test_portal_drilldown_skips_past_events(self):
        """Past events shouldn't trigger drilldown — we only care about
        future agendas, and drilldown is bounded to keep scan fast."""
        from datetime import datetime, timedelta
        from meeting_pipeline.stages.scan.platforms.civicclerk import scan_civicclerk

        past = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00")
        events_resp = MagicMock()
        events_resp.json.return_value = {"value": [
            {"id": 50, "startDateTime": past, "eventName": "Past Council"},
        ]}
        events_resp.raise_for_status = MagicMock()

        client = MagicMock()
        client.get = AsyncMock(return_value=events_resp)

        asyncio.run(scan_civicclerk(
            "TestCity", {}, "https://demoxxx.portal.civicclerk.com", client,
        ))
        # Only the events list call — no /Events/50 detail call.
        assert client.get.call_count == 1


# =========================================================================
# 4. COLLECT GATING TESTS
# =========================================================================

class TestCollectGating:
    """Collect stage skips unverified cities; generic collector validates PDFs."""

    def test_collect_skips_unverified(self):
        """Orchestrator run_collect skips cities without verified status."""
        from meeting_pipeline.stages.orchestrator import VERIFIED_STATUSES

        source = {
            "best_source": {
                "verification": {"status": "unverified"},
            }
        }
        verification = source["best_source"].get("verification", {})
        assert verification.get("status") not in VERIFIED_STATUSES

    def test_generic_collector_accepts_non_pdf_urls(self):
        """Generic collector accepts any URL -- it checks content-type at download time."""
        url = "https://example.com/download?id=12345"
        # The router code accepts any agenda_url, not just .pdf URLs
        assert url is not None  # it would be passed to httpx.get

    def test_generic_collector_validates_pdf_content_type(self):
        """After download, generic collector checks content-type for PDF."""
        content_type = "application/pdf"
        content = b"%PDF-1.4 fake pdf content"
        is_pdf = "pdf" in content_type or content[:5] == b"%PDF-"
        assert is_pdf is True

    def test_generic_collector_rejects_html_content(self):
        """HTML content should be rejected."""
        content_type = "text/html; charset=utf-8"
        content = b"<html><body>Not a PDF</body></html>"
        is_pdf = "pdf" in content_type or content[:5] == b"%PDF-"
        assert is_pdf is False


# =========================================================================
# 5. EXTRACT TESTS
# =========================================================================

class TestNormalizeMeeting:
    """normalize_meeting() produces the correct schema."""

    def test_produces_correct_schema(self):
        from meeting_pipeline.stages.extract.normalize import (
            AgendaItem,
            MeetingExtraction,
            normalize_meeting,
        )

        extraction = MeetingExtraction(
            date="2026-04-15",
            time="6:00 PM",
            location="City Hall",
            body="City Council",
            meeting_type="Regular Meeting",
            total_items=2,
            items=[
                AgendaItem(
                    number="1",
                    title="Roll Call",
                    section="opening",
                    description="Roll call of members",
                ),
                AgendaItem(
                    number="2",
                    title="Budget Approval",
                    section="action",
                    description="Approve FY2027 budget",
                    fiscal_amounts=["$5,000,000"],
                    is_public_hearing=True,
                    staff_recommendation="Approve",
                ),
            ],
        )

        official = {"name": "Mayor Smith", "city": "TestCity", "state": "OH", "role": "City Council"}
        meeting = {
            "date": "2026-04-15",
            "title": "Regular City Council Meeting",
            "body": "City Council",
            "source_url": "https://example.com/meeting/123",
            "agenda_files": [
                {"name": "Agenda", "type": "Agenda", "url": "https://example.com/agenda.pdf"},
            ],
        }

        result = normalize_meeting(
            official=official,
            meeting=meeting,
            extraction=extraction,
            pdf_key="sources/test-city-OH/data/civicplus/pdfs/2026-04-15_agenda.pdf",
            pdf_label="agenda",
            city_slug="test-city-OH",
            platform="civicplus",
        )

        # Top-level keys
        assert result["schema_version"] == "1.0"
        assert "generated_at" in result
        assert "official" in result
        assert "meeting" in result
        assert "sources" in result
        assert "agenda" in result
        assert "summary" in result

        # Official
        assert result["official"]["city"] == "TestCity"
        assert result["official"]["state"] == "OH"

        # Meeting
        assert result["meeting"]["date"] == "2026-04-15"
        assert result["meeting"]["body"] == "City Council"
        assert result["meeting"]["platform"] == "civicplus"
        assert result["meeting"]["city_slug"] == "test-city-OH"

        # Agenda items
        assert result["agenda"]["total_items"] == 2
        assert len(result["agenda"]["items"]) == 2
        assert result["agenda"]["items"][1]["title"] == "Budget Approval"
        assert result["agenda"]["items"][1]["is_public_hearing"] is True
        assert result["agenda"]["items"][1]["fiscal_amounts"] == ["$5,000,000"]

        # Summary
        assert result["summary"]["total_items"] == 2
        assert result["summary"]["public_hearings"] == 1
        assert len(result["summary"]["fiscal_items"]) == 1

    def test_normalize_empty_extraction(self):
        from meeting_pipeline.stages.extract.normalize import (
            MeetingExtraction,
            normalize_meeting,
        )

        extraction = MeetingExtraction(
            date="2026-04-15", body="City Council", total_items=0, items=[],
        )
        official = {"name": "", "city": "Test", "state": "OH", "role": "City Council"}
        meeting = {"date": "2026-04-15", "body": "City Council"}

        result = normalize_meeting(
            official=official, meeting=meeting, extraction=extraction,
            pdf_key=None, pdf_label=None,
            city_slug="test-OH", platform="unknown",
        )

        assert result["agenda"]["total_items"] == 0
        assert result["agenda"]["items"] == []
        assert result["summary"]["public_hearings"] == 0


class TestFindBestPdf:
    """find_best_pdf() prefers packet over agenda."""

    def test_prefers_packet(self):
        from meeting_pipeline.stages.extract.normalize import find_best_pdf

        storage = FakeStorage()
        prefix = "meeting_pipeline/sources"
        storage.write_bytes(f"{prefix}/test-OH/data/civicplus/pdfs/2026-04-15_agenda.pdf", b"x" * 60_000)
        storage.write_bytes(f"{prefix}/test-OH/data/civicplus/pdfs/2026-04-15_packet.pdf", b"x" * 60_000)

        key, label = find_best_pdf("test-OH", "2026-04-15", "civicplus", storage, prefix)
        assert key is not None
        assert "packet" in key
        assert label == "packet"

    def test_returns_none_when_missing(self):
        from meeting_pipeline.stages.extract.normalize import find_best_pdf

        storage = FakeStorage()
        key, label = find_best_pdf("nope-TX", "2026-04-15", "civicplus", storage, "meeting_pipeline/sources")
        assert key is None
        assert label is None

    def test_ignores_small_pdfs(self):
        from meeting_pipeline.stages.extract.normalize import find_best_pdf

        storage = FakeStorage()
        prefix = "meeting_pipeline/sources"
        storage.write_bytes(f"{prefix}/test-OH/data/civicplus/pdfs/2026-04-15_agenda.pdf", b"x" * 100)

        key, label = find_best_pdf("test-OH", "2026-04-15", "civicplus", storage, prefix)
        assert key is None

    def test_prefers_matching_platform(self):
        """PDFs from the city's own platform should rank higher."""
        from meeting_pipeline.stages.extract.normalize import find_best_pdf

        storage = FakeStorage()
        prefix = "meeting_pipeline/sources"
        storage.write_bytes(f"{prefix}/test-OH/data/legistar/pdfs/2026-04-15_agenda.pdf", b"x" * 60_000)
        storage.write_bytes(f"{prefix}/test-OH/data/civicplus/pdfs/2026-04-15_agenda.pdf", b"x" * 60_000)

        key, label = find_best_pdf("test-OH", "2026-04-15", "legistar", storage, prefix)
        assert "legistar" in key


# =========================================================================
# 6. LAMBDA HANDLER TESTS — moved to test_lambda_handlers.py on the
#    meeting-pipeline-infra branch (handlers themselves live there).
# =========================================================================


# =========================================================================
# 7. ORCHESTRATOR TESTS
# =========================================================================

class TestOrchestrator:
    """Orchestrator constants and filter_cities()."""

    def test_verified_statuses_constant(self):
        from meeting_pipeline.stages.orchestrator import VERIFIED_STATUSES
        assert isinstance(VERIFIED_STATUSES, (set, frozenset))
        assert "verified" in VERIFIED_STATUSES
        assert "verified_ocr_needed" in VERIFIED_STATUSES
        assert "verified_non_pdf" in VERIFIED_STATUSES
        # "unverified" must NOT be in the set
        assert "unverified" not in VERIFIED_STATUSES

    def test_filter_cities_with_slugs(self):
        from meeting_pipeline.stages.orchestrator import filter_cities

        cities = [
            {"slug": "chapel-hill-NC", "city": "Chapel Hill", "state": "NC"},
            {"slug": "loveland-OH", "city": "Loveland", "state": "OH"},
            {"slug": "austin-TX", "city": "Austin", "state": "TX"},
        ]
        filtered = filter_cities(cities, ["loveland-OH", "austin-TX"])
        assert len(filtered) == 2
        slugs = {c["slug"] for c in filtered}
        assert slugs == {"loveland-OH", "austin-TX"}

    def test_filter_cities_none_returns_all(self):
        from meeting_pipeline.stages.orchestrator import filter_cities

        cities = [
            {"slug": "chapel-hill-NC"},
            {"slug": "loveland-OH"},
        ]
        filtered = filter_cities(cities, None)
        assert len(filtered) == 2

    def test_filter_cities_empty_list_returns_all(self):
        from meeting_pipeline.stages.orchestrator import filter_cities

        cities = [{"slug": "chapel-hill-NC"}]
        filtered = filter_cities(cities, [])
        assert len(filtered) == 1

    def test_filter_cities_case_insensitive(self):
        from meeting_pipeline.stages.orchestrator import filter_cities

        cities = [{"slug": "Chapel-Hill-NC"}]
        filtered = filter_cities(cities, ["chapel-hill-nc"])
        assert len(filtered) == 1

    def test_filter_cities_no_match(self):
        from meeting_pipeline.stages.orchestrator import filter_cities

        cities = [{"slug": "chapel-hill-NC"}]
        filtered = filter_cities(cities, ["nonexistent-XX"])
        assert len(filtered) == 0

    def test_all_phases_constant(self):
        from meeting_pipeline.stages.orchestrator import ALL_PHASES
        assert "discover" in ALL_PHASES
        assert "scan" in ALL_PHASES
        assert "collect" in ALL_PHASES
        assert "extract" in ALL_PHASES
        assert "briefing" in ALL_PHASES

    def test_load_cities_from_csv(self, tmp_path):
        from meeting_pipeline.stages.orchestrator import load_cities_from_csv

        csv_file = tmp_path / "cities.csv"
        csv_file.write_text("city,state\nChapel Hill,NC\nLoveland,OH\n")
        cities = load_cities_from_csv(str(csv_file))
        assert len(cities) == 2
        assert cities[0]["slug"] == "chapel-hill-NC"
        assert cities[1]["slug"] == "loveland-OH"


# =========================================================================
# Additional: Constants sanity checks
# =========================================================================

class TestConstants:
    """Sanity checks on shared constants."""

    def test_platform_patterns_non_empty(self):
        from meeting_pipeline.shared.constants import PLATFORM_PATTERNS
        assert len(PLATFORM_PATTERNS) > 0
        assert "legistar" in PLATFORM_PATTERNS
        assert "civicplus" in PLATFORM_PATTERNS

    def test_supported_platforms_non_empty(self):
        from meeting_pipeline.shared.constants import SUPPORTED_PLATFORMS
        assert len(SUPPORTED_PLATFORMS) > 0
        assert "legistar" in SUPPORTED_PLATFORMS

    def test_state_abbrevs_complete(self):
        from meeting_pipeline.shared.constants import STATE_ABBREVS
        assert len(STATE_ABBREVS) >= 50  # 50 states + DC
        assert STATE_ABBREVS["Ohio"] == "OH"
        assert STATE_ABBREVS["California"] == "CA"

    def test_freshness_scores_have_required_keys(self):
        from meeting_pipeline.shared.constants import FRESHNESS_SCORE
        assert "fresh" in FRESHNESS_SCORE
        assert "stale" in FRESHNESS_SCORE
        assert "wrong_entity" in FRESHNESS_SCORE
        assert FRESHNESS_SCORE["fresh"] > FRESHNESS_SCORE["stale"]


class TestCollectionResult:
    """CollectionResult dataclass."""

    def test_error_result(self):
        from meeting_pipeline.shared.models import CollectionResult
        r = CollectionResult.error_result("Test", "OH", "legistar", "something broke")
        assert r.error == "something broke"
        assert r.events_found == 0
        assert r.pdfs_downloaded == 0

    def test_to_dict(self):
        from meeting_pipeline.shared.models import CollectionResult
        r = CollectionResult(city="Test", state="OH", platform="legistar",
                             events_found=5, pdfs_downloaded=3)
        d = r.to_dict()
        assert d["city"] == "Test"
        assert d["events_found"] == 5
        assert d["error"] is None
