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


# ============================================================================
# briefing.generate — _normalize_amount + check_fiscal_amounts (bug fix:
# substring match let LLM digit-padding hallucinations slip through)
# ============================================================================

class TestNormalizeAmount:
    def test_strips_dollar_and_commas(self):
        from meeting_pipeline.stages.briefing.generate import _normalize_amount
        assert _normalize_amount("$1,000") == "1000"

    def test_strips_trailing_zero_decimal(self):
        from meeting_pipeline.stages.briefing.generate import _normalize_amount
        assert _normalize_amount("$1,000.00") == "1000"
        assert _normalize_amount("$1000.0") == "1000"

    def test_preserves_non_zero_decimal(self):
        from meeting_pipeline.stages.briefing.generate import _normalize_amount
        assert _normalize_amount("$1,000.50") == "1000.50"

    def test_lowercases_million_suffix(self):
        from meeting_pipeline.stages.briefing.generate import _normalize_amount
        assert _normalize_amount("$1.5 Million") == "1.5million"


class TestCheckFiscalAmountsExactness:
    """Regression: the old guardrail used `norm in s or s in norm`, so any
    digit-prefix substring (e.g. source $1,000 vs briefing $1,000,000) was
    treated as a match and hallucinated digit-padding slipped through."""

    def _briefing_with_amount(self, amount_str):
        return {
            "priorityIssues": [
                {"detail": {"whatIsHappening": f"Approve {amount_str} for X."}}
            ]
        }

    def _cards_with_source(self, source_text):
        from meeting_pipeline.stages.briefing.generate import (
            BriefingCards, PriorityIssueCard, SourceSection,
        )
        # SourceSection (text + label) supplies sourceSections
        cards = BriefingCards(
            executiveHeadline="x",
            executiveSubheadline="y",
            priorityIssues=[
                PriorityIssueCard(
                    agendaItemTitle="t",
                    slug="t",
                    sourcePassage=source_text,
                    sourceSections=[SourceSection(label="Memo", text=source_text)],
                    headline="h",
                    whatYouNeedToDo="w",
                    askThisInTheRoom="a",
                )
            ],
        )
        return cards

    def test_digit_padding_now_flagged(self):
        # Source has $1,000 — briefing has $1,000,000. Substring match used to
        # pass this. With exact equality, it's flagged.
        from meeting_pipeline.stages.briefing.generate import check_fiscal_amounts
        cards = self._cards_with_source("Total cost: $1,000.")
        briefing = self._briefing_with_amount("$1,000,000")
        warnings = check_fiscal_amounts(briefing, cards)
        assert any("$1,000,000" in w for w in warnings), \
            f"expected digit-padding hallucination to be flagged, got {warnings!r}"

    def test_exact_match_passes(self):
        from meeting_pipeline.stages.briefing.generate import check_fiscal_amounts
        cards = self._cards_with_source("Total cost: $1,000.")
        briefing = self._briefing_with_amount("$1,000")
        assert check_fiscal_amounts(briefing, cards) == []

    def test_canonical_decimal_equality(self):
        # $1,000.00 in source ↔ $1,000 in briefing — should match after canonicalization
        from meeting_pipeline.stages.briefing.generate import check_fiscal_amounts
        cards = self._cards_with_source("Total cost: $1,000.00.")
        briefing = self._briefing_with_amount("$1,000")
        assert check_fiscal_amounts(briefing, cards) == []


# ============================================================================
# escribe scanner — date filter must use LOOKBACK_DAYS as lower bound
# (bug fix: filter was today_str <= date <= cutoff_str, which dropped all
# past meetings and made the PastMeetings fallback dead code)
# ============================================================================

