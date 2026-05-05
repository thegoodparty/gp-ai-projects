"""Tests for the Lambda handler — input validation and retry logic."""

import json
import uuid
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from campaign_plan_lambda.handler import (
    handler,
    SqsMessageBody,
    MAX_RECEIVE_COUNT,
)


def _make_sqs_event(body: dict, receive_count: int = 1) -> dict:
    return {
        "Records": [
            {
                "messageId": str(uuid.uuid4()),
                "receiptHandle": "fake",
                "body": json.dumps(body),
                "attributes": {
                    "ApproximateReceiveCount": str(receive_count),
                },
                "messageAttributes": {},
                "md5OfBody": "fake",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-west-2:123:test.fifo",
                "awsRegion": "us-west-2",
            }
        ]
    }


VALID_MESSAGE = {
    "campaignId": 123,
    "electionDate": "2026-11-04",
    "city": "Boston",
    "state": "MA",
    "officeName": "Mayor",
    "officeLevel": "CITY",
    "primaryElectionDate": "2026-06-02",
}

# Legacy payload shape that gp-api sends today — kept supported during transition.
LEGACY_MESSAGE = {
    "campaignId": 123,
    "election_date": "2026-11-04",
    "city": "Boston",
    "state": "MA",
}


class TestInputValidation:
    def test_valid_message_parses(self):
        body = SqsMessageBody(**VALID_MESSAGE)
        assert body.campaignId == 123
        assert body.electionDate == "2026-11-04"
        assert body.state == "MA"
        assert body.city == "Boston"
        assert body.officeName == "Mayor"
        assert body.officeLevel == "CITY"
        assert body.primaryElectionDate == "2026-06-02"

    def test_legacy_election_date_alias_still_works(self):
        body = SqsMessageBody(**LEGACY_MESSAGE)
        assert body.campaignId == 123
        assert body.electionDate == "2026-11-04"
        assert body.city == "Boston"
        assert body.state == "MA"

    def test_only_required_fields_accepted(self):
        body = SqsMessageBody(campaignId=123, electionDate="2026-11-04")
        assert body.campaignId == 123
        assert body.electionDate == "2026-11-04"
        assert body.state is None
        assert body.city is None
        assert body.officeName is None
        assert body.officeLevel is None
        assert body.primaryElectionDate is None

    def test_unknown_fields_ignored(self):
        # Forward compatibility: gp-api can add fields without breaking the Lambda.
        body = SqsMessageBody(
            campaignId=123,
            electionDate="2026-11-04",
            someFutureField="whatever",
            anotherNewThing={"nested": True},
        )
        assert body.campaignId == 123

    def test_missing_campaign_id_rejected(self):
        with pytest.raises(ValidationError):
            SqsMessageBody(**{"electionDate": "2026-11-04"})

    def test_missing_election_date_rejected(self):
        with pytest.raises(ValidationError):
            SqsMessageBody(**{"campaignId": 123})

    def test_invalid_campaign_id_type_rejected(self):
        with pytest.raises(ValidationError):
            SqsMessageBody(**{"campaignId": "not-a-number", "electionDate": "2026-11-04"})

    def test_invalid_election_date_rejected(self):
        with pytest.raises(ValidationError):
            SqsMessageBody(**{"campaignId": 123, "electionDate": "not-a-date"})

    def test_invalid_primary_election_date_rejected(self):
        with pytest.raises(ValidationError):
            SqsMessageBody(
                campaignId=123, electionDate="2026-11-04", primaryElectionDate="not-a-date"
            )

    def test_empty_primary_election_date_allowed(self):
        # gp-api may send an empty string when the field is genuinely absent.
        body = SqsMessageBody(campaignId=123, electionDate="2026-11-04", primaryElectionDate="")
        assert body.primaryElectionDate is None

    def test_empty_strings_on_optional_fields_normalize_to_none(self):
        # gp-api may send "" for any optional field it doesn't have a value for;
        # normalizing to None here means the downstream fallback fires uniformly.
        body = SqsMessageBody(
            campaignId=123,
            electionDate="2026-11-04",
            state="",
            city="",
            officeName="",
            officeLevel="",
            primaryElectionDate="",
        )
        assert body.state is None
        assert body.city is None
        assert body.officeName is None
        assert body.officeLevel is None
        assert body.primaryElectionDate is None

    def test_whitespace_only_optional_fields_normalize_to_none(self):
        # Same as empty strings — a whitespace-only value on a date field would
        # otherwise trip the ISO-format check and DLQ the message.
        body = SqsMessageBody(
            campaignId=123,
            electionDate="2026-11-04",
            state="   ",
            city="\t\n ",
            officeName="  ",
            officeLevel=" ",
            primaryElectionDate="   ",
        )
        assert body.state is None
        assert body.city is None
        assert body.officeName is None
        assert body.officeLevel is None
        assert body.primaryElectionDate is None

    def test_empty_election_date_still_rejected(self):
        # electionDate is required — "" is a real validation error, not a fallback trigger.
        with pytest.raises(ValidationError):
            SqsMessageBody(campaignId=123, electionDate="")

    def test_invalid_json_raises(self):
        event = {
            "Records": [
                {
                    "body": "not valid json{{{",
                    "attributes": {"ApproximateReceiveCount": "1"},
                }
            ]
        }
        with patch("campaign_plan_lambda.handler._inject_secrets"):
            with pytest.raises(json.JSONDecodeError):
                handler(event, None)


