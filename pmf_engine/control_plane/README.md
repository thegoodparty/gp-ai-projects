# pmf_engine control plane — dispatch + priority queue

This package turns an agent-run request (an SQS message from gp-api) into a
running Fargate task, under an exact concurrency cap and in priority order.

## Two-stage flow

```
gp-api ──SQS──▶ ingest Lambda ──▶ DynamoDB job queue ──stream/tick──▶ scheduler Lambda ──RunTask──▶ Fargate
(agent-dispatch-{env}.fifo)        (agent-job-queue-{env})                                    │
                                                                          started/failed ─SQS─┘
                                                                          (agent-results-{env}.fifo) ──▶ gp-api
```

### Ingest (`dispatch_handler.py` `handler`)

Consumes `agent-dispatch-{env}.fifo`. Parses + validates the message (identifier
shapes, manifest routing, `input_schema`, scope derivation, params size), then
**writes a `QUEUED` job** to the `agent-job-queue-{env}` DynamoDB table instead of
launching anything. Manifest version IDs are resolved and pinned into the job row
here, so a job dispatched after a long queue wait still runs against the exact
manifest bytes it was validated against. Validation failures send a `failed`
callback to gp-api (unchanged from before). Ingest never mints a broker token and
never calls `run_task`.

### Job queue (`job_store.py`, table `agent-job-queue-{env}`)

Hash key `run_id`. A sparse GSI `queue-index` (hash `gsi_pk="QUEUED"`, range
`queue_sort = "{0|1}#{created_at:013d}"`) holds exactly the waiting backlog and
sorts `HIGH` (rank 0) before `DEFAULT` (rank 1), oldest-first within a tier.
Claiming a job (`QUEUED → LAUNCHING`) removes the GSI keys so it drops out of the
index. TTL cleans up terminal/dispatched rows.

### Scheduler (`scheduler_handler.py` `handler`)

Triggered by the table's **DynamoDB stream on insert** (arrival, seconds) and a
**1-minute EventBridge tick** (slot-freed reconciliation). Pinned to
**reserved concurrency 1**. Each run: count RUNNING-desired Fargate tasks (the
real concurrency) → `slots = MAX_CONCURRENT_AGENTS - running` → query the GSI for
up to `slots` `QUEUED` jobs → for each, conditionally claim it, mint the broker
token, and `run_task`. On launch it sends a `started` callback (gp-api flips
`QUEUED → RUNNING`); on failure a `failed` callback. A stuck-`LAUNCHING` sweep
fails jobs that were claimed but never launched.

## Why the cap is exact

Exactly one scheduler runs at a time (reserved concurrency 1) and it is the only
caller of `run_task`. Two concurrent schedulers would each read the same
`running` count and each claim _different_ jobs, overshooting the cap — the
conditional claim only prevents double-claiming the _same_ job, not overshoot.
`MAX_CONCURRENT_AGENTS` (`max_concurrent_agents` Terraform variable, default 100,
`0` disables) is the cap; the scheduler counts tasks with `desiredStatus=RUNNING`,
which includes PROVISIONING/PENDING, so a just-launched task is counted on the
next tick.

## Priority

Two tiers: `HIGH` and `DEFAULT`. gp-api sets `HIGH` for user-triggered briefing
dispatches; bulk cohort and resume dispatches stay `DEFAULT`. The priority travels
in the SQS message body and is stored on the job row.

## Status flow (gp-api `ExperimentRun`)

`QUEUED` (enqueued) → `RUNNING` (scheduler `started` callback) →
`COMPLETED`/`FAILED`/`AWAITING_RESUME` (terminal callback). gp-api's 45-minute
stale sweep is scoped to `RUNNING`, so queue-wait time does not count against it;
a separate longer backstop sweep reclaims runs orphaned in `QUEUED`.