class TestEscribeLookbackBounds:
    async def test_admits_past_within_lookback_window(self):
        from datetime import datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock, patch

        from meeting_pipeline.shared.constants import LOOKAHEAD_DAYS, LOOKBACK_DAYS
        from meeting_pipeline.stages.scan.platforms import escribe as escribe_mod

        today = datetime.now()
        recent_past = (today - timedelta(days=10)).strftime("%Y-%m-%d")
        far_future = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        too_far_past = (today - timedelta(days=LOOKBACK_DAYS + 30)).strftime("%Y-%m-%d")
        too_far_future = (today + timedelta(days=LOOKAHEAD_DAYS + 30)).strftime("%Y-%m-%d")

        meetings_payload = [
            {"DateShort": recent_past, "Id": 1},
            {"DateShort": far_future, "Id": 2},
            {"DateShort": too_far_past, "Id": 3},
            {"DateShort": too_far_future, "Id": 4},
        ]

        # Mock httpx.AsyncClient: get() and post() both return a response
        # whose .json() yields the payload above.
        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.json.return_value = {"d": {"Meetings": meetings_payload}}

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = False
        fake_client.get = AsyncMock(return_value=fake_response)
        fake_client.post = AsyncMock(return_value=fake_response)

        config = {"meeting_view_id": "1", "meeting_types": ["Council"]}

        with patch.object(escribe_mod.httpx, "AsyncClient", return_value=fake_client):
            result = await escribe_mod.scan_escribe(
                "test-city", config, "https://example.com", client=None
            )

        dates = [r["date"] for r in result]
        assert recent_past in dates, "regression: past meetings within LOOKBACK_DAYS should be admitted"
        assert far_future in dates, "future within LOOKAHEAD_DAYS should be admitted"
        assert too_far_past not in dates
        assert too_far_future not in dates


# ============================================================================
# orchestrator — skip-existing-briefings filter must compare exact basenames
# (bug fix: substring `in` check was dropping smaller-slug siblings, e.g.
# canton-OH skipped because north-canton-OH had a briefing for the same date)
# ============================================================================

class TestSkipExistingBriefingsExactMatch:
    """Reproduces the substring-skip bug by simulating the filter logic
    inline. The fix: compare exact basenames, not substring containment."""

    def _filter_with_substring(self, norm_keys, existing_names):
        # OLD broken logic: `basename without .json` is a substring of `briefing_basename`
        return [
            k for k in norm_keys
            if not any(k.split("/")[-1].replace(".json", "") in bk for bk in existing_names)
        ]

    def _filter_with_exact_basename(self, norm_keys, existing_names):
        # NEW logic: build the expected briefing basename and check membership
        return [
            k for k in norm_keys
            if f"{k.split('/')[-1].removesuffix('.json')}_briefing.json" not in existing_names
        ]

    def test_substring_filter_drops_canton_when_north_canton_exists(self):
        """Demonstrates the bug — old code would falsely skip canton-OH."""
        norm_keys = [
            "meeting_pipeline/output/normalized/canton-OH_2026-04-15.json",
        ]
        existing_names = {
            "north-canton-OH_2026-04-15_briefing.json",
        }
        result = self._filter_with_substring(norm_keys, existing_names)
        # Old bug: canton-OH dropped because its basename is a substring of
        # north-canton-OH_2026-04-15_briefing.json
        assert result == [], "old substring logic incorrectly drops canton-OH"

    def test_exact_filter_keeps_canton_when_only_north_canton_exists(self):
        """The fixed filter retains canton-OH because its actual briefing
        file (canton-OH_2026-04-15_briefing.json) doesn't exist."""
        norm_keys = [
            "meeting_pipeline/output/normalized/canton-OH_2026-04-15.json",
        ]
        existing_names = {
            "north-canton-OH_2026-04-15_briefing.json",
        }
        result = self._filter_with_exact_basename(norm_keys, existing_names)
        assert result == norm_keys, "canton-OH should not be skipped"

    def test_exact_filter_skips_when_briefing_actually_exists(self):
        """Sanity: when the actual briefing file IS present, the entry is skipped."""
        norm_keys = [
            "meeting_pipeline/output/normalized/canton-OH_2026-04-15.json",
        ]
        existing_names = {
            "canton-OH_2026-04-15_briefing.json",
            "north-canton-OH_2026-04-15_briefing.json",
        }
        result = self._filter_with_exact_basename(norm_keys, existing_names)
        assert result == []


