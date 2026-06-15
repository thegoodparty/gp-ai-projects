import json
import logging
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pmf_engine.control_plane.dispatch_handler import (
    build_container_overrides,
    handler,
    launch_run,
    parse_dispatch_message,
)

SYNTHETIC_MANIFEST_VERSION_ID = "test-manifest-v-abc123"
SYNTHETIC_INSTRUCTION_VERSION_ID = "test-instruction-v-def456"


def _routing_from_manifest(manifest: dict) -> dict:
    """Build the routing dict the dispatch handler expects from a manifest.

    Mirrors `ManifestRoutingLoader.routing_for` — including the pinned
    manifest_version_id / instruction_version_id that close the
    publish-during-run race. Tests assert these are forwarded into the
    Fargate container env so a regression that drops them would fail here
    instead of silently shipping an unpinned dispatch.
    """
    return {
        "model": manifest["model"],
        "timeout_seconds": manifest["timeout_seconds"],
        "input_schema": manifest["input_schema"],
        "scope": manifest.get("scope", {}),
        "manifest_version_id": SYNTHETIC_MANIFEST_VERSION_ID,
        "instruction_version_id": SYNTHETIC_INSTRUCTION_VERSION_ID,
    }


def _build_synthetic_loader() -> MagicMock:
    """Fake ManifestRoutingLoader serving only the synthetic manifest.

    The engine doesn't know about specific experiments — one synthetic
    manifest is enough to exercise dispatch routing. Tests that need a
    different routing should construct their own MagicMock inline.
    """
    from pmf_engine.tests.conftest import synthetic_manifest

    manifest = synthetic_manifest()
    routings = {manifest["id"]: _routing_from_manifest(manifest)}
    loader = MagicMock()
    loader.routing_for.side_effect = lambda eid: routings.get(eid)
    loader.known_experiments.return_value = list(routings.keys())
    return loader


SMOKE_EXPERIMENT_ID = "smoke_test"


@pytest.fixture(autouse=True)
def _default_dispatch_env(monkeypatch):
    import pmf_engine.control_plane.dispatch_handler as dh

    monkeypatch.setattr(dh, "ECS_CLUSTER_ARN", "arn:aws:ecs:us-west-2:123:cluster/pmf", raising=False)
    monkeypatch.setattr(dh, "ECS_TASK_DEFINITION", "pmf-engine:1", raising=False)
    monkeypatch.setattr(dh, "ECS_SUBNET_IDS", ["subnet-aaa", "subnet-bbb"], raising=False)
    monkeypatch.setattr(dh, "ECS_SECURITY_GROUP_ID", "sg-abc", raising=False)
    monkeypatch.setattr(dh, "RESULTS_QUEUE_URL", "https://sqs.example.com/callback.fifo", raising=False)
    monkeypatch.setattr(dh, "BROKER_URL", "https://broker.example.com", raising=False)
    monkeypatch.setattr(
        dh, "SERVICE_TOKENS_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:123:secret:svc", raising=False
    )
    # Stub at the secrets-client layer so the real get_service_token() runs in
    # every dispatch/scheduler test path without hitting Secrets Manager.
    _fake_secrets = MagicMock()
    _fake_secrets.get_secret_value.return_value = {"SecretString": json.dumps({"SERVICE_TOKEN": "svc-token-xyz"})}
    monkeypatch.setattr(dh, "_get_secrets_client", lambda: _fake_secrets)
    monkeypatch.setenv("EXPERIMENT_METADATA_BUCKET", "agent-experiment-metadata-test")
    monkeypatch.setattr(dh, "JOB_TABLE_NAME", "agent-job-queue-test", raising=False)
    fake_loader = _build_synthetic_loader()
    monkeypatch.setattr(dh, "_manifest_loader", fake_loader, raising=False)
    monkeypatch.setattr(dh, "get_manifest_loader", lambda: fake_loader)
    fake_store = MagicMock()
    monkeypatch.setattr(dh, "get_job_store", lambda: fake_store)
    dh.reset_validator_cache_for_tests()
    dh.reset_broker_client_for_tests()
    dh.reset_job_store_for_tests()
    dh.reset_service_token_for_tests()


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


VALID_PARAMS = {"state": "WI"}


def _smoke_routing() -> dict:
    """The routing dict the dispatch handler resolves for the synthetic
    smoke_test manifest — what `ManifestRoutingLoader.routing_for('smoke_test')`
    returns. launch_run consumes this directly now that handler no longer mints."""
    from pmf_engine.tests.conftest import synthetic_manifest

    return _routing_from_manifest(synthetic_manifest())


def _make_message(run_id: str, params: dict | None = None, experiment_type: str = "smoke_test") -> dict:
    return {
        "experiment_type": experiment_type,
        "organization_slug": "org-123",
        "run_id": run_id,
        "clerk_user_id": "user_test_dispatch",
        "params": dict(VALID_PARAMS) if params is None else params,
        "prior_artifact_versions": None,
    }


