"""Tests for S3 output and SQS messaging — key format, message structure."""

import json
import re
import uuid
from unittest.mock import MagicMock

import pytest

import campaign_plan_lambda.output as output_module


@pytest.fixture(autouse=True)
def reset_clients():
    """Reset module-level client singletons between tests."""
    output_module._s3_client = None
    output_module._sqs_client = None
    yield
    output_module._s3_client = None
    output_module._sqs_client = None


@pytest.fixture
def call_log():
    return []


@pytest.fixture
def mock_s3(call_log):
    client = MagicMock()

    def put_object(**kwargs):
        call_log.append(("s3_put_object", kwargs))
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    client.put_object.side_effect = put_object
    output_module._s3_client = client
    return client


@pytest.fixture
def mock_sqs(call_log):
    client = MagicMock()

    def send_message(**kwargs):
        call_log.append(("sqs_send_message", kwargs))
        return {"MessageId": str(uuid.uuid4())}

    client.send_message.side_effect = send_message
    output_module._sqs_client = client
    return client


class TestWriteResultToS3:
    def test_s3_key_contains_campaign_id(self, mock_s3, monkeypatch):
        monkeypatch.setenv("S3_RESULTS_BUCKET", "test-bucket")
        s3_key = output_module.write_result_to_s3(12345, {"test": "data"})
        assert "results/12345/" in s3_key

    def test_s3_key_has_uuid_suffix(self, mock_s3, monkeypatch):
        monkeypatch.setenv("S3_RESULTS_BUCKET", "test-bucket")
        s3_key = output_module.write_result_to_s3(123, {"test": "data"})
        # Format: results/{id}/{timestamp}-{uuid8}.json
        assert re.search(r"-[a-f0-9]{8}\.json$", s3_key)

    def test_s3_key_unique_across_calls(self, mock_s3, monkeypatch):
        monkeypatch.setenv("S3_RESULTS_BUCKET", "test-bucket")
        key1 = output_module.write_result_to_s3(123, {"a": 1})
        key2 = output_module.write_result_to_s3(123, {"b": 2})
        assert key1 != key2

    def test_s3_body_is_valid_json(self, mock_s3, call_log, monkeypatch):
        monkeypatch.setenv("S3_RESULTS_BUCKET", "test-bucket")
        output_module.write_result_to_s3(123, {"tasks": [1, 2, 3]})
        body = call_log[0][1]["Body"]
        parsed = json.loads(body)
        assert parsed["tasks"] == [1, 2, 3]

    def test_s3_content_type(self, mock_s3, call_log, monkeypatch):
        monkeypatch.setenv("S3_RESULTS_BUCKET", "test-bucket")
        output_module.write_result_to_s3(123, {})
        assert call_log[0][1]["ContentType"] == "application/json"

    def test_s3_uses_correct_bucket(self, mock_s3, call_log, monkeypatch):
        monkeypatch.setenv("S3_RESULTS_BUCKET", "my-bucket")
        output_module.write_result_to_s3(123, {})
        assert call_log[0][1]["Bucket"] == "my-bucket"


class TestSendCompletionMessage:
    def test_message_structure(self, mock_sqs, call_log, monkeypatch):
        monkeypatch.setenv("OUTPUT_SQS_QUEUE_URL", "https://sqs.example.com/queue.fifo")
        output_module.send_completion_message(123, "results/123/test.json", 10, "2026-03-31T00:00:00")

        body = json.loads(call_log[0][1]["MessageBody"])
        assert body["type"] == "campaignPlanComplete"
        assert body["data"]["campaignId"] == 123
        assert body["data"]["status"] == "completed"
        assert body["data"]["s3Key"] == "results/123/test.json"
        assert body["data"]["taskCount"] == 10
        assert body["data"]["generationTimestamp"] == "2026-03-31T00:00:00"

    def test_message_group_id(self, mock_sqs, call_log, monkeypatch):
        monkeypatch.setenv("OUTPUT_SQS_QUEUE_URL", "https://sqs.example.com/queue.fifo")
        output_module.send_completion_message(456, "key", 5, "ts")
        assert call_log[0][1]["MessageGroupId"] == "gp-queue-campaign-plan-456"

    def test_deduplication_id_is_unique(self, mock_sqs, call_log, monkeypatch):
        monkeypatch.setenv("OUTPUT_SQS_QUEUE_URL", "https://sqs.example.com/queue.fifo")
        output_module.send_completion_message(1, "k", 0, "t")
        output_module.send_completion_message(1, "k", 0, "t")
        dedup1 = call_log[0][1]["MessageDeduplicationId"]
        dedup2 = call_log[1][1]["MessageDeduplicationId"]
        assert dedup1 != dedup2


class TestSendErrorMessage:
    def test_error_message_structure(self, mock_sqs, call_log, monkeypatch):
        monkeypatch.setenv("OUTPUT_SQS_QUEUE_URL", "https://sqs.example.com/queue.fifo")
        output_module.send_error_message(789, "Something went wrong")

        body = json.loads(call_log[0][1]["MessageBody"])
        assert body["type"] == "campaignPlanComplete"
        assert body["data"]["campaignId"] == 789
        assert body["data"]["status"] == "error"
        assert body["data"]["error"] == "Something went wrong"

    def test_error_message_group_id(self, mock_sqs, call_log, monkeypatch):
        monkeypatch.setenv("OUTPUT_SQS_QUEUE_URL", "https://sqs.example.com/queue.fifo")
        output_module.send_error_message(789, "fail")
        assert call_log[0][1]["MessageGroupId"] == "gp-queue-campaign-plan-789"


class TestS3BeforeSqs:
    def test_s3_write_happens_before_sqs_send(
        self, mock_s3, mock_sqs, call_log, monkeypatch
    ):
        monkeypatch.setenv("S3_RESULTS_BUCKET", "test-bucket")
        monkeypatch.setenv("OUTPUT_SQS_QUEUE_URL", "https://sqs.example.com/queue.fifo")

        output_module.write_result_to_s3(123, {"data": "test"})
        output_module.send_completion_message(123, "key", 5, "ts")

        ops = [c[0] for c in call_log]
        assert ops.index("s3_put_object") < ops.index("sqs_send_message")
