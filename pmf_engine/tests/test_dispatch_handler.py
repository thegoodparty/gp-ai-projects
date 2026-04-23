import json
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pmf_engine.control_plane.dispatch_handler import (
    handler,
    parse_dispatch_message,
    build_container_overrides,
)


@pytest.fixture(autouse=True)
def _default_dispatch_env(monkeypatch):
    import pmf_engine.control_plane.dispatch_handler as dh
    monkeypatch.setattr(dh, "ECS_CLUSTER_ARN", "arn:aws:ecs:us-west-2:123:cluster/pmf", raising=False)
    monkeypatch.setattr(dh, "ECS_TASK_DEFINITION", "pmf-engine:1", raising=False)
    monkeypatch.setattr(dh, "ECS_SUBNET_IDS", ["subnet-aaa", "subnet-bbb"], raising=False)
    monkeypatch.setattr(dh, "ECS_SECURITY_GROUP_ID", "sg-abc", raising=False)
    monkeypatch.setattr(dh, "RESULTS_QUEUE_URL", "https://sqs.example.com/callback.fifo", raising=False)
    monkeypatch.setattr(dh, "BROKER_URL", "https://broker.example.com", raising=False)
    monkeypatch.setattr(dh, "SERVICE_TOKEN", "svc-token-xyz", raising=False)


def _make_sqs_event(body: dict) -> dict:
    return {
        "Records": [
            {
                "messageId": "msg-001",
                "body": json.dumps(body),
                "attributes": {
                    "MessageGroupId": "test-group",
                },
            }
        ]
    }


def _mock_broker_success(broker_token="tok-abc123"):
    mock = MagicMock()
    mock.mint_run_token.return_value = {
        "broker_token": broker_token,
        "exp": 1700000000,
        "params_clean": {},
    }
    return mock


VALID_L2_PARAMS = {
    "state": "WI",
    "city": "Fall River",
    "l2DistrictType": "City",
    "l2DistrictName": "FALL RIVER CITY",
}