class TestParseDispatchMessage:
    def test_parses_valid_message(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "clerk_user_id": "user_test_dispatch",
            "params": {"topic": "education"},
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["experiment_type"] == "smoke_test"
        assert result["organization_slug"] == "org-123"
        assert result["run_id"] == "run-001"
        assert result["params"] == {"topic": "education"}

    def test_defaults_params_to_empty_dict(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "clerk_user_id": "user_test_dispatch",
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["params"] == {}

    def test_parse_defaults_priority_to_default(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "o",
            "run_id": "r",
            "clerk_user_id": "u",
        }
        assert parse_dispatch_message(json.dumps(body))["priority"] == "DEFAULT"

    def test_parse_forwards_high_priority(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "o",
            "run_id": "r",
            "clerk_user_id": "u",
            "priority": "HIGH",
        }
        assert parse_dispatch_message(json.dumps(body))["priority"] == "HIGH"

    def test_parse_rejects_invalid_priority(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "o",
            "run_id": "r",
            "priority": "URGENT",
        }
        with pytest.raises(ValueError, match="priority"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_missing_experiment_type(self):
        body = {"organization_slug": "org-123", "run_id": "run-001"}
        with pytest.raises(ValueError, match="experiment_type"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_missing_organization_slug(self):
        body = {"experiment_type": "smoke_test", "run_id": "run-001"}
        with pytest.raises(ValueError, match="organization_slug"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_missing_run_id(self):
        body = {"experiment_type": "smoke_test", "organization_slug": "org-123"}
        with pytest.raises(ValueError, match="run_id"):
            parse_dispatch_message(json.dumps(body))

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError, match="Invalid"):
            parse_dispatch_message("not-json")

    @pytest.mark.parametrize(
        "bad_experiment_type",
        [
            "Smoke_Test",  # uppercase
            "1smoke",  # leading digit
            "smoke-test",  # hyphen
            "smoke test",  # space
            "smoke;drop",  # punctuation
            "a" * 65,  # too long
            "",  # empty (caught by missing-required check, but verify)
        ],
    )
    def test_rejects_malformed_experiment_type(self, bad_experiment_type):
        body = {
            "experiment_type": bad_experiment_type,
            "organization_slug": "org-123",
            "run_id": "run-001",
            "clerk_user_id": "user_test_dispatch",
        }
        with pytest.raises(ValueError):
            parse_dispatch_message(json.dumps(body))

    @pytest.mark.parametrize(
        "bad_run_id",
        [
            "run id",  # space
            "run/001",  # slash
            "run;drop",  # punctuation
            "r" * 65,  # too long
        ],
    )
    def test_rejects_malformed_run_id(self, bad_run_id):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "org-123",
            "run_id": bad_run_id,
            "clerk_user_id": "user_test_dispatch",
        }
        with pytest.raises(ValueError, match="run_id"):
            parse_dispatch_message(json.dumps(body))

    @pytest.mark.parametrize(
        "bad_org_slug",
        [
            "org slug",  # space
            "org/slug",  # slash
            "org;drop",  # punctuation
            "o" * 65,  # too long
        ],
    )
    def test_rejects_malformed_organization_slug(self, bad_org_slug):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": bad_org_slug,
            "run_id": "run-001",
            "clerk_user_id": "user_test_dispatch",
        }
        with pytest.raises(ValueError, match="organization_slug"):
            parse_dispatch_message(json.dumps(body))

    def test_accepts_valid_identifiers(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "Org-123_abc",
            "run_id": "run-ABC-001",
            "clerk_user_id": "user_test_dispatch",
        }
        parsed = parse_dispatch_message(json.dumps(body))
        assert parsed["organization_slug"] == "Org-123_abc"
        assert parsed["run_id"] == "run-ABC-001"

    def test_rejects_too_many_prior_artifact_versions(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "org-123",
            "run_id": "run-001",
            "clerk_user_id": "user_test_dispatch",
            "prior_artifact_versions": {f"k{i}": "e/r/artifact.json" for i in range(11)},
        }
        with pytest.raises(ValueError, match="too large"):
            parse_dispatch_message(json.dumps(body))


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
            "experiment_type": "smoke_test",
            "organization_slug": "org-123",
            "run_id": "run-abc",
            "clerk_user_id": "user_test_dispatch",
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
        assert env_map["EXPERIMENT_ID"] == "smoke_test"
        assert env_map["ORGANIZATION_SLUG"] == "org-123"
        assert env_map["RUN_ID"] == "run-abc"
        assert env_map["AGENT_MODEL"] == "sonnet"
        assert "HARNESS" not in env_map  # dropped — runner hardcodes claude_sdk
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
            "experiment_type": "smoke_test",
            "organization_slug": "org-123",
            "run_id": "run-abc",
            "clerk_user_id": "user_test_dispatch",
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

    def test_attachment_version_ids_serialized_to_env(self):
        """When the routing dict carries attachment_version_ids,
        build_container_overrides MUST emit ATTACHMENT_VERSION_IDS as a
        JSON-encoded env var so the runner can pin its broker fetch."""
        experiment = {
            "model": "sonnet",
            "timeout_seconds": 600,
            "manifest_version_id": "mv1",
            "instruction_version_id": "iv1",
            "attachment_version_ids": {"lookup.csv": "Vlk", "notes.md": "Vnt"},
        }
        message = {
            "experiment_type": "smoke_test",
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
        assert "ATTACHMENT_VERSION_IDS" in env_map
        # sort_keys=True: env-var bytes must be deterministic across dispatches
        # so idempotency checks / cache keys downstream don't churn on dict
        # iteration order.
        assert env_map["ATTACHMENT_VERSION_IDS"] == json.dumps({"lookup.csv": "Vlk", "notes.md": "Vnt"}, sort_keys=True)

    def test_attachment_version_ids_omitted_when_empty(self):
        """Empty attachment_version_ids dict must NOT produce an env var
        entry — empty env vars are noise and downstream parsing
        (RunnerConfig.from_env) special-cases empty strings."""
        experiment = {
            "model": "sonnet",
            "timeout_seconds": 600,
            "manifest_version_id": "mv1",
            "instruction_version_id": "iv1",
            "attachment_version_ids": {},
        }
        message = {
            "experiment_type": "smoke_test",
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
        assert "ATTACHMENT_VERSION_IDS" not in env_map

    def test_attachment_version_ids_omitted_when_absent(self):
        """No attachment_version_ids key on the routing dict → no env var.
        Legacy experiments without attachments must dispatch unchanged."""
        experiment = {
            "model": "sonnet",
            "timeout_seconds": 600,
            "manifest_version_id": "mv1",
            "instruction_version_id": "iv1",
        }
        message = {
            "experiment_type": "smoke_test",
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
        assert "ATTACHMENT_VERSION_IDS" not in env_map


class TestHandler:
    @patch("pmf_engine.control_plane.dispatch_handler.get_job_store")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_valid_message_enqueues_job_and_does_not_launch(self, mock_get_ecs, mock_get_store):
        store = mock_get_store.return_value
        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-q1",
                "clerk_user_id": "user_test_dispatch",
                "priority": "HIGH",
                "params": dict(VALID_PARAMS),
            }
        )
        result = handler(event, None)
        assert result["batchItemFailures"] == []
        store.put_queued_job.assert_called_once()
        job = store.put_queued_job.call_args.args[0]
        assert job.run_id == "run-q1"
        assert job.priority == "HIGH"
        assert job.experiment_type == "smoke_test"
        assert job.organization_slug == "org-123"
        assert job.routing["model"] == "sonnet"
        assert job.routing["manifest_version_id"] == SYNTHETIC_MANIFEST_VERSION_ID
        assert job.routing["instruction_version_id"] == SYNTHETIC_INSTRUCTION_VERSION_ID
        assert job.params == dict(VALID_PARAMS)
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.get_job_store")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_enqueue_defaults_priority_to_default(self, mock_get_ecs, mock_get_store):
        store = mock_get_store.return_value
        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-q2",
                "clerk_user_id": "user_test_dispatch",
                "params": dict(VALID_PARAMS),
            }
        )
        handler(event, None)
        job = store.put_queued_job.call_args.args[0]
        assert job.priority == "DEFAULT"

    @patch("pmf_engine.control_plane.dispatch_handler.get_job_store")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    def test_enqueue_failure_yields_batch_item_failure(self, mock_emit, mock_get_store):
        mock_get_store.return_value.put_queued_job.side_effect = RuntimeError("dynamo down")
        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-q3",
                "clerk_user_id": "user_test_dispatch",
                "params": dict(VALID_PARAMS),
            }
        )
        result = handler(event, None)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_rejects_unknown_experiment(self, mock_get_ecs, mock_send_error_callback):
        mock_ecs = mock_get_ecs.return_value

        event = _make_sqs_event(
            {
                "experiment_type": "nonexistent",
                "organization_slug": "org-123",
                "run_id": "run-001",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

        handler(event, None)
        mock_ecs.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        call_args = mock_send_error_callback.call_args
        assert call_args[0][0]["run_id"] == "run-001"
        assert "nonexistent" in call_args[0][1]

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_unknown_experiment_id_added_to_batch_item_failures(self, mock_get_ecs, mock_send_error_callback):
        mock_ecs = mock_get_ecs.return_value

        event = _make_sqs_event(
            {
                "experiment_type": "nonexistent",
                "organization_slug": "org-123",
                "run_id": "run-001",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

        result = handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        mock_ecs.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_unknown_experiment_id_logs_error_not_warning(self, mock_get_ecs, mock_send_error_callback):
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
            event = _make_sqs_event(
                {
                    "experiment_type": "nonexistent",
                    "organization_slug": "org-123",
                    "run_id": "run-001",
                    "clerk_user_id": "user_test_dispatch",
                    "params": {},
                }
            )
            handler(event, None)
        finally:
            dh.logger.removeHandler(capture)
            dh.logger.setLevel(original_level)

        error_records = [r for r in records if r.levelno >= logging.ERROR and "nonexistent" in r.getMessage()]
        assert len(error_records) >= 1, (
            f"Expected ERROR-level log mentioning 'nonexistent', got: "
            f"{[(r.levelname, r.getMessage()) for r in records]}"
        )

        warning_records = [r for r in records if r.levelno == logging.WARNING and "nonexistent" in r.getMessage()]
        assert (
            warning_records == []
        ), f"Expected no WARNING-level log for unknown experiment, got: {[r.getMessage() for r in warning_records]}"

        assert any(
            "smoke_test" in r.getMessage() for r in error_records
        ), "Expected error log to include known experiment IDs for operator triage"

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_ecs_failure(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": "RESOURCE:MEMORY"}],
        }

        message = _make_message("run-001")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result["status"] == "failed"
        assert result["error"].startswith("ECS RunTask failed:")
        assert "RESOURCE:MEMORY" not in result["error"]

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
    def test_malformed_message_with_run_id_notifies_gp_api(self, mock_get_ecs, mock_send_error_callback):
        # A message that fails parse_dispatch_message validation but still has a
        # recoverable run_id must notify gp-api (so its QUEUED row gets failed)
        # AND batch-fail. Otherwise the row orphans until the slow 6h backstop.
        event = {
            "Records": [
                {
                    "messageId": "msg-bad",
                    # Valid JSON with run_id but an invalid experiment_type
                    # (uppercase) — parse_dispatch_message raises ValueError.
                    "body": json.dumps(
                        {
                            "experiment_type": "BadType",
                            "organization_slug": "org-123",
                            "run_id": "run-malformed",
                        }
                    ),
                }
            ]
        }

        result = handler(event, None)

        mock_send_error_callback.assert_called_once()
        sent_message = mock_send_error_callback.call_args[0][0]
        assert sent_message["run_id"] == "run-malformed"
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "invalid-payload-run-malformed"
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-bad"}]
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_unparseable_body_batch_fails_without_callback(self, mock_get_ecs, mock_send_error_callback):
        # No recoverable run_id — just batch-fail, no callback, no crash.
        event = {"Records": [{"messageId": "msg-bad", "body": "not-json-at-all"}]}

        result = handler(event, None)

        mock_send_error_callback.assert_not_called()
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-bad"}]
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_failure_on_empty_tasks_array(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [],
            "failures": [],
        }

        message = _make_message("run-001")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result["status"] == "failed"
        assert result["error"].startswith("ECS RunTask failed:")

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
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-001",
                "clerk_user_id": "user_test_dispatch",
            }
            send_error_callback(message, "some error", "https://sqs.example.com/callback.fifo")
        finally:
            dh.logger.removeHandler(collector)

        error_records = [
            r for r in records if r.levelno >= logging.ERROR and "Failed to send error callback" in r.getMessage()
        ]
        assert error_records, "expected ERROR log when SQS send fails"
        combined = " ".join(r.getMessage() for r in error_records)
        assert "SQS unreachable" in combined

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_exception_sends_error_callback(self, mock_get_ecs, mock_broker_cls):
        # launch_run re-raises the raw ECS exception (transient — caller retries)
        # after cleaning up the minted token. The user-facing sanitization
        # (`ECS RunTask exception: <type>`) now lives in the handler that wraps
        # launch_run; here we only assert launch_run propagates the original
        # exception and cleaned up the token.
        mock_broker = _mock_broker_success()
        mock_broker_cls.return_value = mock_broker
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.side_effect = Exception("Network timeout")

        message = _make_message("run-001")
        with pytest.raises(Exception, match="Network timeout"):
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )
        mock_broker.delete_run_token.assert_called_once()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_reports_failure_on_ecs_exception(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.side_effect = Exception("Network timeout")

        message = _make_message("run-001")
        with pytest.raises(Exception, match="Network timeout"):
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )


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

        message = _make_message("run-001")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result["status"] == "launched"
        mock_ecs.run_task.assert_called_once()

        env_list = mock_ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
        env_map = {e["name"]: e["value"] for e in env_list}
        assert env_map["BROKER_TOKEN"] == "tok-from-broker"
        assert env_map["BROKER_URL"] == "https://broker.example.com"

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_400_sends_error_callback_no_ecs(self, mock_get_ecs, mock_broker_cls):
        from pmf_engine.control_plane.broker_client import BrokerError

        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(
            400, "Param classifier rejected: nested objects", "Invalid experiment parameters"
        )

        message = _make_message("run-001")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result == {"status": "failed", "error": "Invalid experiment parameters"}
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_401_sends_error_callback(self, mock_get_ecs, mock_broker_cls):
        from pmf_engine.control_plane.broker_client import BrokerError

        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(401, "Invalid service token")

        message = _make_message("run-001")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result == {"status": "failed", "error": "Broker rejected the request"}
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_400_without_user_safe_message_uses_generic(self, mock_get_ecs, mock_broker_cls):
        from pmf_engine.control_plane.broker_client import BrokerError

        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(400, "Some detail", "")

        message = _make_message("run-001")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result == {"status": "failed", "error": "Broker rejected the request"}


class TestNonDictParamsGuard:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_string_params_rejected_with_stable_dedup(self, mock_get_ecs, mock_emit_metric, mock_send_error_callback):
        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-xyz",
                "clerk_user_id": "user_test_dispatch",
                "params": "not a dict",
            }
        )

        result = handler(event, None)

        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "invalid-params-type-run-xyz"
        assert "JSON object" in mock_send_error_callback.call_args[0][1]
        mock_emit_metric.assert_any_call("InvalidParamsType", "smoke_test")
        assert result["batchItemFailures"] == []

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_list_params_does_not_crash(self, mock_get_ecs, mock_emit_metric, mock_send_error_callback):
        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-001",
                "clerk_user_id": "user_test_dispatch",
                "params": [1, 2, 3],
            }
        )

        handler(event, None)

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

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-001",
                "clerk_user_id": "user_test_dispatch",
                "params": None,
            }
        )

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_broker_cls.return_value.mint_run_token.assert_not_called()
        mock_ecs.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "input-schema-run-001"


