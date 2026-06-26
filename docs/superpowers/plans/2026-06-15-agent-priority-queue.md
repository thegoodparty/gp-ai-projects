# Agent Priority Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fire-and-forget SQS-to-Fargate dispatch with a DynamoDB-backed job queue and a single serialized scheduler Lambda, so agent runs honor a `HIGH`/`DEFAULT` priority order under a concurrency cap that is now exact instead of best-effort.

**Architecture:** Today gp-api enqueues a dispatch message onto `agent-dispatch-{env}.fifo`; the dispatch Lambda consumes one message at a time and immediately launches one Fargate task per message, with no ordering and (after PR #132) only a best-effort cap. We split that Lambda in two. The **ingest** Lambda keeps consuming the SQS queue, does all validation, and writes a `QUEUED` job row to a new DynamoDB table `agent-job-queue-{env}` instead of launching anything. A new **scheduler** Lambda — triggered by DynamoDB Streams on insert (arrival) and an EventBridge 1-minute tick (slot-freed reconciliation), pinned to reserved concurrency 1 — counts running Fargate tasks, computes free slots, queries the table for `QUEUED` jobs ordered by `(priority, created_at)`, conditionally claims each, mints the broker token, and launches it. Because exactly one scheduler runs at a time and it is the only thing that calls `run_task`, the cap is exact. gp-api gains a `QUEUED` status and a `started` callback so its 45-minute stale sweep keeps meaning "actually executing," not "waiting in line."

**Tech Stack:** Python 3.13 Lambdas (boto3, httpx, jsonschema), DynamoDB (PAY_PER_REQUEST + GSI + Streams), Terraform, NestJS/Prisma/Zod (gp-api), `@goodparty_org/contracts` (Zod). Tests: pytest (gp-ai-projects), Vitest + `useTestService` harness (gp-api).

---

## Preconditions

- **Merge gp-ai-projects PR #132 first** (`feat/max-concurrent-agents`). This plan builds on the `MAX_CONCURRENT_AGENTS` env var, the `ecs:ListTasks` IAM grant, and the task-counting helper it introduced, and it **removes** the SQS deferral machinery (visibility extension, `maxReceiveCount = 30`, `maximum_concurrency = 2`) that #132 added — those are obsolete once the queue never backs up. If #132 is abandoned instead, Task 9 and Task 11 must re-introduce the counting helper and `ecs:ListTasks` grant from scratch; notes inline mark where.
- Branch names: `feat/agent-priority-queue` in **both** repos.
- Ship order: **Phase 1 (gp-api) first** — every Phase 1 change is backward-compatible with the _current_ dispatch Lambda (a `QUEUED` row that receives a terminal callback directly is accepted; no `started` event is required). Then **Phase 2 (gp-ai-projects)**. This lets the two repos deploy independently without a flag-day.

## Why each piece exists (read before starting)

- **DynamoDB, not more SQS queues:** SQS cannot reorder or peek, so priority requires a consumer that holds the backlog and chooses. A table also gives a queryable/cancellable/re-prioritizable backlog (future gp-admin surface) and makes the cap exact via a conditional claim.
- **Scheduler reserved concurrency = 1 is load-bearing:** if two scheduler instances ran concurrently they would each read `running=N`, each compute `slots=cap-N`, and each claim _different_ jobs (the conditional claim only stops double-claiming the _same_ job), launching up to `2*slots` and overshooting the cap. One serialized scheduler is what makes the cap exact.
- **`started` callback + `QUEUED` status:** gp-api's stale sweep fails any `RUNNING` row older than 45 minutes. Once jobs legitimately wait in a queue, "enqueued" must not mean `RUNNING` or the sweep kills waiting jobs. So enqueue sets `QUEUED`, the scheduler emits `started` when it actually launches (→ `RUNNING`), and the sweep stays scoped to `RUNNING`.
- **Version IDs pinned at ingest:** ingest resolves the manifest and stores the pinned `manifest_version_id`/`instruction_version_id`/`attachment_version_ids` in the job row. The scheduler dispatches against those exact versions, closing the publish-during-wait race for the entire queue-wait duration.
- **Mint at dispatch, not ingest:** broker tokens are short-lived; minting happens in the scheduler at launch time so a token never sits unused in the queue and a cancelled job never mints one.

---

## File Structure

### gp-ai-projects (Phase 2)

- Create: `pmf_engine/control_plane/job_store.py` — DynamoDB job-queue client (put QUEUED job, query QUEUED-by-priority, conditional claim, mark terminal). One responsibility: the table.
- Create: `pmf_engine/control_plane/scheduler_handler.py` — the scheduler Lambda entrypoint: count slots → query → claim → mint → run_task → started/failed callback.
- Modify: `pmf_engine/control_plane/dispatch_handler.py` — the terminal action of `handler()` changes from mint+`run_task` to `job_store.put_queued_job(...)`; everything before it (parse/validate/route/scope) is unchanged. Extract the mint+`run_task` block into a reusable `launch_run()` used by the scheduler.
- Modify: `pmf_engine/scripts/build_lambda_package.sh` — copy the two new modules into `.lambda_build`.
- Modify: `infrastructure/modules/pmf-engine-control-plane/main.tf` — add the table (GSI + Streams), the scheduler Lambda + its triggers + IAM; remove the #132 deferral bits.
- Create tests: `pmf_engine/tests/test_job_store.py`, `pmf_engine/tests/test_scheduler_handler.py`; modify `pmf_engine/tests/test_dispatch_handler.py`.

### gp-api (Phase 1, in omni monorepo)

- Modify: `packages/contracts/src/generated/enums.ts` — add `QUEUED` to `EXPERIMENT_RUN_STATUS_VALUES`.
- Create: `packages/gp-api/prisma/migrations/<ts>_add_queued_experiment_run_status/migration.sql` — add `QUEUED` enum value.
- Modify: `packages/gp-api/prisma/schema/experimentRun.prisma` — add `QUEUED` to the enum.
- Modify: `packages/gp-api/src/agentExperiments/services/experimentRuns.service.ts` — `priority` in the message + `dispatchRun` input; set `QUEUED` at enqueue.
- Modify: `packages/gp-api/src/queue/queue.types.ts` — add `started` to `AgentExperimentResultSchema.status`.
- Modify: `packages/gp-api/src/queue/consumer/queueConsumer.service.ts` — handle `started` (QUEUED→RUNNING); relax terminal idempotency guard.
- Modify: `packages/gp-api/src/meetings/services/meetingBriefings.service.ts` — user-triggered dispatch passes `priority: 'HIGH'`.

---

# Phase 1 — gp-api (backward-compatible; ship first)

All Phase 1 work happens in `/Users/smolster/Repos/thegoodparty/omni`. Use a worktree if other sessions share the checkout (see memory: omni-shared-checkout-collisions).

### Task 1: Add `QUEUED` to the contracts status enum

**Files:**

- Modify: `packages/contracts/src/generated/enums.ts` (the `EXPERIMENT_RUN_STATUS_VALUES` array)

- [ ] **Step 1: Add the enum value**

In `packages/contracts/src/generated/enums.ts`, change:

```typescript
export const EXPERIMENT_RUN_STATUS_VALUES = [
  "RUNNING",
  "AWAITING_RESUME",
  "COMPLETED",
  "FAILED",
] as const;
```

to:

```typescript
export const EXPERIMENT_RUN_STATUS_VALUES = [
  "QUEUED",
  "RUNNING",
  "AWAITING_RESUME",
  "COMPLETED",
  "FAILED",
] as const;
```

- [ ] **Step 2: Rebuild contracts**

Run: `cd packages/contracts && npm run build`
Expected: builds with no type errors. (If a generator owns this file, run the generator instead — check `packages/contracts/package.json` scripts; if `enums.ts` is under `generated/`, confirm whether it is regenerated from the Prisma schema and, if so, that Task 2's schema change is what drives it. Either way the array must end up containing `QUEUED`.)

- [ ] **Step 3: Restore repo style if a hook reformatted it**

The contracts package has no `.prettierrc`; the format hook applies prettier defaults (see memory: contracts-prettier-hook-defaults). If the file gained semicolons/double-quotes, restore single-quote/no-semicolon style before committing.

- [ ] **Step 4: Commit**

```bash
cd packages/contracts && git add src/generated/enums.ts && git commit -m "feat(contracts): add QUEUED experiment run status"
```

### Task 2: Prisma migration + schema for `QUEUED`

**Files:**

- Modify: `packages/gp-api/prisma/schema/experimentRun.prisma:1-6`
- Create: `packages/gp-api/prisma/migrations/<timestamp>_add_queued_experiment_run_status/migration.sql`

REQUIRED SUB-SKILL: use the gp-api `new-migration` skill to generate the migration safely against the modular schema.

- [ ] **Step 1: Edit the schema enum**

In `packages/gp-api/prisma/schema/experimentRun.prisma`, change:

```prisma
enum ExperimentRunStatus {
  RUNNING
  AWAITING_RESUME
  COMPLETED
  FAILED
}
```

to:

```prisma
enum ExperimentRunStatus {
  QUEUED
  RUNNING
  AWAITING_RESUME
  COMPLETED
  FAILED
}
```

- [ ] **Step 2: Generate the migration**

Run the project's migration command (via the `new-migration` skill) to create a new migration named `add_queued_experiment_run_status`. The generated SQL must be exactly:

```sql
ALTER TYPE "ExperimentRunStatus" ADD VALUE 'QUEUED' BEFORE 'RUNNING';
```

Note: Postgres cannot add an enum value inside a transaction block that also uses it; Prisma handles this as its own migration. Do not hand-edit applied migration SQL.

- [ ] **Step 3: Apply + regenerate client**

Run the project's migrate-dev + `prisma generate` commands. Expected: `ExperimentRunStatus.QUEUED` is now available in generated types.

- [ ] **Step 4: Commit**

```bash
cd packages/gp-api && git add prisma/ && git commit -m "feat(gp-api): add QUEUED to ExperimentRunStatus"
```

### Task 3: Carry `priority` through dispatch + enqueue `QUEUED`

**Files:**

- Modify: `packages/gp-api/src/agentExperiments/services/experimentRuns.service.ts` (`enqueueDispatch` ~67-91, `createAndEnqueueRun` ~93-158, `dispatchRun` ~160-173, and the `ExperimentRunDispatchInput` type)
- Test: `packages/gp-api/src/agentExperiments/services/experimentRuns.service.test.ts` (create if absent; drive via the API/test harness where a route exists)

- [ ] **Step 1: Write the failing test**

Add a test asserting (a) a dispatched run is created with `status: 'QUEUED'`, and (b) the SQS message body carries `priority`. Spy on the `sqs.sendMessage` wrapper used by the service (follow the existing mocking pattern in the agentExperiments tests; if none exists, mock the `sqs` module the service imports).

```typescript
import { describe, it, expect, vi } from "vitest";
// ...harness/imports following the agentExperiments test conventions...

it("creates the run as QUEUED and forwards priority in the SQS body", async () => {
  const sendMessage = vi.fn().mockResolvedValue({});
  // wire sendMessage into the service's sqs dependency per existing pattern
  const run = await service.dispatchRun({
    type: "meeting_briefing",
    organizationSlug: "org-test",
    clerkUserId: "user_test",
    params: { state: "WI" },
    priority: "HIGH",
  });
  expect(run?.status).toBe("QUEUED");
  const body = JSON.parse(sendMessage.mock.calls[0][0].MessageBody);
  expect(body.priority).toBe("HIGH");
  expect(body.run_id).toBe(run?.runId);
});

it("defaults priority to DEFAULT when not given", async () => {
  const sendMessage = vi.fn().mockResolvedValue({});
  await service.dispatchRun({
    type: "meeting_briefing",
    organizationSlug: "org-test",
    clerkUserId: "user_test",
    params: { state: "WI" },
  });
  const body = JSON.parse(sendMessage.mock.calls[0][0].MessageBody);
  expect(body.priority).toBe("DEFAULT");
});
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `cd packages/gp-api && npx vitest run src/agentExperiments/services/experimentRuns.service.test.ts`
Expected: FAIL — `priority` undefined in body and/or `status` is `RUNNING`.

- [ ] **Step 3: Add a priority type**

At the top of `experimentRuns.service.ts` (near other local types), add:

```typescript
export type DispatchPriority = "HIGH" | "DEFAULT";
```

Add an optional `priority?: DispatchPriority` to `ExperimentRunDispatchInput` (the input type for `dispatchRun`) and thread it through. If `ExperimentRunDispatchInput` is defined in another file, add the optional field there.

- [ ] **Step 4: Forward priority in `enqueueDispatch`**

Change the `input` param and `messageBody` of `enqueueDispatch`:

```typescript
  private async enqueueDispatch(
    queueUrl: string,
    input: {
      runId: string
      organizationSlug: string
      experimentType: string
      clerkUserId: string
      params: unknown
      priority: DispatchPriority
    },
  ) {
    const messageBody = {
      run_id: input.runId,
      params: input.params,
      organization_slug: input.organizationSlug,
      experiment_type: input.experimentType,
      clerk_user_id: input.clerkUserId,
      priority: input.priority,
    }

    await sqs.sendMessage({
      QueueUrl: queueUrl,
      MessageBody: JSON.stringify(messageBody),
      MessageGroupId: `agent-dispatch-${input.organizationSlug}`,
      MessageDeduplicationId: randomUUID(),
    })
  }
```

- [ ] **Step 5: Set `QUEUED` and pass priority in `createAndEnqueueRun`**

In `createAndEnqueueRun`, add `priority` to the input object type (`priority?: DispatchPriority`), change the create call's status, and pass priority to `enqueueDispatch`:

```typescript
const result = await this.model.create({
  data: {
    runId,
    experimentType: input.experimentType,
    organizationSlug: input.organizationSlug,
    status: ExperimentRunStatus.QUEUED,
    params: input.params,
    resumeAttempts: input.resumeAttempts ?? 0,
    stage: input.stage ?? null,
  },
});
```

```typescript
await this.enqueueDispatch(queueUrl, {
  runId,
  organizationSlug: input.organizationSlug,
  experimentType: input.experimentType,
  clerkUserId: input.clerkUserId,
  params: input.params,
  priority: input.priority ?? "DEFAULT",
});
```

- [ ] **Step 6: Pass priority through `dispatchRun`**

```typescript
  async dispatchRun<ExperimentType extends keyof AgentJobContracts>(
    input: ExperimentRunDispatchInput<ExperimentType>,
  ) {
    return this.createAndEnqueueRun({
      experimentType: input.type,
      organizationSlug: input.organizationSlug,
      clerkUserId: input.clerkUserId,
      priority: input.priority ?? 'DEFAULT',
      // eslint-disable-next-line @typescript-eslint/no-unsafe-type-assertion
      params: input.params as Prisma.InputJsonObject,
    })
  }
```

Note: the resume path (`resumeRun` → `createAndEnqueueRun`) does **not** pass `priority`, so resumes default to `DEFAULT`. That is intentional.

- [ ] **Step 7: Run the test, verify it passes**

Run: `cd packages/gp-api && npx vitest run src/agentExperiments/services/experimentRuns.service.test.ts`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd packages/gp-api && git add src/agentExperiments && git commit -m "feat(gp-api): enqueue runs as QUEUED and forward dispatch priority"
```

### Task 4: Accept the `started` callback and relax the terminal guard

**Files:**

- Modify: `packages/gp-api/src/queue/queue.types.ts:185-197`
- Modify: `packages/gp-api/src/queue/consumer/queueConsumer.service.ts` (`handleAgentExperimentResult` ~945-1021)
- Test: `packages/gp-api/src/queue/consumer/queueConsumer.service.test.ts` (extend existing; drive through the queue consumer entrypoint per the harness)

- [ ] **Step 1: Write the failing tests**

Add three cases: (a) a `started` event flips a `QUEUED` run to `RUNNING`; (b) a terminal `success` for a still-`QUEUED` run is accepted (relaxed guard — covers the agent-side sweeper failing a never-dispatched job); (c) a terminal event for an already-`COMPLETED`/`FAILED` run is still skipped (idempotency preserved).

```typescript
it("flips a QUEUED run to RUNNING on started", async () => {
  const run = await createExperimentRun({ status: "QUEUED" });
  await consumeAgentExperimentResult({ runId: run.runId, status: "started" });
  const after = await findRun(run.runId);
  expect(after.status).toBe("RUNNING");
});

it("accepts a terminal result for a still-QUEUED run", async () => {
  const run = await createExperimentRun({ status: "QUEUED" });
  await consumeAgentExperimentResult({
    runId: run.runId,
    status: "failed",
    error: "boom",
  });
  const after = await findRun(run.runId);
  expect(after.status).toBe("FAILED");
});

it("skips a terminal result for an already-terminal run", async () => {
  const run = await createExperimentRun({ status: "COMPLETED" });
  await consumeAgentExperimentResult({
    runId: run.runId,
    status: "failed",
    error: "late",
  });
  const after = await findRun(run.runId);
  expect(after.status).toBe("COMPLETED");
});
```

- [ ] **Step 2: Run, verify failure**

Run: `cd packages/gp-api && npx vitest run src/queue/consumer/queueConsumer.service.test.ts`
Expected: FAIL — `started` rejected by the Zod schema; QUEUED terminal dropped by the `!== RUNNING` guard.

- [ ] **Step 3: Add `started` to the result schema**

In `packages/gp-api/src/queue/queue.types.ts`:

```typescript
export const AgentExperimentResultSchema = z.object({
  runId: z.string(),
  status: z.enum(["started", "success", "failed", "contract_violation"]),
  artifactKey: z.string().optional(),
  artifactBucket: z.string().optional(),
  durationSeconds: z.number().optional(),
  costUsd: z.number().optional(),
  error: z.string().optional(),
});
```

- [ ] **Step 4: Handle `started` and relax the guard**

In `handleAgentExperimentResult`, replace the early guard and add the `started` branch. Define a terminal-set helper near the top of the method:

```typescript
  private async handleAgentExperimentResult(data: AgentExperimentResultData) {
    const run = await this.experimentRunsService.findUnique({
      where: { runId: data.runId },
    })

    if (!run) {
      this.logger.error({ data }, 'Experiment run not found')
      return true
    }

    const TERMINAL: ExperimentRunStatus[] = [
      ExperimentRunStatus.COMPLETED,
      ExperimentRunStatus.FAILED,
    ]
    if (TERMINAL.includes(run.status)) {
      this.logger.info(
        { runId: data.runId, status: run.status },
        'Experiment run already terminal, skipping',
      )
      return true
    }

    // The scheduler emits `started` when it actually launches the Fargate task.
    // Move QUEUED -> RUNNING so the 45-minute stale sweep measures execution
    // time, not queue-wait time. Idempotent: a RUNNING/AWAITING_RESUME row is
    // left untouched.
    if (data.status === 'started') {
      await this.experimentRunsService.updateMany({
        where: { runId: data.runId, status: ExperimentRunStatus.QUEUED },
        data: { status: ExperimentRunStatus.RUNNING },
      })
      return true
    }

    const successPatch =
      data.status === 'success' ? await this.resolveSuccessPatch(data) : null

    const updatedRun = await this.experimentRunsService.optimisticLockingUpdate(
      { where: { runId: data.runId } },
      async (currentRun) => {
        if (TERMINAL.includes(currentRun.status)) {
          this.logger.info(
            { runId: data.runId },
            'Experiment run already terminal, skipping',
          )
          throw new Error('Experiment run already terminal')
        }
        return {
          status: successPatch
            ? successPatch.status
            : ExperimentRunStatus.FAILED,
          stage: successPatch?.stage ?? null,
          dataQuality: successPatch?.dataQuality ?? null,
          resumeScheduledFor: successPatch?.resumeScheduledFor ?? null,
          artifactKey: data.artifactKey ?? null,
          artifactBucket: data.artifactBucket ?? null,
          durationSeconds: data.durationSeconds ?? null,
          costUsd: data.costUsd ?? null,
          error: data.error?.slice(0, 1000) ?? null,
        }
      },
    )

    this.logger.info({ updatedRun, data }, 'Updated experiment run from queue event')

    if (updatedRun.status === ExperimentRunStatus.COMPLETED) {
      await this.meetingBriefings
        .onExperimentRunCompleted(updatedRun)
        .catch((err: unknown) =>
          this.logger.error(
            { err, runId: updatedRun.runId },
            'onExperimentRunCompleted failed after run update',
          ),
        )
    }

    await this.campaignStrategy.onExperimentRunCompleted(updatedRun)

    return true
  }
```

If `updateMany` is not already exposed on `experimentRunsService`, use the existing model accessor it wraps (match how `resumeRun`/`sweepStaleRuns` call `this.model.updateMany`); the consumer should call through whatever public method the service offers.

- [ ] **Step 5: Run, verify pass**

Run: `cd packages/gp-api && npx vitest run src/queue/consumer/queueConsumer.service.test.ts`
Expected: PASS (all three).

- [ ] **Step 6: Confirm the stale sweep is unaffected**

`sweepStaleRuns` already filters `status: { in: [ExperimentRunStatus.RUNNING] }`. No change needed — `QUEUED` rows are not swept, and `RUNNING` now means "started executing." Add a one-line code comment above the filter only if the existing code lacks one explaining the RUNNING-only scope. Do not widen it to `QUEUED`.

- [ ] **Step 7: Commit**

```bash
cd packages/gp-api && git add src/queue && git commit -m "feat(gp-api): handle started callback (QUEUED->RUNNING); skip only terminal runs"
```

### Task 5: User-triggered dispatches get `HIGH` priority

**Files:**

- Modify: `packages/gp-api/src/meetings/services/meetingBriefings.service.ts` (`dispatchSchedule` ~267-283, `dispatchBriefing` ~285-328)
- Test: `packages/gp-api/src/meetings/services/meetingBriefings.service.test.ts` (extend; drive through the briefings dispatch route per the harness)

- [ ] **Step 1: Write the failing test**

Assert that a manual ("brief now") dispatch calls `dispatchRun` with `priority: 'HIGH'`. Spy on `experimentRuns.dispatchRun`.

```typescript
it("dispatches a manual briefing at HIGH priority", async () => {
  const dispatchRun = vi
    .spyOn(experimentRuns, "dispatchRun")
    .mockResolvedValue({ runId: "r1" } as any);
  await meetingBriefings.dispatchManual(electedOfficeId, "briefing", false);
  expect(dispatchRun).toHaveBeenCalledWith(
    expect.objectContaining({ priority: "HIGH" }),
  );
});
```

- [ ] **Step 2: Run, verify failure**

Run: `cd packages/gp-api && npx vitest run src/meetings/services/meetingBriefings.service.test.ts`
Expected: FAIL — no `priority` key passed.

- [ ] **Step 3: Add `priority: 'HIGH'` to both dispatch helpers**

In `dispatchSchedule`, add `priority: 'HIGH'` to the `dispatchRun` argument:

```typescript
await this.experimentRuns.dispatchRun({
  type: SCHEDULE_EXPERIMENT_TYPE,
  organizationSlug: ctx.organizationSlug,
  clerkUserId: ctx.clerkUserId,
  priority: "HIGH",
  params: {
    elected_office_id: ctx.electedOfficeId,
    state: ctx.state,
    office: ctx.positionName,
    ...(hint ? { known_schedule_location: hint } : {}),
  },
});
```

In `dispatchBriefing`, likewise add `priority: 'HIGH'` to the `dispatchRun` argument (alongside `type`, `organizationSlug`, `clerkUserId`).

Note: `dispatchSchedule`/`dispatchBriefing` are the helpers behind both the user "brief now" button (`dispatchManual`, gate off) and the daily imminence cron (`dispatchManual` with `useImminenceGate=true`). Marking all of these `HIGH` is acceptable for now — the high-volume bulk cohort path is the `dispatch-imminent-briefings.ts` script, which calls the API directly and will remain `DEFAULT` because it does not set priority. If later you want the daily cron at `DEFAULT`, thread a priority arg through `dispatchManual`; out of scope here.

- [ ] **Step 4: Run, verify pass**

Run: `cd packages/gp-api && npx vitest run src/meetings/services/meetingBriefings.service.test.ts`
Expected: PASS.

- [ ] **Step 5: Full gp-api verify gate**

Run the gp-api verify gate (via the `run-tests` skill). Expected: green.

- [ ] **Step 6: Commit**

```bash
cd packages/gp-api && git add src/meetings && git commit -m "feat(gp-api): dispatch user-triggered briefings at HIGH priority"
```

---

# Phase 2 — gp-ai-projects (the queue + scheduler)

All Phase 2 work happens in `/Users/smolster/Repos/thegoodparty/gp-ai-projects` on `feat/agent-priority-queue` (branched after PR #132 merges to develop). Use the pinned ruff (`v0.6.9`) and pre-commit gate; run `uv sync` first (see memory: agent-dispatch-infra for lint quirks).

### Task 6: DynamoDB job store module

**Files:**

- Create: `pmf_engine/control_plane/job_store.py`
- Test: `pmf_engine/tests/test_job_store.py`

Item shape for table `agent-job-queue-{env}` (hash key `run_id`):

| attribute                 | type | notes                                                                                                        |
| ------------------------- | ---- | ------------------------------------------------------------------------------------------------------------ |
| `run_id`                  | S    | hash key                                                                                                     |
| `status`                  | S    | `QUEUED` \| `LAUNCHING` \| `DISPATCHED` \| `FAILED`                                                          |
| `experiment_type`         | S    |                                                                                                              |
| `organization_slug`       | S    |                                                                                                              |
| `clerk_user_id`           | S    | optional                                                                                                     |
| `priority`                | S    | `HIGH` \| `DEFAULT`                                                                                          |
| `params`                  | S    | JSON string                                                                                                  |
| `routing`                 | S    | JSON: `{model, timeout_seconds, manifest_version_id, instruction_version_id, attachment_version_ids, scope}` |
| `prior_artifact_versions` | S    | JSON string, optional                                                                                        |
| `created_at`              | N    | epoch ms                                                                                                     |
| `attempts`                | N    | claim count                                                                                                  |
| `gsi_pk`                  | S    | constant `"QUEUED"` while queued; **removed** on claim (sparse GSI)                                          |
| `queue_sort`              | S    | `"{0\|1}#{created_at:013d}"` — `0`=HIGH, `1`=DEFAULT; removed on claim                                       |
| `ttl`                     | N    | epoch seconds; set on terminal/dispatched for cleanup                                                        |

GSI `queue-index`: hash `gsi_pk`, range `queue_sort`, projection ALL. Querying `gsi_pk="QUEUED"` ascending returns HIGH-before-DEFAULT, oldest-first. Sparse: only queued items carry `gsi_pk`/`queue_sort`, so the index holds exactly the waiting backlog.

- [ ] **Step 1: Write the failing test (put + query ordering)**

Use `moto` (already a dev dep: `moto[s3,sqs]` — add `dynamodb` to the extra in `pyproject.toml` `[dependency-groups].dev` as `moto[s3,sqs,dynamodb]`, then `uv sync`). Create the table with the GSI in a fixture.

```python
import json
import time

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
```

- [ ] **Step 2: Run, verify failure**

Run: `cd ~/Repos/thegoodparty/gp-ai-projects && uv run pytest pmf_engine/tests/test_job_store.py -q`
Expected: FAIL — `ModuleNotFoundError: pmf_engine.control_plane.job_store`.

- [ ] **Step 3: Implement `job_store.py`**

```python
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
        try:
            self._client.update_item(
                TableName=self._table,
                Key={"run_id": {"S": run_id}},
                UpdateExpression="SET #s = :launching, attempts = attempts + :one REMOVE gsi_pk, queue_sort",
                ConditionExpression="#s = :queued",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":launching": {"S": LAUNCHING},
                    ":queued": {"S": QUEUED},
                    ":one": {"N": "1"},
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

    def _set_terminal(self, run_id: str, status: str) -> None:
        ttl = int(time.time()) + _DISPATCHED_TTL_SECONDS
        self._client.update_item(
            TableName=self._table,
            Key={"run_id": {"S": run_id}},
            UpdateExpression="SET #s = :s, #t = :ttl REMOVE gsi_pk, queue_sort",
            ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
            ExpressionAttributeValues={":s": {"S": status}, ":ttl": {"N": str(ttl)}},
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
                json.loads(item["prior_artifact_versions"]["S"])
                if "prior_artifact_versions" in item
                else None
            ),
            created_at_ms=int(item["created_at"]["N"]),
            attempts=int(item["attempts"]["N"]),
        )
```

- [ ] **Step 4: Run, verify pass**

Run: `cd ~/Repos/thegoodparty/gp-ai-projects && uv run pytest pmf_engine/tests/test_job_store.py -q`
Expected: PASS.

- [ ] **Step 5: Add claim-conflict + limit tests**

```python
def test_claim_drops_job_from_queue_and_blocks_second_claim(store):
    store.put_queued_job(_job("r1", "HIGH", 1000))
    store.claim("r1")
    assert store.query_queued(limit=10) == []
    import pytest
    from pmf_engine.control_plane.job_store import JobClaimConflict
    with pytest.raises(JobClaimConflict):
        store.claim("r1")


def test_query_respects_limit(store):
    for i in range(5):
        store.put_queued_job(_job(f"r{i}", "DEFAULT", 1000 + i))
    assert len(store.query_queued(limit=3)) == 3
```

Run: `uv run pytest pmf_engine/tests/test_job_store.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/Repos/thegoodparty/gp-ai-projects && git add pmf_engine/control_plane/job_store.py pmf_engine/tests/test_job_store.py pyproject.toml uv.lock && git commit -m "feat(pmf-engine): DynamoDB job-queue store with priority-ordered query + conditional claim"
```

### Task 7: Extract `launch_run()` from the dispatch handler

This isolates the mint + `run_task` + error-callback block (currently inline in `handler()` after scope derivation, ~lines 621-733) so both the (soon-to-be-removed inline path and) the scheduler can call it. Pure refactor — behavior unchanged.

**Files:**

- Modify: `pmf_engine/control_plane/dispatch_handler.py`
- Test: `pmf_engine/tests/test_dispatch_handler.py` (existing suite must stay green)

- [ ] **Step 1: Add `launch_run()`**

Add a module-level function that takes the already-validated experiment routing + message fields and does mint → `run_task`, returning a result/raising. Model it exactly on the existing inline block (lines ~621-733), including `_cleanup_minted_token` on `run_task` failure and the `send_error_callback` calls. Signature:

```python
def launch_run(
    *,
    experiment: dict,
    message: dict,
    scope: dict,
    params_json: str,
) -> dict:
    """Mint a broker token and launch the Fargate task. Returns
    {"status": "launched", "task_arn": ...} on success, or
    {"status": "failed", "error": <user-safe>} when the run could not be
    launched (broker rejection, ECS RunTask failure). Raises on transient
    errors the caller should retry (httpx during mint, ECS RunTask exception)."""
```

Move the body verbatim from the inline block, replacing `continue`/`batch_item_failures.append` control flow with returns/raises:

- broker `BrokerError` → return `{"status": "failed", "error": e.user_safe_message or "Broker rejected the request"}`
- `httpx.HTTPError` during mint → `raise` (transient; caller retries)
- unexpected mint `Exception` → return `{"status": "failed", "error": f"Unexpected dispatch error: {type(e).__name__}"}`
- `run_task` returns failures / no tasks → `_cleanup_minted_token(...)`, return `{"status": "failed", "error": f"ECS RunTask failed: {safe_summary}"}`
- `run_task` raises → `_cleanup_minted_token(...)`, `raise`
- success → return `{"status": "launched", "task_arn": task_arn}`

- [ ] **Step 2: Keep the existing handler green via the new function**

Temporarily, `handler()` still calls the same logic — have it call `launch_run(...)` and translate the return into the existing `send_error_callback` + `batch_item_failures` behavior, so the full existing `test_dispatch_handler.py` suite still passes. (This inline call is removed in Task 8.)

- [ ] **Step 3: Run the existing suite**

Run: `cd ~/Repos/thegoodparty/gp-ai-projects && uv run pytest pmf_engine/tests/test_dispatch_handler.py -q`
Expected: PASS — same count as before the refactor (the suite is the behavior spec for this block).

- [ ] **Step 4: Commit**

```bash
git add pmf_engine/control_plane/dispatch_handler.py && git commit -m "refactor(pmf-engine): extract launch_run() for reuse by the scheduler"
```

### Task 8: Ingest writes `QUEUED` jobs instead of launching

**Files:**

- Modify: `pmf_engine/control_plane/dispatch_handler.py` (`handler()` terminal action + `parse_dispatch_message` priority)
- Test: `pmf_engine/tests/test_dispatch_handler.py`

- [ ] **Step 1: Write the failing test**

A valid message should now write a `QUEUED` job and **not** call `run_task`; validation failures still send `send_error_callback`. Patch `JobStore` and assert `put_queued_job` is called with the routing/scope/priority; assert ECS `run_task` is never called.

```python
@patch("pmf_engine.control_plane.dispatch_handler.get_job_store")
@patch("pmf_engine.control_plane.dispatch_handler.get_ecs_client")
def test_valid_message_enqueues_job_and_does_not_launch(self, mock_get_ecs, mock_get_store):
    store = mock_get_store.return_value
    event = _make_sqs_event({
        "experiment_type": "smoke_test",
        "organization_slug": "org-123",
        "run_id": "run-q1",
        "clerk_user_id": "user_test_dispatch",
        "priority": "HIGH",
        "params": dict(VALID_PARAMS),
    })
    result = handler(event, None)
    assert result["batchItemFailures"] == []
    store.put_queued_job.assert_called_once()
    job = store.put_queued_job.call_args.args[0]
    assert job.run_id == "run-q1"
    assert job.priority == "HIGH"
    assert job.routing["model"] == "sonnet"
    mock_get_ecs.return_value.run_task.assert_not_called()


def test_parse_defaults_priority_to_default(self):
    body = {"experiment_type": "smoke_test", "organization_slug": "o", "run_id": "r",
            "clerk_user_id": "u"}
    assert parse_dispatch_message(json.dumps(body))["priority"] == "DEFAULT"
```

- [ ] **Step 2: Run, verify failure**

Run: `uv run pytest pmf_engine/tests/test_dispatch_handler.py -k "enqueues_job or priority_to_default" -q`
Expected: FAIL — `get_job_store` undefined; `run_task` still called.

- [ ] **Step 3: Parse/validate priority**

In `parse_dispatch_message`, after the `clerk_user_id` check, add:

```python
    priority = data.get("priority", "DEFAULT")
    if priority not in ("HIGH", "DEFAULT"):
        raise ValueError("priority must be 'HIGH' or 'DEFAULT'")
    data["priority"] = priority
```

- [ ] **Step 4: Add a process-cached job store accessor + env var**

Near `get_ecs_client`:

```python
JOB_TABLE_NAME = os.environ.get("JOB_TABLE_NAME", "")

_job_store = None


def get_job_store():
    global _job_store
    if _job_store is None:
        from .job_store import JobStore  # local import keeps cold-start lean
        _job_store = JobStore(JOB_TABLE_NAME)
    return _job_store


def reset_job_store_for_tests() -> None:
    global _job_store
    _job_store = None
```

Add `JOB_TABLE_NAME` to `_missing_critical_config()`.

- [ ] **Step 5: Replace the terminal action in `handler()`**

After scope derivation succeeds (where Task 7 left the `launch_run` call), replace that call with a job write. Build the routing blob from the resolved `experiment` and enqueue:

```python
        import time as _time
        from .job_store import QueuedJob

        routing = {
            "model": experiment["model"],
            "timeout_seconds": experiment.get("timeout_seconds", 600),
            "manifest_version_id": experiment.get("manifest_version_id"),
            "instruction_version_id": experiment.get("instruction_version_id"),
            "attachment_version_ids": experiment.get("attachment_version_ids"),
            "scope": scope,
        }
        try:
            get_job_store().put_queued_job(
                QueuedJob(
                    run_id=message["run_id"],
                    experiment_type=experiment_id,
                    organization_slug=message["organization_slug"],
                    clerk_user_id=message.get("clerk_user_id"),
                    priority=message["priority"],
                    params=message["params"],
                    routing=routing,
                    prior_artifact_versions=message.get("prior_artifact_versions"),
                    created_at_ms=int(_time.time() * 1000),
                )
            )
        except Exception as e:
            logger.exception(f"Failed to enqueue job for run {message['run_id']}: {e}")
            emit_dispatch_metric("JobEnqueueFailed", experiment_id)
            batch_item_failures.append({"itemIdentifier": message_id})
            continue
        emit_dispatch_metric("JobEnqueued", experiment_id)
        # Arrival is picked up by the scheduler via the table's DynamoDB stream;
        # no explicit invoke needed here.
```

Remove the now-dead `launch_run` call and the `params_json`/mint locals that only fed it (keep `params_json` if still used by the size check). Keep all earlier validation/error-callback branches exactly as-is.

- [ ] **Step 6: Run, verify pass + whole suite**

Run: `uv run pytest pmf_engine/tests/test_dispatch_handler.py -q`
Expected: PASS. Update any existing tests that asserted `run_task` was called on the happy path — they now assert `put_queued_job` instead (the launch behavior is covered by `test_scheduler_handler.py` in Task 9).

- [ ] **Step 7: Commit**

```bash
git add pmf_engine/control_plane/dispatch_handler.py pmf_engine/tests/test_dispatch_handler.py && git commit -m "feat(pmf-engine): ingest enqueues QUEUED jobs to DynamoDB instead of launching"
```

### Task 9: The scheduler Lambda

**Files:**

- Create: `pmf_engine/control_plane/scheduler_handler.py`
- Test: `pmf_engine/tests/test_scheduler_handler.py`

Behavior per invocation: count RUNNING Fargate tasks (paginated, `desiredStatus="RUNNING"` — this counts PROVISIONING/PENDING too, so a just-launched task is counted on the next tick) → `slots = MAX_CONCURRENT_AGENTS - running` → if `slots <= 0` return → `query_queued(limit=slots)` → for each: `claim()` (skip on `JobClaimConflict`) → `launch_run()` → on `launched` send `started` callback + `mark_dispatched`; on `failed` send failed callback + `mark_failed`; on transient raise, leave the job `LAUNCHING` (a later sweep or manual requeue handles it — log loudly). Reserved concurrency 1 (Terraform) guarantees only one scheduler runs, so the local slot countdown is exact.

- [ ] **Step 1: Write the failing test (respects cap + priority)**

```python
import json
from unittest.mock import MagicMock, patch

import pytest

import pmf_engine.control_plane.scheduler_handler as sched
from pmf_engine.control_plane.job_store import QueuedJob


def _job(run_id, priority):
    return QueuedJob(
        run_id=run_id, experiment_type="smoke_test", organization_slug="org-1",
        clerk_user_id="user_1", priority=priority, params={"state": "WI"},
        routing={"model": "sonnet", "timeout_seconds": 600, "scope": {},
                 "manifest_version_id": None, "instruction_version_id": None,
                 "attachment_version_ids": None},
        prior_artifact_versions=None, created_at_ms=1000,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(sched, "MAX_CONCURRENT_AGENTS", 3, raising=False)
    monkeypatch.setattr(sched, "RESULTS_QUEUE_URL", "https://sqs/cb.fifo", raising=False)


@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
def test_launches_up_to_free_slots(self_count, mock_store, mock_sqs, mock_launch):
    # count is the first positional patch -> bind by name instead
    pass
```

Replace the stub with the real arrangement (the decorator order binds bottom-up):

```python
@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_launches_up_to_free_slots(mock_launch, mock_sqs, mock_store, mock_count):
    mock_count.return_value = 1  # cap 3 -> 2 slots
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r-high", "HIGH"), _job("r-def", "DEFAULT")]
    mock_launch.return_value = {"status": "launched", "task_arn": "arn:task/x"}

    sched.handler({}, None)

    store.query_queued.assert_called_once_with(limit=2)
    assert mock_launch.call_count == 2
    assert store.claim.call_count == 2
    assert store.mark_dispatched.call_count == 2
    # started callback sent for each launched run
    assert mock_sqs.return_value.send_message.call_count == 2
    body = json.loads(mock_sqs.return_value.send_message.call_args_list[0].kwargs["MessageBody"])
    assert body["data"]["status"] == "started"


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
def test_no_launch_when_at_cap(mock_store, mock_count):
    mock_count.return_value = 3  # cap 3 -> 0 slots
    sched.handler({}, None)
    mock_store.return_value.query_queued.assert_not_called()


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_skips_jobs_lost_to_claim_race(mock_launch, mock_sqs, mock_store, mock_count):
    from pmf_engine.control_plane.job_store import JobClaimConflict
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r1", "HIGH")]
    store.claim.side_effect = JobClaimConflict("r1")
    sched.handler({}, None)
    mock_launch.assert_not_called()


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_failed_launch_sends_failed_callback_and_marks_failed(mock_launch, mock_sqs, mock_store, mock_count):
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r1", "HIGH")]
    mock_launch.return_value = {"status": "failed", "error": "Broker rejected the request"}
    sched.handler({}, None)
    store.mark_failed.assert_called_once_with("r1")
    body = json.loads(mock_sqs.return_value.send_message.call_args.kwargs["MessageBody"])
    assert body["data"]["status"] == "failed"
```

- [ ] **Step 2: Run, verify failure**

Run: `uv run pytest pmf_engine/tests/test_scheduler_handler.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `scheduler_handler.py`**

```python
from __future__ import annotations

import json
import os

import boto3

try:
    from shared.logger import get_logger

    logger = get_logger(__name__)
except (ImportError, OSError):
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    from .dispatch_handler import launch_run, get_sqs_client, emit_dispatch_metric
    from .job_store import JobStore, JobClaimConflict
except ImportError:  # Lambda flat-package import
    from dispatch_handler import launch_run, get_sqs_client, emit_dispatch_metric  # type: ignore[no-redef]
    from job_store import JobStore, JobClaimConflict  # type: ignore[no-redef]

MAX_CONCURRENT_AGENTS = int(os.environ.get("MAX_CONCURRENT_AGENTS", "0") or 0)
ECS_CLUSTER_ARN = os.environ.get("ECS_CLUSTER_ARN", "")
RESULTS_QUEUE_URL = os.environ.get("RESULTS_QUEUE_URL", "")
JOB_TABLE_NAME = os.environ.get("JOB_TABLE_NAME", "")

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


def _send_callback(run_id: str, status: str, *, experiment_id: str = "unknown",
                   organization_slug: str = "unknown", error: str | None = None) -> None:
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


def handler(event, context) -> dict:
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

    store = get_job_store()
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
            # so a stuck-LAUNCHING sweep / manual requeue must recover it.
            logger.exception(f"launch_run raised for {job.run_id} ({type(e).__name__}); left LAUNCHING")
            emit_dispatch_metric("SchedulerLaunchTransient", job.experiment_type)
            continue

        if result["status"] == "launched":
            store.mark_dispatched(job.run_id)
            _send_callback(job.run_id, "started",
                           experiment_id=job.experiment_type,
                           organization_slug=job.organization_slug)
            launched += 1
        else:
            store.mark_failed(job.run_id)
            _send_callback(job.run_id, "failed",
                           experiment_id=job.experiment_type,
                           organization_slug=job.organization_slug,
                           error=result.get("error", "dispatch failed"))

    logger.info(f"scheduler launched {launched}/{slots} (running was {running})")
    return {"launched": launched}
```

Note: if PR #132 was abandoned, `launch_run` and the `ecs:ListTasks` IAM grant won't exist on develop — Task 7 still creates `launch_run`, but you must also confirm `count_running_tasks` here is the canonical copy (it is) and that Task 11 adds the IAM grant (it does).

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest pmf_engine/tests/test_scheduler_handler.py -q`
Expected: PASS (all four cases).

- [ ] **Step 5: Add a stuck-`LAUNCHING` sweep test + implementation**

A job left `LAUNCHING` by a transient failure must eventually fail (and notify gp-api) rather than leak. Add to `job_store.py` a `query_stuck_launching(older_than_ms)` (a small GSI on `status` is overkill at this volume — instead store a `claimed_at` and do a bounded `Scan` with a filter, which is fine for hundreds of rows; document the bound) and have the scheduler, at the end of `handler()`, fail any `LAUNCHING` job older than e.g. 10 minutes via `mark_failed` + failed callback. Keep it simple; cap the scan.

```python
def test_sweeps_stuck_launching_jobs(...):
    # query_stuck_launching returns one job claimed >10m ago
    # assert handler marks it failed + sends failed callback
```

Implement `query_stuck_launching` and wire it into `handler()` before the return. Run the test → PASS.

- [ ] **Step 6: Commit**

```bash
git add pmf_engine/control_plane/scheduler_handler.py pmf_engine/control_plane/job_store.py pmf_engine/tests/test_scheduler_handler.py pmf_engine/tests/test_job_store.py && git commit -m "feat(pmf-engine): scheduler Lambda — slot-gated priority dispatch with started/failed callbacks"
```

### Task 10: Package the new modules into the Lambda build

**Files:**

- Modify: `pmf_engine/scripts/build_lambda_package.sh`

- [ ] **Step 1: Copy the new modules**

After the existing `cp "$PMF_DIR/control_plane/jsonschema_errors.py" "$OUTPUT_DIR/"` line, add:

```bash
cp "$PMF_DIR/control_plane/job_store.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/scheduler_handler.py" "$OUTPUT_DIR/"
```

`pydantic` is a transitive runtime need of `job_store.py`. Add it to the vendored deps in the same `pip install` block (it ships manylinux wheels):

```bash
python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  --target "$OUTPUT_DIR" \
  --upgrade \
  httpx \
  jsonschema \
  pydantic \
  >/dev/null
```

(Confirm whether `broker_client.py` already pulls `pydantic` into the package via another path; if the broker package vendors it, mirror that. The flat-package import in `scheduler_handler.py` (`from job_store import ...`) matches how `dispatch_handler.py` already falls back to flat imports.)

- [ ] **Step 2: Build + smoke-check the package**

Run:

```bash
cd ~/Repos/thegoodparty/gp-ai-projects && bash pmf_engine/scripts/build_lambda_package.sh /tmp/lambda_build_check
ls /tmp/lambda_build_check/scheduler_handler.py /tmp/lambda_build_check/job_store.py
python3 -c "import sys; sys.path.insert(0, '/tmp/lambda_build_check'); import scheduler_handler, job_store; print('imports OK')"
```

Expected: both files listed; `imports OK` printed.

- [ ] **Step 3: Commit**

```bash
git add pmf_engine/scripts/build_lambda_package.sh && git commit -m "build(pmf-engine): package job_store + scheduler_handler into the Lambda bundle"
```

### Task 11: Terraform — table, scheduler Lambda, triggers, IAM; remove #132 deferral

**Files:**

- Modify: `infrastructure/modules/pmf-engine-control-plane/main.tf`

- [ ] **Step 1: Add the job-queue table (GSI + Streams)**

After the dispatch queue resources, add:

```hcl
resource "aws_dynamodb_table" "job_queue" {
  name         = "agent-job-queue-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }
  attribute {
    name = "gsi_pk"
    type = "S"
  }
  attribute {
    name = "queue_sort"
    type = "S"
  }

  global_secondary_index {
    name            = "queue-index"
    hash_key        = "gsi_pk"
    range_key       = "queue_sort"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  stream_enabled   = true
  stream_view_type = "KEYS_ONLY"

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = var.environment
  }
}
```

(`KEYS_ONLY` is enough — the scheduler re-queries the table by priority; it does not consume stream record contents, only uses the event as a wake-up.)

- [ ] **Step 2: Add `JOB_TABLE_NAME` to the ingest (dispatch) Lambda env**

In `aws_lambda_function.dispatch`'s `environment.variables`, add:

```hcl
      JOB_TABLE_NAME = aws_dynamodb_table.job_queue.name
```

- [ ] **Step 3: Grant the ingest Lambda write to the table**

In `aws_iam_role_policy.dispatch_lambda_permissions`, add a statement:

```hcl
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.job_queue.arn
      },
```

- [ ] **Step 4: Remove the #132 deferral machinery**

The queue no longer backs up (ingest writes to Dynamo and returns), so:

- In `aws_sqs_queue.dispatch.redrive_policy`, set `maxReceiveCount` back to `5` (a normal poison-message threshold; it was 3 pre-#132, 30 in #132 — 5 is the sensible steady-state value now that messages don't sit at-cap). Remove the 30-receive comment.
- In `aws_lambda_event_source_mapping.dispatch_sqs`, remove the `scaling_config { maximum_concurrency = 2 }` block and its comment — the ingest path no longer launches tasks, so there is no check-then-launch race to bound.
- Remove `sqs:ChangeMessageVisibility` from the dispatch Lambda IAM (added in #132) — ingest no longer defers.

- [ ] **Step 5: Add the scheduler Lambda**

The scheduler reuses the **same** package archive as the dispatch Lambda (`data.archive_file.dispatch_lambda`), with a different handler entrypoint and reserved concurrency 1:

```hcl
resource "aws_iam_role" "scheduler_lambda_role" {
  name = "pmf-engine-scheduler-lambda-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "scheduler_basic" {
  role       = aws_iam_role.scheduler_lambda_role.id
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "scheduler_lambda_permissions" {
  name = "scheduler-permissions"
  role = aws_iam_role.scheduler_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:Scan", "dynamodb:GetItem"]
        Resource = [aws_dynamodb_table.job_queue.arn, "${aws_dynamodb_table.job_queue.arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
        Resource = "${aws_dynamodb_table.job_queue.arn}/stream/*"
      },
      {
        Effect   = "Allow"
        Action   = "ecs:ListTasks"
        Resource = "*"
        Condition = { ArnEquals = { "ecs:cluster" = var.ecs_cluster_arn } }
      },
      {
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${var.ecs_task_definition_family}:*"
        Condition = { ArnEquals = { "ecs:cluster" = var.ecs_cluster_arn } }
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [var.ecs_task_execution_role_arn, var.ecs_task_role_arn]
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.results.arn
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "PMFEngine" } }
      },
    ]
  })
}

resource "aws_lambda_function" "scheduler" {
  function_name    = "pmf-engine-scheduler-${var.environment}"
  filename         = data.archive_file.dispatch_lambda.output_path
  source_code_hash = data.archive_file.dispatch_lambda.output_base64sha256
  handler          = "scheduler_handler.handler"
  runtime          = "python3.13"
  role             = aws_iam_role.scheduler_lambda_role.arn
  timeout          = 120
  memory_size      = 256

  reserved_concurrent_executions = 1

  environment {
    variables = {
      ENVIRONMENT           = var.environment
      ECS_CLUSTER_ARN       = var.ecs_cluster_arn
      ECS_TASK_DEFINITION   = var.ecs_task_definition_family
      ECS_SUBNET_IDS        = join(",", var.ecs_subnet_ids)
      ECS_SECURITY_GROUP_ID = var.ecs_security_group_id
      CONTAINER_NAME        = "pmf-engine"
      BROKER_URL            = var.broker_url
      RESULTS_QUEUE_URL     = aws_sqs_queue.results.url
      SERVICE_TOKEN         = try(jsondecode(data.aws_secretsmanager_secret_version.service_tokens.secret_string)["SERVICE_TOKEN"], "")
      JOB_TABLE_NAME        = aws_dynamodb_table.job_queue.name
      MAX_CONCURRENT_AGENTS = tostring(var.max_concurrent_agents)
    }
  }

  vpc_config {
    subnet_ids         = var.ecs_subnet_ids
    security_group_ids = [aws_security_group.dispatch_lambda.id]
  }

  tags = {
    Environment = var.environment
  }
}
```

(The scheduler needs broker egress — it mints tokens — so it reuses the dispatch Lambda's security group, which already has the broker egress rule. Confirm that SG's egress covers the broker before relying on it; if the broker SG ingress is keyed to a specific Lambda SG, add the scheduler's ENI to the same SG, as done here.)

- [ ] **Step 6: Wire the two triggers**

```hcl
resource "aws_lambda_event_source_mapping" "scheduler_stream" {
  event_source_arn  = aws_dynamodb_table.job_queue.stream_arn
  function_name     = aws_lambda_function.scheduler.arn
  starting_position = "LATEST"
  batch_size        = 100
  # Coalesce a burst of inserts into one scheduler run rather than one-per-row.
  maximum_batching_window_in_seconds = 1
  enabled           = true
}

resource "aws_cloudwatch_event_rule" "scheduler_tick" {
  name                = "pmf-engine-scheduler-tick-${var.environment}"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "scheduler_tick" {
  rule = aws_cloudwatch_event_rule.scheduler_tick.name
  arn  = aws_lambda_function.scheduler.arn
}

resource "aws_lambda_permission" "scheduler_tick" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scheduler_tick.arn
}
```

Note on reserved concurrency 1 + two sources: when both fire together, one runs and the other is throttled and retried (stream blocks its shard until it succeeds; EventBridge async-retries). Both are idempotent — the scheduler just re-reads slot/queue state — so this is safe. `rate(1 minute)` is EventBridge's floor; arrival latency comes from the stream (seconds), not the tick.

- [ ] **Step 7: Validate Terraform**

Run:

```bash
cd ~/Repos/thegoodparty/gp-ai-projects && terraform -chdir=infrastructure/modules/pmf-engine-control-plane fmt && \
terraform -chdir=infrastructure/environments/dev/pmf-engine-control-plane init -backend=false && \
terraform -chdir=infrastructure/environments/dev/pmf-engine-control-plane validate
```

Expected: `fmt` clean, `validate` succeeds. (No default vars changed, so no new wiring needed in the env stacks — `max_concurrent_agents` already flows from the module default; override per-env in `terraform.tfvars` if desired.)

- [ ] **Step 8: Commit**

```bash
git add infrastructure/modules/pmf-engine-control-plane/main.tf && git commit -m "feat(infra): job-queue table + scheduler Lambda (stream + 1-min tick, reserved concurrency 1); drop SQS deferral"
```

### Task 12: Docs + memory

**Files:**

- Modify: nearest doc — `pmf_engine/README.md` or `docs/architecture.md` (whichever documents the dispatch flow)
- Modify: `/Users/smolster/.claude/projects/-Users-smolster-Repos-thegoodparty-omni/memory/agent-dispatch-infra.md`

- [ ] **Step 1: Update the architecture doc**

Replace the "dispatch Lambda launches one task per message" description with the two-stage flow: ingest (SQS → validate → write QUEUED job to `agent-job-queue-{env}`) and scheduler (stream/tick → count slots → priority query → claim → mint → run_task → `started`/`failed` callback). Document the `HIGH`/`DEFAULT` priority, the exact-cap guarantee (reserved concurrency 1 + ECS count), and the `QUEUED`→`RUNNING`→terminal status flow with the `started` callback.

- [ ] **Step 2: Update the memory note**

Update `agent-dispatch-infra.md` to reflect the queue+scheduler architecture, the new table/Lambda names, and that PR #132's deferral was superseded.

- [ ] **Step 3: Commit**

```bash
git add docs/ pmf_engine/README.md 2>/dev/null; git commit -m "docs(pmf-engine): document the priority job queue + scheduler"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** Priority ordering → Task 6 (GSI sort) + Task 9 (query). `HIGH`/`DEFAULT` only (no numbers) → Task 3/5/6/8. Exact cap → Task 9 + Task 11 reserved concurrency 1. Arrival latency under the 1-min floor → Task 11 DynamoDB stream. Stale-sweep correctness → Task 4 (`started` + `QUEUED`). Backward-compatible ship order → Phase 1 first (relaxed guard accepts `QUEUED`→terminal even with the old Lambda). PR #132 reconciliation → Preconditions + Task 11 Step 4.

**Placeholder scan:** Two deliberate "confirm X" notes remain where the codebase must be checked at execution time (contracts generator ownership in Task 1; whether `pydantic`/broker SG are already vendored/wired in Tasks 10/11). These are verification instructions, not unfinished code — the surrounding steps specify the concrete change either way.

**Type consistency:** `QueuedJob`/`JobStore`/`JobClaimConflict`/`launch_run`/`count_running_tasks`/`get_job_store`/`put_queued_job`/`query_queued`/`claim`/`mark_dispatched`/`mark_failed` are used identically across Tasks 6–11. `DispatchPriority` (`'HIGH'|'DEFAULT'`) is consistent across gp-api Tasks 3/5. `started` status value is consistent across gp-api Task 4 and gp-ai-projects Task 9.