class TestParseDispatchMessage:
    def test_parses_valid_message(self):
        body = {
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": {"topic": "education"},
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["experiment_id"] == "voter_targeting"
        assert result["organization_slug"] == "org-123"
        assert result["run_id"] == "run-001"
        assert result["params"] == {"topic": "education"}

    def test_defaults_params_to_empty_dict(self):
        body = {
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["params"] == {}

    def test_raises_on_missing_experiment_id(self):
        body = {"organization_slug": "org-123", "run_id": "run-001"}
        with pytest.raises(ValueError, match="experiment_id"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_missing_organization_slug(self):
        body = {"experiment_id": "voter_targeting", "run_id": "run-001"}
        with pytest.raises(ValueError, match="organization_slug"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_missing_run_id(self):
        body = {"experiment_id": "voter_targeting", "organization_slug": "org-123"}
        with pytest.raises(ValueError, match="run_id"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError, match="Invalid"):
            parse_dispatch_message("not-json")


class TestBuildContainerOverrides:
    def test_builds_overrides_with_new_env_vars(self):
        experiment = {
            "instruction": "Analyze voter data.",
            "harness": "claude_sdk",
            "model": "sonnet",
            "contract": {"type": "json", "s3_key_template": "{experiment_id}/{run_id}/result.json"},
            "max_turns": 30,
            "cpu": "1024",
            "memory": "2048",
        }
        message = {
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-abc",
            "params": {"district": "CA-12"},
        }

        overrides = build_container_overrides(
            experiment=experiment,
            message=message,
            broker_token="tok-abc123",
            broker_url="https://broker.example.com",
            container_name="pmf-engine",
        )

        env_map = {e["name"]: e["value"] for e in overrides["containerOverrides"][0]["environment"]}
        assert env_map["EXPERIMENT_ID"] == "voter_targeting"
        assert env_map["ORGANIZATION_SLUG"] == "org-123"
        assert env_map["RUN_ID"] == "run-abc"
        assert env_map["HARNESS"] == "claude_sdk"
        assert env_map["AGENT_MODEL"] == "sonnet"
        assert env_map["BROKER_TOKEN"] == "tok-abc123"
        assert env_map["BROKER_URL"] == "https://broker.example.com"
        assert env_map["ANTHROPIC_BASE_URL"] == "https://broker.example.com/anthropic"
        assert env_map["ANTHROPIC_API_KEY"] == "tok-abc123"
        assert json.loads(env_map["PARAMS_JSON"]) == {"district": "CA-12"}
        assert env_map["TIMEOUT_SECONDS"] == "600"

    def test_no_artifact_bucket_or_callback_queue_in_overrides(self):
        experiment = {
            "harness": "claude_sdk",
            "model": "sonnet",
            "contract": {"type": "json", "s3_key_template": "t"},
        }
        message = {
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-abc",
            "params": {},
        }
        overrides = build_container_overrides(
            experiment=experiment,
            message=message,
            broker_token="tok",
            broker_url="https://broker.example.com",
            container_name="pmf-engine",
        )
        env_map = {e["name"]: e["value"] for e in overrides["containerOverrides"][0]["environment"]}
        assert "ARTIFACT_BUCKET" not in env_map
        assert "ARTIFACT_KEY_TEMPLATE" not in env_map
        assert "RESULTS_QUEUE_URL" not in env_map


class TestHandler:
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_dispatches_valid_experiment(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success("tok-run001")
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_ecs.run_task.assert_called_once()

        call_kwargs = mock_ecs.run_task.call_args.kwargs
        overrides = call_kwargs["overrides"]
        env_list = overrides["containerOverrides"][0]["environment"]
        env_map = {e["name"]: e["value"] for e in env_list}
        assert env_map["EXPERIMENT_ID"] == "voter_targeting"
        assert env_map["RUN_ID"] == "run-001"
        assert env_map["ORGANIZATION_SLUG"] == "org-123"
        assert env_map["HARNESS"] == "claude_sdk"
        assert env_map["AGENT_MODEL"] == "sonnet"
        assert env_map["BROKER_TOKEN"] == "tok-run001"
        assert json.loads(env_map["PARAMS_JSON"]) == dict(VALID_L2_PARAMS)

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_rejects_unknown_experiment(self, mock_get_ecs, mock_send_error_callback):
        mock_ecs = mock_get_ecs.return_value

        event = _make_sqs_event({
            "experiment_id": "nonexistent",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": {},
        })

        result = handler(event, None)
        mock_ecs.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"
        assert "nonexistent" in call_args[0][1]

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_unknown_experiment_id_added_to_batch_item_failures(
        self, mock_get_ecs, mock_send_error_callback
    ):
        mock_ecs = mock_get_ecs.return_value

        event = _make_sqs_event({
            "experiment_id": "nonexistent",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": {},
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_ecs.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_unknown_experiment_id_logs_error_not_warning(
        self, mock_get_ecs, mock_send_error_callback
    ):
        import pmf_engine.control_plane.dispatch_handler as dh
        records: list[logging.LogRecord] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        capture = _CaptureHandler(level=logging.DEBUG)
        original_level = dh.logger.level
        dh.logger.addHandler(capture)
        dh.logger.setLevel(logging.DEBUG)
        try:
            event = _make_sqs_event({
                "experiment_id": "nonexistent",
                "organization_slug": "org-123",
                "run_id": "run-001",
                "params": {},
            })
            handler(event, None)
        finally:
            dh.logger.removeHandler(capture)
            dh.logger.setLevel(original_level)

        error_records = [
            r for r in records
            if r.levelno >= logging.ERROR and "nonexistent" in r.getMessage()
        ]
        assert len(error_records) >= 1, (
            f"Expected ERROR-level log mentioning 'nonexistent', got: "
            f"{[(r.levelname, r.getMessage()) for r in records]}"
        )

        warning_records = [
            r for r in records
            if r.levelno == logging.WARNING and "nonexistent" in r.getMessage()
        ]
        assert warning_records == [], (
            f"Expected no WARNING-level log for unknown experiment, got: "
            f"{[r.getMessage() for r in warning_records]}"
        )

        assert any(
            "voter_targeting" in r.getMessage() and "walking_plan" in r.getMessage()
            for r in error_records
        ), "Expected error log to include known experiment IDs for operator triage"

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_ecs_failure(self, mock_get_ecs, mock_send_error_callback, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": "RESOURCE:MEMORY"}],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"
        assert call_args[0][1].startswith("ECS RunTask failed:")
        assert "RESOURCE:MEMORY" not in call_args[0][1]

    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_failure_on_invalid_message(self, mock_get_ecs):
        mock_ecs = mock_get_ecs.return_value

        event = {
            "Records": [
                {
                    "messageId": "msg-bad",
                    "body": "not-json",
                }
            ]
        }

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-bad"
        mock_ecs.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_failure_on_empty_tasks_array(self, mock_get_ecs, mock_send_error_callback, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"

    @patch("pmf_engine.control_plane.dispatch_handler.get_sqs_client")
    def test_send_error_callback_sqs_failure_logged_not_thrown(self, mock_get_sqs):
        from pmf_engine.control_plane import dispatch_handler as dh
        from pmf_engine.control_plane.dispatch_handler import send_error_callback

        mock_sqs = mock_get_sqs.return_value
        mock_sqs.send_message.side_effect = Exception("SQS unreachable")

        records: list[logging.LogRecord] = []

        class CollectingHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        collector = CollectingHandler(level=logging.DEBUG)
        dh.logger.addHandler(collector)
        try:
            message = {
                "experiment_id": "voter_targeting",
                "organization_slug": "org-123",
                "run_id": "run-001",
            }
            send_error_callback(message, "some error", "https://sqs.example.com/callback.fifo")
        finally:
            dh.logger.removeHandler(collector)

        error_records = [
            r for r in records
            if r.levelno >= logging.ERROR
            and "Failed to send error callback" in r.getMessage()
        ]
        assert error_records, "expected ERROR log when SQS send fails"
        combined = " ".join(r.getMessage() for r in error_records)
        assert "SQS unreachable" in combined

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_exception_sends_error_callback(self, mock_get_ecs, mock_send_error_callback, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.side_effect = Exception("Network timeout")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"
        assert call_args[0][1] == "ECS RunTask exception: Exception"
        assert "Network timeout" not in call_args[0][1]

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_failure_on_ecs_exception(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.side_effect = Exception("Network timeout")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"


class TestBrokerFlow:
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_mint_success_passes_token_to_ecs(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success("tok-from-broker")
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_ecs.run_task.assert_called_once()

        env_list = mock_ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
        env_map = {e["name"]: e["value"] for e in env_list}
        assert env_map["BROKER_TOKEN"] == "tok-from-broker"
        assert env_map["BROKER_URL"] == "https://broker.example.com"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_400_sends_error_callback_no_ecs(self, mock_get_ecs, mock_broker_cls, mock_send_error_callback):
        from pmf_engine.control_plane.broker_client import BrokerError
        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(
            400, "Param classifier rejected: nested objects", "Invalid experiment parameters"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args[0][1] == "Invalid experiment parameters"
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "broker-rejected-run-001"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_401_sends_error_callback(self, mock_get_ecs, mock_broker_cls, mock_send_error_callback):
        from pmf_engine.control_plane.broker_client import BrokerError
        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(401, "Invalid service token")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args[0][1] == "Broker rejected the request"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_400_without_user_safe_message_uses_generic(self, mock_get_ecs, mock_broker_cls, mock_send_error_callback):
        from pmf_engine.control_plane.broker_client import BrokerError
        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(400, "Some detail", "")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args[0][1] == "Broker rejected the request"


class TestNonDictParamsGuard:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_string_params_rejected_with_stable_dedup(
        self, mock_get_ecs, mock_emit_metric, mock_send_error_callback
    ):
        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-xyz",
            "params": "not a dict",
        })

        result = handler(event, None)

        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "invalid-params-type-run-xyz"
        assert "JSON object" in mock_send_error_callback.call_args[0][1]
        mock_emit_metric.assert_any_call("InvalidParamsType", "voter_targeting")
        assert result["batchItemFailures"] == []

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_list_params_does_not_crash(
        self, mock_get_ecs, mock_emit_metric, mock_send_error_callback
    ):
        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": [1, 2, 3],
        })

        result = handler(event, None)

        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert "JSON object" in mock_send_error_callback.call_args[0][1]

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_null_params_normalized_to_empty_dict_then_rejected_by_required(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        """Null params normalize to {} but every current experiment has required_params,
        so dispatch rejects with missing_params without calling broker or run_task.
        """
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": None,
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_broker_cls.return_value.mint_run_token.assert_not_called()
        mock_ecs.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "missing-params-run-001"


class TestErrorCallbackStableDedup:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_runtask_failure_uses_stable_dedup(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": "RESOURCE:MEMORY"}],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-abc",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "runtask-failed-run-abc"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_exception_uses_stable_dedup(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.side_effect = Exception("Network timeout")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-abc",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "runtask-exception-run-abc"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_rejection_uses_stable_dedup(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        from pmf_engine.control_plane.broker_client import BrokerError
        mock_broker_cls.return_value.mint_run_token.side_effect = BrokerError(
            400, "rejected", "Invalid experiment parameters"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-abc",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "broker-rejected-run-abc"


class TestMissingCriticalEnvVars:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_empty_subnet_ids_does_not_call_run_task(
        self, mock_get_ecs, mock_send_error_callback, monkeypatch
    ):
        import pmf_engine.control_plane.dispatch_handler as dh
        monkeypatch.setattr(dh, "ECS_CLUSTER_ARN", "arn:aws:ecs:us-west-2:123:cluster/pmf")
        monkeypatch.setattr(dh, "ECS_TASK_DEFINITION", "pmf-engine:1")
        monkeypatch.setattr(dh, "ECS_SUBNET_IDS", [])
        monkeypatch.setattr(dh, "ECS_SECURITY_GROUP_ID", "sg-abc")
        monkeypatch.setattr(dh, "RESULTS_QUEUE_URL", "https://sqs.example.com/callback.fifo")
        monkeypatch.setattr(dh, "BROKER_URL", "https://broker.example.com")
        monkeypatch.setattr(dh, "SERVICE_TOKEN", "svc-token")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-xyz",
            "params": {},
        })

        result = handler(event, None)
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        error_msg = mock_send_error_callback.call_args[0][1]
        assert "ECS_SUBNET_IDS" in error_msg
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "dispatch-misconfig-run-xyz"
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_empty_cluster_arn_does_not_call_run_task(
        self, mock_get_ecs, mock_send_error_callback, monkeypatch
    ):
        import pmf_engine.control_plane.dispatch_handler as dh
        monkeypatch.setattr(dh, "ECS_CLUSTER_ARN", "")
        monkeypatch.setattr(dh, "ECS_TASK_DEFINITION", "pmf-engine:1")
        monkeypatch.setattr(dh, "ECS_SUBNET_IDS", ["subnet-aaa"])
        monkeypatch.setattr(dh, "ECS_SECURITY_GROUP_ID", "sg-abc")
        monkeypatch.setattr(dh, "RESULTS_QUEUE_URL", "https://sqs.example.com/callback.fifo")
        monkeypatch.setattr(dh, "BROKER_URL", "https://broker.example.com")
        monkeypatch.setattr(dh, "SERVICE_TOKEN", "svc-token")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-xyz",
            "params": {},
        })

        result = handler(event, None)
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        error_msg = mock_send_error_callback.call_args[0][1]
        assert "ECS_CLUSTER_ARN" in error_msg


class TestParamsSizeLimit:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_oversized_params_rejected_before_ecs(
        self, mock_get_ecs, mock_emit_metric, mock_send_error_callback
    ):
        oversized = {f"key_{i}": "x" * 900 for i in range(12)}

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-big",
            "params": oversized,
        })

        result = handler(event, None)

        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        error_msg = mock_send_error_callback.call_args[0][1].lower()
        assert "size limit" in error_msg or "too large" in error_msg
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "params-too-large-run-big"
        assert any(
            call.args == ("ParamsTooLarge", "voter_targeting")
            for call in mock_emit_metric.call_args_list
        )

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_params_just_under_limit_proceed_to_ecs(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }
        small = {**VALID_L2_PARAMS, "note": "x" * 100}

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "params": small,
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_ecs.run_task.assert_called_once()


class TestRequiredParamsValidation:
    """Dispatcher validates that all required_params are present before minting a token.

    Missing required params → send_error_callback with reason 'missing_params',
    do NOT call run_task (saves a Fargate run).
    """

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_missing_state_rejected_before_mint(
        self, mock_get_ecs, mock_broker_cls, mock_emit_metric, mock_send_error_callback
    ):
        mock_broker_cls.return_value = _mock_broker_success()

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-missing-state",
            "params": {"city": "Yakima", "l2DistrictType": "City", "l2DistrictName": "YAKIMA CITY"},
        })

        handler(event, None)

        mock_broker_cls.return_value.mint_run_token.assert_not_called()
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()

        detail = mock_send_error_callback.call_args[0][1]
        assert "missing" in detail.lower()
        assert "state" in detail.lower()

        dedup = mock_send_error_callback.call_args.kwargs["dedup_id"]
        assert dedup == "missing-params-run-missing-state"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_missing_l2_district_rejected_for_l2_experiment(
        self, mock_get_ecs, mock_broker_cls, mock_emit_metric, mock_send_error_callback
    ):
        mock_broker_cls.return_value = _mock_broker_success()

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-no-district",
            "run_id": "run-no-l2",
            "params": {"state": "NC", "city": "Fayetteville"},
        })

        handler(event, None)

        mock_broker_cls.return_value.mint_run_token.assert_not_called()
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        detail = mock_send_error_callback.call_args[0][1]
        assert "l2districttype" in detail.lower() or "l2district" in detail.lower()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_empty_string_counts_as_missing(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker_cls.return_value = _mock_broker_success()

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-empty",
            "run_id": "run-empty-strings",
            "params": {"state": "", "city": "Fayetteville", "l2DistrictType": "", "l2DistrictName": ""},
        })

        handler(event, None)

        mock_broker_cls.return_value.mint_run_token.assert_not_called()
        mock_send_error_callback.assert_called_once()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_all_required_params_present_proceeds_to_mint(
        self, mock_get_ecs, mock_broker_cls
    ):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/ok"}],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-ok",
            "run_id": "run-ok",
            "params": {
                "state": "WI",
                "city": "Sturgeon Bay",
                "l2DistrictType": "City_Council_Commissioner_District",
                "l2DistrictName": "STURGEON BAY CITY ALDERMANIC 6",
            },
        })

        handler(event, None)

        mock_broker_cls.return_value.mint_run_token.assert_called_once()
        mock_get_ecs.return_value.run_task.assert_called_once()


