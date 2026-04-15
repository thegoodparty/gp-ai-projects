"""Contract test: dispatch Lambda env output round-trips through RunnerConfig.

The env-var contract between dispatch_handler.build_container_overrides and
RunnerConfig.from_env is fragile — a single rename on either side (e.g.
CAND_ID vs CANDIDATE_ID, TIMEOUT vs TIMEOUT_SECONDS) would break every
in-flight dispatch silently because the two layers live in different
deployable units. This test runs a mock dispatch message through
build_container_overrides, installs the result as real os.environ, and
asserts RunnerConfig.from_env sees the expected values.

Any drift on either side of this boundary must fail here, loudly, on every PR.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from pmf_engine.control_plane.dispatch_handler import build_container_overrides
from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY
from pmf_engine.runner.config import RunnerConfig


def _env_list_to_map(env_list: list[dict]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in env_list}


def _base_message(experiment_id: str) -> dict:
    return {
        "experiment_id": experiment_id,
        "run_id": f"run-{experiment_id}-abc123",
        "candidate_id": f"candidate-{experiment_id}",
        "params": {"state": "MI", "district": "Ward 3", "priority": 42},
    }


@pytest.mark.parametrize("experiment_id", sorted(EXPERIMENT_REGISTRY.keys()))
def test_dispatch_env_roundtrips_to_runner_config(experiment_id):
    experiment = EXPERIMENT_REGISTRY[experiment_id]
    message = _base_message(experiment_id)

    overrides = build_container_overrides(
        experiment=experiment,
        message=message,
        artifact_bucket="gp-agent-artifacts-test",
        callback_queue_url="https://sqs.us-west-2.amazonaws.com/333022194791/test.fifo",
        container_name="pmf-engine",
    )

    env_list = overrides["containerOverrides"][0]["environment"]
    env_map = _env_list_to_map(env_list)

    for critical in (
        "EXPERIMENT_ID",
        "RUN_ID",
        "CANDIDATE_ID",
        "HARNESS",
        "AGENT_MODEL",
        "ARTIFACT_BUCKET",
        "ARTIFACT_KEY_TEMPLATE",
        "CALLBACK_QUEUE_URL",
        "PARAMS_JSON",
        "TIMEOUT_SECONDS",
    ):
        assert critical in env_map, (
            f"dispatch_handler no longer sets {critical} — "
            f"RunnerConfig.from_env will read stale/missing values"
        )

    with patch.dict(os.environ, env_map, clear=False):
        os.environ.pop("INSTRUCTION", None)
        config = RunnerConfig.from_env()

    assert config.experiment_id == experiment_id
    assert config.run_id == message["run_id"]
    assert config.candidate_id == message["candidate_id"]
    assert config.params == message["params"]
    assert config.harness == experiment["harness"]
    assert config.model == experiment["model"]
    assert config.artifact_bucket == "gp-agent-artifacts-test"
    assert config.callback_queue_url == (
        "https://sqs.us-west-2.amazonaws.com/333022194791/test.fifo"
    )

    assert config.artifact_key_template == experiment["contract"]["s3_key_template"]
    assert config.artifact_key_template.startswith("{experiment_id}/"), (
        "s3_key_template convention is '{experiment_id}/{run_id}/<file>.json'"
    )
    assert "{run_id}" in config.artifact_key_template

    assert config.timeout_seconds == experiment.get("timeout_seconds", 600)

    resolved = config.resolve_artifact_key()
    assert experiment_id in resolved
    assert message["run_id"] in resolved
    assert "{experiment_id}" not in resolved
    assert "{run_id}" not in resolved

    assert config.instruction, (
        f"RunnerConfig.from_env did not load instruction for {experiment_id} "
        f"— runner would exit with 'No instruction available'"
    )
    assert config.instruction == experiment["instruction"]

    if experiment.get("contract", {}).get("schema"):
        assert config.contract_schema == experiment["contract"]["schema"], (
            f"contract_schema drift for {experiment_id}"
        )

    if experiment.get("contract", {}).get("constraints"):
        assert config.contract_constraints == experiment["contract"]["constraints"]


def test_dispatch_env_params_json_is_valid_json_and_dict():
    experiment_id = next(iter(EXPERIMENT_REGISTRY))
    experiment = EXPERIMENT_REGISTRY[experiment_id]
    message = _base_message(experiment_id)
    overrides = build_container_overrides(
        experiment=experiment,
        message=message,
        artifact_bucket="b",
        callback_queue_url="q",
        container_name="c",
    )
    env_map = _env_list_to_map(overrides["containerOverrides"][0]["environment"])
    parsed = json.loads(env_map["PARAMS_JSON"])
    assert parsed == message["params"]


def test_dispatch_env_timeout_is_string_type():
    experiment_id = next(iter(EXPERIMENT_REGISTRY))
    experiment = EXPERIMENT_REGISTRY[experiment_id]
    overrides = build_container_overrides(
        experiment=experiment,
        message=_base_message(experiment_id),
        artifact_bucket="b",
        callback_queue_url="q",
        container_name="c",
    )
    env_map = _env_list_to_map(overrides["containerOverrides"][0]["environment"])
    assert isinstance(env_map["TIMEOUT_SECONDS"], str), (
        "ECS runTask environment values must be strings"
    )
    int(env_map["TIMEOUT_SECONDS"])
