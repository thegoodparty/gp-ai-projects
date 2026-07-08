# ClickUp Bot — prod environment (terraform)

Terraform for the `clickup-bot-prod` Lambda: config, env vars, IAM, log group,
and the fail-loud alarm (metric filter + SNS + Slack notifier).

The canonical operational doc is `clickup_bot/README.md` (architecture, flow,
monitoring, rollout ordering, webhook recovery). This README covers only what
is specific to this terraform environment.

## Deployment — two paths, no third

**Code** deploys ONLY via `.github/workflows/deploy-clickup-bot.yml` on push to
`prod` (paths: `clickup_bot/**`). The workflow runs `clickup_bot/tests/` and
blocks the deploy if they fail. Never run `aws lambda update-function-code` by
hand and never zip a local `handler.py`: terraform ignores code drift
(`lifecycle.ignore_changes` in the module), so a hand-deployed stale handler
would never be flagged or reverted by an apply. The old
`scripts/deploy.sh` local-deploy script has been deleted for exactly this
reason.

**Config and IAM** deploy via terraform from this directory:

```bash
cd infrastructure/environments/prod/clickup-bot
AWS_PROFILE=work terraform init
AWS_PROFILE=work terraform plan   # read the plan before applying
AWS_PROFILE=work terraform apply
```

No `terraform.tfvars` is required: the prod values (`enable_fargate_trigger = true`,
prod private subnets) are pinned as defaults in this directory's `main.tf`.
`terraform.tfvars.example` documents the variables. History: the prod values
used to live only in a gitignored local tfvars, and an apply without that file
silently stripped the Lambda's `ECS_*` env vars and ECS IAM policy for 12 days
(incident starting 2026-06-26). Do not reintroduce a required local tfvars.

## Webhook configuration

| Field | Value |
|-------|-------|
| Endpoint | `https://ai.goodparty.org/clickup/webhook` |
| Events | `taskTagUpdated` |
| Scope | Whole workspace (no space_id filter) |
| Webhook ID | `f32d86c4-29c4-4cd9-b260-797389eda10c` |
| Team ID | `90132012119` |

### Supported tags

| Tag | Action |
|-----|--------|
| `gpbot-analyze` | Analyze bug and post findings to ClickUp |
| `gpbot-work` | Implement fix and create PR |

## Secrets (AWS Secrets Manager)

Stored in `AI_SECRETS_PROD`:

| Key | Description |
|-----|-------------|
| `CLICKUP_API_KEY` | API key for ClickUp API calls |
| `CLICKUP_WEBHOOK_SECRET` | HMAC secret for verifying webhook signatures |

## Logs

```bash
AWS_PROFILE=work aws logs tail /aws/lambda/clickup-bot-prod --follow
```

## Webhook management

After any sustained failure outage (500s or 401s), check the webhook health —
ClickUp auto-suspends webhooks after consecutive failures. Full runbook:
`clickup_bot/README.md`, "After an outage".

### List webhooks

```bash
curl -s "https://api.clickup.com/api/v2/team/90132012119/webhook" \
  -H "Authorization: $CLICKUP_API_KEY" | jq
```

### Delete webhook

```bash
curl -X DELETE "https://api.clickup.com/api/v2/webhook/{webhook_id}" \
  -H "Authorization: $CLICKUP_API_KEY"
```

### Create webhook (workspace-wide)

```bash
curl -X POST "https://api.clickup.com/api/v2/team/90132012119/webhook" \
  -H "Authorization: $CLICKUP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "endpoint": "https://ai.goodparty.org/clickup/webhook",
    "events": ["taskTagUpdated"]
  }'
```

**Note:** Omitting `space_id` creates a workspace-wide webhook that triggers for all tasks.

## Security

- Webhook requests are verified using HMAC-SHA256 signature (`x-signature` header)
- Invalid signatures return 401 Unauthorized and emit an `ERROR:` log line that
  fires the fail-loud alarm (sustained 401s mean a rotated/mismatched secret)

## Flow

1. User adds `gpbot-analyze` or `gpbot-work` tag to a task
2. ClickUp sends webhook to `https://ai.goodparty.org/clickup/webhook`
3. Lambda verifies signature
4. Lambda checks if task already has a `[GP-Bot] Processing started` comment (skip if yes)
5. Lambda triggers the engineer-agent Fargate task; on any failure it posts a
   `[GP-Bot] Failed to start processing` comment and returns 500 (fail-loud —
   there is no logging-only mode)
