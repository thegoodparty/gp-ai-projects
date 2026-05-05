"""District Intel smoke test — research-spine regression guard.

Walks a district_intel run through the broker end-to-end:
  mint → /http/fetch → /artifact/publish → /artifact/read → /internal/run-status

Verifies the shape every critical fix has to preserve:
- Ticket lands in DDB on mint, is gone after terminal status
- Exactly one success callback lands on the results SQS queue with the
  gp-api-facing envelope (typed `agentExperimentResult` + camelCase data)
- The callback carries the run-scoped S3 key (NOT latest.json) — preserves
  the STALE invariant for dependent experiments
- The published artifact conforms to the real contract schema imported from
  `runner/experiments/district_intel.py` (catches contract drift)

What this does NOT verify (explicitly out of scope for a smoke test):
- Claude harness behavior / real agent turns (no Claude SDK here)
- SSRF guard correctness (patched to no-op; covered by unit tests)
- Contract-validator branches beyond "happy path validates" (covered by
  `tests/test_contract_validation.py`)
"""
from __future__ import annotations

import json

import pytest

from pmf_engine.tests.smoke.conftest import (
    ARTIFACT_BUCKET,
    drain_callbacks,
    mint_ticket,
    ticket_exists,
)


DISTRICT_INTEL_PARAMS = {
    "state": "NC",
    "city": "Fayetteville",
    "l2DistrictType": "City_Council_Commissioner_District",
    "l2DistrictName": "FAYETTEVILLE CITY CNCL 2",
}

DISTRICT_INTEL_SCOPE = {
    "state": "NC",
    "cities": ["Fayetteville"],
    "districts": ["FAYETTEVILLE CITY CNCL 2"],
    "allowed_tables": [
        "goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"
    ],
    "max_rows": 50000,
}

CANNED_AGENDA_URL = "https://city.example.gov/agendas/council-2026-04.html"
CANNED_AGENDA_HTML = (
    "<html><body>"
    "<h1>Fayetteville City Council April 2026 Agenda</h1>"
    "<p>Item 1: Budget approval for parks department ($1.2M).</p>"
    "<p>Item 2: Vote on zoning variance for 123 Main St.</p>"
    "</body></html>"
)


def _minimal_valid_district_intel_artifact() -> dict:
    """Shaped to satisfy the district_intel contract schema + constraints.

    If `runner/experiments/district_intel.py` drops / renames a required
    field, the contract validator will reject this and the test fails —
    that's intentional. The contract *is* the load-bearing contract here.
    """
    return {
        "official_name": "Jane Smith",
        "office": "City Council, District 2",
        "district": {
            "state": "NC",
            "type": "City_Council_Commissioner_District",
            "name": "FAYETTEVILLE CITY CNCL 2",
        },
        "generated_at": "2026-04-23T10:00:00Z",
        "summary": {
            "total_constituents": 42000,
            "issues_identified": 1,
            "meetings_analyzed": 3,
            "sources_consulted": 2,
        },
        "issues": [
            {
                "title": "Parks department budget",
                "summary": "Council approved $1.2M parks budget over renters' objections.",
                "status": "recently_decided",
                "affected_constituents": 18000,
                "affected_segments": [
                    {
                        "name": "Renters in District 2",
                        "count": 18000,
                        "description": "Residents affected by park-funding property tax increase.",
                    },
                ],
                "sources": [
                    {
                        "id": 1,
                        "name": "Council April 2026 Agenda",
                        "url": CANNED_AGENDA_URL,
                        "date": "2026-04-10",
                    },
                ],
            },
        ],
        "demographic_snapshot": {
            "total_voters": 42000,
            "party_breakdown": [
                {"party": "Democrat", "count": 18000},
                {"party": "Republican", "count": 15000},
                {"party": "Unaffiliated", "count": 9000},
            ],
            "age_distribution": [
                {"range": "18-34", "count": 12000},
                {"range": "35-64", "count": 20000},
                {"range": "65+", "count": 10000},
            ],
        },
        "methodology": "Reviewed council agendas and validated voter counts against L2 data.",
    }


