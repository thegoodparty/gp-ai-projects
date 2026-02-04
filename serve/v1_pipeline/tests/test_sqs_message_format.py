#!/usr/bin/env python3
"""
Tests that SQS messages match the format gp-api expects.

gp-api uses Zod schema (from queue.types.ts):

PollAnalysisCompleteEventSchema = z.object({
  type: z.literal('pollAnalysisComplete'),
  data: z.object({
    pollId: z.string(),
    totalResponses: z.number(),
    issues: z.array(
      z.object({
        pollId: z.string(),
        rank: z.number().min(1).max(3),
        theme: z.string(),
        summary: z.string(),
        analysis: z.string(),
        responseCount: z.number(),
        quotes: z.array(
          z.object({ quote: z.string(), phone_number: z.string() }),
        ),
      }),
    ),
  }),
})
"""

import json
import pytest

from serve.v1_pipeline.models.events import (
    PollAnalysisCompleteData,
    PollAnalysisCompleteEvent,
    PollIssueAnalysisData,
)


def build_sample_event() -> PollAnalysisCompleteEvent:
    """Build a sample event matching what sqs_publisher creates."""
    issues = [
        PollIssueAnalysisData(
            pollId="test-poll-123",
            rank=1,
            clusterId=5,
            theme="Traffic Safety",
            summary="Residents concerned about speeding.",
            analysis="Detailed analysis...",
            quotes=[
                {"quote": "Cars go too fast!", "phone_number": "5551234567"},
            ],
            responseCount=35,
        ),
        PollIssueAnalysisData(
            pollId="test-poll-123",
            rank=2,
            clusterId=8,
            theme="Road Repairs",
            summary="Streets need fixing.",
            analysis="More analysis...",
            quotes=[
                {"quote": "Potholes everywhere", "phone_number": "5559876543"},
            ],
            responseCount=28,
        ),
        PollIssueAnalysisData(
            pollId="test-poll-123",
            rank=3,
            clusterId=2,
            theme="Property Taxes",
            summary="Taxes too high.",
            analysis="Tax analysis...",
            quotes=[
                {"quote": "My taxes doubled!", "phone_number": "5555555555"},
            ],
            responseCount=21,
        ),
    ]

    return PollAnalysisCompleteEvent(
        data=PollAnalysisCompleteData(
            pollId="test-poll-123",
            totalResponses=141,
            responsesLocation="output/test/analysis.json",
            issues=issues,
        )
    )


class TestSQSMessageFormat:
    """Test that SQS message format matches gp-api expectations."""

    def test_message_is_object_not_array(self):
        """
        CRITICAL: gp-api does JSON.parse(body).type
        If body is an array, .type is undefined and processing fails.
        """
        event = build_sample_event()
        message_body = event.to_json()

        # Must be a dict (object), not a list (array)
        assert isinstance(message_body, dict), "Message must be an object, not an array"

        # When serialized and parsed, still an object
        serialized = json.dumps(message_body)
        parsed = json.loads(serialized)
        assert isinstance(parsed, dict), "Parsed message must be an object"

    def test_type_field_is_correct(self):
        """gp-api switches on message.type"""
        event = build_sample_event()
        message_body = event.to_json()

        assert message_body["type"] == "pollAnalysisComplete"

    def test_data_has_required_fields(self):
        """gp-api Zod schema requires: pollId, totalResponses, issues"""
        event = build_sample_event()
        data = event.to_json()["data"]

        assert "pollId" in data
        assert "totalResponses" in data
        assert "issues" in data
        assert isinstance(data["issues"], list)

    def test_issues_nested_in_data(self):
        """
        gp-api expects issues INSIDE data, not as separate messages.
        It does: event.data.issues.map(...)
        """
        event = build_sample_event()
        data = event.to_json()["data"]

        assert len(data["issues"]) == 3
        assert data["issues"][0]["rank"] == 1
        assert data["issues"][1]["rank"] == 2
        assert data["issues"][2]["rank"] == 3

    def test_issue_has_required_fields(self):
        """
        gp-api Zod schema for each issue:
        pollId, rank (1-3), theme, summary, analysis, responseCount, quotes
        """
        event = build_sample_event()
        issue = event.to_json()["data"]["issues"][0]

        required_fields = {
            "pollId", "rank", "theme", "summary",
            "analysis", "responseCount", "quotes"
        }
        assert required_fields.issubset(set(issue.keys()))

    def test_rank_values_valid(self):
        """gp-api Zod: rank: z.number().min(1).max(3)"""
        event = build_sample_event()
        issues = event.to_json()["data"]["issues"]

        for issue in issues:
            assert 1 <= issue["rank"] <= 3, f"Rank {issue['rank']} out of range"

    def test_quotes_structure(self):
        """
        gp-api Zod: quotes: z.array(z.object({ quote: z.string(), phone_number: z.string() }))
        """
        event = build_sample_event()
        quotes = event.to_json()["data"]["issues"][0]["quotes"]

        assert len(quotes) > 0
        for quote in quotes:
            assert "quote" in quote
            assert "phone_number" in quote
            assert isinstance(quote["quote"], str)
            assert isinstance(quote["phone_number"], str)

    def test_extra_fields_allowed(self):
        """
        Zod allows extra fields by default (they're stripped but don't fail validation).
        We have: clusterId, responsesLocation - these are additive.
        """
        event = build_sample_event()
        message = event.to_json()

        # Extra fields present (backwards compatible additions)
        assert "responsesLocation" in message["data"]
        assert "clusterId" in message["data"]["issues"][0]

    def test_full_json_round_trip(self):
        """Test the exact JSON that would be sent to SQS."""
        event = build_sample_event()

        # This is what gets sent to SQS
        message_body = json.dumps(event.to_json())

        # gp-api does: JSON.parse(message.Body)
        parsed = json.loads(message_body)

        # Then: queueMessage.type
        assert parsed["type"] == "pollAnalysisComplete"

        # Then validates with Zod and uses: event.data.issues.map(...)
        assert len(parsed["data"]["issues"]) == 3


class TestEmptyPollEvent:
    """Test empty poll (0 responses) format."""

    def test_empty_poll_has_empty_issues_array(self):
        """Empty poll should have issues: [] not missing issues field."""
        event = PollAnalysisCompleteEvent(
            data=PollAnalysisCompleteData(
                pollId="empty-poll",
                totalResponses=0,
                responsesLocation="",
                issues=[],
            )
        )
        message = event.to_json()

        assert message["data"]["issues"] == []
        assert message["data"]["totalResponses"] == 0
