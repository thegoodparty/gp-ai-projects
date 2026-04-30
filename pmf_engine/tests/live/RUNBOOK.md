# Live-Dev Smoke Runbook

Operator runbook for `pmf_engine/tests/live/test_live_smoke.py`. This test dispatches real experiments against the deployed dev PMF stack and asserts the artifact lands in S3. It is the **final gate before merging** any change to the broker, the runner, the dispatch Lambda, the Terraform for any of those, or the agent-experiments contract.

Paired with the fast in-process smoke at `pmf_engine/tests/smoke/`. Run order: in-process first (1.2s, catches code-wiring bugs) → live-dev second (7–15min per experiment post-fix baseline, up to 30min under load).

## The critic-fix-smoke loop

For risky changes (broker, dispatch, runner), iterate in a tight loop until signal is clean:

1. Edit → in-process smoke green (<2s) — catches obvious wiring breaks
2. `~/ppv-apps/ai-rules/` critics on the diff — catches logic/security/lifecycle issues
3. Fix critic findings via red/green TDD — in-process smoke stays green
4. **Deploy to dev** (PR merge OR direct Docker push — see "Direct deploy" below)
5. **Live smoke on dev** — catches the IaC/infra-drift issues the moto harness can't see
6. **Re-run critics** — on the fixed branch. Did the fixes introduce new issues?
7. **Repeat from step 3** until critics find zero CRITICAL and zero HIGH

Stopping criterion: zero CRITICAL/HIGH findings from critics AND both live smokes green. LOW/MEDIUM become a tracked backlog (`ai-rules/` can always find improvement items; chasing them to zero is infeasible).

Real example from 2026-04-23: Round 1 critics flagged 14 CRITICAL. Round 1 fixes landed. Round 2 critics caught 2 residual CRITICAL that the Group A subagents missed (incomplete `delete_ticket` swap, unwrapped initial `report_status`). Round 2 fixes landed. Round 2 live smoke passed. That's a complete loop.

---

## What it proves

Starting at gp-api's handoff boundary (the dispatch SQS queue), it proves the PMF spine is alive end-to-end:

```
SQS agent-dispatch-dev.fifo
  → dispatch Lambda (mint + RunTask)
     → Fargate runner (Claude SDK agent)
        → broker auth → endpoint calls (http/databricks)
           → broker contract validation → S3 artifact upload
              → s3://gp-agent-artifacts-dev/{experiment_id}/{run_id}/artifact.json
```

Success signal = S3 `head_object` returns 200 at that key. That is the single objective fact this test asserts; everything else is diagnostic output.

**What it catches that the in-process moto smoke can't:**
- IAM drift at every hop
- Security groups, VPC, egress, DNS
- Terraform env-var drift
- Real Anthropic streaming through the broker proxy
- Real Claude SDK harness vs. broker contract
- Real Databricks SQL + scope rewriter against live tables
- Real S3 write permissions and bucket policies

