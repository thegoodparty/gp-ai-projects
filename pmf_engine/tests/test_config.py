import json
import os
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


def _set_full_env(monkeypatch):
    """Shared setup for the from_env split tests below — every contract pinned
    independently below was previously bundled into test_from_env_loads_all_fields
    where a failure couldn't tell you which behavior broke."""
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


def test_from_env_pass_through_experiment_id_run_id_organization_slug(
    monkeypatch, patched_broker
):
    """The three identity fields are read straight from env vars unchanged."""
    _set_full_env(monkeypatch)

    config = RunnerConfig.from_env()

    assert config.experiment_id == SYNTHETIC_EXPERIMENT_ID
    assert config.run_id == "run-abc"
    assert config.organization_slug == "org-123"


def test_from_env_uses_broker_envelope_instruction_not_INSTRUCTION_env_var(
    monkeypatch, patched_broker
):
    """Precedence rule: when EXPERIMENT_ID is set, the broker envelope's
    instruction wins. The INSTRUCTION env var is deliberately ignored to
    prevent stale-env footguns on re-runs."""
    _set_full_env(monkeypatch)
    # Even if a stale INSTRUCTION value sneaks into the env, it must not win.
    monkeypatch.setenv("INSTRUCTION", "STALE — must be ignored")

    config = RunnerConfig.from_env()

    assert config.instruction == synthetic_instruction()
    assert "STALE" not in config.instruction


def test_from_env_broker_manifest_model_overrides_agent_model_env(
    monkeypatch, patched_broker
):
    """Precedence rule: AGENT_MODEL env var loses to the broker manifest's
    model. The runner trusts the broker for model choice so ops can swap
    models per-experiment without redeploying the runner."""
    _set_full_env(monkeypatch)
    # AGENT_MODEL is set to opus in _set_full_env; manifest specifies its own.
    assert os.environ.get("AGENT_MODEL") == "opus"

    config = RunnerConfig.from_env()

    assert config.model == synthetic_manifest()["model"]
    assert config.model != "opus", (
        f"manifest model must override AGENT_MODEL env var; got {config.model!r}"
    )


def test_from_env_params_json_parses_to_dict(monkeypatch, patched_broker):
    """PARAMS_JSON must decode into a plain dict and land on .params unchanged."""
    _set_full_env(monkeypatch)

    config = RunnerConfig.from_env()

    assert config.params == {"district": "CA-12"}


def test_from_env_environment_normalized_lowercase(monkeypatch, patched_broker):
    """ENVIRONMENT is normalized to lowercase before being stored — the rest
    of the codebase compares against lowercase tokens like 'prod' / 'dev'."""
    _set_full_env(monkeypatch)
    # Override with a mixed-case value to prove normalization happens.
    monkeypatch.setenv("ENVIRONMENT", "PROD")

    config = RunnerConfig.from_env()

    assert config.environment == "prod"


def test_from_env_broker_url_and_token_pass_through(monkeypatch, patched_broker):
    """BROKER_URL and BROKER_TOKEN are stored on the config so downstream
    re-init / fallback paths can reuse the same credentials."""
    _set_full_env(monkeypatch)

    config = RunnerConfig.from_env()

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


def _envelope_with_output_schema(schema: dict) -> dict:
    manifest = synthetic_manifest()
    return {
        "manifest": {
            "model": manifest["model"],
            "max_turns": manifest["max_turns"],
            "timeout_seconds": manifest["timeout_seconds"],
            "output_schema": schema,
        },
        "instruction": synthetic_instruction(),
    }


def _set_broker_env(monkeypatch, run_id: str = "run-x"):
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", run_id)
    monkeypatch.setenv("ORGANIZATION_SLUG", "test")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("ENVIRONMENT", "dev")


def test_from_env_rejects_combinator_with_empty_branch_objects(monkeypatch):
    """`{"oneOf": [{}]}` is a combinator whose only branch is the empty schema —
    Draft7Validator treats `{}` as a no-op so every artifact would validate.
    The shape check must recurse into branches and reject this."""
    envelope = _envelope_with_output_schema({"oneOf": [{}]})
    _set_broker_env(monkeypatch, run_id="run-empty-branch-obj")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        with pytest.raises(ValueError, match="output_schema"):
            RunnerConfig.from_env()


