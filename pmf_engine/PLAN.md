# PMF Engine: Agent Control Plane

## Context

GoodParty needs a way to rapidly test AI-powered experiments on candidates without building full features. The engineer agent (`gp-ai-projects/engineer_agent/`) already proves the modular pattern: Lambda trigger → Fargate runtime → Claude SDK agent logic. We're generalizing this into a PMF engine where gp-api sends SQS "run experiment X for candidate Y," a control plane validates and dispatches to Fargate, the agent writes an artifact to S3, and a callback flows back to gp-api for delivery.

The callback Lambda forwards results to gp-api's god queue (`{stage}-campaign-queue.fifo`) in the `agentExperimentResult` envelope format. gp-api's existing queue consumer handles the message, validates via Zod, and updates the ExperimentRun record.

## Protocol

```
gp-api ──SQS──▶ Dispatch Lambda ──Fargate──▶ Agent Runner ──S3──▶ artifact
                                              Agent Runner ──SQS──▶ Callback Lambda ──SQS──▶ gp-api god queue
```

Three FIFO queues:
1. `agent-dispatch-{env}.fifo` — gp-api → control plane Lambda
2. `agent-callback-{env}.fifo` — Fargate → control plane Lambda
3. gp-api's god queue (`{stage}-campaign-queue.fifo`) — callback Lambda → gp-api

## Data Flow

Candidate data flows via `PARAMS_JSON` at dispatch time (gp-api passes it). The agent only needs Databricks credentials to query voter data + Haystaq scores from `goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq`.

Env vars the Fargate runner gets (all wired via `AI_SECRETS_{ENV}` in the task definition):
- `ANTHROPIC_API_KEY` (sourced from `PMF_ANTHROPIC_API_KEY` in the secret — isolated from other services so PMF spend is trackable)
- `GEMINI_API_KEY`
- `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_API_KEY`

---

## Phase 1: Skeleton Loop ✅ COMPLETE

Goal: SQS message → Fargate → artifact in S3 → callback on queue. Prove the loop works.

### What was built
- `pmf_engine/control_plane/` — dispatch_handler.py, callback_handler.py, registry.py
- `pmf_engine/runner/` — main.py, config.py, harness/base.py, harness/claude_sdk.py
- `pmf_engine/runner/experiments/hello_world.py` — test experiment
- `pmf_engine/tests/` — 62 tests covering all components
- `infrastructure/modules/pmf-engine-control-plane/` — SQS queues (FIFO + DLQs, maxReceiveCount=3), Lambdas, S3 bucket
- `infrastructure/modules/pmf-engine-fargate/` — ECS cluster, task definition, IAM, security groups
- `infrastructure/environments/dev/` — dev environment instantiation for both modules
- `pmf_engine/Dockerfile`, `entrypoint.sh`, `.github/workflows/build-pmf-engine.yml`

### Alerting & observability
- CloudWatch log group for Fargate runner (30-day retention)
- SNS topic `runner_failures` for ECS task failure events
- Optional email subscription + Slack via `shared_slack_notifier_lambda_arn`
- **Missing**: No DLQ alerting, no CloudWatch dashboards, no structured metrics, no cost tracking

---

## Phase 2: gp-api Integration ✅ COMPLETE

Goal: gp-api can dispatch experiments and receive results programmatically.

### What was built (in gp-api repo)
- `src/agentExperiments/` — module, controller, dispatch service, experiment runs service
- `POST /v1/agent-experiments/dispatch` — admin-only endpoint, creates ExperimentRun (PENDING), sends to dispatch queue
- God queue consumer handles `agentExperimentResult` messages via Zod schema validation
- Status mapping: success→SUCCESS, failed→FAILED, contract_violation→CONTRACT_VIOLATION
- State transition guards prevent duplicate processing of terminal runs
- 30 tests passing (unit tests with mocked dependencies)

### Observability & testing gaps
- No integration tests hitting real SQS/S3 (all mocked)
- No behavioral tests for failure boundaries (SQS timeout, S3 unreachable, malformed callback)
- No structured logging on experiment lifecycle events (dispatch, callback received, status transition)
- No metrics/counters for experiment runs by status

---

## Phase 3: Two Real Experiments ✅ COMPLETE

Goal: prove the PMF engine with two data-driven experiments.

### Prerequisite: Callback → God Queue
- callback_handler.py wraps results in `{type: "agentExperimentResult", data: {camelCase}}` envelope
- Message group ID: `gp-queue-agentExperiments`
- Terraform updated: `gp_api_sqs_queue_url`/`gp_api_sqs_queue_arn` variables
- Dev env points to `Collin_Queue.fifo` for testing

### 3a. Voter Targeting — "People Likely to Vote for You"
- `pmf_engine/runner/experiments/voter_targeting.py`
- Agent reads candidate data from PARAMS_JSON (district, party, issues, P2V data)
- Queries Databricks for L2 voter file + Haystaq modeling scores
- Segments voters into 3 tiers (strong prospects, likely supporters, reachable)
- Produces `voter_targeting.json` artifact with segment counts, demographics, geographic clusters

