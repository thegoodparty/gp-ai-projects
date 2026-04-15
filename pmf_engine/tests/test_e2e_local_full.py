"""Full-stack local end-to-end test for PMF engine.

Marker: `e2e` (default-skipped). Run with:

    cd ~/work/gp-ai-projects
    AWS_PROFILE=work uv run pytest pmf_engine/tests/test_e2e_local_full.py -m e2e -v

Prerequisites:
- Local gp-api running (npm run start:dev) and consuming from Collin_Queue.fifo
- AWS credentials (AWS_PROFILE=work)
- Databricks creds in gp-ai-projects/.env (only needed for voter_targeting/walking_plan)

What it does:
    1. Ensures S3 bucket and dispatch queue exist
    2. Dispatches experiment via local gp-api
    3. Reads the dispatch message from the queue
    4. Runs the runner locally (Claude agent executes the experiment)
    5. Runner uploads artifact to S3 and sends callback to Collin_Queue.fifo
    6. Local gp-api picks up the callback and updates ExperimentRun
    7. Polls gp-api to verify the run reached a terminal state

Cleanup:
    The test deletes S3 objects it created. The gp-api ExperimentRun row is
    best-effort cleaned via DELETE /agent-experiments/{runId} if available;
    otherwise the test logs a warning — rows in dev are expected to be
    transient anyway.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError

from shared.logger import get_logger

logger = get_logger("e2e_local")

REGION = "us-west-2"
ACCOUNT_ID = "333022194791"
ARTIFACT_BUCKET = "gp-agent-artifacts-dev"
DISPATCH_QUEUE_NAME = "agent-dispatch-dev.fifo"
CALLBACK_QUEUE_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/Collin_Queue.fifo"
GP_API_BASE = "http://localhost:3000/v1"

EXPERIMENT_ID = os.environ.get("E2E_EXPERIMENT", "hello_world")
_CANDIDATE_ID_OVERRIDE = os.environ.get("E2E_CANDIDATE_ID", "")
PARAMS = json.loads(os.environ.get("E2E_PARAMS", "{}"))


def get_admin_campaign_id(token: str) -> str:
    if _CANDIDATE_ID_OVERRIDE:
        return _CANDIDATE_ID_OVERRIDE
    resp = httpx.get(
        f"{GP_API_BASE}/campaigns/mine",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"GET /campaigns/mine failed ({resp.status_code}): {resp.text}")
    campaign_id = str(resp.json().get("id") or "")
    if not campaign_id:
        raise RuntimeError("Admin's /campaigns/mine response has no id")
    logger.info(f"Using admin campaign.id={campaign_id} as CANDIDATE_ID")
    return campaign_id


def get_admin_token() -> str:
    token = os.environ.get("GP_API_ADMIN_TOKEN", "")
    if token:
        return token

    logger.info("No GP_API_ADMIN_TOKEN set, logging in as seed admin...")
    resp = httpx.post(
        f"{GP_API_BASE}/authentication/login",
        json={"email": "admin@test.local", "password": "testPassword123"},
        timeout=10,
    )
    if resp.status_code != 201:
        raise RuntimeError(f"Login failed ({resp.status_code}): {resp.text}")
    return resp.json()["token"]


def ensure_s3_bucket(s3_client) -> None:
    try:
        s3_client.head_bucket(Bucket=ARTIFACT_BUCKET)
    except ClientError:
        s3_client.create_bucket(
            Bucket=ARTIFACT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )


def ensure_dispatch_queue(sqs_client) -> str:
    try:
        return sqs_client.get_queue_url(QueueName=DISPATCH_QUEUE_NAME)["QueueUrl"]
    except ClientError:
        resp = sqs_client.create_queue(
            QueueName=DISPATCH_QUEUE_NAME,
            Attributes={
                "FifoQueue": "true",
                "ContentBasedDeduplication": "false",
                "DeduplicationScope": "messageGroup",
                "FifoThroughputLimit": "perMessageGroupId",
                "VisibilityTimeout": "120",
                "MessageRetentionPeriod": "604800",
            },
        )
        return resp["QueueUrl"]


def dispatch_experiment(token: str, candidate_id: str) -> dict:
    logger.info(f"Dispatching {EXPERIMENT_ID} for candidate {candidate_id}")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = httpx.post(
        f"{GP_API_BASE}/agent-experiments/dispatch",
        json={
            "experimentId": EXPERIMENT_ID,
            "candidateId": candidate_id,
            "params": PARAMS,
        },
        headers=headers,
        timeout=10,
    )

    if resp.status_code != 201:
        raise RuntimeError(f"Dispatch failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    logger.info(f"Dispatched: runId={data['runId']} status={data['status']}")
    return data


def read_dispatch_message(sqs_client, queue_url: str) -> dict:
    logger.info("Reading dispatch message from queue...")
    for _ in range(10):
        resp = sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=5,
            AttributeNames=["All"],
        )
        messages = resp.get("Messages", [])
        if messages:
            msg = messages[0]
            body = json.loads(msg["Body"])
            sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
            return body
    raise RuntimeError("No dispatch message received after 50s")


async def run_runner(dispatch_msg: dict) -> None:
    from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY
    from pmf_engine.runner.config import RunnerConfig
    from pmf_engine.runner.main import run_experiment

    experiment = EXPERIMENT_REGISTRY.get(dispatch_msg["experiment_id"])
    if not experiment:
        raise RuntimeError(f"Unknown experiment: {dispatch_msg['experiment_id']}")

    config = RunnerConfig(
        experiment_id=dispatch_msg["experiment_id"],
        run_id=dispatch_msg["run_id"],
        candidate_id=dispatch_msg["candidate_id"],
        instruction=experiment["instruction"],
        params=dispatch_msg.get("params", {}),
        harness=experiment["harness"],
        model=experiment["model"],
        environment="dev",
        artifact_bucket=ARTIFACT_BUCKET,
        artifact_key_template=experiment["contract"]["s3_key_template"],
        callback_queue_url=CALLBACK_QUEUE_URL,
    )

    os.environ["CANDIDATE_ID"] = config.candidate_id
    os.environ["PARAMS_JSON"] = json.dumps(config.params)

    workspace_dir = "/tmp/pmf-e2e-workspace"
    os.makedirs(os.path.join(workspace_dir, "output"), exist_ok=True)
    os.environ["WORKSPACE_DIR"] = workspace_dir

    await run_experiment(config)


def verify_artifact(s3_client, dispatch_msg: dict) -> str:
    from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY

    experiment = EXPERIMENT_REGISTRY[dispatch_msg["experiment_id"]]
    key = experiment["contract"]["s3_key_template"].format(
        experiment_id=dispatch_msg["experiment_id"],
        run_id=dispatch_msg["run_id"],
    )

    resp = s3_client.head_object(Bucket=ARTIFACT_BUCKET, Key=key)
    logger.info(f"Artifact verified: s3://{ARTIFACT_BUCKET}/{key} ({resp['ContentLength']} bytes)")
    return key


_TERMINAL_SUCCESS = {"SUCCESS"}
_TERMINAL_FAILURE = {"FAILED", "CONTRACT_VIOLATION"}


def wait_for_run_completion(
    token: str,
    run_id: str,
    timeout: float = 30,
    poll_interval: float = 2.0,
) -> dict:
    logger.info(f"Polling gp-api for runId={run_id} (timeout={timeout}s)...")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.monotonic() + timeout
    last_status: str | None = None

    with httpx.Client(timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{GP_API_BASE}/agent-experiments/mine", headers=headers)
            except httpx.HTTPError as e:
                logger.warning(f"Poll request failed: {e}; retrying...")
                time.sleep(poll_interval)
                continue

            if resp.status_code != 200:
                time.sleep(poll_interval)
                continue

            runs = resp.json() or []
            run = next((r for r in runs if r.get("runId") == run_id), None)
            if run is None:
                time.sleep(poll_interval)
                continue

            status = run.get("status")
            if status != last_status:
                logger.info(f"Run {run_id} status: {status}")
                last_status = status

            if status in _TERMINAL_SUCCESS:
                return run
            if status in _TERMINAL_FAILURE:
                error = run.get("error") or "(no error field)"
                raise RuntimeError(f"Run {run_id} reached terminal {status}: {error}")

            time.sleep(poll_interval)

    raise TimeoutError(
        f"Run {run_id} did not reach terminal status within {timeout}s "
        f"(last status: {last_status or 'not seen'})"
    )


def _cleanup_s3_objects(s3_client, keys: list[str]) -> None:
    for key in keys:
        try:
            s3_client.delete_object(Bucket=ARTIFACT_BUCKET, Key=key)
            logger.info(f"Cleaned S3 object: s3://{ARTIFACT_BUCKET}/{key}")
        except ClientError as e:
            logger.warning(f"Cleanup failed for s3://{ARTIFACT_BUCKET}/{key}: {e}")


def _cleanup_experiment_run(token: str, run_id: str) -> None:
    """Best-effort cleanup of the gp-api ExperimentRun row. If no DELETE
    endpoint exists, log and move on — dev DB rows are transient."""
    try:
        resp = httpx.delete(
            f"{GP_API_BASE}/agent-experiments/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            logger.info(f"Deleted ExperimentRun {run_id}")
        else:
            logger.warning(
                f"DELETE /agent-experiments/{run_id} returned {resp.status_code}: "
                f"{resp.text[:200]} (dev DB row left in place)"
            )
    except Exception as e:
        logger.warning(f"ExperimentRun cleanup failed for {run_id}: {e}")


@pytest.mark.e2e
def test_pmf_engine_full_stack_local():
    s3 = boto3.client("s3", region_name=REGION)
    sqs = boto3.client("sqs", region_name=REGION)

    ensure_s3_bucket(s3)
    dispatch_queue_url = ensure_dispatch_queue(sqs)

    token = get_admin_token()
    candidate_id = get_admin_campaign_id(token)

    dispatch_result = dispatch_experiment(token, candidate_id)
    run_id = dispatch_result["runId"]

    created_keys: list[str] = []
    try:
        dispatch_msg = read_dispatch_message(sqs, dispatch_queue_url)
        assert dispatch_msg["run_id"] == run_id

        asyncio.run(run_runner(dispatch_msg))

        artifact_key = verify_artifact(s3, dispatch_msg)
        created_keys.append(artifact_key)
        created_keys.append(f"{dispatch_msg['experiment_id']}/latest.json")

        wait_for_run_completion(
            token=token,
            run_id=run_id,
            timeout=float(os.environ.get("E2E_POLL_TIMEOUT", "90")),
        )

        logger.info(f"E2E PASSED: experiment={EXPERIMENT_ID} run={run_id}")
    finally:
        _cleanup_s3_objects(s3, created_keys)
        _cleanup_experiment_run(token, run_id)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-m", "e2e", "-v"]))