def test_from_env_rejects_combinator_with_only_typeless_branches(monkeypatch):
    """A branch with only metadata (`description`, `title`) and no `type` /
    `properties` / nested combinator is structurally a no-op. Reject."""
    envelope = _envelope_with_output_schema(
        {"oneOf": [{"description": "x"}, {"title": "y"}]}
    )
    _set_broker_env(monkeypatch, run_id="run-typeless-branch")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        with pytest.raises(ValueError, match="output_schema"):
            RunnerConfig.from_env()


def test_from_env_accepts_nested_combinator(monkeypatch):
    """Combinators may nest — `oneOf` of an `allOf` of a real object schema is
    legitimate. The recursive shape check must accept it."""
    nested_schema = {
        "oneOf": [
            {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"k": {"type": "string"}},
                    }
                ]
            }
        ]
    }
    envelope = _envelope_with_output_schema(nested_schema)
    _set_broker_env(monkeypatch, run_id="run-nested-combinator")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        config = RunnerConfig.from_env()

    assert config.contract_schema == nested_schema


def test_combinator_validation_error_message_matches_function_contract(monkeypatch):
    """Pin the error message wording so a future refactor of the shape check
    can't silently drift the operator-facing error from what the function
    actually accepts. The old message claimed `type='object'` was required —
    after combinators landed, that lied to the operator."""
    envelope = _envelope_with_output_schema({"name": "string", "count": "number"})
    _set_broker_env(monkeypatch, run_id="run-msg-pin")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        with pytest.raises(ValueError) as exc_info:
            RunnerConfig.from_env()

    msg = str(exc_info.value)
    # Message must mention BOTH supported shapes.
    assert "type='object'" in msg
    assert "properties" in msg
    # And the combinator branch — old message omitted this entirely.
    assert "oneOf/anyOf/allOf" in msg, (
        f"error must name the combinator forms the function actually accepts; got: {msg!r}"
    )
    assert "well-formed branch" in msg or "non-empty" in msg


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


# ---------------------------------------------------------------------------
# Attachments — sidecar files in the broker envelope
# ---------------------------------------------------------------------------
#
# RunnerConfig must thread the envelope's attachments dict through to the
# runner unchanged so main.py can write each entry to /workspace/<basename>.
# The default-empty contract is load-bearing for legacy local-dev runs that
# don't go through the broker.


def test_from_env_populates_attachments_from_envelope(monkeypatch):
    """Broker-resolved attachments must be wired into RunnerConfig so main.py
    can write them to the workspace. Basename-keyed dict, UTF-8 string bodies."""
    envelope = _envelope_for_synthetic()
    envelope["attachments"] = {
        "reference_catalog.md": "# Reference\n\n- item 1\n- item 2\n",
        "lookup.csv": "key,value\nalpha,1\n",
    }
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-attachments")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-a")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        config = RunnerConfig.from_env()

    assert config.attachments == {
        "reference_catalog.md": "# Reference\n\n- item 1\n- item 2\n",
        "lookup.csv": "key,value\nalpha,1\n",
    }


def test_from_env_defaults_attachments_to_empty_dict_when_envelope_omits(monkeypatch):
    """Broker responses from a not-yet-redeployed broker don't include the
    `attachments` field. Runner must default to {} so main.py's iteration
    works without a None-guard at every call site."""
    envelope = _envelope_for_synthetic()
    # Deliberately no `attachments` key — older brokers omit it.
    assert "attachments" not in envelope
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-no-attachments")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-a")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=envelope,
    ):
        config = RunnerConfig.from_env()

    assert config.attachments == {}