### 3b. Weekend Walking Plan
- `pmf_engine/runner/experiments/walking_plan.py`
- Agent reads candidate data + route preferences from PARAMS_JSON
- Queries Databricks for voters with addresses and lat/lng
- Clusters voters geographically into walkable routes
- Produces `walking_plan.json` artifact with ordered stops, talking points, time estimates

### 3c. Registry
- Both experiments registered in `EXPERIMENT_REGISTRY` alongside hello_world

### Critic review fixes applied
- Runner sends `"failed"` not `"error"` (matched gp-api Zod schema)
- S3 non-404 errors caught per-record (prevent batch crash)
- Contract violations logged at ERROR with full context
- Experiment instructions mandate parameterized SQL queries
- `validate_contract` returns False for empty bucket/key
- Callback rejects mismatched artifact buckets
- 73 tests passing

---

## Phase 3.5: Local End-to-End Test 🔲 NEXT

Goal: run the voter_targeting experiment locally, verify it produces a useful artifact.

### Prerequisites
- [ ] Create S3 bucket `gp-agent-artifacts-dev`
- [ ] Get Databricks creds (from gp-ai-projects .env)
- [ ] Pick a real candidate district for test params
- [ ] Run `python -m pmf_engine.runner.main` locally with env vars

### Test flow
```
Local machine (uv run)
  → Claude agent spawns, queries Databricks
  → Writes /workspace/output/voter_targeting.json
  → Uploads to S3 gp-agent-artifacts-dev
  → Sends callback to Collin_Queue.fifo
  → Local gp-api picks up result
```

### What to validate
- Does the agent successfully query Databricks?
- Does the voter targeting JSON have real data and reasonable segments?
- Does the callback message reach gp-api and update ExperimentRun?

---

## Phase 4: Deploy Infrastructure ✅ COMPLETE

- [x] Lambda packaging — build script `pmf_engine/scripts/build_lambda_package.sh` + try/except imports for Lambda compatibility
- [x] Fargate env vars — DATABRICKS_* added to task definition from AI_SECRETS_DEV
- [x] `terraform apply` pmf-engine-fargate (dev) — ECS cluster, task def, IAM, security groups
- [x] `terraform apply` pmf-engine-control-plane (dev) — SQS queues, Lambdas, S3 bucket, callback→Collin_Queue.fifo
- [x] Docker build + push to ECR (`gp-ai-projects:pmf-engine-dev`)
- [x] hello_world e2e: dispatch→Lambda→Fargate→S3→callback→gp-api SUCCESS
- [x] voter_targeting e2e: real Databricks data for Tecumseh, MI
- [x] walking_plan e2e: 600 doors with talking points

### gp-api endpoints (in ~/work/gp-api)
- [x] `GET /v1/agent-experiments/mine` — candidate's runs
- [x] `POST /v1/agent-experiments/request` — candidate-facing dispatch (gated by isAiBetaVip)
- [x] `GET /v1/agent-experiments/:runId/artifact` — S3 artifact proxy
- [x] `isAiBetaVip` flag on campaign details

### gp-webapp (in ~/work/gp-webapp)
- [x] AI Insights dashboard tab with BETA badge
- [x] Tabbed layout: Voter Targeting | Walking Plan
- [x] ExperimentTab: generate/regenerate, polling, loading states
- [x] VoterTargetingResults: segment cards, demographics, CSV download per tier
- [x] WalkingPlanResults: zip-grouped areas, expandable stops, CSV download per area

---

## Instruction Development Workflow

Experiment instructions live as `.md` files in `pmf_engine/runner/experiments/instructions/`. The Python experiment definitions load them at import time.

### Fast iteration loop (no Docker rebuild)

1. **Edit** the `.md` file in `experiments/instructions/`
2. **Test locally** — spawn a Claude Code subagent with the instruction:
   ```
   Agent prompt: <paste the .md content, replace /workspace/output/ with /tmp/pmf-test/,
   add Databricks connection via shared client, provide candidate params inline>
   ```
3. **Check output** — verify `/tmp/pmf-test/<artifact>.json` matches the expected schema
4. **Iterate** — tweak instruction, re-run subagent, seconds not minutes

### Deploy to Fargate

Once the instruction produces correct output locally:
1. Rebuild Docker: `docker build --platform linux/arm64 -f pmf_engine/Dockerfile -t <ecr>:pmf-engine-dev .`
2. Push: `docker push <ecr>:pmf-engine-dev`
3. Regenerate on the UI — Fargate always pulls latest image tag

### Key files
- `experiments/instructions/voter_targeting.md` — voter targeting instruction
- `experiments/instructions/walking_plan.md` — walking plan instruction
- `experiments/voter_targeting.py` — loads .md, defines contract/model/resources
- `experiments/walking_plan.py` — loads .md, defines contract/model/resources