class TestTransientBrokerErrors:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_transient_httpx_error_during_mint_yields_batch_item_failure(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = httpx.ConnectError("DNS failed")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-transient",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)

        assert isinstance(result, dict)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_4xx_still_goes_through_existing_error_callback_path(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        from pmf_engine.control_plane.broker_client import BrokerError
        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(
            400, "Param classifier rejected", "Invalid experiment parameters"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-terminal",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)

        assert result["batchItemFailures"] == []
        mock_send_error_callback.assert_called_once()
        mock_get_ecs.return_value.run_task.assert_not_called()


class TestDispatchHandlerErrorPathResilience:
    """Covers CRITICAL #3: ensure non-HTTPError exceptions during mint land
    an error callback AND a batch_item_failures entry (so gp-api sees the
    failure immediately AND SQS retries eventually reach the DLQ alarm).
    Also covers: if send_error_callback fails at the SQS layer, the caller
    must add to batch_item_failures so the message is re-delivered."""

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_programmer_error_during_mint_sends_callback_and_retries(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker_cls.side_effect = KeyError("missing config key somewhere")
        mock_send_error_callback.return_value = True

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-x",
            "run_id": "run-prog-err",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)

        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert "run-prog-err" in str(call_args) or "run-prog-err" in call_args.kwargs.get(
            "dedup_id", ""
        )
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_validation_error_with_failed_callback_adds_to_batch_item_failures(
        self, mock_get_ecs, mock_send_error_callback
    ):
        mock_send_error_callback.return_value = False

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-x",
            "run_id": "run-missing-params",
            "params": {},
        })

        result = handler(event, None)

        mock_send_error_callback.assert_called_once()
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}], (
            "When the SQS send of the error callback fails, the message must "
            "be retried so gp-api isn't left in PENDING forever"
        )
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_validation_error_with_successful_callback_does_not_retry(
        self, mock_get_ecs, mock_send_error_callback
    ):
        mock_send_error_callback.return_value = True

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-x",
            "run_id": "run-missing-params-ok",
            "params": {},
        })

        result = handler(event, None)

        mock_send_error_callback.assert_called_once()
        assert result["batchItemFailures"] == [], (
            "A successful error callback means gp-api is already aware of "
            "the FAILED state; SQS retry would spam duplicate callbacks"
        )