def test_runner_config_attachments_field_uses_default_factory_dict():
    """Dataclass-level pin: the `attachments` field MUST use
    `field(default_factory=dict)` so direct-construction code paths get an
    empty dict (never None or a shared mutable default).

    Companion behavioral test:
        test_main_works_with_runner_config_default_attachments
    in test_runner_main.py exercises main.py's iteration on the default to
    catch any future runtime regression of the contract this pins."""
    config = RunnerConfig(
        experiment_id="x",
        run_id="r",
        organization_slug="o",
        instruction="hi",
    )
    assert config.attachments == {}
    # No shared-mutable-default footgun: two instances must not share storage.
    other = RunnerConfig(
        experiment_id="y",
        run_id="r2",
        organization_slug="o",
        instruction="hi",
    )
    config.attachments["a.md"] = "leaked"
    assert other.attachments == {}, (
        "default_factory must yield a fresh dict per-instance; got shared state"
    )


# ---------------------------------------------------------------------------
# ATTACHMENT_VERSION_IDS env var → load_from_broker forwarding
#
# The dispatch Lambda serializes attachment_version_ids as a JSON object env
# var (see test_dispatch_handler.TestBuildContainerOverrides). The runner
# must deserialize that and forward it as a kwarg to load_from_broker, or
# the broker silently falls through to "latest" and the publish-during-run
# race re-opens for sidecar files.
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_broker_capturing():
    """Patch load_from_broker so tests can inspect every kwarg passed.

    Unlike `patched_broker` (returns a fixed envelope), this fixture also
    records the kwargs each call received. Use when the assertion is about
    *what was passed to* load_from_broker, not just that RunnerConfig
    populated correctly.
    """
    envelope = _envelope_for_synthetic()
    captured: dict = {}

    def fake_load(**kwargs):
        captured.update(kwargs)
        return envelope

    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        side_effect=fake_load,
    ):
        yield captured


def _base_env_for_attachment_tests(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ID", SYNTHETIC_EXPERIMENT_ID)
    monkeypatch.setenv("RUN_ID", "run-attachpin")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-a")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv("INSTRUCTION", raising=False)


def test_from_env_parses_attachment_version_ids_from_env(monkeypatch, patched_broker_capturing):
    """When ATTACHMENT_VERSION_IDS env var holds a JSON object, RunnerConfig
    must deserialize it and forward the dict to load_from_broker."""
    _base_env_for_attachment_tests(monkeypatch)
    monkeypatch.setenv(
        "ATTACHMENT_VERSION_IDS",
        json.dumps({"a.md": "V1", "b.csv": "V2"}, sort_keys=True),
    )

    RunnerConfig.from_env()

    assert patched_broker_capturing["attachment_version_ids"] == {"a.md": "V1", "b.csv": "V2"}


def test_from_env_with_empty_attachment_version_ids_passes_none(monkeypatch, patched_broker_capturing):
    """Empty / unset ATTACHMENT_VERSION_IDS → load_from_broker called with
    attachment_version_ids=None so the POST body omits the key (older brokers
    that reject unexpected fields stay happy)."""
    _base_env_for_attachment_tests(monkeypatch)
    monkeypatch.delenv("ATTACHMENT_VERSION_IDS", raising=False)

    RunnerConfig.from_env()

    assert patched_broker_capturing["attachment_version_ids"] is None


def test_from_env_with_blank_attachment_version_ids_passes_none(monkeypatch, patched_broker_capturing):
    """Whitespace-only env value is equivalent to unset."""
    _base_env_for_attachment_tests(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_VERSION_IDS", "   ")

    RunnerConfig.from_env()

    assert patched_broker_capturing["attachment_version_ids"] is None


def test_from_env_raises_on_malformed_attachment_version_ids_json(monkeypatch):
    """Garbage JSON in ATTACHMENT_VERSION_IDS must raise — silently falling
    back to None would defeat the entire pinning system."""
    _base_env_for_attachment_tests(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_VERSION_IDS", "not json")
    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=_envelope_for_synthetic(),
    ):
        with pytest.raises(ValueError, match="ATTACHMENT_VERSION_IDS"):
            RunnerConfig.from_env()


def test_from_env_raises_on_non_dict_attachment_version_ids(monkeypatch):
    """JSON value that decodes to a non-dict (list/string/number) must raise."""
    _base_env_for_attachment_tests(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_VERSION_IDS", json.dumps([1, 2, 3]))
    with patch(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        return_value=_envelope_for_synthetic(),
    ):
        with pytest.raises(ValueError, match="object"):
            RunnerConfig.from_env()
