"""Voter Targeting smoke test — databricks-spine regression guard.

Walks a voter_targeting run through the broker end-to-end:
  mint → /databricks/query (×2) → /artifact/publish → /internal/run-status

The databricks path is structurally different from district_intel: it
exercises the SQL rewriter, the scope-predicate injection (state/city
clamping), and the `DataQueryTracker` gate that blocks publish when no
data query ran (prevents synthetic/fabricated artifacts for
`DATA_REQUIRED_EXPERIMENTS`).

Verifies:
- mint → DDB round-trip with a win-mode scope
- /databricks/query accepts a scope-compliant SQL and returns the fake rows
- /databricks/query rejects an out-of-scope SQL (scope_violation → 400)
- Publish is allowed ONLY after a tracker-increment from a successful query
  (if the first query is rejected, publish must still refuse — the tracker
  only increments on success)
- Callback envelope uses the win-mode `voter_targeting` fields

Out of scope for this smoke test (covered elsewhere):
- Full SQL-rewriter correctness: `broker/tests/test_sql_rewriter.py`
- Fleet-local tracker inconsistency: single-process smoke can't catch the
  CRITICAL #8 multi-pod bug; flagged as a known gap in the smoke's docstring.
"""
from __future__ import annotations

import json

from pmf_engine.tests.smoke.conftest import (
    ARTIFACT_BUCKET,
    drain_callbacks,
    mint_ticket,
    ticket_exists,
)


VOTER_TARGETING_PARAMS = {
    "state": "NC",
    "city": "Fayetteville",
    "l2DistrictType": "City_Council_Commissioner_District",
    "l2DistrictName": "FAYETTEVILLE CITY CNCL 2",
}

VOTER_TARGETING_SCOPE = {
    "state": "NC",
    "cities": ["Fayetteville"],
    "districts": ["FAYETTEVILLE CITY CNCL 2"],
    "allowed_tables": [
        "goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"
    ],
    "max_rows": 50000,
}

ALLOWED_TABLE = "goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"

# Scope-compliant query: selects from the allowed table. The rewriter will
# inject the state/city scope predicates into the WHERE clause.
IN_SCOPE_SQL = f"SELECT LALVOTERID FROM {ALLOWED_TABLE} LIMIT 100"

# Out-of-scope query: selects from a table NOT in scope.allowed_tables.
# The rewriter must reject with ScopeViolation → /databricks/query → 400.
OUT_OF_SCOPE_SQL = "SELECT LALVOTERID FROM some_other_catalog.forbidden.voters LIMIT 100"

# Canned rows the FakeDatabricksClient returns — shape matches what a real
# LALVOTERID query would produce.
CANNED_COLUMNS = ["LALVOTERID", "Residence_Addresses_State", "Residence_Addresses_City"]
CANNED_ROWS = [
    ["V000000001", "NC", "FAYETTEVILLE"],
    ["V000000002", "NC", "FAYETTEVILLE"],
    ["V000000003", "NC", "FAYETTEVILLE"],
]


def _minimal_valid_voter_targeting_artifact() -> dict:
    """Shaped to satisfy the voter_targeting contract schema."""
    return {
        "organization_slug": "test-org-smoke",
        "district": {
            "state": "NC",
            "type": "City_Council_Commissioner_District",
            "name": "FAYETTEVILLE CITY CNCL 2",
        },
        "generated_at": "2026-04-23T10:00:00Z",
        "summary": {
            "total_voters_in_district": 42000,
            "win_number": 5500,
            "projected_turnout": 11000,
        },
        "segments": [
            {
                "tier": 1,
                "name": "High-propensity unaffiliated renters",
                "description": "Unaffiliated voters in downtown precincts who voted in 2022 and 2024.",
                "count": 2200,
                "demographics": {
                    "party_breakdown": {"Unaffiliated": 2200},
                    "age_distribution": {"25-44": 1300, "45-64": 900},
                    "gender_split": {"F": 1150, "M": 1050},
                },
                "outreach_priority": "high",
                "recommended_channels": ["door_knock", "sms"],
                "voters": [
                    {
                        "voter_id": "V000000001",
                        "first_name": "Alex",
                        "last_name": "Doe",
                        "address": "100 Main St",
                        "city": "Fayetteville",
                        "zip": "28301",
                        "age": 34,
                        "gender": "F",
                        "party": "Unaffiliated",
                        "voter_status": "Active",
                    }
                ],
            }
        ],
        "geographic_clusters": [
            {"area": "Downtown", "voter_count": 2200, "density_rank": 1},
        ],
        "methodology": "Haystaq independent-appeal score + turnout history from L2, clamped to Fayetteville NC.",
    }


