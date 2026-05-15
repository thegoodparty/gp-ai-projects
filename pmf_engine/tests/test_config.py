import json
from unittest.mock import patch

import pytest

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.tests.conftest import synthetic_instruction, synthetic_manifest


def _envelope_for_synthetic() -> dict:
    """Build a fake broker envelope from the synthetic manifest.

    Used by tests that set EXPERIMENT_ID but don't care about the manifest
    contents — they're testing other config behavior (scheme validation,
    PARAMS_JSON handling, env var precedence). With EXPERIMENT_ID set,
    RunnerConfig.from_env requires BROKER_URL+BROKER_TOKEN and calls the
    broker; patch the broker out via this fixture.
    """
    manifest = synthetic_manifest()
    return {
        "manifest": {
            "model": manifest["model"],
            "max_turns": manifest["max_turns"],
            "timeout_seconds": manifest["timeout_seconds"],
            "output_schema": manifest["output_schema"],
        },
        "instruction": synthetic_instruction(),
    }


SYNTHETIC_EXPERIMENT_ID = synthetic_manifest()["id"]


@pytest.fixture
def patched_broker():
    """Patch load_from_broker to return the synthetic envelope."""
    envelope = _envelope_for_synthetic()
    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ) as p:
        yield p


def test_from_env_loads_all_fields(monkeypatch, patched_broker):
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-abc")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("PARAMS_JSON", json.dumps({"district": "CA-12"}))
    monkeypatch.setenv("HARNESS", "claude_sdk")
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("BROKER_URL", "https://broker.goodparty.org")
    monkeypatch.setenv("BROKER_TOKEN", "tok-secret-123")
    monkeypatch.delenv("INSTRUCTION", raising=False)

    config = RunnerConfig.from_env()

    assert config.experiment_id == SYNTHETIC_EXPERIMENT_ID
    assert config.run_id == "run-abc"
    assert config.organization_slug == "org-123"
    # INSTRUCTION env var is no longer consulted when broker fetch happens.
    # Broker envelope's instruction wins.
    assert config.instruction == synthetic_instruction()
    assert config.params == {"district": "CA-12"}
    assert config.harness == "claude_sdk"
    # AGENT_MODEL was opus, but broker manifest overrides. The runner trusts
    # the broker for model.
    assert config.model == synthetic_manifest()["model"]
    assert config.environment == "prod"
    assert config.broker_url == "https://broker.goodparty.org"
    assert config.broker_token == "tok-secret-123"


def test_from_env_uses_defaults(monkeypatch):
    """Without EXPERIMENT_ID, no broker fetch happens; defaults apply."""
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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


def test_from_env_rejects_non_draft7_output_schema(monkeypatch):
    """A manifest whose output_schema doesn't have type:'object' + properties at
    the root would be vacuously accepted by Draft7Validator and silently pass
    every artifact. Reject loudly at config load instead."""
    bad_manifest = synthetic_manifest()
    # Legacy GP-shape — a literal example dict, not a JSON Schema.
    bad_manifest["output_schema"] = {"name": "string", "count": "number"}
    bad_envelope = {
        "manifest": {
            "model": bad_manifest["model"],
            "max_turns": bad_manifest["max_turns"],
            "timeout_seconds": bad_manifest["timeout_seconds"],
            "output_schema": bad_manifest["output_schema"],
        },
        "instruction": synthetic_instruction(),
    }
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-bad-schema")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=bad_envelope,
    ):
        with pytest.raises(ValueError, match="output_schema"):
            RunnerConfig.from_env()


