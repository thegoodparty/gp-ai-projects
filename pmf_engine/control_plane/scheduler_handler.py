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
) -> bool:
    """Best-effort SQS callback to gp-api's results queue. Never raises —
    returns True on a successful send, False otherwise. The launch path keys
    its `mark_dispatched` decision on this so a failed `started` send leaves
    the job LAUNCHING for the stuck-LAUNCHING sweep rather than orphaning it."""
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
    try:
        get_sqs_client().send_message(
            QueueUrl=RESULTS_QUEUE_URL,
            MessageBody=json.dumps(body),
            MessageGroupId="agentExperiments",
            MessageDeduplicationId=f"{run_id}-{status}",
        )
        return True
    except Exception as e:
        logger.exception(f"callback send failed for {run_id} status={status} ({type(e).__name__})")
        emit_dispatch_metric("SchedulerCallbackSendFailed", experiment_id)
        return False


def _sweep_stuck_launching(store) -> None:
    """Fail jobs stuck in LAUNCHING past the threshold so they don't leak."""
    cutoff = int(time.time() * 1000) - _STUCK_LAUNCHING_MS
    for job in store.query_stuck_launching(older_than_ms=cutoff):
        logger.error(f"job {job.run_id} stuck in LAUNCHING; failing it")
        emit_dispatch_metric("SchedulerStuckLaunchingSwept", job.experiment_type)
        store.mark_failed(job.run_id)
        sent = _send_callback(
            job.run_id,
            "failed",
            experiment_id=job.experiment_type,
            organization_slug=job.organization_slug,
            error="Dispatch stalled while launching the agent task",
        )
        if not sent:
            logger.error(f"sweep failed-callback send failed for {job.run_id}; gp-api row may be orphaned")
            emit_dispatch_metric("SchedulerSweepCallbackFailed", job.experiment_type)


def handler(event, context) -> dict:
    store = get_job_store()
    # The DynamoDB-stream ESM retries a failing batch until records expire and,
    # with reserved concurrency 1, a single raise stalls ALL arrival-triggered
    # dispatch behind it. So the handler must effectively never raise: every
    # fallible step is guarded, and the 1-minute tick + idempotent design
    # reconcile anything an individual op leaves behind.
    try:
        _sweep_stuck_launching(store)
    except Exception as e:
        logger.exception(f"stuck-LAUNCHING sweep failed ({type(e).__name__}); continuing")
        emit_dispatch_metric("SchedulerSweepError", "_unknown")

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

    try:
        jobs = store.query_queued(limit=slots)
    except Exception as e:
        logger.warning(f"query_queued failed ({type(e).__name__}: {e}); skipping this tick")
        return {"launched": 0}
    launched = 0

    for job in jobs:
        if launched >= slots:
            break
        # One job's unexpected error must never abort the batch — log + metric
        # and move on. The transient-launch_run case is handled inside (leaves
        # LAUNCHING for the sweep); this outer guard catches everything else.
        try:
            if _launch_one(store, job):
                launched += 1
        except JobClaimConflict:
            logger.info(f"job {job.run_id} already claimed; skipping")
            continue
        except Exception as e:
            logger.exception(f"unexpected error dispatching {job.run_id} ({type(e).__name__}); continuing")
            emit_dispatch_metric("SchedulerJobError", job.experiment_type)
            continue

    logger.info(f"scheduler launched {launched}/{slots} (running was {running})")
    return {"launched": launched}


def _launch_one(store, job) -> bool:
    """Claim, launch, and reconcile a single queued job. Returns True if a task
    was launched (counts toward the slot budget). Raises JobClaimConflict if the
    claim lost the race; the caller treats any other raise as a per-job error."""
    store.claim(job.run_id)

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
        # so the stuck-LAUNCHING sweep recovers it on a later tick.
        logger.exception(f"launch_run raised for {job.run_id} ({type(e).__name__}); left LAUNCHING")
        emit_dispatch_metric("SchedulerLaunchTransient", job.experiment_type)
        return False

    if result["status"] == "launched":
        # Send the `started` callback BEFORE marking dispatched. If the send
        # fails, leave the job LAUNCHING (do NOT mark dispatched) — the
        # stuck-LAUNCHING sweep reconciles it to FAILED later. This trades a
        # rare ran-but-shows-FAILED for never silently orphaning a launched run.
        sent = _send_callback(
            job.run_id,
            "started",
            experiment_id=job.experiment_type,
            organization_slug=job.organization_slug,
        )
        if not sent:
            logger.error(f"started-callback send failed for {job.run_id}; leaving LAUNCHING for the sweep")
            emit_dispatch_metric("SchedulerStartedCallbackFailed", job.experiment_type)
            return False
        store.mark_dispatched(job.run_id)
        return True

    store.mark_failed(job.run_id)
    sent = _send_callback(
        job.run_id,
        "failed",
        experiment_id=job.experiment_type,
        organization_slug=job.organization_slug,
        error=result.get("error", "dispatch failed"),
    )
    if not sent:
        logger.error(f"failed-callback send failed for {job.run_id}; gp-api row may be orphaned")
        emit_dispatch_metric("SchedulerFailedCallbackFailed", job.experiment_type)
    return False