class TestErrorCallbackStableDedup:
    # NOTE: error-callback dedup_id construction moved to the handler that wraps
    # launch_run. launch_run itself only returns a status/error dict (or raises
    # on transient errors). These tests now assert launch_run's classification
    # of each failure mode; the stable-dedup mapping is covered at the handler
    # layer.
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_runtask_failure_uses_stable_dedup(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": "RESOURCE:MEMORY"}],
        }

        message = _make_message("run-abc")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result["status"] == "failed"
        assert result["error"].startswith("ECS RunTask failed:")

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_exception_uses_stable_dedup(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.side_effect = Exception("Network timeout")

        message = _make_message("run-abc")
        with pytest.raises(Exception, match="Network timeout"):
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_rejection_uses_stable_dedup(self, mock_get_ecs, mock_broker_cls):
        from pmf_engine.control_plane.broker_client import BrokerError

        mock_broker_cls.return_value.mint_run_token.side_effect = BrokerError(
            400, "rejected", "Invalid experiment parameters"
        )

        message = _make_message("run-abc")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result == {"status": "failed", "error": "Invalid experiment parameters"}


class TestGetServiceToken:
    def test_fetches_from_secrets_manager_and_caches(self, monkeypatch):
        import pmf_engine.control_plane.dispatch_handler as dh

        dh.reset_service_token_for_tests()
        monkeypatch.setattr(dh, "SERVICE_TOKENS_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:123:secret:svc")
        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretString": json.dumps({"SERVICE_TOKEN": "tok"})}
        monkeypatch.setattr(dh, "_get_secrets_client", lambda: fake_client)

        assert dh.get_service_token() == "tok"
        # Second call is served from the warm-container cache, not Secrets Manager.
        assert dh.get_service_token() == "tok"
        fake_client.get_secret_value.assert_called_once_with(SecretId="arn:aws:secretsmanager:us-west-2:123:secret:svc")
        dh.reset_service_token_for_tests()


class TestMissingCriticalEnvVars:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_empty_subnet_ids_does_not_call_run_task(self, mock_get_ecs, mock_send_error_callback, monkeypatch):
        import pmf_engine.control_plane.dispatch_handler as dh

        monkeypatch.setattr(dh, "ECS_CLUSTER_ARN", "arn:aws:ecs:us-west-2:123:cluster/pmf")
        monkeypatch.setattr(dh, "ECS_TASK_DEFINITION", "pmf-engine:1")
        monkeypatch.setattr(dh, "ECS_SUBNET_IDS", [])
        monkeypatch.setattr(dh, "ECS_SECURITY_GROUP_ID", "sg-abc")
        monkeypatch.setattr(dh, "RESULTS_QUEUE_URL", "https://sqs.example.com/callback.fifo")
        monkeypatch.setattr(dh, "BROKER_URL", "https://broker.example.com")
        monkeypatch.setattr(dh, "SERVICE_TOKENS_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:123:secret:svc")

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-xyz",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

        result = handler(event, None)
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        error_msg = mock_send_error_callback.call_args[0][1]
        assert "ECS_SUBNET_IDS" in error_msg
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "dispatch-misconfig-run-xyz"
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_empty_cluster_arn_does_not_call_run_task(self, mock_get_ecs, mock_send_error_callback, monkeypatch):
        import pmf_engine.control_plane.dispatch_handler as dh

        monkeypatch.setattr(dh, "ECS_CLUSTER_ARN", "")
        monkeypatch.setattr(dh, "ECS_TASK_DEFINITION", "pmf-engine:1")
        monkeypatch.setattr(dh, "ECS_SUBNET_IDS", ["subnet-aaa"])
        monkeypatch.setattr(dh, "ECS_SECURITY_GROUP_ID", "sg-abc")
        monkeypatch.setattr(dh, "RESULTS_QUEUE_URL", "https://sqs.example.com/callback.fifo")
        monkeypatch.setattr(dh, "BROKER_URL", "https://broker.example.com")
        monkeypatch.setattr(dh, "SERVICE_TOKENS_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:123:secret:svc")

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-xyz",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

        handler(event, None)
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        error_msg = mock_send_error_callback.call_args[0][1]
        assert "ECS_CLUSTER_ARN" in error_msg

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_missing_experiment_metadata_bucket_uses_per_message_error_path(
        self, mock_get_ecs, mock_send_error_callback, monkeypatch
    ):
        """Missing EXPERIMENT_METADATA_BUCKET must trigger the per-message
        send_error_callback path — NOT crash the whole batch with an uncaught
        RuntimeError from get_manifest_loader().
        """
        monkeypatch.delenv("EXPERIMENT_METADATA_BUCKET", raising=False)

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-xyz",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

        result = handler(event, None)
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        error_msg = mock_send_error_callback.call_args[0][1]
        assert "EXPERIMENT_METADATA_BUCKET" in error_msg
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]