class TestSendErrorCallbackReturnValue:
    """send_error_callback signals success (True) or failure (False) so
    callers can decide whether to retry via batch_item_failures."""

    @patch("pmf_engine.control_plane.dispatch_handler.get_sqs_client")
    def test_returns_true_on_successful_sqs_send(self, mock_get_sqs):
        from pmf_engine.control_plane.dispatch_handler import send_error_callback

        mock_sqs = MagicMock()
        mock_sqs.send_message.return_value = {"MessageId": "ok"}
        mock_get_sqs.return_value = mock_sqs

        result = send_error_callback(
            {"experiment_id": "x", "organization_slug": "y", "run_id": "r1"},
            "err",
            "https://sqs.example.com/q.fifo",
        )
        assert result is True

    @patch("pmf_engine.control_plane.dispatch_handler.get_sqs_client")
    def test_returns_false_on_sqs_failure(self, mock_get_sqs):
        from pmf_engine.control_plane.dispatch_handler import send_error_callback

        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = RuntimeError("SQS unavailable")
        mock_get_sqs.return_value = mock_sqs

        result = send_error_callback(
            {"experiment_id": "x", "organization_slug": "y", "run_id": "r1"},
            "err",
            "https://sqs.example.com/q.fifo",
        )
        assert result is False

    def test_returns_false_when_queue_url_is_empty(self):
        from pmf_engine.control_plane.dispatch_handler import send_error_callback

        result = send_error_callback(
            {"experiment_id": "x", "organization_slug": "y", "run_id": "r1"},
            "err",
            "",
        )
        assert result is False, (
            "Empty queue URL means the callback went nowhere; caller must "
            "retry via batch_item_failures rather than silently ACK"
        )