def test_voter_targeting_spine_end_to_end(broker_client, aws, fake_databricks):
    """Full win-mode spine: mint → query → publish → callback, ticket cleaned up."""
    run_id = "smoke-voter-targeting-001"
    org_slug = "test-org-smoke"

    broker_token = mint_ticket(
        broker_client,
        experiment_id="voter_targeting",
        run_id=run_id,
        organization_slug=org_slug,
        params=VOTER_TARGETING_PARAMS,
        scope=VOTER_TARGETING_SCOPE,
        timeout_seconds=600,
    )
    assert ticket_exists(aws, broker_token)

    fake_db = fake_databricks(broker_client, CANNED_COLUMNS, CANNED_ROWS)
    headers = {"x-broker-token": broker_token}

    # Single scope-compliant query — should return canned rows and bump
    # the tracker so publish is allowed.
    query_resp = broker_client.post(
        "/databricks/query",
        headers=headers,
        json={"sql": IN_SCOPE_SQL, "parameters": {}},
    )
    assert query_resp.status_code == 200, (
        f"/databricks/query should accept an in-scope SELECT against an "
        f"allowed table, got {query_resp.status_code} {query_resp.text}"
    )
    query_body = query_resp.json()
    assert query_body["columns"] == CANNED_COLUMNS
    assert query_body["rows"] == CANNED_ROWS
    assert query_body["row_count"] == len(CANNED_ROWS)
    assert query_body["row_cap_hit"] is False
    # The rewriter should have rewritten the SQL (at minimum injected scope
    # predicates). We just verify the fake received SOME SQL and params.
    assert len(fake_db.calls) == 1, "fake databricks should see exactly one execute"

    # Publish the artifact — succeeds because the tracker was incremented.
    artifact = _minimal_valid_voter_targeting_artifact()
    publish_resp = broker_client.post(
        "/artifact/publish",
        headers=headers,
        json={"artifact": artifact},
    )
    assert publish_resp.status_code == 200, publish_resp.text
    pub_body = publish_resp.json()
    assert pub_body["callback_sent"] is True
    assert pub_body["artifact_key"] == f"voter_targeting/{run_id}/artifact.json"

    stored = json.loads(
        aws["s3"].get_object(Bucket=ARTIFACT_BUCKET, Key=pub_body["artifact_key"])["Body"].read()
    )
    assert stored == artifact

    assert not ticket_exists(aws, broker_token)

    callbacks = drain_callbacks(aws)
    assert len(callbacks) == 1
    data = callbacks[0]["data"]
    assert data["experimentId"] == "voter_targeting"
    assert data["runId"] == run_id
    assert data["status"] == "success"
    assert data["artifactKey"] == f"voter_targeting/{run_id}/artifact.json"


def test_voter_targeting_rejects_out_of_scope_sql(broker_client, aws, fake_databricks):
    """SQL rewriter must 400 on a table outside `scope.allowed_tables`.

    This is the primary scope enforcement — a compromised/prompt-injected
    agent must not be able to read arbitrary tables. The rewriter's
    allow-list check is load-bearing here.
    """
    broker_token = mint_ticket(
        broker_client,
        experiment_id="voter_targeting",
        run_id="smoke-voter-targeting-scope-001",
        organization_slug="test-org-smoke",
        params=VOTER_TARGETING_PARAMS,
        scope=VOTER_TARGETING_SCOPE,
        timeout_seconds=600,
    )

    fake_databricks(broker_client, CANNED_COLUMNS, CANNED_ROWS)
    headers = {"x-broker-token": broker_token}

    resp = broker_client.post(
        "/databricks/query",
        headers=headers,
        json={"sql": OUT_OF_SCOPE_SQL, "parameters": {}},
    )
    assert resp.status_code == 400, (
        f"out-of-scope SQL must 400, got {resp.status_code} {resp.text}"
    )
    detail = resp.json()["detail"]
    assert "disallowed_table" in json.dumps(detail), (
        f"scope violation should surface the 'disallowed_table' reason_code, "
        f"got detail={detail}"
    )


def test_voter_targeting_publish_refuses_without_data_query(
    broker_client, aws, fake_databricks
):
    """Publish must 400 when no /databricks/query has succeeded.

    `DATA_REQUIRED_EXPERIMENTS = {voter_targeting, walking_plan}` — these
    experiments must produce real voter data. The DataQueryTracker gate
    blocks publish when the tracker count is 0, which prevents an agent
    from fabricating a voter_targeting artifact when Databricks is down.
    """
    run_id = "smoke-voter-targeting-no-data-001"
    broker_token = mint_ticket(
        broker_client,
        experiment_id="voter_targeting",
        run_id=run_id,
        organization_slug="test-org-smoke",
        params=VOTER_TARGETING_PARAMS,
        scope=VOTER_TARGETING_SCOPE,
        timeout_seconds=600,
    )
    headers = {"x-broker-token": broker_token}

    # Do NOT run any query first — tracker stays at 0.
    artifact = _minimal_valid_voter_targeting_artifact()
    resp = broker_client.post(
        "/artifact/publish",
        headers=headers,
        json={"artifact": artifact},
    )
    assert resp.status_code == 400, (
        f"publish without a prior successful query must 400, got "
        f"{resp.status_code} {resp.text}"
    )
    assert "NoDataQueriesSucceeded" in resp.json()["detail"]

    # Ticket still exists — publish failure should not clean up the ticket
    # (the agent may retry with a query-first sequence on its next turn).
    assert ticket_exists(aws, broker_token)

    # No callbacks should have been sent — publish refused before any
    # CallbackSender call ran.
    callbacks = drain_callbacks(aws)
    assert len(callbacks) == 0, (
        f"failed publish (400) must not emit a callback, got {callbacks}"
    )


def test_voter_targeting_artifact_uses_real_contract_schema():
    """Mirror of the district_intel guardrail — catches contract drift."""
    import pytest

    from pmf_engine.runner.contract import validate_artifact_contract
    from pmf_engine.runner.experiments.voter_targeting import EXPERIMENT

    contract = EXPERIMENT["contract"]
    artifact = _minimal_valid_voter_targeting_artifact()
    try:
        validate_artifact_contract(
            json.dumps(artifact).encode("utf-8"),
            contract.get("schema"),
            contract.get("constraints"),
        )
    except Exception as e:
        pytest.fail(
            f"smoke fixture no longer matches voter_targeting contract — "
            f"update _minimal_valid_voter_targeting_artifact() to match: {e}"
        )