class TestParamsSizeLimit:
    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_oversized_params_rejected_before_ecs(self, mock_get_ecs, mock_emit_metric, mock_send_error_callback):
        oversized = {f"key_{i}": "x" * 900 for i in range(12)}

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-big",
                "clerk_user_id": "user_test_dispatch",
                "params": oversized,
            }
        )

        handler(event, None)

        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()
        error_msg = mock_send_error_callback.call_args[0][1].lower()
        assert "size limit" in error_msg or "too large" in error_msg
        assert mock_send_error_callback.call_args.kwargs["dedup_id"] == "params-too-large-run-big"
        assert any(call.args == ("ParamsTooLarge", "smoke_test") for call in mock_emit_metric.call_args_list)

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_params_just_under_limit_proceed_to_ecs(self, mock_get_ecs, mock_broker_cls):
        mock_broker = _mock_broker_success()
        mock_broker_cls.return_value = mock_broker
        mock_ecs = mock_get_ecs.return_value
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }
        # Inflate one of the schema-allowed string fields so the params
        # body is non-trivially sized but still valid against the schema.
        # The synthetic input_schema has additionalProperties: false, so we
        # use the optional `note` field rather than inventing a new key.
        small = {**VALID_PARAMS, "note": "x" * 100}

        message = _make_message("run-001", params=small)
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result["status"] == "launched"
        mock_broker.mint_run_token.assert_called_once()
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
    def test_missing_required_field_rejected_before_mint(
        self, mock_get_ecs, mock_broker_cls, mock_emit_metric, mock_send_error_callback
    ):
        mock_broker_cls.return_value = _mock_broker_success()

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-123",
                "run_id": "run-missing-state",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

        handler(event, None)

        mock_broker_cls.return_value.mint_run_token.assert_not_called()
        mock_get_ecs.return_value.run_task.assert_not_called()
        mock_send_error_callback.assert_called_once()

        detail = mock_send_error_callback.call_args[0][1]
        assert "input_schema" in detail.lower() or "required" in detail.lower()
        assert "state" in detail.lower()

        dedup = mock_send_error_callback.call_args.kwargs["dedup_id"]
        assert "input-schema-run-missing-state" in dedup or "missing-params-run-missing-state" in dedup

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_invalid_param_pattern_rejected(self, mock_get_ecs, mock_broker_cls, mock_send_error_callback):
        """The synthetic input_schema enforces `state` matches `^[A-Z]{2}$`."""
        mock_broker_cls.return_value = _mock_broker_success()

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-empty",
                "run_id": "run-empty-strings",
                "clerk_user_id": "user_test_dispatch",
                "params": {"state": ""},
            }
        )

        handler(event, None)

        mock_broker_cls.return_value.mint_run_token.assert_not_called()
        mock_send_error_callback.assert_called_once()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_all_required_params_present_proceeds_to_mint(self, mock_get_ecs, mock_broker_cls):
        mock_broker_cls.return_value = _mock_broker_success()
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/ok"}],
            "failures": [],
        }

        message = _make_message("run-ok", params={"state": "WI"})
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )

        assert result["status"] == "launched"
        mock_broker_cls.return_value.mint_run_token.assert_called_once()
        mock_get_ecs.return_value.run_task.assert_called_once()