# ============================================================================
# scripts/tools/check_city — SUPPORTED_PLATFORMS must use canonical keys
# (bug fix: was using "escribemeetings" and missing boarddocs/municode/novus)
# ============================================================================

class TestCheckCitySupportedPlatforms:
    def test_uses_canonical_escribe_key(self):
        from meeting_pipeline.scripts.tools.check_city import SUPPORTED_PLATFORMS
        # Wrong: "escribemeetings". Right: "escribe" (matches shared.constants).
        assert "escribe" in SUPPORTED_PLATFORMS
        assert "escribemeetings" not in SUPPORTED_PLATFORMS

    def test_includes_all_platforms_with_collectors(self):
        from meeting_pipeline.scripts.tools.check_city import SUPPORTED_PLATFORMS
        for platform in ("civicclerk", "civicplus", "granicus", "legistar",
                         "escribe", "boarddocs", "municode", "novus"):
            assert platform in SUPPORTED_PLATFORMS, f"missing {platform}"


# ============================================================================
# scan/platforms/granicus — Swagit fallback must accept abbreviated month
# names like "Apr 21 2026" (was %B-only, dropping ~11/12 of meetings)
# ============================================================================

class TestSwagitDateAbbreviated:
    def test_full_month_name_parses(self):
        from datetime import datetime
        # Verify the format we kept handling
        assert datetime.strptime("April 21 2026", "%B %d %Y").date().isoformat() == "2026-04-21"

    def test_abbreviated_month_name_parses(self):
        from datetime import datetime
        # The new fallback — was failing under %B-only logic
        assert datetime.strptime("Apr 21 2026", "%b %d %Y").date().isoformat() == "2026-04-21"

    def test_format_fallback_handles_both_forms(self):
        """The fix is a try-%B-then-%b loop. Verify it accepts both forms."""
        from datetime import datetime
        for raw in ("April 21 2026", "Apr 21 2026"):
            dt = None
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            assert dt is not None, f"format-fallback should accept {raw!r}"
            assert dt.date().isoformat() == "2026-04-21"


# ============================================================================
# scan/platforms/boarddocs — must read lowercase BoardDocs JSON fields
# (numberdate, name, unique), not Legistar field names (EventComment etc.)
# ============================================================================

class TestBoarddocsScanFields:
    async def test_reads_name_unique_and_constructs_agenda_url(self):
        from datetime import datetime, timedelta
        from unittest.mock import AsyncMock, patch
        from meeting_pipeline.stages.scan.platforms import boarddocs as bd_mod

        today = datetime.now().date()
        future_d = today + timedelta(days=14)
        future_numberdate = future_d.strftime("%Y%m%d")  # BoardDocs format

        meetings_payload = [
            {
                "numberdate": future_numberdate + "120000",
                "name": "Regular City Council Meeting",
                "unique": "ABC1XYZ",
            },
        ]
        committees = [{"id": "C1", "name": "City Council"}]

        # scan_boarddocs lazy-imports these from the collectors module — patch there.
        with patch("meeting_pipeline.collectors.boarddocs._fetch_committees",
                   new=AsyncMock(return_value=committees)), \
             patch("meeting_pipeline.collectors.boarddocs._fetch_meetings",
                   new=AsyncMock(return_value=meetings_payload)):
            result = await bd_mod.scan_boarddocs(
                city="TestCity",
                config={},
                source_url="https://go.boarddocs.com/oh/testcity/Board.nsf/Public",
                client=None,
            )

        assert len(result) == 1
        m = result[0]
        assert m["title"] == "Regular City Council Meeting", "should use 'name' field, not EventComment"
        assert m["event_id"] == "ABC1XYZ", "should use 'unique' field, not EventId"
        assert m["agenda_url"] == "https://go.boarddocs.com/oh/testcity/Board.nsf/goto?open&id=ABC1XYZ"
        assert m["agenda_posted"] is True


# ============================================================================
# scan/platforms/{novus,municode} — status must reflect past vs upcoming
# (bug fix: both were hardcoding "upcoming" regardless of date)
# ============================================================================

