from unittest.mock import patch, MagicMock
import pytest

from stitch_golden_data.prod_gold_data.production_matcher import (
    ProductionMatcher,
    EmbeddingDistrict,
)


@pytest.fixture(autouse=True)
def reset_braintrust_singleton():
    from shared.braintrust import BraintrustClient
    BraintrustClient.reset_instance()
    yield
    BraintrustClient.reset_instance()


@pytest.fixture
def mock_dependencies():
    with (
        patch("stitch_golden_data.prod_gold_data.production_matcher.DatabricksClient") as mock_db,
        patch("stitch_golden_data.prod_gold_data.production_matcher.Gemini3Client") as mock_llm_cls,
        patch("stitch_golden_data.prod_gold_data.production_matcher.GeminiEmbeddingClient") as mock_emb_cls,
        patch("stitch_golden_data.prod_gold_data.production_matcher.init_braintrust") as mock_init_bt,
        patch("stitch_golden_data.prod_gold_data.production_matcher.cache_prompt") as mock_cache,
    ):
        mock_llm = MagicMock()
        mock_llm_cls.return_value = mock_llm
        mock_emb = MagicMock()
        mock_emb_cls.return_value = mock_emb

        yield {
            "databricks": mock_db,
            "llm_cls": mock_llm_cls,
            "llm": mock_llm,
            "embedding_cls": mock_emb_cls,
            "embedding": mock_emb,
            "init_braintrust": mock_init_bt,
            "cache_prompt": mock_cache,
        }


class TestBraintrustInit:
    def test_init_braintrust_called_on_construction(self, mock_dependencies):
        matcher = ProductionMatcher()

        mock_dependencies["init_braintrust"].assert_called_once_with(project="stitch-golden-data")

    def test_cache_prompt_called_on_construction(self, mock_dependencies):
        matcher = ProductionMatcher()

        mock_dependencies["cache_prompt"].assert_called_once_with(
            "stitch-golden-data-matcher",
        )


class TestPromptBuilding:
    def test_build_cached_prompt_called_with_correct_variables(self, mock_dependencies):
        matcher = ProductionMatcher()

        districts = [
            EmbeddingDistrict(
                l2_district_name="City Council District 1",
                l2_district_type="CITY_COUNCIL",
                similarity_score=0.95,
                l2_full_text="City Council District 1",
                state="DE",
            ),
            EmbeddingDistrict(
                l2_district_name="County Board District 2",
                l2_district_type="COUNTY_BOARD",
                similarity_score=0.85,
                l2_full_text="County Board District 2",
                state="DE",
            ),
        ]

        mock_dependencies["llm"].generate_structured_content.return_value = {
            "selected_candidate_number": 1,
            "selection_confidence": 90,
            "reasoning": "Best geographic match",
        }

        with patch("stitch_golden_data.prod_gold_data.production_matcher.build_cached_prompt") as mock_build:
            mock_build.return_value = "rendered prompt"

            import asyncio
            result = asyncio.run(matcher.llm_select_best_match("Wilmington City Council", districts))

            mock_build.assert_called_once()
            call_args = mock_build.call_args
            assert call_args[0][0] == "stitch-golden-data-matcher"

            variables = call_args[0][1]
            assert variables["br_name"] == "Wilmington City Council"
            assert variables["state"] == "DE"
            assert variables["num_districts"] == "2"
            assert "City Council District 1" in variables["districts_text"]
            assert "County Board District 2" in variables["districts_text"]

            assert call_args[1]["fallback_prompt"] is not None
            assert len(call_args[1]["fallback_prompt"]) > 0

class TestTraceNamePassthrough:
    def test_trace_name_passed_to_generate_structured_content(self, mock_dependencies):
        matcher = ProductionMatcher()

        districts = [
            EmbeddingDistrict(
                l2_district_name="School Board",
                l2_district_type="SCHOOL_BOARD",
                similarity_score=0.88,
                l2_full_text="School Board",
                state="NY",
            ),
        ]

        mock_dependencies["llm"].generate_structured_content.return_value = {
            "selected_candidate_number": 1,
            "selection_confidence": 92,
            "reasoning": "Exact match",
        }

        with patch("stitch_golden_data.prod_gold_data.production_matcher.build_cached_prompt", return_value="prompt"):
            import asyncio
            asyncio.run(matcher.llm_select_best_match("School Board", districts))

        call_kwargs = mock_dependencies["llm"].generate_structured_content.call_args[1]
        assert call_kwargs["trace_name"] == "stitch-match-selection"


