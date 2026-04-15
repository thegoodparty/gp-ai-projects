import importlib
import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ARTIFACT_BUCKET", "gp-agent-artifacts-dev")

from pmf_engine.control_plane import callback_handler as callback_handler_module
from pmf_engine.control_plane.callback_handler import (
    artifact_exists,
    format_gp_api_result_message,
    handler,
    parse_callback_message,
)

validate_contract = artifact_exists

GOD_QUEUE_MESSAGE_GROUP_ID = "gp-queue-agentExperiments"


def _make_sqs_event(body: dict) -> dict:
    return {
        "Records": [
            {
                "messageId": "msg-001",
                "body": json.dumps(body),
                "attributes": {"MessageGroupId": "test-group"},
            }
        ]
    }


def _valid_callback_body(**overrides) -> dict:
    defaults = {
        "experiment_id": "hello_world",
        "run_id": "run-001",
        "candidate_id": "cand-123",
        "status": "success",
        "artifact_key": "hello_world/run-001/result.json",
        "artifact_bucket": "gp-agent-artifacts-dev",
        "cost_usd": 0.05,
        "duration_seconds": 12.3,
        "error": None,
    }
    defaults.update(overrides)
    return defaults


class TestParseCallbackMessage:
    def test_parses_valid_success_message(self):
        result = parse_callback_message(json.dumps(_valid_callback_body()))
        assert result["experiment_id"] == "hello_world"
        assert result["status"] == "success"
        assert result["artifact_key"] == "hello_world/run-001/result.json"

    def test_parses_error_message(self):
        result = parse_callback_message(json.dumps(_valid_callback_body(
            status="error", error="Agent crashed", artifact_key=""
        )))
        assert result["status"] == "error"
        assert result["error"] == "Agent crashed"

    def test_raises_on_missing_required_field(self):
        body = {"experiment_id": "hello_world"}
        with pytest.raises(ValueError, match="run_id"):
            parse_callback_message(json.dumps(body))

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError, match="Invalid"):
            parse_callback_message("not-json")


class TestValidateContract:
    def test_returns_true_when_artifact_exists(self):
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 1234}

        result = validate_contract(
            s3_client=mock_s3,
            bucket="gp-agent-artifacts-dev",
            key="hello_world/run-001/result.json",
        )
        assert result is True
        mock_s3.head_object.assert_called_once_with(
            Bucket="gp-agent-artifacts-dev",
            Key="hello_world/run-001/result.json",
        )

    def test_returns_false_when_artifact_missing_404(self):
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        result = validate_contract(
            s3_client=mock_s3,
            bucket="gp-agent-artifacts-dev",
            key="hello_world/run-001/result.json",
        )
        assert result is False

    def test_raises_on_non_404_s3_error(self):
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )

        with pytest.raises(ClientError):
            validate_contract(
                s3_client=mock_s3,
                bucket="gp-agent-artifacts-dev",
                key="hello_world/run-001/result.json",
            )

    def test_returns_false_when_bucket_or_key_is_empty(self):
        mock_s3 = MagicMock()
        assert validate_contract(mock_s3, bucket="gp-agent-artifacts-dev", key="") is False
        assert validate_contract(mock_s3, bucket="", key="some/key") is False
        assert validate_contract(mock_s3, bucket="", key="") is False
        mock_s3.head_object.assert_not_called()


