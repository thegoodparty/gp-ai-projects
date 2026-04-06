"""Tests for event_generator — date validation and task construction."""

from datetime import date
from unittest.mock import Mock

import pytest

from campaign_plan_lambda.event_generator import (
    _filter_and_structure_events,
    LlmEventResult,
    LlmEventResultList,
    CampaignEventTask,
)


class TestFilterAndStructureEvents:
    @pytest.mark.asyncio
    async def test_raises_when_llm_returns_events_but_all_dates_invalid(self):
        mock_client = Mock()
        mock_client.generate_structured_content.return_value = LlmEventResultList(
            events=[
                LlmEventResult(title="Bad Event 1", description="Test", date="not-a-date"),
                LlmEventResult(title="Bad Event 2", description="Test", date="9999-99-99"),
            ]
        )

        with pytest.raises(RuntimeError, match="none had valid dates"):
            await _filter_and_structure_events(
                mock_client, "Boston", "MA", date(2026, 11, 4), "raw events text"
            )

    @pytest.mark.asyncio
    async def test_raises_when_all_events_out_of_range(self):
        mock_client = Mock()
        mock_client.generate_structured_content.return_value = LlmEventResultList(
            events=[
                LlmEventResult(title="Past Event", description="Test", date="2020-01-01"),
                LlmEventResult(title="Future Event", description="Test", date="2030-01-01"),
            ]
        )

        with pytest.raises(RuntimeError, match="none had valid dates"):
            await _filter_and_structure_events(
                mock_client, "Boston", "MA", date(2026, 11, 4), "raw events text"
            )

    @pytest.mark.asyncio
    async def test_returns_empty_when_llm_returns_no_events(self):
        mock_client = Mock()
        mock_client.generate_structured_content.return_value = LlmEventResultList(
            events=[]
        )

        result = await _filter_and_structure_events(
            mock_client, "Boston", "MA", date(2026, 11, 4), "raw events text"
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_filters_invalid_dates_keeps_valid(self):
        mock_client = Mock()
        mock_client.generate_structured_content.return_value = LlmEventResultList(
            events=[
                LlmEventResult(title="Good Event", description="Voter engagement", date="2026-07-04"),
                LlmEventResult(title="Bad Date", description="Test", date="not-a-date"),
                LlmEventResult(title="Past Event", description="Test", date="2020-01-01"),
            ]
        )

        result = await _filter_and_structure_events(
            mock_client, "Boston", "MA", date(2026, 11, 4), "raw events text"
        )

        assert len(result) == 1
        assert result[0].title == "Good Event"
        assert result[0].flowType == "events"
        assert result[0].cta == "Attend event"

    @pytest.mark.asyncio
    async def test_week_countdown_calculation(self):
        mock_client = Mock()
        mock_client.generate_structured_content.return_value = LlmEventResultList(
            events=[
                LlmEventResult(title="Election Week Event", description="Test", date="2026-11-04"),
                LlmEventResult(title="One Week Before", description="Test", date="2026-10-28"),
            ]
        )

        result = await _filter_and_structure_events(
            mock_client, "Boston", "MA", date(2026, 11, 4), "raw events text"
        )

        assert len(result) == 2
        election_week_task = next(t for t in result if t.title == "Election Week Event")
        one_week_task = next(t for t in result if t.title == "One Week Before")
        assert election_week_task.week == 1
        assert one_week_task.week == 2
