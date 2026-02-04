#!/usr/bin/env python3
"""Tests for poll analysis event structure."""

import pytest

from serve.v1_pipeline.models.events import (
    PollAnalysisCompleteData,
    PollAnalysisCompleteEvent,
    PollIssueAnalysisData,
    PollIssueAnalysisEvent,
)


class TestPollIssueAnalysisEvent:
    def test_to_json_structure(self):
        """Verify pollIssueAnalysis event structure (for S3 file format)."""
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

        assert json_output["type"] == "pollIssueAnalysis"
        assert "data" in json_output

        data = json_output["data"]
        assert data["pollId"] == "test-poll-123"
        assert data["rank"] == 1
        assert data["clusterId"] == 5
        assert data["theme"] == "Rising Property Taxes"
        assert data["responseCount"] == 42
        assert len(data["quotes"]) == 2

    def test_required_fields_present(self):
        """Verify all required fields are in the output."""
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
        """Verify pollAnalysisComplete event has nested issues (gp-api format)."""
        issues = [
            PollIssueAnalysisData(
                pollId="test-poll-123",
                rank=1,
                clusterId=5,
                theme="Theme 1",
                summary="Summary 1",
                analysis="Analysis 1",
                quotes=[{"quote": "Quote 1", "phone_number": "5551234567"}],
                responseCount=35,
            )
        ]
        complete_data = PollAnalysisCompleteData(
            pollId="test-poll-123",
            totalResponses=100,
            responsesLocation="output/test-poll-123/analysis.json",
            issues=issues,
        )
        event = PollAnalysisCompleteEvent(data=complete_data)
        json_output = event.to_json()

        assert json_output["type"] == "pollAnalysisComplete"
        assert "data" in json_output

        data = json_output["data"]
        assert data["pollId"] == "test-poll-123"
        assert data["totalResponses"] == 100
        assert data["responsesLocation"] == "output/test-poll-123/analysis.json"
        assert len(data["issues"]) == 1

    def test_has_nested_issues(self):
        """Verify pollAnalysisComplete HAS nested issues (gp-api requires this)."""
        issues = [
            PollIssueAnalysisData(
                pollId="test-poll-123",
                rank=1,
                clusterId=0,
                theme="Theme",
                summary="Summary",
                analysis="Analysis",
                quotes=[],
                responseCount=10,
            )
        ]
        complete_data = PollAnalysisCompleteData(
            pollId="test-poll-123",
            totalResponses=100,
            responsesLocation="",
            issues=issues,
        )
        event = PollAnalysisCompleteEvent(data=complete_data)
        data = event.to_json()["data"]

        assert "issues" in data
        assert isinstance(data["issues"], list)

    def test_required_fields_present(self):
        """Verify all required fields are in the output."""
        complete_data = PollAnalysisCompleteData(
            pollId="p1",
            totalResponses=0,
            responsesLocation="",
            issues=[],
        )
        event = PollAnalysisCompleteEvent(data=complete_data)
        data = event.to_json()["data"]

        required_fields = {"pollId", "totalResponses", "issues"}
        assert required_fields.issubset(set(data.keys()))


class TestS3FileFormat:
    """Test the S3 file format (array of separate events for backwards compat)."""

    def test_s3_file_array_structure(self):
        """
        S3 file contains array with:
        - pollIssueAnalysis events (separate, for backwards compat)
        - pollAnalysisComplete event (with nested issues for gp-api)
        """
        poll_id = "test-poll-456"

        # Build issues
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

        # Build S3 file array (v1 compat format)
        all_events = []

        # Add individual issue events
        for issue_data in issues:
            issue_event = PollIssueAnalysisEvent(data=issue_data)
            all_events.append(issue_event.to_json())

        # Add completion event with nested issues
        complete_data = PollAnalysisCompleteData(
            pollId=poll_id,
            totalResponses=30,
            responsesLocation="output/path/analysis.json",
            issues=issues,
        )
        complete_event = PollAnalysisCompleteEvent(data=complete_data)
        all_events.append(complete_event.to_json())

        # Verify array structure
        assert len(all_events) == 4

        # First 3 should be pollIssueAnalysis
        for i in range(3):
            assert all_events[i]["type"] == "pollIssueAnalysis"
            assert all_events[i]["data"]["rank"] == i + 1

        # Last should be pollAnalysisComplete with nested issues
        assert all_events[3]["type"] == "pollAnalysisComplete"
        assert len(all_events[3]["data"]["issues"]) == 3

    def test_empty_poll_event(self):
        """Verify empty poll has empty issues array."""
        complete_data = PollAnalysisCompleteData(
            pollId="empty-poll",
            totalResponses=0,
            responsesLocation="",
            issues=[],
        )
        event = PollAnalysisCompleteEvent(data=complete_data)

        assert event.to_json()["data"]["issues"] == []
        assert event.to_json()["data"]["totalResponses"] == 0
