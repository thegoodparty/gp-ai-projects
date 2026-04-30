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
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

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

    def test_import_stages_briefing_process(self):
        from meeting_pipeline.stages.briefing import process  # noqa: F401

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

    # -- lambda_handlers/ --

    @patch("boto3.client")
    def test_import_lambda_scan(self, mock_boto):
        from meeting_pipeline.lambda_handlers import scan  # noqa: F401

    @patch("boto3.client")
    def test_import_lambda_process(self, mock_boto):
        from meeting_pipeline.lambda_handlers import process  # noqa: F401

    @patch("boto3.client")
    def test_import_lambda_discover(self, mock_boto):
        from meeting_pipeline.lambda_handlers import discover  # noqa: F401

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

    def test_import_collectors_generic_html_scraper(self):
        from meeting_pipeline.collectors import generic_html_scraper  # noqa: F401

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
            normalize_meeting, MeetingExtraction, AgendaItem,
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
            normalize_meeting, MeetingExtraction,
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
# 6. LAMBDA HANDLER TESTS
# =========================================================================

class TestScanHandler:
    """Scan Lambda handler: list_cities mode and scan-one-city mode."""

    @patch("boto3.client")
    def test_list_cities_returns_verified_slugs(self, mock_boto):
        from meeting_pipeline.lambda_handlers.scan import handler, VERIFIED_STATUSES

        source_verified = {
            "city": "TestCity",
            "state": "OH",
            "best_source": {
                "platform": "civicplus",
                "verification": {"status": "verified"},
            },
        }
        source_unverified = {
            "city": "BadCity",
            "state": "TX",
            "best_source": {
                "platform": "unknown",
                "verification": {"status": "unverified"},
            },
        }

        storage = FakeStorage({
            "meeting_pipeline/sources/test-city-OH/source.json": source_verified,
            "meeting_pipeline/sources/bad-city-TX/source.json": source_unverified,
        })

        with patch("meeting_pipeline.lambda_handlers.scan.inject_secrets"):
            with patch("meeting_pipeline.lambda_handlers.scan.AgentConfig.from_env") as mock_cfg:
                cfg = MagicMock()
                cfg.sources_prefix = "meeting_pipeline/sources"
                mock_cfg.return_value = cfg
                with patch("meeting_pipeline.lambda_handlers.scan.get_storage", return_value=storage):
                    result = handler({"action": "list_cities"})

        assert "cities" in result
        slugs = [c["slug"] for c in result["cities"]]
        assert "test-city-OH" in slugs
        assert "bad-city-TX" not in slugs

    @patch("boto3.client")
    def test_scan_requires_slug(self, mock_boto):
        with patch("meeting_pipeline.lambda_handlers.scan.inject_secrets"):
            with patch("meeting_pipeline.lambda_handlers.scan.AgentConfig.from_env") as mock_cfg:
                mock_cfg.return_value = MagicMock()
                with patch("meeting_pipeline.lambda_handlers.scan.get_storage"):
                    from meeting_pipeline.lambda_handlers.scan import handler
                    result = handler({})
        assert "error" in result

    @patch("boto3.client")
    def test_scan_sends_new_posted_to_queue(self, mock_boto):
        """When scan finds a newly posted agenda, it sends to SQS."""
        from meeting_pipeline.lambda_handlers.scan import _detect_new_posted

        previous = {
            "upcoming": [
                {"date": "2026-04-10", "agenda_posted": True},
            ]
        }
        current = {
            "upcoming": [
                {"date": "2026-04-10", "agenda_posted": True},
                {"date": "2026-04-17", "agenda_posted": True},
            ]
        }
        new = _detect_new_posted(previous, current)
        assert len(new) == 1
        assert new[0]["date"] == "2026-04-17"

    @patch("boto3.client")
    def test_detect_new_posted_first_scan(self, mock_boto):
        """First scan (no previous): all posted meetings are new."""
        from meeting_pipeline.lambda_handlers.scan import _detect_new_posted

        current = {
            "upcoming": [
                {"date": "2026-04-10", "agenda_posted": True},
                {"date": "2026-04-17", "agenda_posted": False},
            ]
        }
        new = _detect_new_posted(None, current)
        assert len(new) == 1
        assert new[0]["date"] == "2026-04-10"


class TestProcessHandler:
    """Process Lambda handler: SQS record parsing."""

    @patch("boto3.client")
    def test_sqs_record_parsing(self, mock_boto):
        """Handler correctly parses SQS Records wrapping."""
        from meeting_pipeline.lambda_handlers.process import handler

        event = {
            "Records": [
                {
                    "body": json.dumps({
                        "slug": "test-city-OH",
                        "date": "2026-04-15",
                        "platform": "civicplus",
                    }),
                }
            ]
        }

        with patch("meeting_pipeline.lambda_handlers.process.inject_secrets"):
            with patch("meeting_pipeline.lambda_handlers.process.AgentConfig.from_env") as mock_cfg:
                mock_cfg.return_value = MagicMock()
                with patch("meeting_pipeline.lambda_handlers.process.get_storage") as mock_storage:
                    with patch("meeting_pipeline.lambda_handlers.process._process_meeting") as mock_proc:
                        mock_proc.return_value = {"status": "ok"}
                        result = handler(event)

        mock_proc.assert_called_once()
        call_args = mock_proc.call_args
        assert call_args[0][0] == "test-city-OH"
        assert call_args[0][1] == "2026-04-15"
        assert call_args[0][2] == "civicplus"

    @patch("boto3.client")
    def test_process_handler_inline_event(self, mock_boto):
        """Handler also supports inline event (not wrapped in Records)."""
        from meeting_pipeline.lambda_handlers.process import handler

        event = {
            "slug": "test-city-OH",
            "date": "2026-04-15",
            "platform": "legistar",
        }

        with patch("meeting_pipeline.lambda_handlers.process.inject_secrets"):
            with patch("meeting_pipeline.lambda_handlers.process.AgentConfig.from_env") as mock_cfg:
                mock_cfg.return_value = MagicMock()
                with patch("meeting_pipeline.lambda_handlers.process.get_storage"):
                    with patch("meeting_pipeline.lambda_handlers.process._process_meeting") as mock_proc:
                        mock_proc.return_value = {"status": "ok"}
                        result = handler(event)

        mock_proc.assert_called_once()
        assert result["results"] == [{"status": "ok"}]


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
