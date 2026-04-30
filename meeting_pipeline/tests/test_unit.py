"""
Unit tests for individual functions in the meeting pipeline.

These tests verify current behavior BEFORE refactoring, so we can confirm
nothing breaks when we consolidate duplicates, remove hacks, etc.
"""

import json
from io import StringIO
from unittest.mock import patch

# ============================================================================
# url_utils — detect_platform
# ============================================================================

class TestDetectPlatform:
    def test_legistar(self):
        from meeting_pipeline.shared.url_utils import detect_platform
        assert detect_platform("https://webapi.legistar.com/v1/foo/events") == "legistar"

    def test_civicplus(self):
        from meeting_pipeline.shared.url_utils import detect_platform
        assert detect_platform("https://www.cityofx.com/AgendaCenter") == "civicplus"

    def test_granicus(self):
        from meeting_pipeline.shared.url_utils import detect_platform
        assert detect_platform("https://cityname.granicus.com/ViewPublisher.php") == "granicus"

    def test_boarddocs(self):
        from meeting_pipeline.shared.url_utils import detect_platform
        assert detect_platform("https://go.boarddocs.com/oh/canton/Board.nsf") == "boarddocs"

    def test_unknown(self):
        from meeting_pipeline.shared.url_utils import detect_platform
        assert detect_platform("https://www.example.com/meetings") == "unknown"

    def test_escribe(self):
        from meeting_pipeline.shared.url_utils import detect_platform
        assert detect_platform("https://pub-burnaby.escribemeetings.com") == "escribe"

    def test_case_insensitive(self):
        from meeting_pipeline.shared.url_utils import detect_platform
        assert detect_platform("https://WEBAPI.LEGISTAR.COM/v1/foo") == "legistar"


# ============================================================================
# url_utils — normalize_platform_url
# ============================================================================

class TestNormalizePlatformUrl:
    def test_escribe_strips_path(self):
        from meeting_pipeline.shared.url_utils import normalize_platform_url
        url = "https://pub-burnaby.escribemeetings.com/Meeting.aspx?Id=123"
        result = normalize_platform_url(url, "escribe")
        assert result == "https://pub-burnaby.escribemeetings.com"

    def test_granicus_adds_viewpublisher(self):
        from meeting_pipeline.shared.url_utils import normalize_platform_url
        url = "https://cityname.granicus.com/GeneratedAgendaViewer.php?view_id=1"
        result = normalize_platform_url(url, "granicus")
        assert result == "https://cityname.granicus.com/ViewPublisher.php"

    def test_granicus_already_has_viewpublisher(self):
        from meeting_pipeline.shared.url_utils import normalize_platform_url
        url = "https://cityname.granicus.com/ViewPublisher.php?view_id=1"
        result = normalize_platform_url(url, "granicus")
        assert result == url  # unchanged

    def test_other_platform_unchanged(self):
        from meeting_pipeline.shared.url_utils import normalize_platform_url
        url = "https://legistar.com/something"
        result = normalize_platform_url(url, "legistar")
        assert result == url


# ============================================================================
# url_utils — is_wrong_entity
# ============================================================================

class TestIsWrongEntity:
    def test_school_board(self):
        from meeting_pipeline.shared.url_utils import is_wrong_entity
        assert is_wrong_entity("Springfield School Board Meeting") is True

    def test_county_commission(self):
        from meeting_pipeline.shared.url_utils import is_wrong_entity
        assert is_wrong_entity("County Commission Regular Meeting") is True

    def test_city_council(self):
        from meeting_pipeline.shared.url_utils import is_wrong_entity
        assert is_wrong_entity("City Council Regular Meeting") is False

    def test_case_insensitive(self):
        from meeting_pipeline.shared.url_utils import is_wrong_entity
        assert is_wrong_entity("SCHOOL DISTRICT Budget Meeting") is True


# ============================================================================
# url_utils — is_wrong_city
# ============================================================================

