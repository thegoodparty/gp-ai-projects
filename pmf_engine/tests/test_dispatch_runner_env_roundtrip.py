"""Contract test: dispatch Lambda env output round-trips through RunnerConfig.

The env-var contract between dispatch_handler.build_container_overrides and
RunnerConfig.from_env is fragile — a single rename on either side (e.g.
CAND_ID vs ORGANIZATION_SLUG, TIMEOUT vs TIMEOUT_SECONDS) would break every
in-flight dispatch silently because the two layers live in different
deployable units. This test runs a mock dispatch message through
build_container_overrides, installs the result as real os.environ, and
asserts RunnerConfig.from_env sees the expected values.

Any drift on either side of this boundary must fail here, loudly, on every PR.

The engine is generic — one synthetic experiment is enough to lock the
env-var contract. Per-experiment routing/contract checks belong in runbooks.

The runner-side broker fetch is exercised through a real
``httpx.MockTransport`` — patching ``load_from_broker`` itself would void the
boundary the test claims to defend (see test_manifest_loader.py for the same
pattern at the unit-test layer).
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import httpx

from pmf_engine.control_plane.dispatch_handler import build_container_overrides
from pmf_engine.runner.config import RunnerConfig
from pmf_engine.tests.conftest import synthetic_instruction, synthetic_manifest


def _env_list_to_map(env_list: list[dict]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in env_list}


def _experiment_from_manifest(manifest: dict) -> dict:
    """Build the experiment dict shape that build_container_overrides expects.

    build_container_overrides reads: model, timeout_seconds. That's it.
    """
    return {
        "model": manifest["model"],
        "timeout_seconds": manifest["timeout_seconds"],
    }


def _base_message(experiment_id: str) -> dict:
    return {
        "experiment_type": experiment_id,
        "run_id": f"run-{experiment_id}-abc123",
        "organization_slug": f"organization-{experiment_id}",
        "params": {"state": "MI"},
    }


def _broker_envelope(manifest: dict, instruction: str) -> dict:
    """Shape the runner expects from POST /experiment/manifest."""
    return {
        "manifest": {
            "model": manifest["model"],
            "max_turns": manifest["max_turns"],
            "timeout_seconds": manifest["timeout_seconds"],
            "output_schema": manifest["output_schema"],
        },
        "instruction": instruction,
    }


_REAL_HTTPX_CLIENT = httpx.Client


class _ClientFactory:
    """Builds httpx.Client instances pre-wired with a MockTransport.

    Substituted in for ``httpx.Client`` inside
    ``pmf_engine.runner.manifest_loader`` so the real ``load_from_broker``
    runs end-to-end (request shape, header pass-through, status handling,
    envelope validation) against an in-memory transport — no patching out
    the boundary itself. We capture the real ``httpx.Client`` at module load
    so the patched factory can still instantiate one without recursing into
    itself.
    """

    def __init__(self, handler):
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, *args, **kwargs):
        # load_from_broker passes base_url, headers, timeout — preserve them so
        # the real header/auth wiring is exercised.
        kwargs["transport"] = httpx.MockTransport(self._record_then_handle)
        return _REAL_HTTPX_CLIENT(*args, **kwargs)

    def _record_then_handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def test_dispatch_env_roundtrips_to_runner_config():
    manifest = synthetic_manifest()
    experiment_id = manifest["id"]
    instruction = synthetic_instruction()
    experiment = _experiment_from_manifest(manifest)
    message = _base_message(experiment_id)

    overrides = build_container_overrides(
        experiment=experiment,
        message=message,
        broker_token="tok-test-123",
        broker_url="https://broker.example.com",
        container_name="pmf-engine",
    )

    env_list = overrides["containerOverrides"][0]["environment"]
    env_map = _env_list_to_map(env_list)

    for critical in (
        "EXPERIMENT_ID",
        "RUN_ID",
        "ORGANIZATION_SLUG",
        "AGENT_MODEL",
        "BROKER_TOKEN",
        "BROKER_URL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "PARAMS_JSON",
        "TIMEOUT_SECONDS",
    ):
        assert critical in env_map, (
            f"dispatch_handler no longer sets {critical} — "
            f"RunnerConfig.from_env will read stale/missing values"
        )

    assert env_map["BROKER_TOKEN"] == "tok-test-123"
    assert env_map["BROKER_URL"] == "https://broker.example.com"
    assert env_map["ANTHROPIC_BASE_URL"] == "https://broker.example.com/anthropic"
    assert env_map["ANTHROPIC_API_KEY"] == "tok-test-123"

    envelope = _broker_envelope(manifest, instruction)

    def handler(request: httpx.Request) -> httpx.Response:
        # The runner must POST to /experiment/manifest with the broker token,
        # carrying the experiment_id from env. Asserting these here proves the
        # whole env→manifest_loader→broker chain stayed wired up.
        assert request.url.path == "/experiment/manifest"
        assert request.headers["x-broker-token"] == env_map["BROKER_TOKEN"]
        body = json.loads(request.content)
        assert body["experiment_id"] == experiment_id
        return httpx.Response(200, json=envelope)

    factory = _ClientFactory(handler)

    with patch.dict(os.environ, env_map, clear=False), \
         patch("pmf_engine.runner.manifest_loader.httpx.Client", factory):
        os.environ.pop("INSTRUCTION", None)
        config = RunnerConfig.from_env()

    assert len(factory.requests) == 1, (
        f"runner must hit broker exactly once on cold from_env(), "
        f"got {len(factory.requests)} requests"
    )

    assert config.experiment_id == experiment_id
    assert config.run_id == message["run_id"]
    assert config.organization_slug == message["organization_slug"]
    assert config.params == message["params"]
    assert config.harness == "claude_sdk"
    assert config.model == manifest["model"]
    assert config.timeout_seconds == manifest["timeout_seconds"]
    assert config.instruction == instruction
    assert config.contract_schema == manifest["output_schema"]


def test_dispatch_env_params_json_is_valid_json_and_dict():
    manifest = synthetic_manifest()
    experiment = _experiment_from_manifest(manifest)
    message = _base_message(manifest["id"])
    overrides = build_container_overrides(
        experiment=experiment,
        message=message,
        broker_token="tok",
        broker_url="https://broker.example.com",
        container_name="c",
    )
    env_map = _env_list_to_map(overrides["containerOverrides"][0]["environment"])
    parsed = json.loads(env_map["PARAMS_JSON"])
    assert parsed == message["params"]


def test_dispatch_env_timeout_is_string_type():
    manifest = synthetic_manifest()
    experiment = _experiment_from_manifest(manifest)
    overrides = build_container_overrides(
        experiment=experiment,
        message=_base_message(manifest["id"]),
        broker_token="tok",
        broker_url="https://broker.example.com",
        container_name="c",
    )
    env_map = _env_list_to_map(overrides["containerOverrides"][0]["environment"])
    assert isinstance(env_map["TIMEOUT_SECONDS"], str), (
        "ECS runTask environment values must be strings"
    )
    int(env_map["TIMEOUT_SECONDS"])


def test_attachment_version_ids_round_trip_dispatch_to_runner_request(monkeypatch):
    """Attachment VersionIds captured at dispatch MUST reach the broker
    request body unchanged.

    Whole-pipeline contract test: this fails if ANY link in
        dispatch_handler → ECS env → runner config → manifest_loader → POST body
    drops or mangles the attachment_version_ids dict.

    Until this test landed, the whole pipeline was broken at runtime: the
    broker advertises attachment_version_ids on the request body but nothing
    upstream populated it, so every attachment fetch silently fell through
    to 'latest' — re-opening the publish-during-run race for sidecars.
    """
    pin_dict = {"lookup.csv": "V-lookup-abc", "notes.md": "V-notes-xyz"}
    manifest = synthetic_manifest()
    experiment_id = manifest["id"]
    instruction = synthetic_instruction()
    message = _base_message(experiment_id)

    # 1. dispatch_handler serializes ATTACHMENT_VERSION_IDS env var
    overrides = build_container_overrides(
        experiment={
            "model": manifest["model"],
            "timeout_seconds": manifest["timeout_seconds"],
            "manifest_version_id": "Mv1",
            "instruction_version_id": "Iv1",
            "attachment_version_ids": pin_dict,
        },
        message=message,
        broker_token="tok-attach-rt",
        broker_url="https://broker.example.com",
        container_name="pmf-engine",
    )
    env_map = _env_list_to_map(overrides["containerOverrides"][0]["environment"])
    assert env_map["ATTACHMENT_VERSION_IDS"] == json.dumps(pin_dict, sort_keys=True), (
        "dispatch_handler must serialize attachment_version_ids as sort_keys JSON"
    )

    # 2. runner config parses + forwards to load_from_broker
    envelope = _broker_envelope(manifest, instruction)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # The key behavior under test: attachment_version_ids made it into
        # the broker request body. If anything between dispatch and here drops
        # it, the assertion fails.
        assert body.get("attachment_version_ids") == pin_dict, (
            f"runner did not forward pin dict to broker; body was {body!r}"
        )
        return httpx.Response(200, json=envelope)

    factory = _ClientFactory(handler)

    with patch.dict(os.environ, env_map, clear=False), \
         patch("pmf_engine.runner.manifest_loader.httpx.Client", factory):
        os.environ.pop("INSTRUCTION", None)
        RunnerConfig.from_env()

    assert len(factory.requests) == 1, (
        f"runner must hit broker exactly once, got {len(factory.requests)}"
    )