class TestNovusMunicodeStatus:
    def _make_module_meeting(self, days_offset: int) -> dict:
        from datetime import datetime, timedelta
        d = (datetime.now() + timedelta(days=days_offset)).strftime("%Y-%m-%d")
        return {"date": d, "title": "Council", "agendaUrl": "https://x/agenda.pdf"}

    async def test_novus_marks_past_meetings_past(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from meeting_pipeline.stages.scan.platforms import novus as novus_mod
        past = self._make_module_meeting(-7)
        future = self._make_module_meeting(14)

        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.text = "<html></html>"  # ignored — _parse_meetings is patched
        fake_client = MagicMock()
        fake_client.get = AsyncMock(return_value=fake_response)

        # scan_novus lazy-imports _parse_meetings from collectors.novus_scraper
        with patch("meeting_pipeline.collectors.novus_scraper._parse_meetings",
                   return_value=[past, future]):
            result = await novus_mod.scan_novus("Kyle", {}, "https://kyle.novusagenda.com/agendapublic", fake_client)

        statuses = {m["date"]: m["status"] for m in result}
        assert statuses[past["date"]] == "past"
        assert statuses[future["date"]] == "upcoming"

    async def test_municode_marks_past_meetings_past(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from meeting_pipeline.stages.scan.platforms import municode as muni_mod
        past = self._make_module_meeting(-7)
        future = self._make_module_meeting(14)

        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.text = "<html></html>"
        fake_client = MagicMock()
        fake_client.get = AsyncMock(return_value=fake_response)

        # scan_municode lazy-imports _parse_meetings from collectors.municode
        with patch("meeting_pipeline.collectors.municode._parse_meetings",
                   return_value=[past, future]):
            result = await muni_mod.scan_municode("Austell", {}, "https://example.com", fake_client)

        statuses = {m["date"]: m["status"] for m in result}
        assert statuses[past["date"]] == "past"
        assert statuses[future["date"]] == "upcoming"


# ============================================================================
# eSCRIBE PDFs must include the meeting date in the filename so find_best_pdf
# can match them. Was previously meeting_{event_id}_doc_{doc_id}.pdf — invisible.
# ============================================================================

class TestEscribePdfFilename:
    async def test_filename_includes_meeting_date(self):
        from unittest.mock import AsyncMock, MagicMock
        from meeting_pipeline.collectors.escribemeetings import (
            EscribeConfig, _download_meeting_pdfs,
        )

        # Capture write_bytes calls to inspect the storage key
        captured_keys: list[str] = []
        storage = MagicMock()
        storage.exists.return_value = False
        storage.write_bytes = lambda key, _data: captured_keys.append(key)

        # Fake httpx response — valid PDF magic
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.content = b"%PDF-1.4 fake content"
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_response)

        config = EscribeConfig(
            base_url="https://example.com",
            city_name="Test",
            output_prefix="meeting_pipeline/sources/test-OH/data/escribe",
            storage=storage,
            rate_limit_delay=0,
        )
        meeting_links = [{"Url": "Document.aspx?DocumentId=42", "Format": ".pdf"}]

        await _download_meeting_pdfs(
            fake_client, config, meeting_links, event_id=7, meeting_date="2026-05-04"
        )

        assert len(captured_keys) == 1
        key = captured_keys[0]
        assert "2026-05-04" in key, f"meeting date must appear in filename: {key}"
        # confirms find_best_pdf's `date in filename` check would match
        assert "/attachments/" in key


# ============================================================================
# CivicClerk: future event query must use ascending order so the API's
# 15-event cap doesn't silently drop the nearest upcoming meetings.
# ============================================================================

class TestCivicClerkSchemaTolerance:
    """Both portal (camelCase) and legacy (PascalCase) tenants must read OK."""

    def test_evt_date_portal(self):
        from meeting_pipeline.collectors.civicclerk import _evt_date
        assert _evt_date({"eventDate": "2026-04-15T00:00:00Z"}) == "2026-04-15T00:00:00Z"

    def test_evt_date_legacy(self):
        from meeting_pipeline.collectors.civicclerk import _evt_date
        assert _evt_date({"MeetingStartDate": "2026-04-15T18:30:00"}) == "2026-04-15T18:30:00"

    def test_evt_id_portal(self):
        from meeting_pipeline.collectors.civicclerk import _evt_id
        assert _evt_id({"id": 42}) == "42"

    def test_evt_id_legacy(self):
        from meeting_pipeline.collectors.civicclerk import _evt_id
        assert _evt_id({"EventId": 99}) == "99"

    def test_evt_category_portal(self):
        from meeting_pipeline.collectors.civicclerk import _evt_category
        assert _evt_category({"categoryName": "City Council"}) == "City Council"

    def test_evt_category_legacy_missing(self):
        from meeting_pipeline.collectors.civicclerk import _evt_category
        # Legacy tenants may not expose category metadata at all; helper must
        # return "" so downstream filters fall through to exclude-pattern logic.
        assert _evt_category({"EventName": "Council Meeting"}) == ""

    def test_router_detects_portal(self):
        """Router must set is_portal=True iff source URL contains portal.civicclerk.com."""
        from pathlib import Path
        src = Path(__file__).parent.parent / "stages" / "collect" / "router.py"
        text = src.read_text()
        assert '"portal.civicclerk.com" in url' in text
        assert "is_portal=is_portal" in text


class TestCivicClerkFutureOrdering:
    def test_future_query_uses_asc_ordering(self):
        """Past window must use desc, future window must use asc — otherwise
        the API's 15-event cap silently drops the nearest upcoming meetings.
        Reading the source guards against future regressions; running the
        full collector would need a live API."""
        from pathlib import Path
        src = Path(__file__).parent.parent / "collectors" / "civicclerk.py"
        text = src.read_text()
        # The orderby is built dynamically from a date_field that varies by
        # tenant generation (eventDate vs MeetingStartDate), so check the
        # direction is paired with the right date window in the loop tuple.
        assert '(cutoff_date, today, "desc")' in text, "past window should use desc"
        assert '(today, future_cutoff, "asc")' in text, "future window should use asc"


# ============================================================================
# BoardDocs attachments must NOT be discoverable by find_best_pdf as agendas
# (they're per-item attachments, not a meeting-level agenda PDF).
# ============================================================================

class TestBoarddocsAttachmentPath:
    async def test_attachments_path_excludes_pdfs_and_date(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from meeting_pipeline.collectors.boarddocs import (
            BoardDocsConfig, _download_item_attachments,
        )

        captured_keys: list[str] = []
        storage = MagicMock()
        storage.exists.return_value = False
        storage.write_bytes = lambda key, _data: captured_keys.append(key)

        config = BoardDocsConfig(
            base_url="https://go.boarddocs.com/oh/test/Board.nsf",
            city_name="Test",
            output_prefix="meeting_pipeline/sources/test-OH/data/boarddocs",
            storage=storage,
        )

        files = [{"href": "/path.pdf", "name": "doc.pdf"}]
        # Fake _fetch_files to return our list and _try_download_pdf to "succeed"
        async def fake_download(_client, _base, _url, key, _storage):
            captured_keys.append(key)
            return 1

        with patch("meeting_pipeline.collectors.boarddocs._fetch_files",
                   new=AsyncMock(return_value=files)), \
             patch("meeting_pipeline.collectors.boarddocs._try_download_pdf",
                   new=fake_download):
            await _download_item_attachments(
                client=AsyncMock(), config=config, item_unique="ABC", matter_id="42",
                committee_id="C1", meeting_date="20260504",
            )

        assert len(captured_keys) >= 1
        for key in captured_keys:
            assert "/pdfs/" not in key, f"BoardDocs attachments must not go under /pdfs/ — got {key!r}"
            assert "/attachments/" in key, f"BoardDocs attachments should be under /attachments/ — got {key!r}"
            assert "2026-05-04" not in key, f"date must not be in filename — got {key!r}"
            assert "20260504" not in key, f"date must not be in filename — got {key!r}"
