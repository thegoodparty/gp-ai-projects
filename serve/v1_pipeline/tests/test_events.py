#!/usr/bin/env python3
"""Tests for poll analysis event structure (v1 format compatibility)."""

import pytest

from serve.v1_pipeline.models.events import (
    PollAnalysisCompleteData,
    PollAnalysisCompleteEvent,
    PollIssueAnalysisData,
    PollIssueAnalysisEvent,
)


class TestPollIssueAnalysisEvent:
    def test_to_json_structure(self):
        """Verify pollIssueAnalysis event has correct v1 structure."""
        issue_data = PollIssueAnalysisData(
            pollId="test-poll-123",
            rank=1,
            clusterId=5,
            theme="Rising Property Taxes",
            summary="Citizens are concerned about taxes.",
            analysis="Detailed analysis of the tax concerns...",
            quotes=[
                {"quote": "Taxes are too high!", "phone_number": "5551234567"},
                {"quote": "Property tax doubled", "phone_number": "5559876543"},
            ],
            responseCount=42,
        )
        event = PollIssueAnalysisEvent(data=issue_data)
        json_output = event.to_json()

        # Check top-level structure
        assert json_output["type"] == "pollIssueAnalysis"
        assert "data" in json_output

        # Check data fields
        data = json_output["data"]
        assert data["pollId"] == "test-poll-123"
        assert data["rank"] == 1
        assert data["clusterId"] == 5
        assert data["theme"] == "Rising Property Taxes"
        assert data["summary"] == "Citizens are concerned about taxes."
        assert data["analysis"] == "Detailed analysis of the tax concerns..."
        assert data["responseCount"] == 42

        # Check quotes structure
        assert len(data["quotes"]) == 2
        assert data["quotes"][0]["quote"] == "Taxes are too high!"
        assert data["quotes"][0]["phone_number"] == "5551234567"

    def test_required_fields_present(self):
        """Verify all required v1 fields are in the output."""
        issue_data = PollIssueAnalysisData(
            pollId="p1",
            rank=1,
            clusterId=0,
            theme="Theme",
            summary="Summary",
            analysis="Analysis",
            quotes=[],
            responseCount=1,
        )
        event = PollIssueAnalysisEvent(data=issue_data)
        data = event.to_json()["data"]

        required_fields = {"pollId", "rank", "theme", "summary", "analysis", "quotes", "responseCount"}
        assert required_fields.issubset(set(data.keys()))


class TestPollAnalysisCompleteEvent:
    def test_to_json_structure(self):
        """Verify pollAnalysisComplete event has correct v1 structure (no nested issues)."""
        complete_data = PollAnalysisCompleteData(
            pollId="test-poll-123",
            totalResponses=100,
            responsesLocation="output/test-poll-123/analysis.json",
        )
        event = PollAnalysisCompleteEvent(data=complete_data)
        json_output = event.to_json()

        # Check top-level structure
        assert json_output["type"] == "pollAnalysisComplete"
        assert "data" in json_output

        # Check data fields
        data = json_output["data"]
        assert data["pollId"] == "test-poll-123"
        assert data["totalResponses"] == 100
        assert data["responsesLocation"] == "output/test-poll-123/analysis.json"

    def test_no_nested_issues(self):
        """Verify pollAnalysisComplete does NOT have nested issues (v1 format)."""
        complete_data = PollAnalysisCompleteData(
            pollId="test-poll-123",
            totalResponses=100,
            responsesLocation="",
        )
        event = PollAnalysisCompleteEvent(data=complete_data)
        data = event.to_json()["data"]

        # v1 format: issues are separate objects, not nested in complete event
        assert "issues" not in data

    def test_required_fields_present(self):
        """Verify all required v1 fields are in the output."""
        complete_data = PollAnalysisCompleteData(
            pollId="p1",
            totalResponses=0,
            responsesLocation="",
        )
        event = PollAnalysisCompleteEvent(data=complete_data)
        data = event.to_json()["data"]

        required_fields = {"pollId", "totalResponses"}
        assert required_fields.issubset(set(data.keys()))


class TestV1EventArrayFormat:
    """Test that the full event array matches v1 format expectations."""

    def test_full_event_array_structure(self):
        """
        Verify the full array structure matches v1 format:
        [
            { type: "pollIssueAnalysis", data: {...} },
            { type: "pollIssueAnalysis", data: {...} },
            { type: "pollIssueAnalysis", data: {...} },
            { type: "pollAnalysisComplete", data: {...} }
        ]
        """
        poll_id = "test-poll-456"

        # Build issues (like sqs_publisher does)
        issues = []
        for rank in range(1, 4):
            issue_data = PollIssueAnalysisData(
                pollId=poll_id,
                rank=rank,
                clusterId=rank * 10,
                theme=f"Theme {rank}",
                summary=f"Summary {rank}",
                analysis=f"Analysis {rank}",
                quotes=[{"quote": f"Quote {rank}", "phone_number": f"555000000{rank}"}],
                responseCount=rank * 5,
            )
            issues.append(issue_data)

        # Build events array (v1 format)
        all_events = []

        # Add individual issue events first
        for issue_data in issues:
            issue_event = PollIssueAnalysisEvent(data=issue_data)
            all_events.append(issue_event.to_json())

        # Then add completion event
        complete_data = PollAnalysisCompleteData(
            pollId=poll_id,
            totalResponses=30,
            responsesLocation="output/path/analysis.json",
        )
        complete_event = PollAnalysisCompleteEvent(data=complete_data)
        all_events.append(complete_event.to_json())

        # Verify array structure
        assert len(all_events) == 4

        # First 3 should be pollIssueAnalysis
        for i in range(3):
            assert all_events[i]["type"] == "pollIssueAnalysis"
            assert all_events[i]["data"]["rank"] == i + 1
            assert all_events[i]["data"]["pollId"] == poll_id

        # Last should be pollAnalysisComplete
        assert all_events[3]["type"] == "pollAnalysisComplete"
        assert all_events[3]["data"]["pollId"] == poll_id
        assert all_events[3]["data"]["totalResponses"] == 30
        assert "issues" not in all_events[3]["data"]

    def test_empty_poll_event_array(self):
        """Verify empty poll produces single pollAnalysisComplete event."""
        complete_data = PollAnalysisCompleteData(
            pollId="empty-poll",
            totalResponses=0,
            responsesLocation="",
        )
        event = PollAnalysisCompleteEvent(data=complete_data)
        events = [event.to_json()]

        assert len(events) == 1
        assert events[0]["type"] == "pollAnalysisComplete"
        assert events[0]["data"]["totalResponses"] == 0
