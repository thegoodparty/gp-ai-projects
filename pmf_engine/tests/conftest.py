"""Shared pytest config + fixtures for pmf_engine tests.

The PMF engine is a generic dispatch+run+validate engine. It does NOT know
about specific experiments — those live in the runbooks repo
(`~/work/runbooks/experiments/<id>/`). Engine tests therefore exercise the
engine against a SYNTHETIC manifest defined inline below; per-experiment
contract validation is the runbooks repo's job.

If you find yourself reaching for a real experiment fixture here, that's a
smell — the test belongs in runbooks, not in pmf_engine.
"""
from __future__ import annotations

import copy

import pytest


@pytest.fixture(autouse=True)
def _default_environment_to_test(monkeypatch):
    """Default ENVIRONMENT to a non-deployment value ('test') for every test.

    ``RunnerConfig.from_env`` / ``main`` default ENVIRONMENT to 'dev' when unset,
    and 'dev' is an AWS deployment env (config._AWS_DEPLOYMENT_ENVS), so
    ``validate_broker_url_scheme`` raises ``BrokerUrlSchemeError`` whenever
    BROKER_URL is unset. Tests that don't care about env then crash on init
    before exercising the behavior under test. Pinning 'test' (not in
    _AWS_DEPLOYMENT_ENVS) takes the local/in-process path. Env-validation tests
    that explicitly set ENVIRONMENT to dev/qa/prod via monkeypatch.setenv still
    override this (later setenv wins)."""
    monkeypatch.setenv("ENVIRONMENT", "test")

# Synthetic manifest — minimal but realistic shape that satisfies the meta
# schema at `~/work/runbooks/experiments/_schema/manifest.schema.json`. Used
# by every engine test that needs a manifest. The id `smoke_test` does NOT
# exist in runbooks; it's purely a placeholder for engine-level coverage.
SYNTHETIC_MANIFEST: dict = {
    "id": "smoke_test",
    "version": 1,
    "model": "sonnet",
    "max_turns": 10,
    "timeout_seconds": 600,
    "scope": {
        "allowed_tables": [
            "goodparty_data_catalog.dbt.synthetic_table"
        ],
        "max_rows": 1000,
    },
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["state"],
        "properties": {
            "state": {
                "type": "string",
                "pattern": "^[A-Z]{2}$",
                "description": "2-letter state code.",
            },
            "note": {
                "type": "string",
                "description": "Optional free-form note (kept tiny intentionally — used by size-limit tests).",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["summary"],
        "additionalProperties": True,
        "properties": {
            "summary": {
                "type": "object",
                "required": ["total"],
                "additionalProperties": True,
                "properties": {
                    "total": {"type": "number"},
                },
            },
        },
    },
}

SYNTHETIC_INSTRUCTION = "# Smoke test instruction\n\nThis is a synthetic instruction used by engine tests.\n"


def synthetic_manifest() -> dict:
    """Return a fresh deep copy of the synthetic manifest.

    Tests that mutate the returned dict (e.g. to override one field for a
    targeted assertion) won't pollute other tests. Always return a copy.
    """
    return copy.deepcopy(SYNTHETIC_MANIFEST)


def synthetic_instruction() -> str:
    return SYNTHETIC_INSTRUCTION


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: full-stack local end-to-end test "
        "(requires local gp-api, AWS creds, Claude agent; run with -m e2e)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m") == "e2e":
        return
    skip_e2e = __import__("pytest").mark.skip(reason="e2e test — run with `pytest -m e2e`")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