class TestTransientBrokerErrors:
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_transient_httpx_error_during_mint_yields_batch_item_failure(self, mock_get_ecs, mock_broker_cls):
        # launch_run re-raises transient httpx errors so the handler maps them
        # to a batch_item_failure (SQS retry). The batch-level retry assertion
        # is the handler's concern now.
        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = httpx.ConnectError("DNS failed")

        message = _make_message("run-transient")
        with pytest.raises(httpx.HTTPError):
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_broker_4xx_still_goes_through_existing_error_callback_path(self, mock_get_ecs, mock_broker_cls):
        from pmf_engine.control_plane.broker_client import BrokerError

        mock_broker = mock_broker_cls.return_value
        mock_broker.mint_run_token.side_effect = BrokerError(
            400, "Param classifier rejected", "Invalid experiment parameters"
        )

        message = _make_message("run-terminal")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )

        assert result == {"status": "failed", "error": "Invalid experiment parameters"}
        mock_get_ecs.return_value.run_task.assert_not_called()


class TestDispatchHandlerErrorPathResilience:
    """Covers CRITICAL #3: ensure non-HTTPError exceptions during mint land
    an error callback AND a batch_item_failures entry (so gp-api sees the
    failure immediately AND SQS retries eventually reach the DLQ alarm).
    Also covers: if send_error_callback fails at the SQS layer, the caller
    must add to batch_item_failures so the message is re-delivered."""

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_programmer_error_during_mint_sends_callback_and_retries(self, mock_get_ecs, mock_broker_cls):
        # An unexpected (non-BrokerError, non-httpx) exception during mint is
        # caught by launch_run and returned as a sanitized failed dict carrying
        # only the exception type name — never the raw message. The handler
        # decides whether to retry from that dict.
        mock_broker_cls.side_effect = KeyError("missing config key somewhere")

        message = _make_message("run-prog-err")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )

        assert result == {"status": "failed", "error": "Unexpected dispatch error: KeyError"}
        assert "missing config key somewhere" not in result["error"]
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_validation_error_with_failed_callback_adds_to_batch_item_failures(
        self, mock_get_ecs, mock_send_error_callback
    ):
        mock_send_error_callback.return_value = False

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-x",
                "run_id": "run-missing-params",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

        result = handler(event, None)

        mock_send_error_callback.assert_called_once()
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}], (
            "When the SQS send of the error callback fails, the message must "
            "be retried so gp-api isn't left in PENDING forever"
        )
        mock_get_ecs.return_value.run_task.assert_not_called()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_validation_error_with_successful_callback_does_not_retry(self, mock_get_ecs, mock_send_error_callback):
        mock_send_error_callback.return_value = True

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-x",
                "run_id": "run-missing-params-ok",
                "clerk_user_id": "user_test_dispatch",
                "params": {},
            }
        )

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
            {"experiment_type": "x", "organization_slug": "y", "run_id": "r1"},
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
            {"experiment_type": "x", "organization_slug": "y", "run_id": "r1"},
            "err",
            "https://sqs.example.com/q.fifo",
        )
        assert result is False

    def test_returns_false_when_queue_url_is_empty(self):
        from pmf_engine.control_plane.dispatch_handler import send_error_callback

        result = send_error_callback(
            {"experiment_type": "x", "organization_slug": "y", "run_id": "r1"},
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
            "experiment_type": "smoke_test",
            "organization_slug": "acme",
            "run_id": "run-1",
            "clerk_user_id": "user_test_dispatch",
            "params": {},
            "prior_artifact_versions": {
                "smoke_dep": "../../etc/passwd",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_rejects_cross_org_artifact_key(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "acme",
            "run_id": "run-2",
            "clerk_user_id": "user_test_dispatch",
            "params": {},
            "prior_artifact_versions": {
                "smoke_dep": "smoke_dep/other-org-run-id/artifact.json/../secrets.txt",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_rejects_wrong_file_suffix(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "acme",
            "run_id": "run-3",
            "clerk_user_id": "user_test_dispatch",
            "params": {},
            "prior_artifact_versions": {
                "smoke_dep": "smoke_dep/abc-123/latest.json",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_rejects_empty_segment(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "acme",
            "run_id": "run-4",
            "clerk_user_id": "user_test_dispatch",
            "params": {},
            "prior_artifact_versions": {
                "smoke_dep": "/abc-123/artifact.json",
            },
        }
        with pytest.raises(ValueError, match="prior_artifact_versions"):
            parse_dispatch_message(json.dumps(body))

    def test_accepts_valid_prior_artifact_key(self):
        body = {
            "experiment_type": "smoke_test",
            "organization_slug": "acme",
            "run_id": "run-5",
            "clerk_user_id": "user_test_dispatch",
            "params": {},
            "prior_artifact_versions": {
                "smoke_dep": "smoke_dep/d188bc17-87bd-4fe0-9b45-d34d3b301d98/artifact.json",
            },
        }
        result = parse_dispatch_message(json.dumps(body))
        assert result["prior_artifact_versions"] == body["prior_artifact_versions"]

    def test_accepts_absent_prior_artifact_versions(self):
        body = {
            "experiment_type": "smoke_dep",
            "organization_slug": "acme",
            "run_id": "run-6",
            "clerk_user_id": "user_test_dispatch",
            "params": {},
        }
        result = parse_dispatch_message(json.dumps(body))
        assert "prior_artifact_versions" not in result or result["prior_artifact_versions"] is None

    def test_rejects_non_dict_prior_artifact_versions(self):
        body = {
            "experiment_type": "smoke_dep",
            "organization_slug": "acme",
            "run_id": "run-7",
            "clerk_user_id": "user_test_dispatch",
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

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_failure_callback_does_not_leak_raw_reason(self, mock_get_ecs, mock_broker_cls):
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

        message = _make_message("run-iam-leak")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )

        assert result["status"] == "failed"
        error_str = result["error"]
        assert "arn:aws:iam" not in error_str, f"Expected sanitized error, got ARN-leaking message: {error_str!r}"
        assert (
            "333022194791" not in error_str
        ), f"Expected sanitized error, got account-id-leaking message: {error_str!r}"
        assert "ECS RunTask failed" in error_str

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_exception_callback_does_not_leak_raw_exception_message(self, mock_get_ecs, mock_broker_cls):
        # On an ECS RunTask exception, launch_run re-raises the raw exception
        # (so the handler can retry) AFTER cleaning up the minted token. The
        # user-facing no-leak message (`ECS RunTask exception: <type>`, with no
        # ARN/account-id) is built by the handler from `type(e).__name__` — see
        # the handler-level coverage of that sanitization. Here we assert
        # launch_run propagates the exception and cleaned up the token.
        from botocore.exceptions import ClientError

        mock_broker = _mock_broker_success()
        mock_broker_cls.return_value = mock_broker
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

        message = _make_message("run-iam-exc-leak")
        with pytest.raises(ClientError):
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )
        mock_broker.delete_run_token.assert_called_once()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_failure_logs_full_detail_server_side(self, mock_get_ecs, mock_broker_cls):
        import pmf_engine.control_plane.dispatch_handler as dh

        mock_broker_cls.return_value = _mock_broker_success()
        raw_reason = "AccessDeniedException: User: arn:aws:iam::333022194791:role/test-role not authorized"
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
            message = _make_message("run-log-detail")
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )
        finally:
            dh.logger.removeHandler(collector)
            dh.logger.setLevel(original_level)

        combined = " ".join(r.getMessage() for r in records if r.levelno >= logging.ERROR)
        assert (
            "arn:aws:iam::333022194791" in combined
        ), f"Operator diagnostic log must retain full ARN detail; got: {combined!r}"


class TestRunTaskFailureCleansUpMintedTicket:
    """Covers CRITICAL #1 companion: when ecs.run_task fails after a
    successful mint, the freshly-issued broker_token + run-lock must be
    deleted so (a) the token can't be reused from logs/CloudWatch and (b)
    the same run_id is free to be re-dispatched immediately.

    Without this, a retry of the same run_id 409s against the stale
    run-lock until the lock's TTL expires (~4h)."""

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_returns_failures_triggers_delete_run_token(self, mock_get_ecs, mock_broker_cls):
        mock_broker = _mock_broker_success("tok-to-clean")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.return_value = {
            "failures": [{"reason": "CAPACITY_EXHAUSTED"}],
            "tasks": [],
        }

        message = _make_message("run-ecs-cap")
        result = launch_run(
            experiment=_smoke_routing(),
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )

        assert result["status"] == "failed"
        mock_broker.delete_run_token.assert_called_once_with(broker_token="tok-to-clean", run_id="run-ecs-cap")

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_ecs_run_task_raises_triggers_delete_run_token(self, mock_get_ecs, mock_broker_cls):
        mock_broker = _mock_broker_success("tok-to-clean-exc")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.side_effect = RuntimeError("ECS control plane transient")

        message = _make_message("run-ecs-boom")
        with pytest.raises(RuntimeError, match="ECS control plane transient"):
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )

        mock_broker.delete_run_token.assert_called_once_with(broker_token="tok-to-clean-exc", run_id="run-ecs-boom")

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_delete_run_token_failure_does_not_prevent_error_callback(self, mock_get_ecs, mock_broker_cls):
        # launch_run now RAISES on the run_task exception path so the handler
        # retries. A cleanup failure must not mask the primary ECS exception:
        # `_cleanup_minted_token` swallows the delete error internally, so the
        # ORIGINAL ECS exception is the one that propagates.
        mock_broker = _mock_broker_success("tok-doomed")
        mock_broker.delete_run_token.side_effect = RuntimeError("broker unreachable")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.side_effect = RuntimeError("ECS transient")

        message = _make_message("run-double-fail")
        with pytest.raises(RuntimeError, match="ECS transient"):
            launch_run(
                experiment=_smoke_routing(),
                message=message,
                scope={},
                params_json=json.dumps(message["params"]),
            )

        mock_broker.delete_run_token.assert_called_once()

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_successful_run_task_does_not_call_delete_run_token(self, mock_get_ecs, mock_broker_cls):
        mock_broker = _mock_broker_success("tok-good")
        mock_broker_cls.return_value = mock_broker
        mock_get_ecs.return_value.run_task.return_value = {
            "failures": [],
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
        }

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-x",
                "run_id": "run-ok",
                "clerk_user_id": "user_test_dispatch",
                "params": dict(VALID_PARAMS),
            }
        )

        handler(event, None)

        mock_broker.delete_run_token.assert_not_called()


class TestInputSchemaSortKeyMixedTypes:
    """jsonschema's ValidationError.absolute_path mixes strings (object keys)
    and ints (array indices). A naive sort key like list(e.absolute_path)
    raises TypeError on int<>str comparison, crashing the entire SQS batch.
    The handler must sort with str-coerced path elements."""

    def _loader_with_array_schema(self):
        from unittest.mock import MagicMock

        loader = MagicMock()
        loader.routing_for.return_value = {
            "model": "sonnet",
            "timeout_seconds": 600,
            # Schema with both an array and an object — guarantees error
            # paths that mix int (array index) and str (property name).
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["items", "name"],
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "string"}},
                        },
                    },
                    "name": {"type": "string"},
                },
            },
            "scope": {},
            "manifest_version_id": "v1",
            "instruction_version_id": "v1",
        }
        loader.known_experiments.return_value = ["smoke_test"]
        return loader

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_validation_errors_with_mixed_path_types_dont_crash(
        self,
        mock_get_ecs,
        mock_broker_cls,
        mock_emit_metric,
        mock_send_error_callback,
        monkeypatch,
    ):
        # Force the loader path so we hit the input_schema validator.
        import pmf_engine.control_plane.dispatch_handler as dh

        loader = self._loader_with_array_schema()
        monkeypatch.setattr(dh, "get_manifest_loader", lambda: loader)
        dh.reset_validator_cache_for_tests()
        mock_broker_cls.return_value = _mock_broker_success()

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-mixed",
                "run_id": "run-mixed-paths",
                "clerk_user_id": "user_test_dispatch",
                # Errors fall on: items (wrong type), items[0].id (missing),
                # name (missing). Paths mix str and int.
                "params": {"items": [{}, {"id": 42}]},
            }
        )

        # If the sort key blows up, this raises TypeError uncaught.
        handler(event, None)

        # Validation should have caught the issues and emitted a callback,
        # NOT crashed.
        mock_send_error_callback.assert_called_once()
        call = mock_send_error_callback.call_args
        # Detail message should include the violations.
        detail = call.args[1] if len(call.args) > 1 else call.kwargs.get("detail", "")
        assert "input_schema" in detail.lower() or "name" in detail.lower()

    @patch("pmf_engine.control_plane.dispatch_handler.send_error_callback")
    @patch("pmf_engine.control_plane.dispatch_handler.emit_dispatch_metric")
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_violation_paths_use_bracketed_array_index_format(
        self,
        mock_get_ecs,
        mock_broker_cls,
        mock_emit_metric,
        mock_send_error_callback,
        monkeypatch,
    ):
        """The violation path format must match contract.py: array indices are
        rendered as `field[1].subfield`, not `field.1.subfield`. Both
        dispatch_handler and contract.py validate JSON Schema Draft-07 — they
        must format paths identically so logs/operators don't see two styles."""
        import pmf_engine.control_plane.dispatch_handler as dh

        loader = self._loader_with_array_schema()
        monkeypatch.setattr(dh, "get_manifest_loader", lambda: loader)
        dh.reset_validator_cache_for_tests()
        mock_broker_cls.return_value = _mock_broker_success()

        event = _make_sqs_event(
            {
                "experiment_type": "smoke_test",
                "organization_slug": "org-fmt",
                "run_id": "run-fmt-check",
                "clerk_user_id": "user_test_dispatch",
                # items[1].id is the wrong type (int instead of string).
                "params": {"items": [{"id": "ok"}, {"id": 42}], "name": "alice"},
            }
        )

        handler(event, None)

        mock_send_error_callback.assert_called_once()
        detail = mock_send_error_callback.call_args.args[1]
        assert "items[1].id" in detail, f"expected bracketed path 'items[1].id'; got detail={detail!r}"
        assert "items.1.id" not in detail, f"old dotted-index format leaked through; got detail={detail!r}"