class TestInvalidMessageErrorNotification:
    @patch("campaign_plan_lambda.handler._inject_secrets")
    def test_no_error_sent_on_first_attempt(self, _mock_secrets):
        event = _make_sqs_event({
            "campaignId": 456,
            "election_date": "not-a-date",
            "city": "Boston",
            "state": "MA",
        }, receive_count=1)

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(ValidationError):
                handler(event, None)
            mock_send.assert_not_called()

    @patch("campaign_plan_lambda.handler._inject_secrets")
    def test_sends_error_on_final_attempt_when_campaign_id_present(self, _mock_secrets):
        event = _make_sqs_event({
            "campaignId": 456,
            "election_date": "not-a-date",
            "city": "Boston",
            "state": "MA",
        }, receive_count=MAX_RECEIVE_COUNT)

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(ValidationError):
                handler(event, None)
            mock_send.assert_called_once_with(456, "Invalid message format")

    @patch("campaign_plan_lambda.handler._inject_secrets")
    def test_sends_error_on_final_attempt_when_missing_fields(self, _mock_secrets):
        event = _make_sqs_event({
            "campaignId": 789,
        }, receive_count=MAX_RECEIVE_COUNT)

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(ValidationError):
                handler(event, None)
            mock_send.assert_called_once_with(789, "Invalid message format")

    @patch("campaign_plan_lambda.handler._inject_secrets")
    def test_no_error_sent_when_campaign_id_missing(self, _mock_secrets):
        event = _make_sqs_event({
            "election_date": "2026-11-04",
            "city": "Boston",
        }, receive_count=MAX_RECEIVE_COUNT)

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(ValidationError):
                handler(event, None)
            mock_send.assert_not_called()

    @patch("campaign_plan_lambda.handler._inject_secrets")
    def test_raises_when_json_unparseable(self, _mock_secrets):
        event = {
            "Records": [
                {
                    "body": "not json{{{",
                    "attributes": {"ApproximateReceiveCount": str(MAX_RECEIVE_COUNT)},
                }
            ]
        }

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(json.JSONDecodeError):
                handler(event, None)
            mock_send.assert_not_called()


class TestRetryLogic:
    @patch("campaign_plan_lambda.handler._inject_secrets")
    @patch("campaign_plan_lambda.handler.asyncio")
    def test_error_message_not_sent_on_first_attempt(self, mock_asyncio, mock_secrets):
        mock_asyncio.run.side_effect = RuntimeError("Gemini API failed")
        event = _make_sqs_event(VALID_MESSAGE, receive_count=1)

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(RuntimeError):
                handler(event, None)
            mock_send.assert_not_called()

    @patch("campaign_plan_lambda.handler._inject_secrets")
    @patch("campaign_plan_lambda.handler.asyncio")
    def test_error_message_not_sent_on_second_attempt(self, mock_asyncio, mock_secrets):
        mock_asyncio.run.side_effect = RuntimeError("Gemini API failed")
        event = _make_sqs_event(VALID_MESSAGE, receive_count=2)

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(RuntimeError):
                handler(event, None)
            mock_send.assert_not_called()

    @patch("campaign_plan_lambda.handler._inject_secrets")
    @patch("campaign_plan_lambda.handler.asyncio")
    def test_error_message_sent_on_final_attempt(self, mock_asyncio, mock_secrets):
        mock_asyncio.run.side_effect = RuntimeError("Gemini API failed")
        event = _make_sqs_event(VALID_MESSAGE, receive_count=MAX_RECEIVE_COUNT)

        with patch("campaign_plan_lambda.output.send_error_message") as mock_send:
            with pytest.raises(RuntimeError):
                handler(event, None)
            mock_send.assert_called_once_with(123, "Campaign plan generation failed")

    @patch("campaign_plan_lambda.handler._inject_secrets")
    @patch("campaign_plan_lambda.handler.asyncio")
    def test_exception_always_reraised_for_sqs_retry(self, mock_asyncio, mock_secrets):
        mock_asyncio.run.side_effect = RuntimeError("fail")

        for count in [1, 2, 3]:
            event = _make_sqs_event(VALID_MESSAGE, receive_count=count)
            with pytest.raises(RuntimeError, match="fail"):
                handler(event, None)
