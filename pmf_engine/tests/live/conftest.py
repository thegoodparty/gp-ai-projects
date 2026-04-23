"""Live-dev smoke test harness.

These tests hit REAL deployed infrastructure. They start at gp-api's
handoff point — the `agent-dispatch-{env}.fifo` SQS queue — and assert
on S3 after the PMF spine runs. They cost real money (Anthropic +
Databricks) and leave artifacts in S3. For that reason they are guarded
by TWO concentric locks — BOTH must open for a test to fire:

  1. `LIVE_SMOKE_ENABLE=1` env var (collection-time — default deny)
  2. `-m live_dev` pytest marker expression (opt-in selection)

Plus the test body requires AWS credentials (AWS_PROFILE=work); boto3
will skip if they're missing.

Nothing in CI sets any of these, so `uv run pytest` from any clean
environment cannot fire a live run by accident.

Usage:
    # Before merging a risky fix to the PMF spine, dispatch real runs
    # and confirm the artifact lands in S3.
    export LIVE_SMOKE_ENABLE=1
    export AWS_PROFILE=work
    uv run pytest pmf_engine/tests/live/ -m live_dev -v -s

    # Run just one experiment:
    uv run pytest pmf_engine/tests/live/ -m live_dev -k district_intel -v -s

Config (env vars):
    LIVE_SMOKE_ENABLE            REQUIRED — set to 1/true/yes to unlock
    AWS_PROFILE (or keys)        REQUIRED — AWS creds with s3:GetObject and
                                 sqs:SendMessage on the dev account
    LIVE_SMOKE_ENV               optional (default "dev")
    LIVE_SMOKE_ACCOUNT           optional (default "333022194791")
    LIVE_SMOKE_ARTIFACT_BUCKET   optional (default "gp-agent-artifacts-{env}")
    LIVE_SMOKE_DISPATCH_QUEUE_URL optional (default derived from env+account+region)
    LIVE_SMOKE_ORG_SLUG          optional (default "smoke-test-pmf")
    LIVE_SMOKE_TIMEOUT_MINUTES   optional (default 20)
    LIVE_SMOKE_POLL_SECONDS      optional (default 15)
"""
from __future__ import annotations

import os

import pytest

_ENABLE_VALUES = {"1", "true", "yes", "on"}


def _is_live_enabled() -> bool:
    return os.environ.get("LIVE_SMOKE_ENABLE", "").strip().lower() in _ENABLE_VALUES


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_dev: live-dev smoke test — real SQS dispatch + Fargate + S3, "
        "costs real money, needs LIVE_SMOKE_ENABLE=1 AND `-m live_dev` AND "
        "AWS creds",
    )


def pytest_report_header(config):
    """Loud banner in pytest's session header if the opt-in is set.

    If someone accidentally leaves LIVE_SMOKE_ENABLE=1 exported in their
    shell, they see this on every pytest run and can catch it before any
    -m live_dev invocation fires a dispatch.
    """
    if _is_live_enabled():
        return [
            "!" * 68,
            "! LIVE_SMOKE_ENABLE=1 — live-dev smoke tests are UNLOCKED.       !",
            "! Any `-m live_dev` selection will dispatch REAL experiments    !",
            "! to dev and cost real money. Unset LIVE_SMOKE_ENABLE to lock.  !",
            "!" * 68,
        ]
    return []


def pytest_collection_modifyitems(config, items):
    """Default-deny gate for `live_dev` tests.

    Two checks must both pass for a live test to run:
      (a) LIVE_SMOKE_ENABLE=1 (belt)
      (b) `-m live_dev` marker requested (suspenders)

    Plus the test body checks for AWS credentials (parachute). Missing any
    of the three → skip. This hook covers (a) and (b); the AWS check lives
    in the test body so pytest can report "credentials missing" distinctly.
    """
    enable = _is_live_enabled()
    requested = (config.getoption("-m") or "").strip()
    marker_requested = requested == "live_dev"

    if enable and marker_requested:
        return

    if not enable and not marker_requested:
        reason = (
            "live_dev disabled — needs LIVE_SMOKE_ENABLE=1 AND `-m live_dev` "
            "AND AWS creds"
        )
    elif not enable:
        reason = (
            "live_dev locked — LIVE_SMOKE_ENABLE is not set (env-var kill "
            "switch; marker alone is not enough to fire a real dispatch)"
        )
    else:
        reason = (
            "live_dev not selected — run with `pytest -m live_dev` exactly "
            "(compound -m expressions are treated as no-opt-in for safety)"
        )

    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "live_dev" in item.keywords:
            item.add_marker(skip)