class TestFormatGodQueueMessage:
    def test_wraps_in_envelope_with_camel_case_keys(self):
        callback = _valid_callback_body()
        result = format_gp_api_result_message(callback)

        assert result["type"] == "agentExperimentResult"
        data = result["data"]
        assert data["experimentId"] == "hello_world"
        assert data["runId"] == "run-001"
        assert data["candidateId"] == "cand-123"
        assert data["status"] == "success"
        assert data["artifactKey"] == "hello_world/run-001/result.json"
        assert data["artifactBucket"] == "gp-agent-artifacts-dev"
        assert data["durationSeconds"] == 12.3

    def test_excludes_cost_usd_from_envelope(self):
        callback = _valid_callback_body(cost_usd=1.50)
        result = format_gp_api_result_message(callback)
        assert "costUsd" not in result["data"]
        assert "cost_usd" not in result["data"]

    def test_includes_error_for_failed_status(self):
        callback = _valid_callback_body(status="failed", error="Agent crashed")
        result = format_gp_api_result_message(callback)
        assert result["data"]["status"] == "failed"
        assert result["data"]["error"] == "Agent crashed"

    def test_maps_contract_violation_status(self):
        callback = _valid_callback_body(
            status="contract_violation",
            error="artifact not found",
        )
        result = format_gp_api_result_message(callback)
        assert result["data"]["status"] == "contract_violation"
        assert result["data"]["error"] == "artifact not found"

    def test_omits_none_error(self):
        callback = _valid_callback_body(error=None)
        result = format_gp_api_result_message(callback)
        assert "error" not in result["data"]

    def test_omits_empty_artifact_fields(self):
        callback = _valid_callback_body(artifact_key="", artifact_bucket="")
        result = format_gp_api_result_message(callback)
        assert "artifactKey" not in result["data"]
        assert "artifactBucket" not in result["data"]


class TestHandler:
    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_forwards_success_in_gp_api_result_envelope(self, mock_get_s3, mock_get_sqs):
        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value
        mock_s3.head_object.return_value = {"ContentLength": 100}

        event = _make_sqs_event(_valid_callback_body())
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        mock_sqs.send_message.assert_called_once()
        call_kwargs = mock_sqs.send_message.call_args[1]

        assert call_kwargs["MessageDeduplicationId"] == "run-001-result"
        sent_body = json.loads(call_kwargs["MessageBody"])
        assert sent_body["type"] == "agentExperimentResult"
        assert sent_body["data"]["experimentId"] == "hello_world"
        assert sent_body["data"]["status"] == "success"
        assert sent_body["data"]["artifactKey"] == "hello_world/run-001/result.json"

    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_uses_gp_api_result_message_group_id(self, mock_get_s3, mock_get_sqs):
        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value
        mock_s3.head_object.return_value = {"ContentLength": 100}

        event = _make_sqs_event(_valid_callback_body())
        handler(event, None)

        call_kwargs = mock_sqs.send_message.call_args[1]
        assert call_kwargs["MessageGroupId"] == GOD_QUEUE_MESSAGE_GROUP_ID

    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_rewrites_status_on_contract_violation(self, mock_get_s3, mock_get_sqs):
        from botocore.exceptions import ClientError

        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        event = _make_sqs_event(_valid_callback_body())
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        sent_body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert sent_body["data"]["status"] == "contract_violation"
        assert "Artifact missing from S3" in sent_body["data"]["error"]

    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_forwards_error_status_without_contract_check(self, mock_get_s3, mock_get_sqs):
        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value

        event = _make_sqs_event(_valid_callback_body(
            status="error", error="Agent crashed", artifact_key=""
        ))
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        mock_s3.head_object.assert_not_called()
        sent_body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert sent_body["type"] == "agentExperimentResult"
        assert sent_body["data"]["status"] == "error"
        assert sent_body["data"]["error"] == "Agent crashed"

    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_reports_failure_on_invalid_message(self, mock_get_s3, mock_get_sqs):
        event = {
            "Records": [{"messageId": "msg-bad", "body": "not-json"}]
        }
        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-bad"

    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_reports_failure_when_sqs_send_fails(self, mock_get_s3, mock_get_sqs):
        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value
        mock_s3.head_object.return_value = {"ContentLength": 100}
        mock_sqs.send_message.side_effect = Exception("SQS unavailable")

        event = _make_sqs_event(_valid_callback_body())
        result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"

    @patch("pmf_engine.control_plane.callback_handler.ARTIFACT_BUCKET", "gp-agent-artifacts-dev")
    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_rejects_mismatched_artifact_bucket(self, mock_get_s3, mock_get_sqs):
        mock_sqs = mock_get_sqs.return_value

        event = _make_sqs_event(_valid_callback_body(artifact_bucket="some-other-bucket"))
        result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        mock_sqs.send_message.assert_not_called()

    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_reports_failure_on_s3_non_404_error(self, mock_get_s3, mock_get_sqs):
        from botocore.exceptions import ClientError

        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )

        event = _make_sqs_event(_valid_callback_body())
        result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_sqs.send_message.assert_not_called()


