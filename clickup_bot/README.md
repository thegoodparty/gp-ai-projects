# ClickUp Bot

Webhook handler that triggers engineer_agent based on ClickUp task tags.

## Architecture

```
ClickUp (tag added)
    ↓ webhook POST
ALB → Lambda (verify signature, validate task_id/tag, fast-ack 200)
    ↓ lambda:InvokeFunction (async self-invoke; falls back to inline until IAM lands)
Lambda worker invocation (dedup check, trigger, ack comment)
    ↓ ecs:runTask (CLICKUP_TASK_ID, INSTRUCTION, AGENT_MODEL overrides)
Fargate (engineer_agent)
    ↓ based on the instruction
ClickUp comment OR GitHub PR
```

## Tag → Action Mapping

| Tag | Label | Model | Result |
|-----|-------|-------|--------|
| `gpbot-analyze` | analyze | opus | Posts bug analysis as [GP-Bot] comment |
| `gpbot-work` | implement | opus | Creates PR and posts link to ClickUp |

## Flow

1. User adds tag to a ClickUp task (e.g., `gpbot-analyze`)
2. ClickUp sends `taskTagUpdated` webhook to Lambda
3. Webhook invocation (ClickUp's critical path — must answer in well under
   ClickUp's webhook response timeout):
   - Verify the signature, validate `task_id`, resolve the tag in `TAG_CONFIG`
   - Self-invoke the same Lambda asynchronously with
     `{"gpbot_async": true, "task_id": ..., "matched_tag": ...}` and return
     `200 {"status": "accepted"}` immediately — zero ClickUp API calls in-path
   - If the self-invoke is unavailable (missing IAM — the initial state until
     the follow-up terraform lands — or any invoke error), fall back to running
     the worker steps inline, exactly the pre-fast-ack behavior
4. Worker invocation (async self-invoke; internal payloads are recognized only
   by a top-level `gpbot_async` key with no ALB envelope keys, which an
   internet request cannot produce — an ALB-wrapped body stays a string inside
   `event["body"]`):
   - Dedup check: does the task already have a **recent** `[GP-Bot] Processing
     started` comment **for the same label**? → Skip. Analyze and implement
     dedup independently. See "Dedup semantics" below.
   - Atomic dedup claim: conditional DynamoDB write on `{task_id}#{label}` —
     exactly one concurrent worker wins; losers skip quietly. See "Dedup
     semantics" below.
   - Triggers Fargate, passing `CLICKUP_TASK_ID`, `INSTRUCTION`, and
     `AGENT_MODEL` as container-override env vars (the instruction encodes the
     analyze-vs-implement contract; there is no `OUTPUT_ACTION`)
   - Posts the `[GP-Bot] Processing started (...)` comment — which also tells
     the user the re-tag cooldown — only after the Fargate task actually
     launched. In the async worker it is retried once on failure (this comment
     is the dedup marker, and the worker is off ClickUp's timeout budget); the
     inline fallback posts it exactly once, because there ClickUp is still
     waiting on the webhook response.
   - Never lets an exception escape: an unhandled exception in an async
     invocation makes Lambda auto-retry it, which would duplicate the launch.
     Failures log an alarm-matching `ERROR: Async processing failed` line (the
     only fail-loud channel — no caller receives an HTTP error) and attempt a
     failure comment.
5. engineer_agent executes based on action type

There is no feature flag and no logging-only mode. A matched tag always attempts the
Fargate trigger. If the trigger fails for any reason (missing `ECS_*` env vars, IAM
denial, ECS error), the Lambda posts a `[GP-Bot] Failed to start processing` comment
on the task and returns HTTP 500.

To retry after a failure (e.g. once the config is fixed): remove and re-add the tag.
Failure comments do not mark the task as processed, so the retry re-triggers.

## Dedup semantics

Only the `[GP-Bot] Processing started` success marker **for the same label**
counts as processed (mirroring the atomic layer's `{task_id}#{label}` key: a fresh
analyze marker never suppresses a gpbot-work trigger), and only while it is recent: within `DEDUP_COMMENT_WINDOW_SECONDS` (default 900) of the
comment's `date`. The marker exists to absorb webhook retry storms and double-tags,
which play out over seconds to minutes. A human re-tagging a task hours later is a
deliberate re-run and must not be silently ignored — dedup had never actually fired
before the 2026-07-14 fix (see below), so "re-tag always re-runs" is the behavior
users already know; an unbounded marker would have silently changed it. Markers with
a missing or unparseable `date` do NOT block — blocking would have no age bound,
so a ClickUp date-format drift would permanently disable re-tag re-runs — and log an
alarm-matching `ERROR` line, because that shape drift is an integration break an
operator must see; the duplicate risk it fails toward is bounded by the atomic
DynamoDB layer. `[GP-Bot] Failed to start processing`
comments never block a retry, regardless of age.

Comment-based dedup is best-effort: two deliveries processed close enough together
both see "no marker yet" and both launch (the check and the comment post are not
atomic). Because it is best-effort, a comments GET failure in the async worker does
not drop the delivery: with `DEDUP_TABLE_NAME` configured the worker proceeds (the
atomic claim still guards duplicates); without it the worker stops and posts the
standard failure comment so the tagger has a visible remove-and-re-add retry path.
The synchronous path instead returns 500 and lets ClickUp redeliver. The authoritative layer is an **atomic DynamoDB claim**: after the comment
check passes, the worker does a conditional `PutItem` on `{task_id}#{label}` into
`DEDUP_TABLE_NAME` — DynamoDB serializes conditional writes, so exactly one worker
wins even under fully concurrent deliveries; losers skip quietly with a 200. Claim
details:

- The claim carries an `expires_at` of now + `DEDUP_TTL_SECONDS` (default 900,
  matching the comment window: same product contract, retry storms are absorbed and
  a deliberate re-tag ~15 minutes later re-runs). DynamoDB TTL garbage-collects old
  claims, and because TTL deletion can lag hours, the conditional write also treats
  an expired-but-not-yet-deleted claim as free.
- A failed launch releases its claim (`DeleteItem`), so the remove-and-re-add-the-tag
  retry contract survives launch failures. A successful launch keeps its claim until
  the TTL expires.
- If `DEDUP_TABLE_NAME` is unset (the state between a code deploy and the terraform
  apply that creates the table), the claim step is a quiet no-op and only
  comment-based dedup applies.
- If the table is broken (throttled, deleted, IAM stripped), the handler **fails
  open** — the launch still happens — and logs an alarm-matching
  `ERROR: dedup table unavailable` line: a duplicate agent costs a few dollars, a
  bot that cannot launch at all is an outage, but an operator must see the breakage.

### Stranded dedup claims

**Symptom:** re-tagging a task does nothing (no ack comment, no Fargate launch, no
failure comment) and the handler-errors alarm fired earlier — e.g. a worker hard
timeout (`Task timed out`) or a `Failed to release dedup lock` line. A claim was
written but the launch behind it died (or a delayed PutItem landed after the
release `DeleteItem` — an accepted race, see `release_dedup_lock` in the handler),
so the claim now suppresses every re-tag until its TTL expires.

**Check:** scan the table for the task's claim:

```bash
aws dynamodb scan --table-name clickup-bot-dedup-prod \
  --filter-expression "contains(pk, :t)" \
  --expression-attribute-values '{":t": {"S": "<task_id>"}}'
```

**Fix:** delete the stranded claim (key is `pk = "{task_id}#{label}"`, label is
`analyze` or `implement`), then re-tag:

```bash
aws dynamodb delete-item --table-name clickup-bot-dedup-prod \
  --key '{"pk": {"S": "<task_id>#<label>"}}'
```

Or just wait: the claim's `expires_at` bounds the suppression at
`DEDUP_TTL_SECONDS` (15 minutes by default) — after that a re-tag reclaims it.

### 2026-07-14 incident

ClickUp delivered ONE `taskTagUpdated` event (gpbot-analyze) six times — the
original plus five timeout retries — and all six launched a Fargate opus agent.
Two bugs compounded. First, every invocation ran 7.6–20.5s of serial synchronous
ClickUp API calls in the webhook path during a ClickUp slowdown, exceeding
ClickUp's webhook response timeout, so ClickUp kept retrying (and counted every
timeout toward the ~100 consecutive failures after which it auto-disables the
webhook — slow responses are an outage risk, not just a duplication risk; the
fast-ack flow above is the fix). Second, the dedup matcher required a
`"type": "text"` key on comment items that the real
`GET /task/{id}/comment` response does not have — verified live, it matched 0 of
13 real comments, including six of the bot's own ack comments — so dedup had
never fired since the feature shipped. The old test fixture had invented the
`type` field (oracle problem); fixtures now use the live-captured shape.

## Monitoring

Handled 500s and 401s do not fire the Lambda `Errors` metric, so terraform
(`infrastructure/modules/clickup-bot/`) creates a CloudWatch log metric filter on the
handler's `ERROR` / `Failed to` log lines (plus the runtime-emitted
`Task timed out`, so a hard-timed-out async worker — which no caller observes and
which Lambda never retries — still alarms), an alarm on that metric, and a
`clickup-bot-failures-prod` SNS topic wired to the shared Slack notifier. The alarm
covers those log lines — not literally every failure path. The
operationally significant failures each emit one and alarm within ~5 minutes: the
fail-loud 500s (missing `ECS_*` config, `ecs:RunTask` failure, comment-fetch failure,
secrets outage), the failure-comment and ack-comment posts the handler deliberately
swallows (ClickUp API down or a rotated `CLICKUP_API_KEY`), signature-verification
401s on gpbot-tagged deliveries (rotated or mismatched `CLICKUP_WEBHOOK_SECRET`), which
would otherwise silently end in ClickUp suspending the webhook, and any failure inside
the async worker invocation (`ERROR: Async processing failed`), whose caller is Lambda's
async plumbing rather than ClickUp, so the alarm is its only fail-loud channel. The
expected fast-ack fallback while self-invoke IAM is absent logs a deliberately quiet
`Async self-invoke unavailable, processing synchronously` line and does NOT alarm.
The deliberately quiet
400s do NOT alarm: a missing `task_id`, malformed JSON, and non-object JSON all log
without `ERROR` / `Failed to` on purpose, so a client cannot fire the alarm through the
public endpoint by echoing those terms in a request.