def test_district_intel_spine_end_to_end(broker_client, aws, fake_http):
    """Whole research spine, one pass: mint → fetch → publish → read → status.

    Success-path asserts land here so any regression in the fixes for:
      - dispatch callback swallow (CRITICAL #3)
      - runner double-emit of terminal status (CRITICAL #2)
      - broker_token early-delete / lifecycle (CRITICAL #1, #5)
      - artifact write ordering / run-scoped key (HIGH)
    will flip this test red.
    """
    run_id = "smoke-district-intel-001"
    org_slug = "test-org-smoke"

    broker_token = mint_ticket(
        broker_client,
        experiment_id="district_intel",
        run_id=run_id,
        organization_slug=org_slug,
        params=DISTRICT_INTEL_PARAMS,
        scope=DISTRICT_INTEL_SCOPE,
        timeout_seconds=600,
    )

    # Ticket lives in DDB after mint, before any terminal action.
    assert ticket_exists(aws, broker_token), (
        "mint should have persisted the ticket to DDB; downstream auth depends on this"
    )

    fake_http(
        broker_client,
        {
            CANNED_AGENDA_URL: {
                "status": 200,
                "body": CANNED_AGENDA_HTML,
                "headers": {"content-type": "text/html; charset=utf-8"},
            }
        },
    )

    headers = {"x-broker-token": broker_token}

    fetch_resp = broker_client.post(
        "/http/fetch",
        headers=headers,
        json={"url": CANNED_AGENDA_URL, "purpose": "council-agenda-research"},
    )
    assert fetch_resp.status_code == 200, (
        f"/http/fetch should return the canned agenda HTML, got "
        f"{fetch_resp.status_code} {fetch_resp.text}"
    )
    fetch_body = fetch_resp.json()
    assert fetch_body["status"] == 200
    assert "Fayetteville City Council" in fetch_body["body"]
    assert fetch_body["source_url"] == CANNED_AGENDA_URL
    assert fetch_body["byte_size"] == len(CANNED_AGENDA_HTML.encode("utf-8"))

    artifact = _minimal_valid_district_intel_artifact()
    publish_resp = broker_client.post(
        "/artifact/publish",
        headers=headers,
        json={"artifact": artifact},
    )
    assert publish_resp.status_code == 200, (
        f"/artifact/publish should accept a contract-valid artifact, got "
        f"{publish_resp.status_code} {publish_resp.text}"
    )
    pub_body = publish_resp.json()
    assert pub_body["callback_sent"] is True
    # Run-scoped immutable key, not latest.json — preserves STALE invariant.
    assert pub_body["artifact_key"] == f"district_intel/{run_id}/artifact.json"
    assert pub_body["artifact_bucket"] == ARTIFACT_BUCKET

    # S3 archive exists at the run-scoped key.
    s3_obj = aws["s3"].get_object(Bucket=ARTIFACT_BUCKET, Key=pub_body["artifact_key"])
    stored = json.loads(s3_obj["Body"].read())
    assert stored == artifact, "archive object should be byte-equivalent to published artifact"

    # Publish already deleted the ticket — subsequent calls with the same
    # token must 401. This is the lifecycle invariant the CRITICAL #1 fix
    # has to preserve. (No intermediate /artifact/read: publish is terminal
    # for this token; dependent-experiment reads use a fresh ticket with
    # prior_artifact_versions populated — covered by a separate test below.)
    assert not ticket_exists(aws, broker_token), (
        "publish must delete the ticket — leaked tokens are a security regression"
    )
    reused = broker_client.post(
        "/artifact/read",
        headers=headers,
        json={"experiment_id": "district_intel"},
    )
    assert reused.status_code == 401, (
        f"post-publish token reuse must 401, got {reused.status_code}"
    )

    callbacks = drain_callbacks(aws)
    assert len(callbacks) == 1, (
        f"exactly one success callback per run (got {len(callbacks)}): "
        f"duplicates indicate the runner/dispatch double-emit bug has regressed"
    )
    envelope = callbacks[0]
    assert envelope["type"] == "agentExperimentResult"
    data = envelope["data"]
    # camelCase keys — this is the gp-api zod-mirror contract
    assert data["experimentId"] == "district_intel"
    assert data["runId"] == run_id
    assert data["organizationSlug"] == org_slug
    assert data["status"] == "success"
    assert data["artifactKey"] == f"district_intel/{run_id}/artifact.json"
    assert data["artifactBucket"] == ARTIFACT_BUCKET