class TestIsWrongCity:
    def test_correct_city(self):
        from meeting_pipeline.shared.url_utils import is_wrong_city
        assert is_wrong_city(
            "https://www.durham.gov/meetings", "Durham City Council", "Durham", "NC"
        ) is False

    def test_wrong_entity_in_url(self):
        from meeting_pipeline.shared.url_utils import is_wrong_city
        assert is_wrong_city(
            "https://www.example.com/school-board", "School Board", "Durham", "NC"
        ) is True

    def test_wrong_domain_pattern(self):
        from meeting_pipeline.shared.url_utils import is_wrong_city
        assert is_wrong_city(
            "https://springfield.k12.oh.us/board", "Board Meeting", "Springfield", "OH"
        ) is True

    def test_city_specific_pattern(self):
        from meeting_pipeline.shared.url_utils import is_wrong_city
        # El Paso IL pattern should flag for El Paso TX
        assert is_wrong_city(
            "https://elpasoil.gov/meetings", "Council Meeting", "El Paso", "TX"
        ) is True

    def test_different_state_name_in_text(self):
        from meeting_pipeline.shared.url_utils import is_wrong_city
        # A "california" mention for an OH city should flag
        assert is_wrong_city(
            "https://example.com", "California City Council", "Springfield", "OH"
        ) is True

    def test_gov_domain_wrong_state(self):
        from meeting_pipeline.shared.url_utils import is_wrong_city
        # A .gov domain ending with different state abbrev
        assert is_wrong_city(
            "https://cityca.gov/meetings", "Council Meeting", "Springfield", "OH"
        ) is True

    def test_no_state_no_flag(self):
        from meeting_pipeline.shared.url_utils import is_wrong_city
        # Without state, skip state-based checks
        assert is_wrong_city(
            "https://example.com/council", "Regular Meeting", "Springfield"
        ) is False


# ============================================================================
# url_utils — is_non_agenda_url
# ============================================================================

class TestIsNonAgendaUrl:
    def test_normal_url(self):
        from meeting_pipeline.shared.url_utils import is_non_agenda_url
        assert is_non_agenda_url("https://www.durhamcity.gov/AgendaCenter") is False

    def test_tv_domain(self):
        from meeting_pipeline.shared.url_utils import is_non_agenda_url
        assert is_non_agenda_url("https://news.tv/meetings") is True


# ============================================================================
# config — city_to_slug
# ============================================================================

