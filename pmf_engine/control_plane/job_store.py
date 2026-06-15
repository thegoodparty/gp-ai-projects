from __future__ import annotations

import json
import time

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

QUEUED = "QUEUED"
LAUNCHING = "LAUNCHING"
DISPATCHED = "DISPATCHED"
FAILED = "FAILED"

_GSI_NAME = "queue-index"
_PRIORITY_RANK = {"HIGH": 0, "DEFAULT": 1}
_DISPATCHED_TTL_SECONDS = 24 * 3600
_STUCK_SCAN_LIMIT = 200


class JobClaimConflict(Exception):
    """Raised when a conditional claim loses the race (already not QUEUED)."""


class QueuedJob(BaseModel):
    run_id: str
    experiment_type: str
    organization_slug: str
    clerk_user_id: str | None
    priority: str
    params: dict
    routing: dict
    prior_artifact_versions: dict[str, str] | None
    created_at_ms: int
    attempts: int = 0


def _queue_sort(priority: str, created_at_ms: int) -> str:
    rank = _PRIORITY_RANK.get(priority, _PRIORITY_RANK["DEFAULT"])
    return f"{rank}#{created_at_ms:013d}"


class JobStore:
    def __init__(self, table_name: str, dynamodb_client=None):
        self._table = table_name
        self._client = dynamodb_client or boto3.client("dynamodb")

    def put_queued_job(self, job: QueuedJob) -> None:
        item = {
            "run_id": {"S": job.run_id},
            "status": {"S": QUEUED},
            "experiment_type": {"S": job.experiment_type},
            "organization_slug": {"S": job.organization_slug},
            "priority": {"S": job.priority},
            "params": {"S": json.dumps(job.params)},
            "routing": {"S": json.dumps(job.routing)},
            "created_at": {"N": str(job.created_at_ms)},
            "attempts": {"N": str(job.attempts)},
            "gsi_pk": {"S": QUEUED},
            "queue_sort": {"S": _queue_sort(job.priority, job.created_at_ms)},
        }
        if job.clerk_user_id is not None:
            item["clerk_user_id"] = {"S": job.clerk_user_id}
        if job.prior_artifact_versions is not None:
            item["prior_artifact_versions"] = {"S": json.dumps(job.prior_artifact_versions)}
        # Idempotent on the SQS run_id: a redelivered ingest message must not
        # overwrite an already-claimed/dispatched job back to QUEUED.
        try:
            self._client.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(run_id)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def query_queued(self, limit: int) -> list[QueuedJob]:
        resp = self._client.query(
            TableName=self._table,
            IndexName=_GSI_NAME,
            KeyConditionExpression="gsi_pk = :q",
            ExpressionAttributeValues={":q": {"S": QUEUED}},
            ScanIndexForward=True,
            Limit=limit,
        )
        return [self._to_job(i) for i in resp.get("Items", [])]

    def claim(self, run_id: str) -> None:
        """QUEUED -> LAUNCHING, dropping the job out of the sparse GSI.
        Raises JobClaimConflict if it is no longer QUEUED."""
        now_ms = int(time.time() * 1000)
        try:
            self._client.update_item(
                TableName=self._table,
                Key={"run_id": {"S": run_id}},
                UpdateExpression=(
                    "SET #s = :launching, attempts = attempts + :one, " "claimed_at = :now REMOVE gsi_pk, queue_sort"
                ),
                ConditionExpression="#s = :queued",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":launching": {"S": LAUNCHING},
                    ":queued": {"S": QUEUED},
                    ":one": {"N": "1"},
                    ":now": {"N": str(now_ms)},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise JobClaimConflict(run_id) from e
            raise

    def mark_dispatched(self, run_id: str) -> None:
        self._set_terminal(run_id, DISPATCHED)

    def mark_failed(self, run_id: str) -> None:
        self._set_terminal(run_id, FAILED)

    def query_stuck_launching(self, older_than_ms: int) -> list[QueuedJob]:
        """Bounded scan for jobs stuck in LAUNCHING since before `older_than_ms`.

        A transient failure during launch leaves a job LAUNCHING and out of the
        GSI, so it never gets re-picked. A small bounded Scan (capped at
        _STUCK_SCAN_LIMIT) is sufficient at this volume — the LAUNCHING set is
        only the in-flight launches, which is at most the concurrency cap."""
        resp = self._client.scan(
            TableName=self._table,
            FilterExpression="#s = :launching AND claimed_at < :cutoff",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":launching": {"S": LAUNCHING},
                ":cutoff": {"N": str(older_than_ms)},
            },
            Limit=_STUCK_SCAN_LIMIT,
        )
        return [self._to_job(i) for i in resp.get("Items", [])]

    def _set_terminal(self, run_id: str, status: str) -> None:
        ttl = int(time.time()) + _DISPATCHED_TTL_SECONDS
        self._client.update_item(
            TableName=self._table,
            Key={"run_id": {"S": run_id}},
            UpdateExpression="SET #s = :s, #t = :ttl REMOVE gsi_pk, queue_sort",
            ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
            ExpressionAttributeValues={
                ":s": {"S": status},
                ":ttl": {"N": str(ttl)},
            },
        )

    def _to_job(self, item: dict) -> QueuedJob:
        return QueuedJob(
            run_id=item["run_id"]["S"],
            experiment_type=item["experiment_type"]["S"],
            organization_slug=item["organization_slug"]["S"],
            clerk_user_id=item.get("clerk_user_id", {}).get("S"),
            priority=item["priority"]["S"],
            params=json.loads(item["params"]["S"]),
            routing=json.loads(item["routing"]["S"]),
            prior_artifact_versions=(
                json.loads(item["prior_artifact_versions"]["S"]) if "prior_artifact_versions" in item else None
            ),
            created_at_ms=int(item["created_at"]["N"]),
            attempts=int(item["attempts"]["N"]),
        )
