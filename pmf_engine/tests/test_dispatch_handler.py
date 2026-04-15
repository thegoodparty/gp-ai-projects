import json
import logging
from unittest.mock import MagicMock, patch

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
    monkeypatch.setattr(dh, "CALLBACK_QUEUE_URL", "https://sqs.example.com/callback.fifo", raising=False)
    monkeypatch.setattr(dh, "ARTIFACT_BUCKET", "gp-agent-artifacts-dev", raising=False)


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


class TestParseDispatchMessage:
    def test_parses_valid_message(self):
        body = {
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {"topic": "education"},
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["experiment_id"] == "voter_targeting"
        assert result["candidate_id"] == "cand-123"
        assert result["run_id"] == "run-001"
        assert result["params"] == {"topic": "education"}

    def test_defaults_params_to_empty_dict(self):
        body = {
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["params"] == {}

    def test_raises_on_missing_experiment_id(self):
        body = {"candidate_id": "cand-123", "run_id": "run-001"}
        with pytest.raises(ValueError, match="experiment_id"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_missing_candidate_id(self):
        body = {"experiment_id": "voter_targeting", "run_id": "run-001"}
        with pytest.raises(ValueError, match="candidate_id"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_missing_run_id(self):
        body = {"experiment_id": "voter_targeting", "candidate_id": "cand-123"}
        with pytest.raises(ValueError, match="run_id"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError, match="Invalid"):
            parse_dispatch_message("not-json")


class TestBuildContainerOverrides:
    def test_builds_overrides_with_all_fields(self):
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
            "candidate_id": "cand-123",
            "run_id": "run-abc",
            "params": {"district": "CA-12"},
        }

        overrides = build_container_overrides(
            experiment=experiment,
            message=message,
            artifact_bucket="gp-agent-artifacts-dev",
            callback_queue_url="https://sqs.example.com/callback.fifo",
            container_name="pmf-engine",
        )

        env_map = {e["name"]: e["value"] for e in overrides["containerOverrides"][0]["environment"]}
        assert env_map["EXPERIMENT_ID"] == "voter_targeting"
        assert env_map["CANDIDATE_ID"] == "cand-123"
        assert env_map["RUN_ID"] == "run-abc"
        assert env_map["HARNESS"] == "claude_sdk"
        assert env_map["AGENT_MODEL"] == "sonnet"
        assert env_map["ARTIFACT_BUCKET"] == "gp-agent-artifacts-dev"
        assert env_map["CALLBACK_QUEUE_URL"] == "https://sqs.example.com/callback.fifo"
        assert json.loads(env_map["PARAMS_JSON"]) == {"district": "CA-12"}
        assert env_map["ARTIFACT_KEY_TEMPLATE"] == "{experiment_id}/{run_id}/result.json"
        assert env_map["TIMEOUT_SECONDS"] == "600"


class TestHandler:
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_dispatches_valid_experiment(self, mock_get_ecs):
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {},
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
        assert env_map["CANDIDATE_ID"] == "cand-123"
        assert env_map["HARNESS"] == "claude_sdk"
        assert env_map["AGENT_MODEL"] == "sonnet"
        assert env_map["ARTIFACT_KEY_TEMPLATE"] == "{experiment_id}/{run_id}/voter_targeting.json"
        assert json.loads(env_map["PARAMS_JSON"]) == {}

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_rejects_unknown_experiment(self, mock_get_ecs, mock_send_error_callback):
        mock_ecs = mock_get_ecs.return_value

        event = _make_sqs_event({
            "experiment_id": "nonexistent",
            "candidate_id": "cand-123",
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
            "candidate_id": "cand-123",
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
                "candidate_id": "cand-123",
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

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_ecs_failure(self, mock_get_ecs, mock_send_error_callback):
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": "RESOURCE:MEMORY"}],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {},
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"
        assert "RESOURCE:MEMORY" in call_args[0][1]

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

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_failure_on_empty_tasks_array(self, mock_get_ecs, mock_send_error_callback):
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {},
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"

    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_missing_s3_key_template_raises_key_error(self, mock_get_ecs):
        from pmf_engine.control_plane.dispatch_handler import build_container_overrides
        experiment_no_template = {
            "harness": "claude_sdk",
            "model": "opus",
            "timeout_seconds": 600,
            "contract": {},
        }
        message = {
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {},
        }

        with pytest.raises(KeyError, match="s3_key_template"):
            build_container_overrides(
                experiment=experiment_no_template,
                message=message,
                artifact_bucket="bucket",
                callback_queue_url="https://sqs.example.com/q.fifo",
                container_name="pmf-engine",
            )

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
                "candidate_id": "cand-123",
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

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_exception_sends_error_callback(self, mock_get_ecs, mock_send_error_callback):
        """Fix #7: ECS RunTask exception path must send error callback, not just add to batch failures."""
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.side_effect = Exception("Network timeout")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {},
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"
        assert "Network timeout" in call_args[0][1]

    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_screening_rejection_skips_ecs_and_sends_callback(
        self, mock_get_ecs, mock_send_error_callback, mock_emit_metric
    ):
        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {"topic": {"nested": "object"}},
        })

        result = handler(event, None)

        assert result["batchItemFailures"] == []
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args[0][1] == "Invalid experiment parameters"
        mock_emit_metric.assert_called_once_with("voter_targeting", "cand-123", "nested_object")

    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_screening_pass_proceeds_to_ecs(self, mock_get_ecs, mock_screen):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=True)
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {"city": "Hendersonville"},
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_ecs.run_task.assert_called_once()

    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_failure_on_ecs_exception(self, mock_get_ecs):
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.side_effect = Exception("Network timeout")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {},
        })

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"