**What it does NOT check:**
- gp-api's REST layer / autoParams logic (we start downstream)
- gp-api's queue consumer / DB row update (gp-api logs "run not found" and ACKs — harmless side effect)
- Cost regressions (runs are ~$0.30–$0.60; operator watches the bill)
- Artifact content quality (contract validation passed = structurally valid; factual quality is the model's job)

---

## Prerequisites

1. **AWS creds for dev** — `AWS_PROFILE=work` is the assumed setup per workspace convention. The profile needs:
   - `sqs:SendMessage` on `arn:aws:sqs:us-west-2:333022194791:agent-dispatch-dev.fifo`
   - `s3:GetObject` on `arn:aws:s3:::gp-agent-artifacts-dev/*`
   - `ecs:DescribeTasks` + `logs:TailLogStream` (optional — only for diagnostics on failure)
2. **Deploy is reachable** — broker ECS service running in dev, dispatch Lambda healthy, ANTHROPIC + Databricks secrets valid.
3. **Triple-lock unlocked:** set `LIVE_SMOKE_ENABLE=1` and use `-m live_dev`. Without both, tests skip. There is no way to fire this by accident from a clean env.

---

## Running

**Both experiments in parallel** (recommended — truly independent Fargate tasks):

```bash
cd ~/work/gp-ai-projects

# Tab 1
LIVE_SMOKE_ENABLE=1 AWS_PROFILE=work \
  uv run pytest pmf_engine/tests/live/ -m live_dev -k district_intel -v -s

# Tab 2
LIVE_SMOKE_ENABLE=1 AWS_PROFILE=work \
  uv run pytest pmf_engine/tests/live/ -m live_dev -k voter_targeting -v -s
```

**Serial run** (one at a time, roughly doubles wall clock):

```bash
LIVE_SMOKE_ENABLE=1 AWS_PROFILE=work \
  uv run pytest pmf_engine/tests/live/ -m live_dev -v -s
```

**Timeout:** default 30 min per experiment. Override with `LIVE_SMOKE_TIMEOUT_MINUTES=45` if dev is backlogged or the agent is slow.

---

## What good looks like

Banner at the start confirms the kill switch:

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
! LIVE_SMOKE_ENABLE=1 — live-dev smoke tests are UNLOCKED.       !
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

Per-test output:

```
[live smoke] experiment=district_intel env=dev org=smoke-test-pmf
[live smoke] run_id=d188bc17-87bd-4fe0-9b45-d34d3b301d98
[live smoke] dispatch queue: https://sqs.us-west-2.amazonaws.com/333022194791/agent-dispatch-dev.fifo
[live smoke] target S3 key:  s3://gp-agent-artifacts-dev/district_intel/d188bc17-.../artifact.json
[live smoke] SQS message sent — MessageId=...
[s3] still waiting for s3://... (t+60s)
[s3] still waiting for s3://... (t+120s)
[s3] artifact landed after 28 polls (t+420s)
[live smoke] artifact OK — top-level keys: ['demographic_snapshot', 'district', 'generated_at', 'issues', 'methodology', 'office', 'official_name', 'summary']
PASSED
```

Known timings (pmf-wip-v2 post-fix, 2026-04-23):

| Experiment | Wall clock | Artifact | Notes |
|---|---|---|---|
| voter_targeting | 7:09 | ~10.6 MB | Full L2 Databricks queries + voter segment build |
| district_intel | 12:29 | ~10 KB | Web research + issue analysis; mostly prose |

Expect ±30% variance depending on Anthropic latency and Databricks warehouse warm/cold state. Bucket: **under 10 min = fast**, **10–20 min = normal**, **>20 min = probably a real issue — check CloudWatch**.

---

## Direct deploy to dev (bypass PR merge)

When you want to test fixes on dev BEFORE merging PR to `develop` — e.g., running live smoke on a feature branch to validate before asking for review. The GH Actions workflows only fire on push to `develop/qa/prod`, so this path builds + pushes images locally.

```bash
# Prereqs: Docker running (Colima/Docker Desktop), AWS_PROFILE=work valid, ECR access
AWS_PROFILE=work aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin 333022194791.dkr.ecr.us-west-2.amazonaws.com

# 1. Build + push broker (arm64, ~3-5min)
docker buildx build --platform linux/arm64 \
  -f broker/Dockerfile \
  -t 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:broker-dev \
  --push .

# 2. Build + push pmf-engine runner (arm64, ~3-5min; can run in parallel with #1)
docker buildx build --platform linux/arm64 \
  -f pmf_engine/Dockerfile \
  -t 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:pmf-engine-dev \
  --push .

# 3. Roll broker ECS (brings up new task with fresh image)
AWS_PROFILE=work AWS_REGION=us-west-2 aws ecs update-service \
  --cluster broker-dev --service broker-dev \
  --force-new-deployment

# 4. Build + deploy dispatch Lambda
bash pmf_engine/scripts/build_lambda_package.sh
cd pmf_engine/.lambda_build \
  && zip -r /tmp/dispatch_lambda.zip . -x "*.pyc" "__pycache__/*" \
  && cd -
AWS_PROFILE=work AWS_REGION=us-west-2 aws lambda update-function-code \
  --function-name pmf-engine-dispatch-dev \
  --zip-file fileb:///tmp/dispatch_lambda.zip
```

**Post-deploy verification (before running live smoke):**

```bash
# Broker rollout must be COMPLETED — new task is live
AWS_PROFILE=work aws ecs describe-services \
  --cluster broker-dev --services broker-dev \
  --query 'services[0].deployments[0].{rollout:rolloutState,running:runningCount,desired:desiredCount}' \
  --output table

# Lambda CodeSha256 should match your new zip (sanity check)
AWS_PROFILE=work aws lambda get-function-configuration \
  --function-name pmf-engine-dispatch-dev \
  --query '{hash:CodeSha256,modified:LastModified,size:CodeSize}'

# PMF-engine image digest in ECR (next Fargate RunTask will pull this)
AWS_PROFILE=work aws ecr describe-images \
  --repository-name gp-ai-projects \
  --image-ids imageTag=pmf-engine-dev \
  --query 'imageDetails[0].{pushed:imagePushedAt,digest:imageDigest}'
```

**Note on the broker hostname:** `broker-dev.ai.goodparty.org` resolves to an **internal ALB** — it's not reachable from operator laptops (expect `curl` to timeout). Don't diagnose this as the broker being down. Only in-VPC clients (Fargate, Lambda) reach it. Verify broker health indirectly via: (a) ECS rollout reaching `COMPLETED`, or (b) live smoke succeeding (the smoke is the health check).

After verification, run live smoke (next section).

---

## Failure modes

### S3 artifact never lands (within timeout)

The test writes the following diagnostics block on timeout. **Read it in order** — the first non-empty log locates the break.

```
diagnostics (pick whichever hop you suspect):
  - dispatch Lambda: aws logs tail /aws/lambda/pmf-engine-dispatch-dev --since 30m | grep {run_id}
  - Fargate agent:   aws logs tail /aws/ecs/pmf-engine-dev --since 30m | grep {run_id}
  - broker:          aws logs tail /aws/ecs/pmf-broker-dev --since 30m | grep {run_id}
```

**Decision tree:**

- **No dispatch-Lambda log line for your run_id** → the SQS message didn't reach the Lambda. Check:
  - `aws sqs get-queue-attributes --queue-url .../agent-dispatch-dev.fifo --attribute-names ApproximateNumberOfMessages` — if > 0, Lambda isn't consuming (misconfig or concurrency limit).
  - `aws lambda get-function --function-name pmf-engine-dispatch-dev` — check `State: Active`.
- **Dispatch logs show "Broker rejected"** → broker mint endpoint is failing. Hit `broker-dev.ai.goodparty.org/health`. Check ECS service status for `pmf-broker-dev`.
- **Dispatch logs show "ECS RunTask failed"** → ECS capacity / IAM / task-def drift. `aws ecs describe-tasks --cluster pmf-engine-dev --tasks ...` for the last failed task; `stoppedReason` tells you why.
- **Fargate task started but no broker calls in broker logs** → agent can't reach the broker. Check Fargate SG → broker ALB SG path, `BROKER_URL` env var in the task def.
- **Broker logs show 401 / scope_ticket_missing** → broker_token regression (CRITICAL #1 or #5 territory). Check `broker-scope-tickets-dev` DynamoDB for the ticket; verify `exp` is in the future.
- **Broker logs show publish 500 / S3 error** → S3 write IAM broken, or `ARTIFACT_BUCKET` env var mismatch.
- **Agent finishes but contract_violation callback** → artifact shape changed. Check `rejected/{run_id}.json` in S3 for the rejected artifact.

### Test skipped when you expected it to run

You missed a lock. In order of how easily forgotten:

| Symptom | Fix |
|---|---|
| `2 skipped in 0.05s` with no banner | `LIVE_SMOKE_ENABLE=1` not set |
| `2 skipped` WITH banner | `-m live_dev` not passed (or passed as part of a compound expression; the guard requires exact match for safety) |
| Skipped with "AWS credentials not available" | `AWS_PROFILE=work` not set (or profile expired — run `aws sts get-caller-identity --profile work`) |

### Test times out despite obvious progress in CloudWatch

Agent may genuinely be slow (Anthropic 5xx retries, Databricks cold warehouse). Re-run **once** before treating it as a code regression — transient upstream failures are real. If it fails a second time, it's the code.

### One smoke passes, the other fails

Usually an experiment-specific regression. district_intel uses `/http/fetch` (SSRF guard, redirect loop) — if it fails while voter_targeting passes, look at `http_fetch.py` changes. voter_targeting uses `/databricks/query` (SQL rewriter, `DataQueryTracker`) — if it fails while district_intel passes, look at `databricks_query.py` + `sql_rewriter.py`. Both use `/artifact/publish`, `/internal/run-status`, mint, DDB lock — if both fail, the regression is in the shared spine.

---

## Side effects

Each run leaves traces on dev. None are load-bearing; none need cleanup unless storage cost matters.

| Where | What | Cleanup |
|---|---|---|
| `agent-dispatch-dev.fifo` | Message consumed by Lambda | Automatic (nothing to clean) |
| `broker-scope-tickets-dev` DynamoDB | Ticket row created, deleted by broker on terminal status; otherwise TTL sweeps in ~1hr | Automatic |
| `gp-agent-artifacts-dev` S3 | `{experiment_id}/{run_id}/artifact.json` + `latest.json` overwritten | Accumulates; prune with `aws s3 rm --recursive` if desired |
| `agent-results-dev.fifo` | Callback message queued by broker | gp-api consumer ACKs with "Experiment run not found" (harmless log line) |
| gp-api dev DB | **No new `ExperimentRun` row** — the test skips gp-api's REST layer | Nothing to clean |

---

## When to run this

- **Before merging** a change to: broker, pmf_engine/runner, pmf_engine/control_plane, pmf_engine/runner/experiments/*, infrastructure/modules/broker, infrastructure/modules/pmf-engine-*, any `.github/workflows/build-broker.yml` or `build-pmf-engine.yml`.
- **Before a cross-stack Terraform apply** to dev — catches IAM/SG regressions early.
- **After a shared-library dep bump** (boto3, httpx, claude-agent-sdk) — catches SDK-behavior drift that unit tests would miss.

**Not necessary for:**
- Pure refactors of unit-test-covered code (in-process smoke is enough)
- Documentation-only changes
- gp-api changes upstream of the dispatch queue (covered by gp-api's own tests)

---

## Config reference

| Env var | Default | Purpose |
|---|---|---|
| `LIVE_SMOKE_ENABLE` | — | **Required.** Set to `1`/`true`/`yes` to unlock. |
| `AWS_PROFILE` | — | **Required.** Must have SQS + S3 permissions on dev. |
| `LIVE_SMOKE_ENV` | `dev` | Environment slug. Rarely changed; `qa` would exercise the same code path against QA. |
| `LIVE_SMOKE_ACCOUNT` | `333022194791` | AWS account ID — only change if work account is migrated. |
| `LIVE_SMOKE_ARTIFACT_BUCKET` | `gp-agent-artifacts-{env}` | Override target S3 bucket. |
| `LIVE_SMOKE_DISPATCH_QUEUE_URL` | Derived | Override dispatch queue URL. |
| `LIVE_SMOKE_ORG_SLUG` | `smoke-test-pmf` | Used for `MessageGroupId` and S3 path segment. Grep-friendly — leave as default. |
| `LIVE_SMOKE_TIMEOUT_MINUTES` | `30` | Max wait for S3 artifact. |
| `LIVE_SMOKE_POLL_SECONDS` | `15` | S3 poll interval. |

---

## Source-of-truth references

- Test: `pmf_engine/tests/live/test_live_smoke.py`
- Lock config: `pmf_engine/tests/live/conftest.py`
- Dispatch message contract: `gp-api/src/agentExperiments/services/agentDispatch.service.ts:56-71`
- Dispatch message parser: `pmf_engine/control_plane/dispatch_handler.py:153-165`
- Harmless gp-api callback path for unknown run_id: `gp-api/src/queue/consumer/queueConsumer.service.ts:859-862`
- Paired in-process smoke: `pmf_engine/tests/smoke/`