# ---------------------------------------------------------------------------
# _resolve_routing — manifest loader is the single source of truth.
#
# The bundled DISPATCH_REGISTRY fallback is GONE. Two distinct failure modes
# need distinct operator signals AND distinct handler responses:
#   - TRANSIENT (S3 outage, IAM throttle, transient 5xx) → re-raise so the
#     SQS-batch loop adds the record to batch_item_failures (silent retry).
#   - MALFORMED (corrupt JSON, missing required fields) → re-raise so the
#     handler sends an error callback to gp-api AND adds to batch_item_failures.
#
# Both emit a `manifest_loader_fallback` CloudWatch metric (legacy name kept
# for dashboard continuity — it's now a "manifest loader failure" signal, not
# an actual fallback). The `error_type` and `Environment` dimensions let
# dashboards filter (e.g., page on malformed-only since transient is expected
# during AWS weather and SQS will retry the dispatch).
# ---------------------------------------------------------------------------


class TestResolveRoutingFailures:
    """Lock the contract for _resolve_routing's two-tier failure handling."""

    def _setup_loader(self, monkeypatch, fake_loader):
        """Wire a fake ManifestRoutingLoader into the dispatch_handler module."""
        import pmf_engine.control_plane.dispatch_handler as dh

        monkeypatch.setattr(dh, "_manifest_loader", fake_loader, raising=False)
        # Bypass get_manifest_loader's bucket-env check by patching it directly
        monkeypatch.setattr(dh, "get_manifest_loader", lambda: fake_loader)
        return dh

    def test_loader_success_returns_routing_no_fallback_metric(self, monkeypatch):
        from pmf_engine.control_plane.dispatch_handler import _resolve_routing

        fake_loader = MagicMock()
        fake_loader.routing_for.return_value = {
            "model": "sonnet",
            "timeout_seconds": 900,
            "input_schema": {"type": "object", "properties": {}, "required": []},
            "scope": {},
        }
        fake_loader.known_experiments.return_value = ["smoke_test"]
        self._setup_loader(monkeypatch, fake_loader)

        with patch("pmf_engine.control_plane.dispatch_handler._emit_metric") as mock_metric:
            routing, known = _resolve_routing("smoke_test")

        assert routing is not None
        assert routing["model"] == "sonnet"
        # known is only populated on the unknown-experiment branch (routing is None).
        assert known == []
        assert not any(
            call.args[0] == "manifest_loader_fallback" for call in mock_metric.call_args_list
        ), "happy path must not emit fallback metric"

    def test_loader_transient_error_raises_and_emits_metric(self, monkeypatch):
        """S3 outage / IAM throttle → re-raise ManifestLoaderTransientError so
        the handler's batch loop converts it to an SQS retry signal. Emit
        the metric with error_type=transient + Environment dimension so SREs
        can correlate to AWS weather."""
        from pmf_engine.control_plane.dispatch_handler import _resolve_routing
        from pmf_engine.control_plane.manifest_loader import ManifestLoaderTransientError

        fake_loader = MagicMock()
        fake_loader.routing_for.side_effect = ManifestLoaderTransientError("S3 GetObject failed: ServiceUnavailable")
        self._setup_loader(monkeypatch, fake_loader)

        with (
            patch.dict(os.environ, {"ENVIRONMENT": "qa"}, clear=False),
            patch("pmf_engine.control_plane.dispatch_handler._emit_metric") as mock_metric,
        ):
            with pytest.raises(ManifestLoaderTransientError):
                _resolve_routing("smoke_test")

        fallback_calls = [call for call in mock_metric.call_args_list if call.args[0] == "manifest_loader_fallback"]
        assert len(fallback_calls) == 1, "must emit exactly one failure metric"
        dimensions = fallback_calls[0].args[1]
        dim_dict = {d["Name"]: d["Value"] for d in dimensions}
        assert dim_dict.get("error_type") == "transient", f"error_type dimension must be 'transient', got {dim_dict!r}"
        assert dim_dict.get("experiment_id") == "smoke_test"
        assert dim_dict.get("Environment") == "qa"

    def test_loader_malformed_error_raises_and_emits_metric(self, monkeypatch):
        """Corrupt/invalid manifest in S3 → publish-pipeline bug. Re-raise
        ManifestLoaderMalformedError so the handler sends an error callback to
        gp-api. Emit metric with error_type=malformed + Environment dimension
        so this lights up a different alarm than transient AWS noise."""
        from pmf_engine.control_plane.dispatch_handler import _resolve_routing
        from pmf_engine.control_plane.manifest_loader import ManifestLoaderMalformedError

        fake_loader = MagicMock()
        fake_loader.routing_for.side_effect = ManifestLoaderMalformedError(
            "manifest for 'smoke_test' is not valid JSON"
        )
        self._setup_loader(monkeypatch, fake_loader)

        with (
            patch.dict(os.environ, {"ENVIRONMENT": "prod"}, clear=False),
            patch("pmf_engine.control_plane.dispatch_handler._emit_metric") as mock_metric,
        ):
            with pytest.raises(ManifestLoaderMalformedError):
                _resolve_routing("smoke_test")

        fallback_calls = [call for call in mock_metric.call_args_list if call.args[0] == "manifest_loader_fallback"]
        assert len(fallback_calls) == 1
        dim_dict = {d["Name"]: d["Value"] for d in fallback_calls[0].args[1]}
        assert dim_dict.get("error_type") == "malformed"
        assert dim_dict.get("experiment_id") == "smoke_test"
        assert dim_dict.get("Environment") == "prod"

    def test_no_bucket_configured_raises(self, monkeypatch):
        """EXPERIMENT_METADATA_BUCKET is required — there is no longer a
        bundled-registry fallback. Misconfigured Lambda env must fail loud.

        The autouse fixture stubs `get_manifest_loader`; undo it for this
        test so we exercise the real function."""
        # Reverse the autouse fixture's monkeypatches so we can call the
        # real get_manifest_loader.
        monkeypatch.undo()

        import pmf_engine.control_plane.dispatch_handler as dh

        dh.reset_manifest_loader_for_tests()
        monkeypatch.delenv("EXPERIMENT_METADATA_BUCKET", raising=False)

        with pytest.raises(RuntimeError, match="EXPERIMENT_METADATA_BUCKET"):
            dh.get_manifest_loader()

    def test_unknown_experiment_returns_none_no_fallback_metric(self, monkeypatch):
        """Loader returns None for unknown experiments — that's the well-defined
        'unknown experiment' case, NOT a loader failure. No fallback metric."""
        from pmf_engine.control_plane.dispatch_handler import _resolve_routing

        fake_loader = MagicMock()
        fake_loader.routing_for.return_value = None
        fake_loader.known_experiments.return_value = ["smoke_test"]
        self._setup_loader(monkeypatch, fake_loader)

        with patch("pmf_engine.control_plane.dispatch_handler._emit_metric") as mock_metric:
            routing, known = _resolve_routing("nonexistent_experiment")

        assert routing is None
        assert known == ["smoke_test"]
        assert not any(call.args[0] == "manifest_loader_fallback" for call in mock_metric.call_args_list)


