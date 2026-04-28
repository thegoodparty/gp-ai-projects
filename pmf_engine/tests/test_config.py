import json
import os

import pytest

from pmf_engine.runner.config import RunnerConfig


def test_from_env_loads_all_fields(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-abc")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze voter data.")
    monkeypatch.setenv("PARAMS_JSON", json.dumps({"district": "CA-12"}))
    monkeypatch.setenv("HARNESS", "claude_sdk")
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("BROKER_URL", "https://broker.goodparty.org")
    monkeypatch.setenv("BROKER_TOKEN", "tok-secret-123")

    config = RunnerConfig.from_env()

    assert config.experiment_id == "voter_targeting"
    assert config.run_id == "run-abc"
    assert config.organization_slug == "org-123"
    assert config.instruction == "Analyze voter data."
    assert config.params == {"district": "CA-12"}
    assert config.harness == "claude_sdk"
    assert config.model == "opus"
    assert config.environment == "prod"
    assert config.broker_url == "https://broker.goodparty.org"
    assert config.broker_token == "tok-secret-123"


def test_from_env_uses_defaults(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "hello_world")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("ENVIRONMENT", "local")
    for key in ["PARAMS_JSON", "HARNESS", "AGENT_MODEL",
                "BROKER_URL", "BROKER_TOKEN"]:
        monkeypatch.delenv(key, raising=False)

    config = RunnerConfig.from_env()

    assert config.params == {}
    assert config.harness == "claude_sdk"
    assert config.model == "sonnet"
    assert config.environment == "local"
    assert config.broker_url == ""
    assert config.broker_token == ""


def test_from_env_raises_loud_on_invalid_json_params(monkeypatch):
    """Corrupted PARAMS_JSON must fail loud — a silent default to {} causes
    the agent to run on empty inputs and produce plausible but wrong artifacts
    that gp-api reports as SUCCESS."""
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("PARAMS_JSON", "not-valid-json")

    with pytest.raises(ValueError, match="PARAMS_JSON"):
        RunnerConfig.from_env()


def test_from_env_raises_on_non_object_params(monkeypatch):
    """PARAMS_JSON must decode to a JSON object, not a list/string/number."""
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("PARAMS_JSON", '[1, 2, 3]')

    with pytest.raises(ValueError, match="object"):
        RunnerConfig.from_env()


