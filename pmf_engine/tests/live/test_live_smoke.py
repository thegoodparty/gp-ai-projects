"""Live-dev smoke: dispatch PMF experiments directly to SQS (downstream of
gp-api), poll S3 for the artifact, assert it lands.

The handoff boundary is the dispatch SQS queue. gp-api's
`AgentDispatchService.dispatch()` creates an ExperimentRun row in its DB
and then sends a `{experiment_id, organization_slug, run_id, params}`
message to `agent-dispatch-{env}.fifo`. That's everything gp-api does —
the rest of the pipeline is ours. This test reproduces that SQS message
verbatim (same body shape, same MessageGroupId, same dedup style) and
then asserts on the PMF stack's output directly.

**Success signal = S3 artifact at `{experiment_id}/{run_id}/artifact.json`**.
Agent + broker succeeding → artifact lands there deterministically, which
proves the entire spine we actually own:
  dispatch Lambda → mint + Fargate RunTask → broker auth →
  Claude SDK run → broker contract-validates → S3 upload (archive+latest)

What this catches (that the moto smoke can't):
- IAM drift across every hop (Lambda RunTask, Fargate → broker, broker →
  Databricks/S3/Secrets Manager, etc.)
- Security groups, VPC, egress, DNS
- Terraform env-var drift (new code expects a var the task def doesn't set)
- Real Anthropic streaming via the broker proxy
- Real Claude Agent SDK harness vs. the broker contract
- Real Databricks SQL + scope rewriter against live tables

What it does NOT catch (on purpose — different layer, different owner):
- gp-api's REST validation / autoParams logic (we're downstream of that)
- gp-api's queue consumer / DB row update (separate test, separate fix)

**Harmless side effect:** the broker's success callback lands on
`agent-results-dev.fifo`. gp-api's consumer reads it, finds no matching
`ExperimentRun` row for our synthetic run_id (we skipped gp-api), logs
"Experiment run not found for callback" once, and ACKs the message.
Verified in `gp-api/src/queue/consumer/queueConsumer.service.ts:859-862`.
One log line per smoke run, no DLQ poisoning.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError

DEFAULT_REGION = "us-west-2"
DEFAULT_ACCOUNT = "333022194791"
DEFAULT_ENV = "dev"
DEFAULT_ARTIFACT_BUCKET_FMT = "gp-agent-artifacts-{env}"
DEFAULT_DISPATCH_QUEUE_FMT = "agent-dispatch-{env}.fifo"
DEFAULT_ORG_SLUG = "smoke-test-pmf"

DEFAULT_TIMEOUT_MINUTES = 30
DEFAULT_POLL_SECONDS = 15

# Known-working district per pmf_engine/pilot data (Fayetteville NC is where
# the stephon-ferguson pilot lived). Satisfies required_params for both
# district_intel and voter_targeting (state, city, l2DistrictType, l2DistrictName).
DEFAULT_SMOKE_PARAMS = {
    "state": "NC",
    "city": "Fayetteville",
    "l2DistrictType": "City_Council_Commissioner_District",
    "l2DistrictName": "FAYETTEVILLE CITY CNCL 2",
    # Extra params gp-api would auto-populate; harmless if unused by the
    # experiment instruction.
    "districtType": "City Council",
    "districtName": "District 2",
    "office": "City Council, District 2",
    "officialName": "Smoke Test Official",
    "topIssues": ["housing", "transit"],
}


def _config() -> dict[str, Any]:
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    env = os.environ.get("LIVE_SMOKE_ENV", DEFAULT_ENV)
    account = os.environ.get("LIVE_SMOKE_ACCOUNT", DEFAULT_ACCOUNT)
    default_queue_url = (
        f"https://sqs.{region}.amazonaws.com/{account}/"
        + DEFAULT_DISPATCH_QUEUE_FMT.format(env=env)
    )
    return {
        "region": region,
        "env": env,
        "bucket": os.environ.get(
            "LIVE_SMOKE_ARTIFACT_BUCKET",
            DEFAULT_ARTIFACT_BUCKET_FMT.format(env=env),
        ),
        "dispatch_queue_url": os.environ.get(
            "LIVE_SMOKE_DISPATCH_QUEUE_URL", default_queue_url
        ),
        "org_slug": os.environ.get("LIVE_SMOKE_ORG_SLUG", DEFAULT_ORG_SLUG),
        "timeout_minutes": int(
            os.environ.get("LIVE_SMOKE_TIMEOUT_MINUTES", str(DEFAULT_TIMEOUT_MINUTES))
        ),
        "poll_seconds": int(
            os.environ.get("LIVE_SMOKE_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))
        ),
    }


def _aws_client(service: str, region: str):
    try:
        return boto3.client(service, region_name=region)
    except NoCredentialsError:
        pytest.skip(
            f"AWS credentials not available for {service}. "
            f"Set AWS_PROFILE=work (or export AWS_ACCESS_KEY_ID/SECRET) before "
            f"running live smoke."
        )


def _build_dispatch_message(
    experiment_id: str, organization_slug: str, run_id: str, params: dict
) -> dict[str, Any]:
    """Exact shape gp-api's AgentDispatchService puts on the dispatch queue.

    Matches `gp-api/src/agentExperiments/services/agentDispatch.service.ts:56-61`.
    If that producer changes, dispatch_handler.parse_dispatch_message will
    reject our message with the same error it would give gp-api — which is
    exactly the regression we want this smoke to catch.
    """
    return {
        "experiment_id": experiment_id,
        "organization_slug": organization_slug,
        "run_id": run_id,
        "params": params,
    }


def _send_to_dispatch_queue(
    sqs, queue_url: str, body: dict[str, Any], organization_slug: str
) -> str:
    """Send to agent-dispatch-{env}.fifo with gp-api's FIFO metadata shape.

    MessageGroupId = `agent-dispatch-{organizationSlug}` per gp-api's
    `agentDispatch.service.ts:70`. MessageDeduplicationId is a fresh UUID,
    also per gp-api (line 63). Returns SQS's assigned MessageId for logging.
    """
    dedup_id = str(uuid.uuid4())
    resp = sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(body),
        MessageGroupId=f"agent-dispatch-{organization_slug}",
        MessageDeduplicationId=dedup_id,
    )
    return resp.get("MessageId", "")


def _poll_s3_for_artifact(
    s3, bucket: str, key: str, timeout_minutes: int, poll_seconds: int
) -> None:
    """Poll head_object until the artifact lands.

    head_object is cheap (no body transfer) — just needs s3:GetObject on
    the key. Returns on 200, ignores 404/NoSuchKey/NotFound, fails loudly
    on any other S3 error (IAM regression, etc.) rather than quietly
    burning the whole timeout.
    """
    deadline = time.monotonic() + timeout_minutes * 60
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            s3.head_object(Bucket=bucket, Key=key)
            elapsed = int(timeout_minutes * 60 - (deadline - time.monotonic()))
            print(f"  [s3] artifact landed after {attempts} polls (t+{elapsed}s)")
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in ("404", "NoSuchKey", "NotFound"):
                pytest.fail(
                    f"unexpected S3 error polling s3://{bucket}/{key}: {code} {e}"
                )
        if attempts % 4 == 0:
            elapsed = int(timeout_minutes * 60 - (deadline - time.monotonic()))
            print(f"  [s3] still waiting for s3://{bucket}/{key} (t+{elapsed}s)")
        time.sleep(poll_seconds)

    run_id = key.split("/")[1] if "/" in key else key
    pytest.fail(
        f"artifact did not land at s3://{bucket}/{key} in {timeout_minutes}min. "
        f"The pipeline between dispatch SQS and S3 upload is broken somewhere. "
        f"Diagnostics (pick whichever hop you suspect):\n"
        f"  - dispatch Lambda: aws logs tail /aws/lambda/pmf-engine-dispatch-dev --since 30m | grep {run_id}\n"
        f"  - Fargate agent:   aws logs tail /aws/ecs/pmf-engine-dev --since 30m | grep {run_id}\n"
        f"  - broker:          aws logs tail /aws/ecs/pmf-broker-dev --since 30m | grep {run_id}"
    )


def _read_s3_artifact(s3, bucket: str, key: str) -> dict:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:
        pytest.fail(
            f"artifact at s3://{bucket}/{key} is unreadable or not valid JSON: {e}"
        )


def _run_live_experiment(experiment_id: str):
    """Build → send to dispatch SQS → poll S3 → assert artifact is valid JSON.

    Shared implementation for both experiments. The only difference is the
    experiment_id — both use the same params fixture (required_params
    overlap 100%) and both produce an artifact at the same key pattern.
    """
    cfg = _config()
    sqs = _aws_client("sqs", cfg["region"])
    s3 = _aws_client("s3", cfg["region"])

    run_id = str(uuid.uuid4())
    artifact_key = f"{experiment_id}/{run_id}/artifact.json"

    body = _build_dispatch_message(
        experiment_id=experiment_id,
        organization_slug=cfg["org_slug"],
        run_id=run_id,
        params=DEFAULT_SMOKE_PARAMS,
    )

    print(
        f"\n  [live smoke] experiment={experiment_id} env={cfg['env']} "
        f"org={cfg['org_slug']}"
    )
    print(f"  [live smoke] run_id={run_id}")
    print(f"  [live smoke] dispatch queue: {cfg['dispatch_queue_url']}")
    print(f"  [live smoke] target S3 key:  s3://{cfg['bucket']}/{artifact_key}")

    msg_id = _send_to_dispatch_queue(
        sqs, cfg["dispatch_queue_url"], body, cfg["org_slug"]
    )
    print(f"  [live smoke] SQS message sent — MessageId={msg_id}")

    _poll_s3_for_artifact(
        s3, cfg["bucket"], artifact_key, cfg["timeout_minutes"], cfg["poll_seconds"]
    )

    artifact = _read_s3_artifact(s3, cfg["bucket"], artifact_key)
    assert isinstance(artifact, dict) and artifact, (
        f"S3 artifact for run {run_id} is empty or not a JSON object: {artifact!r}"
    )
    print(
        f"  [live smoke] artifact OK — top-level keys: "
        f"{sorted(artifact.keys())[:8]}"
    )


@pytest.mark.live_dev
def test_district_intel_live_smoke():
    """Real serve-mode dispatch via SQS — catches serve-side regressions."""
    _run_live_experiment("district_intel")


@pytest.mark.live_dev
def test_voter_targeting_live_smoke():
    """Real win-mode dispatch via SQS — exercises the full Databricks spine
    (SQL rewriter, scope clamp, data_query_tracker, broker → Databricks)."""
    _run_live_experiment("voter_targeting")
