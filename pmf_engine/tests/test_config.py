import json
import os

import pytest

from pmf_engine.runner.config import RunnerConfig


def test_from_env_loads_all_fields(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-abc")
    monkeypatch.setenv("CANDIDATE_ID", "cand-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze voter data.")
    monkeypatch.setenv("PARAMS_JSON", json.dumps({"district": "CA-12"}))
    monkeypatch.setenv("HARNESS", "claude_sdk")
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("ARTIFACT_BUCKET", "gp-agent-artifacts-prod")
    monkeypatch.setenv("ARTIFACT_KEY_TEMPLATE", "{experiment_id}/{run_id}/result.json")
    monkeypatch.setenv("CALLBACK_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/agent-callback-prod.fifo")

    config = RunnerConfig.from_env()

    assert config.experiment_id == "voter_targeting"
    assert config.run_id == "run-abc"
    assert config.candidate_id == "cand-123"
    assert config.instruction == "Analyze voter data."
    assert config.params == {"district": "CA-12"}
    assert config.harness == "claude_sdk"
    assert config.model == "opus"
    assert config.environment == "prod"
    assert config.artifact_bucket == "gp-agent-artifacts-prod"
    assert config.artifact_key_template == "{experiment_id}/{run_id}/result.json"
    assert config.callback_queue_url == "https://sqs.us-west-2.amazonaws.com/123/agent-callback-prod.fifo"


def test_from_env_uses_defaults(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "hello_world")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("CANDIDATE_ID", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    for key in ["PARAMS_JSON", "HARNESS", "AGENT_MODEL", "ENVIRONMENT",
                "ARTIFACT_BUCKET", "ARTIFACT_KEY_TEMPLATE", "CALLBACK_QUEUE_URL"]:
        monkeypatch.delenv(key, raising=False)

    config = RunnerConfig.from_env()

    assert config.params == {}
    assert config.harness == "claude_sdk"
    assert config.model == "sonnet"
    assert config.environment == "dev"
    assert config.artifact_bucket == ""
    assert config.artifact_key_template == ""
    assert config.callback_queue_url == ""


def test_from_env_raises_loud_on_invalid_json_params(monkeypatch):
    """Corrupted PARAMS_JSON must fail loud — a silent default to {} causes
    the agent to run on empty inputs and produce plausible but wrong artifacts
    that gp-api reports as SUCCESS."""
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("CANDIDATE_ID", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("PARAMS_JSON", "not-valid-json")

    with pytest.raises(ValueError, match="PARAMS_JSON"):
        RunnerConfig.from_env()


def test_from_env_raises_on_non_object_params(monkeypatch):
    """PARAMS_JSON must decode to a JSON object, not a list/string/number."""
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("CANDIDATE_ID", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("PARAMS_JSON", '[1, 2, 3]')

    with pytest.raises(ValueError, match="object"):
        RunnerConfig.from_env()


def test_from_env_normalizes_null_params_to_empty_dict(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("CANDIDATE_ID", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("PARAMS_JSON", "null")

    config = RunnerConfig.from_env()
    assert config.params == {}


def test_resolve_artifact_key():
    config = RunnerConfig(
        experiment_id="voter_targeting",
        run_id="run-abc",
        candidate_id="cand-123",
        instruction="",
        params={},
        artifact_key_template="{experiment_id}/{run_id}/result.json",
    )
    key = config.resolve_artifact_key()
    assert key == "voter_targeting/run-abc/result.json"


def test_timeout_seconds_loaded_from_env_overrides_registry(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("CANDIDATE_ID", "test")
    monkeypatch.setenv("INSTRUCTION", "")
    monkeypatch.setenv("TIMEOUT_SECONDS", "1200")

    config = RunnerConfig.from_env()
    assert config.timeout_seconds == 1200


def test_timeout_seconds_loaded_from_registry_when_not_in_env(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("CANDIDATE_ID", "test")
    monkeypatch.delenv("INSTRUCTION", raising=False)
    monkeypatch.delenv("TIMEOUT_SECONDS", raising=False)

    config = RunnerConfig.from_env()

    from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY
    expected_timeout = EXPERIMENT_REGISTRY["voter_targeting"]["timeout_seconds"]
    assert config.timeout_seconds == expected_timeout


def test_timeout_seconds_non_integer_raises_value_error(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("CANDIDATE_ID", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ValueError):
        RunnerConfig.from_env()


def test_resolve_artifact_key_passes_attacker_run_id_literally():
    """run_id that looks like a Python format spec must pass through as
    literal text. Use .replace() to keep substitution non-evaluating."""
    config = RunnerConfig(
        experiment_id="voter_targeting",
        run_id="{0.__class__}",
        candidate_id="cand-123",
        instruction="",
        params={},
        artifact_key_template="{experiment_id}/{run_id}/x.json",
    )
    key = config.resolve_artifact_key()
    assert key == "voter_targeting/{0.__class__}/x.json"


def test_resolve_artifact_key_does_not_evaluate_attribute_traversal_in_template():
    """If the template contained {experiment_id.__class__} (e.g., from a
    compromised env var), str.format would evaluate it to <class 'str'>.
    Using .replace() makes the substitution literal, so unknown placeholders
    stay as-is and cannot traverse attributes."""
    config = RunnerConfig(
        experiment_id="voter_targeting",
        run_id="run-abc",
        candidate_id="cand-123",
        instruction="",
        params={},
        artifact_key_template="{experiment_id.__class__}/{run_id}/x.json",
    )
    key = config.resolve_artifact_key()
    assert "<class" not in key
    assert key == "{experiment_id.__class__}/run-abc/x.json"


def test_resolve_artifact_key_empty_template():
    config = RunnerConfig(
        experiment_id="test",
        run_id="run-001",
        candidate_id="test",
        instruction="",
        params={},
        artifact_key_template="",
    )
    key = config.resolve_artifact_key()
    assert key == ""