The filter pattern and the handler's log wording are a two-sided contract: the
handler must emit `ERROR` / `Failed to` on every failure path, and must never echo
unauthenticated request content to the logs (the endpoint is public — an echoed body
containing "ERROR" would let anyone fire the alarm). Both sides are locked by
`clickup_bot/tests/test_handler.py`; reword log lines and the terraform pattern
together.

### After an outage: check webhook health

During a Secrets Manager outage the Lambda cannot verify signatures for gpbot-tagged
deliveries and returns 500 for them (irrelevant deliveries are filtered before
signature verification and still return 200). A rotated or mismatched
`CLICKUP_WEBHOOK_SECRET` behaves the same way with 401s. ClickUp tracks consecutive
delivery failures per webhook and auto-suspends the webhook after sustained failures.
A suspended webhook stays suspended after the outage is fixed: the bot receives
nothing, logs nothing, and looks healthy.

After ANY sustained failure outage (500s or 401s), check and re-enable the webhook.
Check `health.fail_count` even when `health.status` is still `active`: ClickUp counts
consecutive failures toward auto-disable (~100), and slow responses count too — after
the 2026-07-14 incident the webhook sat at `failing` / `fail_count: 6` from a single
retried delivery. A nonzero fail_count that keeps climbing means deliveries are still
failing (or timing out) and the webhook is walking toward suspension:

```bash
# health.status must be "active"; a climbing health.fail_count is a warning even before suspension
curl -s -H "Authorization: $CLICKUP_API_KEY" \
  "https://api.clickup.com/api/v2/team/<team_id>/webhook" | jq '.webhooks[] | {id, endpoint, health}'

# re-enable a suspended webhook
curl -s -X PUT -H "Authorization: $CLICKUP_API_KEY" -H "Content-Type: application/json" \
  -d '{"endpoint": "https://ai.goodparty.org/clickup/webhook", "events": ["taskTagUpdated"], "status": "active"}' \
  "https://api.clickup.com/api/v2/webhook/<webhook_id>"
```

## Environment Variables

Set by terraform (`infrastructure/environments/prod/clickup-bot/`), not by hand.

| Variable | Description |
|----------|-------------|
| `ECS_CLUSTER_ARN` | ECS cluster the engineer_agent task runs in |
| `ECS_TASK_DEFINITION` | Task definition family (latest revision) or ARN |
| `ECS_SUBNET_IDS` | Comma-separated private subnet IDs for the task |
| `ECS_SECURITY_GROUP_ID` | Security group for the task |
| `ENVIRONMENT` | Selects the `AI_SECRETS_<ENV>` secret that provides `CLICKUP_API_KEY` and `CLICKUP_WEBHOOK_SECRET` |
| `DEDUP_COMMENT_WINDOW_SECONDS` | Optional (default 900): how long a `Processing started` comment blocks re-triggering — see "Dedup semantics" |
| `DEDUP_TABLE_NAME` | DynamoDB table for atomic dedup claims (`clickup-bot-dedup-<env>`). Unset = quiet no-op, comment-based dedup only — see "Dedup semantics" |
| `DEDUP_TTL_SECONDS` | Optional (default 900): lifetime of an atomic dedup claim — see "Dedup semantics" |
| `ENABLE_FARGATE` | Transition compatibility only: the current handler ignores it, but the previous handler silently no-ops without it. Remove from the module only after the fail-loud handler is confirmed live. |

## Adding New Tags

Add to `TAG_CONFIG` in `handler.py`:

```python
TAG_CONFIG = {
    "gpbot-analyze": {"instruction": ANALYZE_INSTRUCTION, "label": "analyze", "model": "opus"},
    "gpbot-work": {"instruction": IMPLEMENT_INSTRUCTION, "label": "implement", "model": "opus"},
    "new-tag": {"instruction": YOUR_INSTRUCTION, "label": "your-label", "model": "opus"},
}
```

## Deployment

Two separate paths. Do not mix them.

**One-time prerequisite — the deploy IAM role must exist first.** The deploy
workflow assumes the `github-actions-lambda-deploy` role created by
`infrastructure/shared/github-actions-iam`, which nothing in CI applies. For any
change that introduces or re-points a workflow at that role, `terraform apply` that
stack BEFORE merging, or the triggered deploy dies at the configure-aws-credentials
step and the new handler never ships. The same apply removes Lambda deploy
permissions from the old `github-actions-ecr-push` role, so the workflow change must
be promoted through `qa` and `prod` immediately afterward — see the rollout section
in `infrastructure/shared/github-actions-iam/README.md`. This is the one case where
a terraform apply legitimately precedes the code merge; the code-first rule below
applies to this environment's own terraform (`infrastructure/environments/prod/clickup-bot/`),
not the shared IAM stack.

**Rollout ordering — code first, then config.** When a change touches both the
handler and terraform, merge/push to `prod` and confirm the deploy workflow succeeded
BEFORE running `terraform apply`. The previous (pre-fail-loud) handler gates on
`ENABLE_FARGATE` and silently no-ops when it is absent; the module keeps
`ENABLE_FARGATE = "true"` in the env map as transition compatibility so an
out-of-order apply cannot re-create the silent no-op, but do not rely on that:
terraform cannot deploy code anymore (`lifecycle.ignore_changes`), so an apply alone
never ships a handler fix.

The fast-ack + atomic-dedup rollout follows exactly that ordering, and the handler
is written so each half is safe alone:

1. Merge/deploy the code first. Without the terraform it is a safe no-op on both
   new paths: the async self-invoke lacks IAM and quietly falls back to the old
   synchronous flow, and `DEDUP_TABLE_NAME` is unset so the atomic claim is a quiet
   no-op (comment-based dedup only).
2. Then `terraform apply` in `infrastructure/environments/prod/clickup-bot`. This
   creates the dedup table, grants `dynamodb:PutItem`/`DeleteItem` and the
   self-invoke `lambda:InvokeFunction`, sets `DEDUP_TABLE_NAME` on the Lambda, and
   pins async `maximum_retry_attempts = 0`. The apply is what ACTIVATES both
   fast-ack and atomic dedup — until then the bot runs exactly the old flow.

**Code** deploys automatically via `.github/workflows/deploy-clickup-bot.yml` on push
to `prod` (paths: `clickup_bot/**`). The workflow runs `clickup_bot/tests/` first and
blocks the deploy if they fail. No manual zip/upload. There is no `clickup-bot-dev`
Lambda — only `clickup-bot-prod` exists, so the workflow deploys prod only.

**Config and IAM** (env vars, role policies, log group) are managed by terraform in
`infrastructure/environments/prod/clickup-bot/`. Terraform seeds the function code
once at creation and then ignores it (`lifecycle.ignore_changes` on
`filename`/`source_code_hash` in the module), so a `terraform apply` can never roll
back code that CI deployed:

```bash
cd infrastructure/environments/prod/clickup-bot
terraform init
terraform plan   # review before applying
terraform apply
```

> **Warning**
> - Do not run `aws lambda update-function-configuration` by hand. Terraform will
>   revert your change on the next apply (drift).
> - Do not run `aws lambda update-function-code` by hand either. Push to `prod` and
>   let the workflow deploy.
> - A `terraform apply` from a checkout without the correct variables previously
>   disabled the bot in prod: `enable_fargate_trigger` defaulted to `false` and the
>   real value lived only in a gitignored local `terraform.tfvars`, so an apply
>   without that file stripped the Lambda's `ECS_*` env vars and ECS IAM policy
>   (silent for 12 days, 2026-06-26). Prod defaults are now pinned in that
>   directory's `main.tf`. Always read the plan output before applying.

## ClickUp Webhook Setup

1. Go to ClickUp Settings → Integrations → Webhooks
2. Create webhook with:
   - Endpoint: `https://ai.goodparty.org/clickup/webhook`
   - Events: `taskTagUpdated`
   - Scope: whole workspace (omit `space_id`). The handler filters non-target
     deliveries *before* signature verification precisely because it receives the
     entire workspace's tag updates — a space-scoped webhook would break the outage
     reasoning in Monitoring / "After an outage". The terraform env README
     (`infrastructure/environments/prod/clickup-bot/README.md`) is the source of
     truth for the live webhook (ID, team ID, exact create command).
