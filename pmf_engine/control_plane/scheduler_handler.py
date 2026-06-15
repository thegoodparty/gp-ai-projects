from __future__ import annotations

import json
import os
import time

import boto3

try:
    from shared.logger import get_logger

    logger = get_logger(__name__)
except (ImportError, OSError):
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    from .dispatch_handler import emit_dispatch_metric, get_sqs_client, launch_run
    from .job_store import JobClaimConflict, JobStore
except ImportError:  # Lambda flat-package import
    from dispatch_handler import emit_dispatch_metric, get_sqs_client, launch_run  # type: ignore[no-redef]
    from job_store import JobClaimConflict, JobStore  # type: ignore[no-redef]

MAX_CONCURRENT_AGENTS = int(os.environ.get("MAX_CONCURRENT_AGENTS", "0") or 0)
ECS_CLUSTER_ARN = os.environ.get("ECS_CLUSTER_ARN", "")
RESULTS_QUEUE_URL = os.environ.get("RESULTS_QUEUE_URL", "")
JOB_TABLE_NAME = os.environ.get("JOB_TABLE_NAME", "")

# A job left LAUNCHING by a transient failure must eventually fail (and notify
# gp-api) rather than leak. Anything claimed longer ago than this is swept.
_STUCK_LAUNCHING_MS = 10 * 60 * 1000

_ecs_client = None
_job_store = None


def get_ecs_client():
    global _ecs_client
    if _ecs_client is None:
        _ecs_client = boto3.client("ecs")
    return _ecs_client


def get_job_store():
    global _job_store
    if _job_store is None:
        _job_store = JobStore(JOB_TABLE_NAME)
    return _job_store


def count_running_tasks() -> int:
    """RUNNING-desired tasks on the cluster (includes PROVISIONING/PENDING),
    paginated — list_tasks returns at most 100 ARNs per page."""
    paginator = get_ecs_client().get_paginator("list_tasks")
    count = 0
    for page in paginator.paginate(cluster=ECS_CLUSTER_ARN, desiredStatus="RUNNING"):
        count += len(page.get("taskArns", []))
    return count


def _send_callback(
    run_id: str,
    status: str,
    *,
    experiment_id: str = "unknown",
    organization_slug: str = "unknown",
    error: str | None = None,
) -> None:
    body = {
        "type": "agentExperimentResult",
        "data": {
            "experimentId": experiment_id,
            "runId": run_id,
            "organizationSlug": organization_slug,
            "status": status,
            **({"error": error, "detail": error} if error else {}),
        },
    }
    get_sqs_client().send_message(
        QueueUrl=RESULTS_QUEUE_URL,
        MessageBody=json.dumps(body),
        MessageGroupId="agentExperiments",
        MessageDeduplicationId=f"{run_id}-{status}",
    )


def _sweep_stuck_launching(store) -> None:
    """Fail jobs stuck in LAUNCHING past the threshold so they don't leak."""
    cutoff = int(time.time() * 1000) - _STUCK_LAUNCHING_MS
    for job in store.query_stuck_launching(older_than_ms=cutoff):
        logger.error(f"job {job.run_id} stuck in LAUNCHING; failing it")
        emit_dispatch_metric("SchedulerStuckLaunchingSwept", job.experiment_type)
        store.mark_failed(job.run_id)
        _send_callback(
            job.run_id,
            "failed",
            experiment_id=job.experiment_type,
            organization_slug=job.organization_slug,
            error="Dispatch stalled while launching the agent task",
        )


def handler(event, context) -> dict:
    store = get_job_store()
    _sweep_stuck_launching(store)

    if MAX_CONCURRENT_AGENTS <= 0:
        logger.warning("MAX_CONCURRENT_AGENTS unset/0; scheduler will not launch")
        return {"launched": 0}

    try:
        running = count_running_tasks()
    except Exception as e:
        logger.warning(f"count_running_tasks failed ({type(e).__name__}: {e}); skipping this tick")
        return {"launched": 0}

    slots = MAX_CONCURRENT_AGENTS - running
    if slots <= 0:
        logger.info(f"At cap: {running}/{MAX_CONCURRENT_AGENTS}; no slots")
        return {"launched": 0}

    jobs = store.query_queued(limit=slots)
    launched = 0

    for job in jobs:
        if launched >= slots:
            break
        try:
            store.claim(job.run_id)
        except JobClaimConflict:
            logger.info(f"job {job.run_id} already claimed; skipping")
            continue

        message = {
            "run_id": job.run_id,
            "experiment_type": job.experiment_type,
            "organization_slug": job.organization_slug,
            "clerk_user_id": job.clerk_user_id,
            "params": job.params,
            "prior_artifact_versions": job.prior_artifact_versions,
        }
        experiment = dict(job.routing)  # model, timeout_seconds, *_version_id(s)
        params_json = json.dumps(job.params)

        try:
            result = launch_run(
                experiment=experiment,
                message=message,
                scope=job.routing.get("scope", {}),
                params_json=params_json,
            )
        except Exception as e:
            # Transient (httpx during mint, ECS RunTask exception). Leave the
            # job LAUNCHING; next tick won't re-pick it (it's out of the GSI),
            # so the stuck-LAUNCHING sweep above recovers it on a later tick.
            logger.exception(f"launch_run raised for {job.run_id} ({type(e).__name__}); left LAUNCHING")
            emit_dispatch_metric("SchedulerLaunchTransient", job.experiment_type)
            continue

        if result["status"] == "launched":
            store.mark_dispatched(job.run_id)
            _send_callback(
                job.run_id,
                "started",
                experiment_id=job.experiment_type,
                organization_slug=job.organization_slug,
            )
            launched += 1
        else:
            store.mark_failed(job.run_id)
            _send_callback(
                job.run_id,
                "failed",
                experiment_id=job.experiment_type,
                organization_slug=job.organization_slug,
                error=result.get("error", "dispatch failed"),
            )

    logger.info(f"scheduler launched {launched}/{slots} (running was {running})")
    return {"launched": launched}