def test_from_env_normalizes_null_params_to_empty_dict(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("PARAMS_JSON", "null")

    config = RunnerConfig.from_env()
    assert config.params == {}


def test_timeout_seconds_loaded_from_env_overrides_registry(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("TIMEOUT_SECONDS", "1200")

    config = RunnerConfig.from_env()
    assert config.timeout_seconds == 1200


def test_timeout_seconds_loaded_from_registry_when_not_in_env(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.delenv("INSTRUCTION", raising=False)
    monkeypatch.delenv("TIMEOUT_SECONDS", raising=False)

    config = RunnerConfig.from_env()

    from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY
    expected_timeout = EXPERIMENT_REGISTRY["voter_targeting"]["timeout_seconds"]
    assert config.timeout_seconds == expected_timeout


def test_timeout_seconds_non_integer_raises_value_error(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ValueError):
        RunnerConfig.from_env()


@pytest.mark.parametrize("env", ["dev", "qa", "prod"])
def test_from_env_rejects_plaintext_broker_url_in_aws_env(monkeypatch, env):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("BROKER_URL", "http://broker.example.test:8080")

    with pytest.raises(ValueError, match="BROKER_URL must use https"):
        RunnerConfig.from_env()


@pytest.mark.parametrize("env", ["local", "development", "test"])
def test_from_env_allows_plaintext_broker_url_in_local_envs(monkeypatch, env):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("BROKER_URL", "http://127.0.0.1:8080")

    config = RunnerConfig.from_env()
    assert config.broker_url == "http://127.0.0.1:8080"


@pytest.mark.parametrize("env", ["local", "development", "dev", "qa", "prod"])
def test_from_env_allows_https_broker_url_in_any_env(monkeypatch, env):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("BROKER_URL", "https://broker.ai.goodparty.org")

    config = RunnerConfig.from_env()
    assert config.broker_url == "https://broker.ai.goodparty.org"


@pytest.mark.parametrize("env", ["dev", "qa", "prod"])
@pytest.mark.parametrize("broker_url_missing_mode", ["unset", "empty"])
def test_from_env_rejects_missing_broker_url_in_aws_env(monkeypatch, env, broker_url_missing_mode):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", env)
    if broker_url_missing_mode == "empty":
        monkeypatch.setenv("BROKER_URL", "")
    else:
        monkeypatch.delenv("BROKER_URL", raising=False)

    with pytest.raises(ValueError, match="BROKER_URL must be set"):
        RunnerConfig.from_env()


@pytest.mark.parametrize("env", ["local", "development", "test"])
def test_from_env_allows_empty_broker_url_in_local_envs(monkeypatch, env):
    monkeypatch.setenv("EXPERIMENT_ID", "test")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Do.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.delenv("BROKER_URL", raising=False)

    config = RunnerConfig.from_env()
    assert config.broker_url == ""


@pytest.mark.parametrize("env", ["PROD", "Dev", "QA", "prod ", " dev", "Prod"])
def test_from_env_rejects_plaintext_with_case_or_whitespace_env(monkeypatch, env):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("BROKER_URL", "http://broker.example.test:8080")

    with pytest.raises(ValueError, match="BROKER_URL must use https"):
        RunnerConfig.from_env()


@pytest.mark.parametrize(
    "bad_url",
    [
        "HTTP://broker.example.test:8080",
        " http://broker.example.test:8080",
        "http://broker.example.test:8080\n",
        "ftp://broker.example.test",
        "//broker.example.test",
        "httpsx://broker.example.test",
        "https:broker.example.test",
    ],
)
def test_from_env_rejects_non_https_scheme_variants(monkeypatch, bad_url):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("BROKER_URL", bad_url)

    with pytest.raises(ValueError, match="BROKER_URL must use https"):
        RunnerConfig.from_env()


@pytest.mark.parametrize(
    "raw_url,expected_stored",
    [
        ("HTTPS://broker.ai.goodparty.org", "HTTPS://broker.ai.goodparty.org"),
        (" https://broker.ai.goodparty.org", "https://broker.ai.goodparty.org"),
        ("https://broker.ai.goodparty.org\n", "https://broker.ai.goodparty.org"),
    ],
    ids=["uppercase-scheme-preserved", "leading-whitespace-stripped", "trailing-newline-stripped"],
)
def test_from_env_accepts_https_with_case_or_whitespace(monkeypatch, raw_url, expected_stored):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("BROKER_URL", raw_url)

    config = RunnerConfig.from_env()
    assert config.broker_url == expected_stored


def test_from_env_error_message_includes_env_and_scheme(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "qa")
    monkeypatch.setenv("BROKER_URL", "http://host/path")

    with pytest.raises(ValueError) as exc_info:
        RunnerConfig.from_env()

    msg = str(exc_info.value)
    assert "environment='qa'" in msg
    assert "scheme='http'" in msg


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://user:secret@host/path",
        "http://user:secret@host:8080/path?q=1",
        "http://user@host/path",
    ],
)
def test_from_env_error_message_redacts_userinfo_credentials(monkeypatch, bad_url):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "qa")
    monkeypatch.setenv("BROKER_URL", bad_url)

    with pytest.raises(ValueError) as exc_info:
        RunnerConfig.from_env()

    msg = str(exc_info.value)
    assert "secret" not in msg
    assert "user" not in msg
    assert "user:secret" not in msg


def test_from_env_error_message_redacts_userinfo_on_malformed_host(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "qa")
    monkeypatch.setenv("BROKER_URL", "http://user:secret@/path")

    with pytest.raises(ValueError) as exc_info:
        RunnerConfig.from_env()

    msg = str(exc_info.value)
    assert "secret" not in msg
    assert "user:secret" not in msg


def test_from_env_error_message_preserves_ipv6_brackets(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", "voter_targeting")
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "qa")
    monkeypatch.setenv("BROKER_URL", "http://[::1]:8080/path")

    with pytest.raises(ValueError) as exc_info:
        RunnerConfig.from_env()

    msg = str(exc_info.value)
    assert "[::1]:8080" in msg, f"IPv6 brackets lost in error message: {msg}"