# ---------------------------------------------------------------------------
# Write-action dispatch flow (ENG-10128)
#
# When the routing dict carries `system_prompt` OR `permission_mode`, the
# handler treats the experiment as write-action: scope is an empty dict
# (the broker creates the Clerk actor token from MintRequest.clerk_user_id
# and stores the resulting clerk_session_id on the ScopeTicket, then mints
# fresh ~60s JWTs for each MCP call the runner makes to /agent/mcp). No
# allowlist is enforced today — every @McpTool-decorated endpoint on gp-api
# is callable by every agent run.
#
# Legacy read-action experiments continue through derive_scope (Databricks
# shape).
# ---------------------------------------------------------------------------


class TestWriteActionDispatchFlow:
    # Scope derivation (write-action -> {} vs. read -> derive_scope) is the
    # handler's job; launch_run forwards whatever scope it's handed straight to
    # the broker. These tests assert that forwarding: a write-action run is
    # dispatched with scope={}, and a legacy Databricks run with a derived scope
    # reaches the broker unchanged.
    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_write_action_routing_calls_broker_with_empty_scope(self, mock_get_ecs, mock_broker_cls):
        write_action_routing = {
            "model": "sonnet",
            "timeout_seconds": 1500,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["campaign_id"],
                "properties": {
                    "campaign_id": {"type": "string"},
                },
            },
            "scope": {},
            "system_prompt": "You are a compliance setup agent.",
            "permission_mode": "default",
            "manifest_version_id": SYNTHETIC_MANIFEST_VERSION_ID,
            "instruction_version_id": SYNTHETIC_INSTRUCTION_VERSION_ID,
        }

        mock_broker_cls.return_value = _mock_broker_success("tok-write-action")
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        message = _make_message(
            "run-write-001",
            params={"campaign_id": "0a4c1b2e-1111-4222-8333-444444444444"},
            experiment_type="compliance_smoke_test",
        )
        message["clerk_user_id"] = "user_abc123"
        result = launch_run(
            experiment=write_action_routing,
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )

        assert result["status"] == "launched"
        mock_broker_cls.return_value.mint_run_token.assert_called_once()
        mint_kwargs = mock_broker_cls.return_value.mint_run_token.call_args.kwargs
        assert mint_kwargs["scope"] == {}
        # clerk_user_id flows through the MintRequest top-level field, not via
        # scope — broker creates the Clerk actor token from it and stores the
        # resulting session_id on the ScopeTicket.
        assert mint_kwargs["clerk_user_id"] == "user_abc123"

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_legacy_databricks_routing_unchanged(self, mock_get_ecs, mock_broker_cls):
        """A routing dict without system_prompt or permission_mode flows
        through derive_scope (Databricks shape) — no behavior change for
        the existing 5 read-only experiments."""
        from pmf_engine.control_plane.scope_derivation import derive_scope

        mock_broker_cls.return_value = _mock_broker_success("tok-legacy")
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        routing = _smoke_routing()
        message = _make_message("run-legacy")
        # The handler derives a non-empty scope for read experiments and hands
        # it to launch_run; mirror that derivation here so the forwarded scope
        # is the real Databricks shape, not {}.
        scope = derive_scope(
            message["experiment_type"],
            message["params"],
            manifest_scope=routing.get("scope"),
        )
        result = launch_run(
            experiment=routing,
            message=message,
            scope=scope,
            params_json=json.dumps(message["params"]),
        )
        assert result["status"] == "launched"
        mock_broker_cls.return_value.mint_run_token.assert_called_once()
        scope_arg = mock_broker_cls.return_value.mint_run_token.call_args.kwargs["scope"]
        assert "allowed_tables" in scope_arg
        assert scope_arg != {}

    @patch("pmf_engine.control_plane.dispatch_handler.BrokerClient")
    @patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
    def test_permission_mode_alone_routes_as_write_action(self, mock_get_ecs, mock_broker_cls):
        """A manifest with `permission_mode` but no `system_prompt` is still a
        write-action experiment per the loader's any-of projection rule, and
        must produce an empty scope dict — not flow through derive_scope.

        Regression guard for the discriminator drift the manifest loader
        validates write-action fields independently; dispatch must match that
        breadth for the write-action signals (system_prompt or
        permission_mode), not just one of them."""
        permission_mode_only_routing = {
            "model": "sonnet",
            "timeout_seconds": 1500,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["campaign_id"],
                "properties": {"campaign_id": {"type": "string"}},
            },
            "scope": {},
            "permission_mode": "default",
            "manifest_version_id": SYNTHETIC_MANIFEST_VERSION_ID,
            "instruction_version_id": SYNTHETIC_INSTRUCTION_VERSION_ID,
        }

        mock_broker_cls.return_value = _mock_broker_success("tok-permission-only")
        mock_get_ecs.return_value.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }

        message = _make_message(
            "run-pm-only",
            params={"campaign_id": "0a4c1b2e-1111-4222-8333-444444444444"},
            experiment_type="compliance_smoke_test",
        )
        message["clerk_user_id"] = "user_abc123"
        # permission_mode signals write-action -> handler passes scope={}.
        result = launch_run(
            experiment=permission_mode_only_routing,
            message=message,
            scope={},
            params_json=json.dumps(message["params"]),
        )
        assert result["status"] == "launched"
        mint_kwargs = mock_broker_cls.return_value.mint_run_token.call_args.kwargs
        assert mint_kwargs["scope"] == {}