class TestPriorArtifactVersionsValidation:
    """Covers CRITICAL #9: a malicious/compromised SQS producer must not be
    able to pin cross-org or path-traversal artifact keys via
    prior_artifact_versions. The broker's artifact_read trusts the ticket's
    prior_artifact_versions for dependency reads, so dispatch must validate
    the shape before minting a ticket with untrusted values.

    Allowed pattern: `{experiment_id}/{run_id}/artifact.json` where each
    segment is `[A-Za-z0-9_-]{1,64}`.
    """

    def test_rejects_path_traversal_in_value(self):
        body = {
            "experiment_id": "peer_city_benchmarking",
            "organization_slug": "acme",
            "run_id": "run-1",
            "params": {},
            "prior_artifact_versions": {
                "district_intel": "../../etc/passwd",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_rejects_cross_org_artifact_key(self):
        body = {
            "experiment_id": "peer_city_benchmarking",
            "organization_slug": "acme",
            "run_id": "run-2",
            "params": {},
            "prior_artifact_versions": {
                "district_intel": "district_intel/other-org-run-id/artifact.json/../secrets.txt",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_rejects_wrong_file_suffix(self):
        body = {
            "experiment_id": "peer_city_benchmarking",
            "organization_slug": "acme",
            "run_id": "run-3",
            "params": {},
            "prior_artifact_versions": {
                "district_intel": "district_intel/abc-123/latest.json",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_rejects_empty_segment(self):
        body = {
            "experiment_id": "peer_city_benchmarking",
            "organization_slug": "acme",
            "run_id": "run-4",
            "params": {},
            "prior_artifact_versions": {
                "district_intel": "/abc-123/artifact.json",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_accepts_valid_prior_artifact_key(self):
        body = {
            "experiment_id": "peer_city_benchmarking",
            "organization_slug": "acme",
            "run_id": "run-5",
            "params": {},
            "prior_artifact_versions": {
                "district_intel": "district_intel/d188bc17-87bd-4fe0-9b45-d34d3b301d98/artifact.json",
            },
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["prior_artifact_versions"] == body["prior_artifact_versions"]

    def test_accepts_absent_prior_artifact_versions(self):
        body = {
            "experiment_id": "district_intel",
            "organization_slug": "acme",
            "run_id": "run-6",
            "params": {},
        }
        result = parse_dispatch_message(json.dumps(body))
        assert "prior_artifact_versions" not in result or result["prior_artifact_versions"] is None

    def test_rejects_non_dict_prior_artifact_versions(self):
        body = {
            "experiment_id": "district_intel",
            "organization_slug": "acme",
            "run_id": "run-7",
            "params": {},
            "prior_artifact_versions": "not-a-dict",
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))


class TestEcsErrorCallbackDoesNotLeakRawDetail:
    """Security: raw ECS failure reasons and exception messages often contain
    IAM role ARNs, account IDs, and policy details (e.g.,
    'AccessDeniedException: User: arn:aws:iam::333022194791:role/...'). These
    land in gp-api's ExperimentRun.error field and surface to users. The
    dispatcher must log the full detail server-side but pass a SANITIZED
    generic message to send_error_callback.
    """

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_failure_callback_does_not_leak_raw_reason(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [],
            "failures": [
                {
                    "reason": (
                        "AccessDeniedException: User: "
                        "arn:aws:iam::333022194791:role/test-dispatch-role is "
                        "not authorized to perform: ecs:RunTask on resource: "
                        "arn:aws:ecs:us-west-2:333022194791:task-definition/pmf-engine:42"
                    )
                }
            ],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-iam-leak",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)

        mock_send_error_callback.assert_called_once()
        error_str = mock_send_error_callback.call_args[0][1]
        assert "arn:aws:iam" not in error_str, (
            f"Expected sanitized error, got ARN-leaking message: {error_str!r}"
        )
        assert "333022194791" not in error_str, (
            f"Expected sanitized error, got account-id-leaking message: {error_str!r}"
        )
        assert "ECS RunTask failed" in error_str

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_exception_callback_does_not_leak_raw_exception_message(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        from botocore.exceptions import ClientError
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.side_effect = ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": (
                        "User: arn:aws:iam::333022194791:role/test-dispatch-role "
                        "is not authorized to perform: ecs:RunTask"
                    ),
                }
            },
            "RunTask",
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-123",
            "run_id": "run-iam-exc-leak",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)

        mock_send_error_callback.assert_called_once()
        error_str = mock_send_error_callback.call_args[0][1]
        assert "arn:aws:iam" not in error_str, (
            f"Expected sanitized error, got ARN-leaking message: {error_str!r}"
        )
        assert "333022194791" not in error_str, (
            f"Expected sanitized error, got account-id-leaking message: {error_str!r}"
        )
        assert "ClientError" in error_str, (
            f"Expected exception type name in sanitized message, got: {error_str!r}"
        )

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_failure_logs_full_detail_server_side(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        import pmf_engine.control_plane.dispatch_handler as dh
        mock_broker_cls.return_value = _mock_broker_success()
        raw_reason = (
            "AccessDeniedException: User: "
            "arn:aws:iam::333022194791:role/test-role not authorized"
        )
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": raw_reason}],
        }

        records: list[logging.LogRecord] = []

        class CollectingHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        collector = CollectingHandler(level=logging.DEBUG)
        original_level = dh.logger.level
        dh.logger.addHandler(collector)
        dh.logger.setLevel(logging.DEBUG)
        try:
            event = _make_sqs_event({
                "experiment_id": "voter_targeting",
                "organization_slug": "org-123",
                "run_id": "run-log-detail",
                "params": dict(VALID_L2_PARAMS),
            })
            handler(event, None)
        finally:
            dh.logger.removeHandler(collector)
            dh.logger.setLevel(original_level)

        combined = " ".join(r.getMessage() for r in records if r.levelno >= logging.ERROR)
        assert "arn:aws:iam::333022194791" in combined, (
            f"Operator diagnostic log must retain full ARN detail; got: {combined!r}"
        )


class TestRunTaskFailureCleansUpMintedTicket:
    """Covers CRITICAL #1 companion: when ecs.run_task fails after a
    successful mint, the freshly-issued broker_token + run-lock must be
    deleted so (a) the token can't be reused from logs/CloudWatch and (b)
    the same run_id is free to be re-dispatched immediately.

    Without this, a retry of the same run_id 409s against the stale
    run-lock until the lock's TTL expires (~4h)."""

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_returns_failures_triggers_delete_run_token(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker = _mock_broker_success("tok-to-clean")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.return_value = {
            "failures": [{"reason": "CAPACITY_EXHAUSTED"}],
            "tasks": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-x",
            "run_id": "run-ecs-cap",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)

        mock_broker.delete_run_token.assert_called_once_with(
            broker_token="tok-to-clean", run_id="run-ecs-cap"
        )

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_raises_triggers_delete_run_token(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker = _mock_broker_success("tok-to-clean-exc")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.side_effect = RuntimeError(
            "ECS control plane transient"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-x",
            "run_id": "run-ecs-boom",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)

        mock_broker.delete_run_token.assert_called_once_with(
            broker_token="tok-to-clean-exc", run_id="run-ecs-boom"
        )

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_delete_run_token_failure_does_not_prevent_error_callback(
        self, mock_get_ecs, mock_broker_cls, mock_send_error_callback
    ):
        mock_broker = _mock_broker_success("tok-doomed")
        mock_broker.delete_run_token.side_effect = RuntimeError("broker unreachable")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.side_effect = RuntimeError(
            "ECS transient"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-x",
            "run_id": "run-double-fail",
            "params": dict(VALID_L2_PARAMS),
        })

        result = handler(event, None)

        mock_send_error_callback.assert_called_once()
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}], (
            "delete_run_token failing must NOT mask the primary ECS failure — "
            "the error callback + batch_item_failures must still fire"
        )

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_successful_run_task_does_not_call_delete_run_token(
        self, mock_get_ecs, mock_broker_cls
    ):
        mock_broker = _mock_broker_success("tok-good")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.return_value = {
            "failures": [],
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "organization_slug": "org-x",
            "run_id": "run-ok",
            "params": dict(VALID_L2_PARAMS),
        })

        handler(event, None)

        mock_broker.delete_run_token.assert_not_called()
