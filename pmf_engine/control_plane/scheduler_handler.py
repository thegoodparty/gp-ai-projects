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

# The live cap is the SSM parameter named by MAX_CONCURRENT_AGENTS_PARAM, read
# each tick. When the parameter isn't configured or the read fails we fall back
# to a conservative hard-coded floor rather than disabling the cap — a transient
# SSM blip must not drop the cap to 0 and stall all dispatch.
_FALLBACK_MAX_CONCURRENT_AGENTS = 50
MAX_CONCURRENT_AGENTS_PARAM = os.environ.get("MAX_CONCURRENT_AGENTS_PARAM", "")
ECS_CLUSTER_ARN = os.environ.get("ECS_CLUSTER_ARN", "")
RESULTS_QUEUE_URL = os.environ.get("RESULTS_QUEUE_URL", "")
JOB_TABLE_NAME = os.environ.get("JOB_TABLE_NAME", "")

# A job left LAUNCHING by a transient failure must eventually fail (and notify
# gp-api) rather than leak. Anything claimed longer ago than this is swept.
_STUCK_LAUNCHING_MS = 10 * 60 * 1000

_ecs_client = None
_job_store = None
_ssm_client = None


def get_ecs_client():
    global _ecs_client
    if _ecs_client is None:
        _ecs_client = boto3.client("ecs")
    return _ecs_client


def get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def get_max_concurrent_agents() -> int:
    """Live cap, read from SSM each tick so an operator can change it with a
    single `ssm put-parameter` and no deploy. Falls back to the hard-coded
    _FALLBACK_MAX_CONCURRENT_AGENTS when the parameter isn't configured or the
    read fails (transient SSM error / param missing) — never disables the cap."""
    if not MAX_CONCURRENT_AGENTS_PARAM:
        return _FALLBACK_MAX_CONCURRENT_AGENTS
    try:
        resp = get_ssm_client().get_parameter(Name=MAX_CONCURRENT_AGENTS_PARAM)
        return int(resp["Parameter"]["Value"])
    except Exception as e:
        logger.warning(
            f"SSM get_parameter({MAX_CONCURRENT_AGENTS_PARAM}) failed ({type(e).__name__}: {e}); "
            f"falling back to {_FALLBACK_MAX_CONCURRENT_AGENTS}"
        )
        return _FALLBACK_MAX_CONCURRENT_AGENTS


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


def _has_live_task(run_id: str) -> bool:
    """True if a Fargate task tagged with this run_id is still desired-RUNNING.
    Tasks are launched with startedBy=run_id. Fail open (return True) on an ECS
    error so a transient ListTasks blip can't cause the sweep to fail a job that
    may have a live task."""
    try:
        resp = get_ecs_client().list_tasks(cluster=ECS_CLUSTER_ARN, startedBy=run_id, desiredStatus="RUNNING")
        return len(resp.get("taskArns", [])) > 0
    except Exception as e:
        logger.warning(
            f"list_tasks(startedBy={run_id}) failed ({type(e).__name__}); treating as live to avoid wrong-fail"
        )
        return True


def _sweep_stuck_launching(store) -> None:
    """Fail jobs stuck in LAUNCHING past the threshold so they don't leak."""
    cutoff = int(time.time() * 1000) - _STUCK_LAUNCHING_MS
    for job in store.query_stuck_launching(older_than_ms=cutoff):
        # A job can be stuck LAUNCHING with a LIVE task if mark_dispatched threw
        # after run_task succeeded. Failing it here would kill a running agent
        # and drop its result. Check ECS first: if a task tagged with this run_id
        # is alive, reconcile the row to DISPATCHED instead of failing it.
        if _has_live_task(job.run_id):
            logger.warning(f"job {job.run_id} stuck LAUNCHING but has a live task; reconciling to DISPATCHED")
            emit_dispatch_metric("SchedulerStuckLaunchingReconciled", job.experiment_type)
            try:
                store.mark_dispatched(job.run_id)
            except Exception as e:
                logger.warning(
                    f"reconcile mark_dispatched failed for {job.run_id} ({type(e).__name__}); next sweep retries"
                )
            continue
        logger.error(f"job {job.run_id} stuck in LAUNCHING; failing it")
        emit_dispatch_metric("SchedulerStuckLaunchingSwept", job.experiment_type)
        # Notify gp-api BEFORE committing FAILED. If the SQS send fails, leave the
        # job in LAUNCHING so the next sweep retries — once FAILED is persisted the
        # job drops out of query_stuck_launching and the callback can never be
        # re-sent, orphaning the gp-api row.
        sent = _send_callback(
            job.run_id,
            "failed",
            experiment_id=job.experiment_type,
            organization_slug=job.organization_slug,
            error="Dispatch stalled while launching the agent task",
        )
        if not sent:
            logger.error(f"sweep failed-callback send failed for {job.run_id}; will retry on next tick")
            emit_dispatch_metric("SchedulerSweepCallbackFailed", job.experiment_type)
            continue
        store.mark_failed(job.run_id)


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

    cap = get_max_concurrent_agents()
    if cap <= 0:
        logger.warning("max concurrent agents unset/0; scheduler will not launch")
        return {"launched": 0}

    try:
        running = count_running_tasks()
    except Exception as e:
        logger.warning(f"count_running_tasks failed ({type(e).__name__}: {e}); skipping this tick")
        return {"launched": 0}

    slots = cap - running
    if slots <= 0:
        logger.info(f"At cap: {running}/{cap}; no slots")
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
        # User-input prefetch (develop): thread the persisted input_files back
        # onto the dispatch envelope so launch_run's mint + the
        # INPUT_FILES_JSON env builder see it, same as the pre-queue path did.
        "_input_files": job.input_files,
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
        # RunTask succeeded — a real Fargate task is now running, so that is the
        # source of truth: mark DISPATCHED unconditionally so the job leaves the
        # stuck-LAUNCHING sweep's view. The `started` callback is best-effort: if
        # the SQS send fails, do NOT leave the job LAUNCHING (the sweep would then
        # send a `failed` callback and FAIL a live task, dropping its real result
        # when the terminal callback lands). gp-api instead reconciles via the
        # task's terminal callback (its relaxed guard accepts QUEUED -> terminal),
        # or, if the task dies silently, the ECS task-reaper Lambda sends a
        # `failed` callback keyed on startedBy=run_id (there is no longer a
        # time-based QUEUED backstop in gp-api).
        store.mark_dispatched(job.run_id)
        sent = _send_callback(
            job.run_id,
            "started",
            experiment_id=job.experiment_type,
            organization_slug=job.organization_slug,
        )
        if not sent:
            logger.error(
                f"started-callback send failed for {job.run_id}; task is running, gp-api will reconcile on terminal callback / backstop"
            )
            emit_dispatch_metric("SchedulerStartedCallbackFailed", job.experiment_type)
        return True

    # Notify gp-api BEFORE committing FAILED. If the send fails, leave the job in
    # LAUNCHING (don't mark_failed) so the stuck-LAUNCHING sweep retries the
    # callback later — a FAILED row drops out of query_stuck_launching, so a
    # never-sent callback would orphan the gp-api row with no recovery path.
    sent = _send_callback(
        job.run_id,
        "failed",
        experiment_id=job.experiment_type,
        organization_slug=job.organization_slug,
        error=result.get("error", "dispatch failed"),
    )
    if not sent:
        logger.error(f"failed-callback send failed for {job.run_id}; leaving LAUNCHING for the sweep")
        emit_dispatch_metric("SchedulerFailedCallbackFailed", job.experiment_type)
        return False
    store.mark_failed(job.run_id)
    return False