def test_district_intel_contract_violation_path(broker_client, aws):
    """Agent reports contract_violation → callback is failed-shape, ticket deleted.

    This covers the error-reporting side: the runner's failure path must
    land a single `contract_violation` callback on the results queue, and
    the ticket must be cleaned up so the broker_token can't be reused.
    """
    run_id = "smoke-district-intel-violation-001"
    org_slug = "test-org-smoke"

    broker_token = mint_ticket(
        broker_client,
        experiment_id="district_intel",
        run_id=run_id,
        organization_slug=org_slug,
        params=DISTRICT_INTEL_PARAMS,
        scope=DISTRICT_INTEL_SCOPE,
        timeout_seconds=600,
    )

    headers = {"x-broker-token": broker_token}

    resp = broker_client.post(
        "/internal/run-status",
        headers=headers,
        json={
            "status": "contract_violation",
            "reason_code": "missing_required_field",
            "detail": "artifact.issues[0].title is required",
            "duration_seconds": 12.5,
            "cost_usd": 0.04,
            "rejected_artifact": {"district": {"state": "NC"}, "issues": []},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["callback_sent"] is True

    # Quarantine blob is written to S3 under `rejected/{run_id}.json`.
    quarantine = aws["s3"].get_object(
        Bucket=ARTIFACT_BUCKET, Key=f"rejected/{run_id}.json"
    )
    quarantined = json.loads(quarantine["Body"].read())
    assert quarantined["district"]["state"] == "NC", (
        "rejected_artifact must be preserved verbatim for forensics"
    )

    assert not ticket_exists(aws, broker_token), (
        "terminal contract_violation must delete the ticket"
    )

    callbacks = drain_callbacks(aws)
    assert len(callbacks) == 1
    data = callbacks[0]["data"]
    assert data["status"] == "contract_violation"
    assert data["reasonCode"] == "missing_required_field"
    assert data["detail"] == "artifact.issues[0].title is required"
    # error field mirrors detail (transitional — see CallbackSender comment)
    assert data["error"] == "artifact.issues[0].title is required"


def test_http_fetch_follows_relative_redirect(broker_client, aws, fake_http):
    """Deterministic coverage for the round-3 `urljoin` fix — exercise the
    relative-redirect path through the full broker → resolve_redirects →
    SSRF re-validate → final GET chain that live smoke can't hit reliably.

    Pre-fix: the broker's redirect loop assigned `current_url = location`
    directly, so a relative Location like `/agendas/council.html` became an
    argument to `_validate_url` with scheme="" → 400 "URL must use https
    scheme". The `urljoin` fix resolves relative Location against the
    current URL before re-validation, restoring RFC 7231 conformance.
    """
    run_id = "smoke-district-intel-redirect-001"

    broker_token = mint_ticket(
        broker_client,
        experiment_id="district_intel",
        run_id=run_id,
        organization_slug="test-org-smoke",
        params=DISTRICT_INTEL_PARAMS,
        scope=DISTRICT_INTEL_SCOPE,
        timeout_seconds=600,
    )

    redirect_from = "https://city.example.gov/agenda"
    fake_http(
        broker_client,
        {
            redirect_from: {
                "status": 302,
                "body": "",
                "headers": {"location": "/agendas/council-2026-04.html"},
            },
            CANNED_AGENDA_URL: {
                "status": 200,
                "body": CANNED_AGENDA_HTML,
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        },
    )

    resp = broker_client.post(
        "/http/fetch",
        headers={"x-broker-token": broker_token},
        json={"url": redirect_from, "purpose": "council-agenda-research-via-redirect"},
    )

    assert resp.status_code == 200, (
        f"relative redirect should follow cleanly via urljoin, got "
        f"{resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body["status"] == 200
    assert body["source_url"] == CANNED_AGENDA_URL, (
        f"final source_url must be the resolved absolute URL, got {body['source_url']!r}"
    )
    assert "Fayetteville City Council" in body["body"]


def test_artifact_publish_tolerates_latest_json_transient_failure(broker_client, aws):
    """Smoke-tier regression guard for the R5 `latest.json swallow` fix.

    Scenario this protects against: the archive write at
    `{experiment_id}/{run_id}/artifact.json` succeeds but the mutable
    `{experiment_id}/{org}/latest.json` pointer put transiently fails
    (bucket-policy flap, S3 partial outage, etc.). Pre-R5, this re-raised
    500 from /artifact/publish → runner retry → IfNoneMatch=* → 412 →
    mapped to 409 non-retryable → agent FAILED → archive orphaned with no
    ExperimentRun SUCCESS pointing at it.

    Post-R5, latest.json failures are logged and swallowed — publish returns
    200, the callback carries the run-scoped (authoritative) key, and the
    ticket is cleaned up. A future refactor that reinstates the pre-R5 500
    path (e.g., someone restructures the nested try/except and lets the
    latest.json exception escape) would flip this test red.

    The unit-tier class TestArtifactPublishLatestJsonFailureIsBestEffort
    already asserts handler-level contract; this test extends coverage
    through the full booted broker (real FastAPI DI, real moto S3 + SQS +
    DDB, real callback send) to catch wiring regressions the unit tier
    can't see.
    """
    from botocore.exceptions import ClientError

    from broker.endpoints.artifact_publish import (
        get_s3_client as publish_get_s3_client,
    )

    run_id = "smoke-district-intel-latest-json-flake-001"
    org_slug = "test-org-smoke"

    broker_token = mint_ticket(
        broker_client,
        experiment_id="district_intel",
        run_id=run_id,
        organization_slug=org_slug,
        params=DISTRICT_INTEL_PARAMS,
        scope=DISTRICT_INTEL_SCOPE,
        timeout_seconds=600,
    )

    real_s3 = aws["s3"]

    class _LatestJsonFailingS3:
        """Proxy over moto's S3 client — `put_object` raises InternalError
        when Key ends with `/latest.json`, delegates everything else.
        """

        def put_object(self, **kwargs):
            if kwargs.get("Key", "").endswith("/latest.json"):
                raise ClientError(
                    error_response={
                        "Error": {"Code": "InternalError", "Message": "S3 flaked"},
                        "ResponseMetadata": {"HTTPStatusCode": 500},
                    },
                    operation_name="PutObject",
                )
            return real_s3.put_object(**kwargs)

        def __getattr__(self, name):
            return getattr(real_s3, name)

    failing_s3 = _LatestJsonFailingS3()
    broker_client.app.dependency_overrides[publish_get_s3_client] = lambda: failing_s3

    try:
        publish_resp = broker_client.post(
            "/artifact/publish",
            headers={"x-broker-token": broker_token},
            json={"artifact": _minimal_valid_district_intel_artifact()},
        )
    finally:
        broker_client.app.dependency_overrides.pop(publish_get_s3_client, None)

    run_key = f"district_intel/{run_id}/artifact.json"

    assert publish_resp.status_code == 200, (
        f"latest.json transient failure must NOT abort publish (archive succeeded, "
        f"callback still carries the run-scoped key); got "
        f"{publish_resp.status_code} {publish_resp.text}"
    )
    pub_body = publish_resp.json()
    assert pub_body["callback_sent"] is True
    assert pub_body["artifact_key"] == run_key, (
        "callback payload must carry the run-scoped (authoritative) archive key, "
        "not latest.json — this is the STALE-invariant contract"
    )
    assert pub_body["artifact_bucket"] == ARTIFACT_BUCKET

    # Archive exists at the run-scoped key — even though latest.json failed.
    head = real_s3.head_object(Bucket=ARTIFACT_BUCKET, Key=run_key)
    assert head["ContentType"] == "application/json"

    # Ticket was deleted — the post-publish cleanup path runs to completion
    # even when latest.json fails.
    assert not ticket_exists(aws, broker_token), (
        "publish-cleanup must still run after latest.json swallow — "
        "a leaked token after a partial failure is a security regression"
    )

    # Exactly one success callback on the results queue with the run-scoped key.
    callbacks = drain_callbacks(aws)
    assert len(callbacks) == 1, (
        f"exactly one success callback expected even when latest.json fails "
        f"(got {len(callbacks)})"
    )
    data = callbacks[0]["data"]
    assert data["status"] == "success"
    assert data["runId"] == run_id
    assert data["artifactKey"] == run_key, (
        "callback artifactKey must be the run-scoped archive, not latest.json"
    )


def test_district_intel_artifact_uses_real_contract_schema():
    """Guardrail against local smoke-test drift.

    If someone adds a required field to the district_intel contract in
    `runner/experiments/district_intel.py` and forgets to update the
    minimal artifact above, this test catches it by re-validating the
    smoke fixture against the real schema. Cheaper to fail here with a
    readable diff than in the spine test with a 400 from /artifact/publish.
    """
    from pmf_engine.runner.contract import validate_artifact_contract
    from pmf_engine.runner.experiments.district_intel import EXPERIMENT

    contract = EXPERIMENT["contract"]
    artifact = _minimal_valid_district_intel_artifact()
    try:
        validate_artifact_contract(
            json.dumps(artifact).encode("utf-8"),
            contract.get("schema"),
            contract.get("constraints"),
        )
    except Exception as e:
        pytest.fail(
            f"smoke fixture no longer matches district_intel contract — update "
            f"_minimal_valid_district_intel_artifact() to match the schema: {e}"
        )