class TestNonClientErrorIsolatedToFailingRecord:
    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_botocore_error_only_fails_the_bad_record_in_batch(self, mock_get_s3, mock_get_sqs):
        from botocore.exceptions import BotoCoreError

        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value

        call_count = {"n": 0}

        def side_effect(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise BotoCoreError()
            return {"ContentLength": 100}

        mock_s3.head_object.side_effect = side_effect

        event = {
            "Records": [
                {
                    "messageId": "msg-bad",
                    "body": json.dumps(_valid_callback_body(run_id="run-bad")),
                    "attributes": {"MessageGroupId": "g"},
                },
                {
                    "messageId": "msg-good",
                    "body": json.dumps(_valid_callback_body(run_id="run-good")),
                    "attributes": {"MessageGroupId": "g"},
                },
            ]
        }

        result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-bad"
        assert mock_sqs.send_message.call_count == 1
        sent_body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert sent_body["data"]["runId"] == "run-good"


class TestArtifactBucketRequired:
    def test_module_reload_fails_when_artifact_bucket_empty(self):
        with patch.dict(os.environ, {"ARTIFACT_BUCKET": ""}, clear=False):
            with pytest.raises((RuntimeError, ValueError), match="ARTIFACT_BUCKET"):
                importlib.reload(callback_handler_module)

        os.environ["ARTIFACT_BUCKET"] = "gp-agent-artifacts-dev"
        importlib.reload(callback_handler_module)


class TestBucketMismatchLogging:
    @patch("pmf_engine.control_plane.callback_handler.ARTIFACT_BUCKET", "gp-agent-artifacts-dev")
    @patch("pmf_engine.control_plane.callback_handler.get_cloudwatch_client")
    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_mismatch_log_has_candidate_and_message_id_and_emits_metric(
        self, mock_get_s3, mock_get_sqs, mock_get_cw
    ):
        mock_cw = mock_get_cw.return_value

        captured = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                captured.append(record)

        list_handler = ListHandler(level=logging.ERROR)
        module_logger = logging.getLogger("pmf_engine.control_plane.callback_handler")
        module_logger.addHandler(list_handler)
        try:
            event = _make_sqs_event(_valid_callback_body(
                artifact_bucket="attacker-bucket",
                candidate_id="cand-999",
            ))
            handler(event, None)
        finally:
            module_logger.removeHandler(list_handler)

        mismatch_logs = [r for r in captured if "attacker-bucket" in r.getMessage()]
        assert len(mismatch_logs) >= 1
        msg = mismatch_logs[0].getMessage()
        assert "cand-999" in msg
        assert "msg-001" in msg
        assert "gp-agent-artifacts-dev" in msg

        mock_cw.put_metric_data.assert_called_once()
        call_kwargs = mock_cw.put_metric_data.call_args[1]
        metric_data = call_kwargs["MetricData"][0]
        assert metric_data["MetricName"] == "BucketMismatch"
        dim_names = {d["Name"] for d in metric_data["Dimensions"]}
        assert "Environment" in dim_names
        assert "ExperimentId" in dim_names


class TestArtifactExistsRename:
    def test_artifact_exists_importable(self):
        from pmf_engine.control_plane.callback_handler import artifact_exists as _ae
        assert callable(_ae)

    @patch("pmf_engine.control_plane.callback_handler.get_sqs_client")
    @patch("pmf_engine.control_plane.callback_handler.get_s3_client")
    def test_missing_artifact_error_says_artifact_missing_from_s3(self, mock_get_s3, mock_get_sqs):
        from botocore.exceptions import ClientError

        mock_s3 = mock_get_s3.return_value
        mock_sqs = mock_get_sqs.return_value
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        event = _make_sqs_event(_valid_callback_body())
        handler(event, None)

        sent_body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert sent_body["data"]["status"] == "contract_violation"
        assert "Artifact missing from S3" in sent_body["data"]["error"]


class TestParseCallbackMessageEmptyStrings:
    @pytest.mark.parametrize("field", ["experiment_id", "run_id", "candidate_id", "status"])
    def test_raises_on_empty_string_field(self, field):
        body = _valid_callback_body()
        body[field] = ""
        with pytest.raises(ValueError, match=field):
            parse_callback_message(json.dumps(body))
