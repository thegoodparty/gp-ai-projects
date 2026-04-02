"""Tests for the Lambda handler — input validation and retry logic."""

import json
import uuid
from unittest.mock import patch

import pytest

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
    "election_date": "2026-11-04",
    "city": "Boston",
    "state": "MA",
}


class TestInputValidation:
    def test_valid_message_parses(self):
        body = SqsMessageBody(**VALID_MESSAGE)
        assert body.campaignId == 123
        assert body.city == "Boston"
        assert body.state == "MA"
        assert body.election_date == "2026-11-04"

    def test_missing_campaign_id_rejected(self):
        with pytest.raises(Exception):
            SqsMessageBody(**{"election_date": "2026-11-04", "city": "Boston", "state": "MA"})

    def test_missing_city_rejected(self):
        with pytest.raises(Exception):
            SqsMessageBody(**{"campaignId": 123, "election_date": "2026-11-04", "state": "MA"})

    def test_missing_state_rejected(self):
        with pytest.raises(Exception):
            SqsMessageBody(**{"campaignId": 123, "election_date": "2026-11-04", "city": "Boston"})

    def test_missing_election_date_rejected(self):
        with pytest.raises(Exception):
            SqsMessageBody(**{"campaignId": 123, "city": "Boston", "state": "MA"})

    def test_invalid_campaign_id_type_rejected(self):
        with pytest.raises(Exception):
            SqsMessageBody(**{"campaignId": "not-a-number", "election_date": "2026-11-04", "city": "Boston", "state": "MA"})

    def test_invalid_election_date_rejected(self):
        with pytest.raises(Exception):
            SqsMessageBody(**{"campaignId": 123, "election_date": "not-a-date", "city": "Boston", "state": "MA"})

    def test_invalid_json_skipped(self):
        event = {
            "Records": [
                {
                    "body": "not valid json{{{",
                    "attributes": {"ApproximateReceiveCount": "1"},
                }
            ]
        }
        with patch("campaign_plan_lambda.handler._inject_secrets"):
            handler(event, None)


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
