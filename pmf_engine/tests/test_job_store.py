import boto3
import pytest
from moto import mock_aws

from pmf_engine.control_plane.job_store import JobStore, QueuedJob

TABLE = "agent-job-queue-test"


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "gsi_pk", "AttributeType": "S"},
                {"AttributeName": "queue_sort", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "queue-index",
                    "KeySchema": [
                        {"AttributeName": "gsi_pk", "KeyType": "HASH"},
                        {"AttributeName": "queue_sort", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        yield JobStore(TABLE, dynamodb_client=client)


def _job(run_id, priority, created_at_ms):
    return QueuedJob(
        run_id=run_id,
        experiment_type="smoke_test",
        organization_slug="org-1",
        clerk_user_id="user_1",
        priority=priority,
        params={"state": "WI"},
        routing={"model": "sonnet", "timeout_seconds": 600, "scope": {}},
        prior_artifact_versions=None,
        created_at_ms=created_at_ms,
    )


def test_query_orders_high_before_default_then_oldest_first(store):
    store.put_queued_job(_job("r-default-old", "DEFAULT", 1000))
    store.put_queued_job(_job("r-high-new", "HIGH", 3000))
    store.put_queued_job(_job("r-high-old", "HIGH", 2000))
    ids = [j.run_id for j in store.query_queued(limit=10)]
    assert ids == ["r-high-old", "r-high-new", "r-default-old"]


def test_claim_drops_job_from_queue_and_blocks_second_claim(store):
    store.put_queued_job(_job("r1", "HIGH", 1000))
    store.claim("r1")
    assert store.query_queued(limit=10) == []
    from pmf_engine.control_plane.job_store import JobClaimConflict

    with pytest.raises(JobClaimConflict):
        store.claim("r1")


def test_query_respects_limit(store):
    for i in range(5):
        store.put_queued_job(_job(f"r{i}", "DEFAULT", 1000 + i))
    assert len(store.query_queued(limit=3)) == 3


def test_put_is_idempotent_on_run_id(store):
    store.put_queued_job(_job("r1", "HIGH", 1000))
    store.claim("r1")
    # A redelivered ingest message must not overwrite the claimed job back to
    # QUEUED.
    store.put_queued_job(_job("r1", "HIGH", 1000))
    assert store.query_queued(limit=10) == []


def test_mark_dispatched_and_failed_drop_from_queue(store):
    store.put_queued_job(_job("r1", "HIGH", 1000))
    store.claim("r1")
    store.mark_dispatched("r1")
    assert store.query_queued(limit=10) == []

    store.put_queued_job(_job("r2", "HIGH", 1000))
    store.claim("r2")
    store.mark_failed("r2")
    assert store.query_queued(limit=10) == []


def test_query_stuck_launching_returns_old_claimed_jobs(store):
    import time

    store.put_queued_job(_job("r-fresh", "HIGH", 1000))
    store.put_queued_job(_job("r-stale", "HIGH", 1000))
    store.claim("r-fresh")
    store.claim("r-stale")
    # Both claimed just now; cutoff in the past returns none.
    assert store.query_stuck_launching(older_than_ms=0) == []
    # Cutoff in the future treats both as stuck.
    future = int(time.time() * 1000) + 60_000
    ids = {j.run_id for j in store.query_stuck_launching(older_than_ms=future)}
    assert ids == {"r-fresh", "r-stale"}