@pytest.mark.parametrize("combinator", ["oneOf", "anyOf", "allOf"])
def test_from_env_accepts_combinator_at_root_output_schema(monkeypatch, combinator):
    """Schemas using oneOf/anyOf/allOf at the root (e.g. status-discriminated
    artifact shapes like meeting_briefing and meeting_schedule) are real
    Draft-07 schemas and must be accepted. They have no top-level
    `type: 'object'` or `properties` dict — that's the whole point of the
    combinator pattern. Only the legacy GP-shape dict should be rejected."""
    manifest = synthetic_manifest()
    combinator_schema = {
        "title": "TestCombinatorSchema",
        combinator: [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {
                    "status": {"type": "string", "const": "ok"},
                    "data": {"type": "string"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {
                    "status": {"type": "string", "const": "error"},
                    "error": {"type": "string"},
                },
            },
        ],
    }
    envelope = {
        "manifest": {
            "model": manifest["model"],
            "max_turns": manifest["max_turns"],
            "timeout_seconds": manifest["timeout_seconds"],
            "output_schema": combinator_schema,
        },
        "instruction": synthetic_instruction(),
    }
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", f"run-{combinator}-schema")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        config = RunnerConfig.from_env()

    assert config.contract_schema == combinator_schema


def test_from_env_rejects_empty_combinator_output_schema(monkeypatch):
    """`{"oneOf": []}` is structurally a combinator but declares no constraints
    — every artifact validates. Reject as a legacy-shape-equivalent."""
    manifest = synthetic_manifest()
    envelope = {
        "manifest": {
            "model": manifest["model"],
            "max_turns": manifest["max_turns"],
            "timeout_seconds": manifest["timeout_seconds"],
            "output_schema": {"oneOf": []},
        },
        "instruction": synthetic_instruction(),
    }
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-empty-oneof")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        with pytest.raises(ValueError, match="output_schema"):
            RunnerConfig.from_env()


def test_from_env_raises_loud_on_invalid_json_params(monkeypatch):
    """Corrupted PARAMS_JSON must fail loud — a silent default to {} causes
    the agent to run on empty inputs and produce plausible but wrong artifacts
    that gp-api reports as SUCCESS."""
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("PARAMS_JSON", "not-valid-json")

    with pytest.raises(ValueError, match="PARAMS_JSON"):
        RunnerConfig.from_env()


def test_from_env_raises_on_non_object_params(monkeypatch):
    """PARAMS_JSON must decode to a JSON object, not a list/string/number."""
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("PARAMS_JSON", '[1, 2, 3]')

    with pytest.raises(ValueError, match="object"):
        RunnerConfig.from_env()


def test_from_env_normalizes_null_params_to_empty_dict(monkeypatch):
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("PARAMS_JSON", "null")

    config = RunnerConfig.from_env()
    assert config.params == {}


def test_timeout_seconds_loaded_from_env_overrides_registry(monkeypatch, patched_broker):
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("TIMEOUT_SECONDS", "1200")
    monkeypatch.delenv("INSTRUCTION", raising=False)

    config = RunnerConfig.from_env()
    assert config.timeout_seconds == 1200


def test_timeout_seconds_loaded_from_manifest_when_not_in_env(monkeypatch, patched_broker):
    """When TIMEOUT_SECONDS env var is absent, the manifest's timeout_seconds
    (delivered via the broker envelope) is the source of truth."""
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.delenv("INSTRUCTION", raising=False)
    monkeypatch.delenv("TIMEOUT_SECONDS", raising=False)

    config = RunnerConfig.from_env()

    expected_timeout = synthetic_manifest()["timeout_seconds"]
    assert config.timeout_seconds == expected_timeout


def test_timeout_seconds_non_integer_raises_value_error(monkeypatch):
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("INSTRUCTION", "Do stuff.")
    monkeypatch.setenv("TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ValueError):
        RunnerConfig.from_env()


@pytest.mark.parametrize("env", ["dev", "qa", "prod"])
def test_from_env_rejects_plaintext_broker_url_in_aws_env(monkeypatch, env):
    """Scheme validation runs after broker fetch. Without EXPERIMENT_ID set,
    we skip broker fetch entirely and validate the scheme directly."""
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("BROKER_URL", "http://broker.example.test:8080")

    with pytest.raises(ValueError, match="BROKER_URL must use https"):
        RunnerConfig.from_env()


@pytest.mark.parametrize("env", ["local", "development", "test"])
def test_from_env_allows_plaintext_broker_url_in_local_envs(monkeypatch, env):
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("BROKER_URL", "http://127.0.0.1:8080")

    config = RunnerConfig.from_env()
    assert config.broker_url == "http://127.0.0.1:8080"


@pytest.mark.parametrize("env", ["local", "development", "dev", "qa", "prod"])
def test_from_env_allows_https_broker_url_in_any_env(monkeypatch, env):
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Do.")
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.delenv("BROKER_URL", raising=False)

    config = RunnerConfig.from_env()
    assert config.broker_url == ""


@pytest.mark.parametrize("env", ["PROD", "Dev", "QA", "prod ", " dev", "Prod"])
def test_from_env_rejects_plaintext_with_case_or_whitespace_env(monkeypatch, env):
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("BROKER_URL", raw_url)

    config = RunnerConfig.from_env()
    assert config.broker_url == expected_stored


def test_from_env_error_message_includes_env_and_scheme(monkeypatch):
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
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
    monkeypatch.delenv("EXPERIMENT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-123")
    monkeypatch.setenv("INSTRUCTION", "Analyze.")
    monkeypatch.setenv("ENVIRONMENT", "qa")
    monkeypatch.setenv("BROKER_URL", "http://[::1]:8080/path")

    with pytest.raises(ValueError) as exc_info:
        RunnerConfig.from_env()

    msg = str(exc_info.value)
    assert "[::1]:8080" in msg, f"IPv6 brackets lost in error message: {msg}"


def test_from_env_with_experiment_id_requires_broker_creds(monkeypatch):
    """When EXPERIMENT_ID is set, BROKER_URL+BROKER_TOKEN are mandatory.
    There is no bundled-registry fallback after the registry deletion —
    the broker is the only manifest source."""
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.delenv("BROKER_URL", raising=False)
    monkeypatch.delenv("BROKER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="BROKER_URL and BROKER_TOKEN"):
        RunnerConfig.from_env()


class TestTimeoutSecondsErrorMessage:
    """L2: TIMEOUT_SECONDS must produce an actionable error when malformed.
    The previous int(os.environ.get(...)) raised a bare 'invalid literal for
    int()' that didn't name the env var, so operators triaging a CrashLoop in
    CloudWatch had to grep the source to figure out which env was bad.
    """

    def test_non_integer_timeout_seconds_error_names_env_and_value(self, monkeypatch):
        monkeypatch.delenv("EXPERIMENT_ID", raising=False)
        monkeypatch.setenv("RUN_ID", "run-001")
        monkeypatch.setenv("ORGANIZATION_SLUG", "test")
        monkeypatch.setenv("INSTRUCTION", "Do stuff.")
        monkeypatch.setenv("TIMEOUT_SECONDS", "not-a-number")

        with pytest.raises(ValueError) as exc_info:
            RunnerConfig.from_env()

        msg = str(exc_info.value)
        assert "TIMEOUT_SECONDS" in msg, (
            f"error must name the offending env var, got: {msg!r}"
        )
        assert "not-a-number" in msg, (
            f"error must echo the bad value so operators can diff against "
            f"the task definition, got: {msg!r}"
        )

    def test_empty_timeout_seconds_falls_back_to_default(self, monkeypatch):
        """An empty TIMEOUT_SECONDS env (e.g. accidental ``""`` in task def)
        should NOT explode — fall back to the manifest/default value. The old
        code did int("") which threw ValueError on a perfectly recoverable
        condition.
        """
        monkeypatch.delenv("EXPERIMENT_ID", raising=False)
        monkeypatch.setenv("RUN_ID", "run-001")
        monkeypatch.setenv("ORGANIZATION_SLUG", "test")
        monkeypatch.setenv("INSTRUCTION", "Do stuff.")
        monkeypatch.setenv("TIMEOUT_SECONDS", "")

        config = RunnerConfig.from_env()
        assert config.timeout_seconds == 600


class TestValidateBrokerUrlSchemeStandalone:
    """H4: validate_broker_url_scheme is the single source of truth for
    the https-only guard. The runner entrypoint calls it BEFORE init_config
    so a misconfigured plaintext URL never gets baked into the broker client.
    """

    def test_helper_raises_for_plaintext_in_prod(self):
        from pmf_engine.runner.config import (
            BrokerUrlSchemeError,
            validate_broker_url_scheme,
        )
        with pytest.raises(BrokerUrlSchemeError, match="https"):
            validate_broker_url_scheme("http://broker.example.test", "prod")

    def test_helper_allows_https_in_prod(self):
        from pmf_engine.runner.config import validate_broker_url_scheme
        validate_broker_url_scheme("https://broker.ai.goodparty.org", "prod")

    def test_helper_allows_plaintext_in_local_envs(self):
        from pmf_engine.runner.config import validate_broker_url_scheme
        for env in ("local", "development", "test"):
            validate_broker_url_scheme("http://127.0.0.1:8080", env)

    def test_helper_raises_when_broker_url_missing_in_aws_env(self):
        from pmf_engine.runner.config import (
            BrokerUrlSchemeError,
            validate_broker_url_scheme,
        )
        for env in ("dev", "qa", "prod"):
            with pytest.raises(BrokerUrlSchemeError, match="must be set"):
                validate_broker_url_scheme("", env)

    def test_helper_normalizes_environment_case_and_whitespace(self):
        from pmf_engine.runner.config import (
            BrokerUrlSchemeError,
            validate_broker_url_scheme,
        )
        for env in ("PROD", " dev", "QA", "Prod"):
            with pytest.raises(BrokerUrlSchemeError, match="https"):
                validate_broker_url_scheme("http://broker.example.test", env)

    def test_helper_redacts_userinfo_in_error_message(self):
        from pmf_engine.runner.config import (
            BrokerUrlSchemeError,
            validate_broker_url_scheme,
        )
        with pytest.raises(BrokerUrlSchemeError) as exc_info:
            validate_broker_url_scheme(
                "http://user:secret@broker.example.test/path", "prod",
            )
        msg = str(exc_info.value)
        assert "secret" not in msg
        assert "user:secret" not in msg


class TestRedactUserinfoEdgeCases:
    """L5: _redact_userinfo MUST default to a known-safe placeholder when it
    cannot positively confirm the URL has no userinfo. Returning the raw URL
    on parse failure or unparseable hostname risked leaking credentials into
    error messages and CloudWatch logs.
    """

    def test_unparseable_url_returns_redacted_placeholder(self):
        from pmf_engine.runner.config import _redact_userinfo
        result = _redact_userinfo("http://[::1")
        assert result == "<url-redacted>", (
            f"unparseable URL must default to safe placeholder, got: {result!r}"
        )

    def test_no_hostname_no_userinfo_returns_redacted_placeholder(self):
        from pmf_engine.runner.config import _redact_userinfo
        result = _redact_userinfo("not-a-url")
        assert result == "<url-redacted>", (
            f"URL with no parseable hostname must default to safe placeholder, "
            f"got: {result!r}"
        )
