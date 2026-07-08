# ClickUp Bot

Webhook handler that triggers engineer_agent based on ClickUp task tags.

## Architecture

```
ClickUp (tag added)
    ↓ webhook POST
ALB → Lambda (validate, check comments, trigger)
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
3. Lambda checks:
   - Is this a configured tag? → Look up in `TAG_CONFIG`
   - Does the task already have a `[GP-Bot] Processing started` comment? → Skip.
     Only that success marker counts as processed; `[GP-Bot] Failed to start
     processing` comments never block a retry.
4. Lambda triggers Fargate, passing `CLICKUP_TASK_ID`, `INSTRUCTION`, and
   `AGENT_MODEL` as container-override env vars (the instruction encodes the
   analyze-vs-implement contract; there is no `OUTPUT_ACTION`)
5. Lambda posts the `[GP-Bot] Processing started (...)` comment, only after the
   Fargate task actually launched
6. engineer_agent executes based on action type

There is no feature flag and no logging-only mode. A matched tag always attempts the
Fargate trigger. If the trigger fails for any reason (missing `ECS_*` env vars, IAM
denial, ECS error), the Lambda posts a `[GP-Bot] Failed to start processing` comment
on the task and returns HTTP 500.

To retry after a failure (e.g. once the config is fixed): remove and re-add the tag.
Failure comments do not mark the task as processed, so the retry re-triggers.

## Monitoring

Handled 500s and 401s do not fire the Lambda `Errors` metric, so terraform
(`infrastructure/modules/clickup-bot/`) creates a CloudWatch log metric filter on the
handler's `ERROR` / `Failed to` log lines, an alarm on that metric, and a
`clickup-bot-failures-prod` SNS topic wired to the shared Slack notifier. The alarm
covers the `ERROR` / `Failed to` log lines — not literally every failure path. The
operationally significant failures each emit one and alarm within ~5 minutes: the
fail-loud 500s (missing `ECS_*` config, `ecs:RunTask` failure, comment-fetch failure,
secrets outage), the failure-comment and ack-comment posts the handler deliberately
swallows (ClickUp API down or a rotated `CLICKUP_API_KEY`), and signature-verification
401s on gpbot-tagged deliveries (rotated or mismatched `CLICKUP_WEBHOOK_SECRET`), which
would otherwise silently end in ClickUp suspending the webhook. The deliberately quiet
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

After ANY sustained failure outage (500s or 401s), check and re-enable the webhook:

```bash
# health.status must be "active"; look at health.fail_count too
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