class TestCityToSlug:
    def test_basic(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("Chapel Hill", "NC") == "chapel-hill-NC"

    def test_preserves_state_case(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("durham", "nc") == "durham-NC"

    def test_removes_dots(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("St. Louis", "MO") == "st-louis-MO"

    def test_removes_apostrophes(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("O'Fallon", "IL") == "ofallon-IL"

    def test_multiword(self):
        from meeting_pipeline.shared.config import city_to_slug
        assert city_to_slug("Canal Winchester", "OH") == "canal-winchester-OH"


# ============================================================================
# notification_log — log_event
# ============================================================================

class TestLogEvent:
    def test_returns_payload(self):
        from meeting_pipeline.shared.notification_log import COLLECTION_SUCCESS, log_event
        result = log_event(COLLECTION_SUCCESS, "Durham", "NC", url="https://example.com")
        assert result["event_type"] == "COLLECTION_SUCCESS"
        assert result["city"] == "Durham"
        assert result["state"] == "NC"
        assert result["url"] == "https://example.com"
        assert "ts" in result

    def test_emits_to_stderr(self):
        from meeting_pipeline.shared.notification_log import DISCOVERY_STARTED, log_event
        captured = StringIO()
        with patch("sys.stderr", captured):
            log_event(DISCOVERY_STARTED, "Austin", "TX")
        output = captured.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["event_type"] == "DISCOVERY_STARTED"
        assert parsed["city"] == "Austin"

    def test_detail_kwargs(self):
        from meeting_pipeline.shared.notification_log import COLLECTION_FAILED, log_event
        result = log_event(COLLECTION_FAILED, "Cleveland", "OH", reason="timeout", attempts=3)
        assert result["reason"] == "timeout"
        assert result["attempts"] == 3


# ============================================================================
# body_validation — score_body_match
# ============================================================================

class TestScoreBodyMatch:
    def test_exact_match(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("City Council", "City Council") == 100

    def test_case_insensitive_exact(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("city council", "City Council") == 100

    def test_expected_in_candidate(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("Regular City Council Meeting", "City Council") == 80

    def test_candidate_in_expected(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("Council", "City Council Meeting") == 70

    def test_shared_governing_keyword(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("Town Council Regular", "Town Council Special") == 50

    def test_governing_keyword_in_candidate_only(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("Board of Trustees", "Finance Committee") == 30

    def test_reject_advisory(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("Advisory Board", "City Council") == -1

    def test_reject_planning(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("Planning Commission", "City Council") == -1

    def test_no_match(self):
        from meeting_pipeline.shared.body_validation import score_body_match
        assert score_body_match("Finance Committee", "City Council") == 0


# ============================================================================
# body_validation — best_body_match
# ============================================================================

class TestBestBodyMatch:
    def test_picks_highest_score(self):
        from meeting_pipeline.shared.body_validation import best_body_match
        candidates = ["Advisory Board", "City Council", "Planning Commission"]
        best, score = best_body_match(candidates, "City Council")
        assert best == "City Council"
        assert score == 100

    def test_all_rejected(self):
        from meeting_pipeline.shared.body_validation import best_body_match
        candidates = ["Advisory Board", "Planning Commission"]
        best, score = best_body_match(candidates, "City Council")
        assert best is None
        assert score == -1

    def test_empty_candidates(self):
        from meeting_pipeline.shared.body_validation import best_body_match
        best, score = best_body_match([], "City Council")
        assert best is None
        assert score == -1

    def test_skips_empty_strings(self):
        from meeting_pipeline.shared.body_validation import best_body_match
        candidates = ["", "City Council", ""]
        best, score = best_body_match(candidates, "City Council")
        assert best == "City Council"
        assert score == 100


# ============================================================================
# manifest — _is_wrong_entity
# ============================================================================

class TestManifestIsWrongEntity:
    def test_school_district(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("springfield school district") is True

    def test_board_of_education(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("board of education meeting") is True

    def test_city_council(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("city council regular meeting") is False

    def test_superintendent(self):
        from meeting_pipeline.shared.manifest import _is_wrong_entity
        assert _is_wrong_entity("superintendent report") is True


# ============================================================================
# manifest — validate_against_manifest
# ============================================================================

class TestValidateAgainstManifest:
    def test_valid_body_match(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council", "expected_city": "Durham"}
        is_valid, reason = validate_against_manifest(manifest, ["City Council Regular Meeting"])
        assert is_valid is True
        assert reason is None

    def test_all_wrong_entity(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council", "expected_city": "Durham"}
        is_valid, reason = validate_against_manifest(manifest, ["School Board", "Board of Education"])
        assert is_valid is False
        assert "wrong entity" in reason.lower()

    def test_governing_body_synonym_accepted(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council", "expected_city": "Manchester"}
        # "Board of Mayor and Aldermen" should match because both contain governing synonyms
        is_valid, reason = validate_against_manifest(manifest, ["Board of Mayor and Aldermen"])
        assert is_valid is True

    def test_no_match(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        manifest = {"expected_body": "City Council", "expected_city": "Durham"}
        is_valid, reason = validate_against_manifest(manifest, ["Finance Committee", "Parks Board"])
        assert is_valid is False
        assert "No collected body matches" in reason

    def test_empty_manifest(self):
        from meeting_pipeline.shared.manifest import validate_against_manifest
        is_valid, reason = validate_against_manifest({}, ["City Council"])
        assert is_valid is True  # No constraints = valid


# ============================================================================
# main_flow — _is_council_body
# ============================================================================

class TestIsCouncilBody:
    def test_city_council(self):
        from meeting_pipeline.stages.discover.main_flow import _is_council_body
        assert _is_council_body("City Council") is True

    def test_board_of_trustees(self):
        from meeting_pipeline.stages.discover.main_flow import _is_council_body
        assert _is_council_body("Board of Trustees") is True

    def test_planning_commission(self):
        from meeting_pipeline.stages.discover.main_flow import _is_council_body
        assert _is_council_body("Planning Commission") is False

    def test_select_board(self):
        from meeting_pipeline.stages.discover.main_flow import _is_council_body
        assert _is_council_body("Select Board") is True

    def test_case_insensitive(self):
        from meeting_pipeline.stages.discover.main_flow import _is_council_body
        assert _is_council_body("CITY COUNCIL") is True


# ============================================================================
# date_utils — parse_date_from_filename
# ============================================================================

class TestParseDateFromFilename:
    def test_mdy_dashes(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import parse_date_from_filename
        assert parse_date_from_filename("agenda_04-15-2026.pdf") == date(2026, 4, 15)

    def test_ymd_dashes(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import parse_date_from_filename
        assert parse_date_from_filename("2026-04-15_council.pdf") == date(2026, 4, 15)

    def test_mdy_dots(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import parse_date_from_filename
        assert parse_date_from_filename("agenda_4.15.2026.pdf") == date(2026, 4, 15)

    def test_two_digit_year(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import parse_date_from_filename
        assert parse_date_from_filename("agenda_04-15-26.pdf") == date(2026, 4, 15)

    def test_no_date(self):
        from meeting_pipeline.shared.date_utils import parse_date_from_filename
        assert parse_date_from_filename("council_minutes.pdf") is None

    def test_out_of_range(self):
        from meeting_pipeline.shared.date_utils import parse_date_from_filename
        # 2019 is before 2020 cutoff
        assert parse_date_from_filename("agenda_01-01-2019.pdf") is None


# ============================================================================
# date_utils — extract_dates
# ============================================================================

class TestExtractDates:
    def test_mm_dd_yyyy(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import extract_dates
        dates = extract_dates("Meeting on 4/15/2026", today=date(2026, 4, 20))
        assert date(2026, 4, 15) in dates

    def test_month_name(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import extract_dates
        dates = extract_dates("April 15, 2026", today=date(2026, 4, 20))
        assert date(2026, 4, 15) in dates

    def test_iso_format(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import extract_dates
        dates = extract_dates("Date: 2026-04-15", today=date(2026, 4, 20))
        assert date(2026, 4, 15) in dates

    def test_empty_text(self):
        from meeting_pipeline.shared.date_utils import extract_dates
        assert extract_dates("") == []

    def test_future_within_range(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import extract_dates
        # A date 100 days in the future should be included
        dates = extract_dates("Meeting on 7/28/2026", today=date(2026, 4, 20))
        assert date(2026, 7, 28) in dates


# ============================================================================
# date_utils — classify_freshness
# ============================================================================

class TestClassifyFreshness:
    def test_fresh(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import classify_freshness
        assert classify_freshness(date(2026, 4, 20), today=date(2026, 4, 25)) == "fresh"

    def test_stale(self):
        from datetime import date

        from meeting_pipeline.shared.date_utils import classify_freshness
        assert classify_freshness(date(2025, 1, 1), today=date(2026, 4, 25)) == "stale"

    def test_unknown(self):
        from meeting_pipeline.shared.date_utils import classify_freshness
        assert classify_freshness(None) == "unknown"


# ============================================================================
# verify_source — _check_pdf_content date extraction
# ============================================================================

class TestCheckPdfContentDates:
    def test_returns_most_recent_date(self):
        # Create a minimal PDF with a date in the text
        import fitz

        from meeting_pipeline.shared.verify_source import _check_pdf_content
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), (
            "City Council Regular Meeting Agenda\nApril 15, 2026\n"
            "I. Roll Call and Pledge of Allegiance\n"
            "II. Motion to Approve Minutes of the Previous Council Meeting\n"
            "III. Public Hearing on Proposed Ordinance Number 2026-04\n"
            "IV. Resolution to Approve the Annual City Budget for Fiscal Year 2027\n"
            "V. Consent Agenda Items for Council Review and Vote\n"
            "VI. Adjournment of the Regular Meeting Session\n"
        ))
        pdf_bytes = doc.tobytes()
        result = _check_pdf_content(pdf_bytes)
        assert result["most_recent_date"] is not None
        assert "2026" in result["most_recent_date"]
        assert result["is_agenda"] is True

    def test_returns_none_when_no_dates(self):
        import fitz

        from meeting_pipeline.shared.verify_source import _check_pdf_content
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "City Council Meeting Agenda\nRoll Call\nApprove Minutes")
        pdf_bytes = doc.tobytes()
        result = _check_pdf_content(pdf_bytes)
        assert result["most_recent_date"] is None

    def test_returns_none_for_invalid_pdf(self):
        from meeting_pipeline.shared.verify_source import _check_pdf_content
        result = _check_pdf_content(b"not a pdf")
        assert result["most_recent_date"] is None