class TestNonDictParamsGuard:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_string_params_rejected_with_stable_dedup(
        self, mock_get_ecs, mock_emit_metric, mock_send_error_callback
    ):
        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-xyz",
            "params": "not a dict",
        })

        result = handler(event, None)

        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "invalid-params-type-run-xyz"
        assert "JSON object" in mock_send_error_callback.call_args[0][1]
        mock_emit_metric.assert_any_call("voter_targeting", "cand-123", "invalid_params_type")
        assert result["batchItemFailures"] == []

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_list_params_does_not_crash(
        self, mock_get_ecs, mock_emit_metric, mock_send_error_callback
    ):
        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": [1, 2, 3],
        })

        result = handler(event, None)

        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert "JSON object" in mock_send_error_callback.call_args[0][1]

    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_null_params_normalized_to_empty_dict(self, mock_get_ecs, mock_screen):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=True)
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": None,
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_ecs.run_task.assert_called_once()
        env_list = mock_ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
        env_map = {e["name"]: e["value"] for e in env_list}
        assert env_map["PARAMS_JSON"] == "{}"


class TestErrorCallbackStableDedup:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_llm_flagged_uses_stable_dedup(
        self, mock_get_ecs, mock_screen, mock_emit_metric, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(
            safe=False, reason="llm_flagged", flagged_key="topic"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-abc",
            "params": {"topic": "bad"},
        })

        handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "screening-rejected-run-abc"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_runtask_failure_uses_stable_dedup(
        self, mock_get_ecs, mock_screen, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=True)
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": "RESOURCE:MEMORY"}],
        }

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-abc",
            "params": {},
        })

        handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "runtask-failed-run-abc"

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_exception_uses_stable_dedup(
        self, mock_get_ecs, mock_screen, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=True)
        mock_get_ecs.return_value.run_task.side_effect = Exception("Network timeout")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-abc",
            "params": {},
        })

        handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "runtask-exception-run-abc"


class TestScreenerOutageDistinction:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_screener_unavailable_retries_via_sqs_no_callback(
        self, mock_get_ecs, mock_screen, mock_emit_metric, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(
            safe=False, reason="screener_unavailable: RuntimeError"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {"topic": "legitimate"},
        })

        result = handler(event, None)
        mock_send_error_callback.assert_not_called()
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_screener_not_configured_retries_no_callback(
        self, mock_get_ecs, mock_screen, mock_emit_metric, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=False, reason="screener_not_configured")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {"topic": "x"},
        })

        result = handler(event, None)
        mock_send_error_callback.assert_not_called()
        assert len(result["batchItemFailures"]) == 1

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_screener_invalid_response_retries_no_callback(
        self, mock_get_ecs, mock_screen, mock_emit_metric, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=False, reason="screener_invalid_response")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {"topic": "x"},
        })

        result = handler(event, None)
        mock_send_error_callback.assert_not_called()
        assert len(result["batchItemFailures"]) == 1

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_llm_flagged_sends_terminal_callback_no_retry(
        self, mock_get_ecs, mock_screen, mock_emit_metric, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(
            safe=False, reason="llm_flagged", flagged_key="topic"
        )

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": {"topic": "prompt injection attempt"},
        })

        result = handler(event, None)
        mock_send_error_callback.assert_called_once()
        assert result["batchItemFailures"] == []
        mock_get_ecs.return_value.run_task.assert_not_called()


class TestMissingCriticalEnvVars:
    """At handler invocation, the dispatch Lambda must fail fast if any
    required ECS/SQS env var is missing or empty. Silently passing
    subnets=[] or cluster='' to run_task produces opaque ClientErrors that
    are hard to debug and slow to surface to gp-api."""

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
        monkeypatch.setattr(dh, "CALLBACK_QUEUE_URL", "https://sqs.example.com/callback.fifo")
        monkeypatch.setattr(dh, "ARTIFACT_BUCKET", "gp-agent-artifacts-dev")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
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
        monkeypatch.setattr(dh, "CALLBACK_QUEUE_URL", "https://sqs.example.com/callback.fifo")
        monkeypatch.setattr(dh, "ARTIFACT_BUCKET", "gp-agent-artifacts-dev")

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
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
    @patch("pmf_engine.control_plane.dispatch_handler.emit_screening_rejected_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_oversized_params_rejected_before_ecs(
        self, mock_get_ecs, mock_screen, mock_emit_metric, mock_send_error_callback
    ):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=True)
        oversized = {f"key_{i}": "x" * 900 for i in range(12)}

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
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
            call.args == ("voter_targeting", "cand-123", "params_too_large")
            for call in mock_emit_metric.call_args_list
        )

    @patch("pmf_engine.control_plane.dispatch_handler.screen_params")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_params_just_under_limit_proceed_to_ecs(self, mock_get_ecs, mock_screen):
        from pmf_engine.control_plane.param_screening import ScreeningResult
        mock_screen.return_value = ScreeningResult(safe=True)
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }
        small = {"key": "x" * 100}

        event = _make_sqs_event({
            "experiment_id": "voter_targeting",
            "candidate_id": "cand-123",
            "run_id": "run-001",
            "params": small,
        })

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_ecs.run_task.assert_called_once()
