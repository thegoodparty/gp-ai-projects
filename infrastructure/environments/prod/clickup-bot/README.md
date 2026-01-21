# ClickUp Bot

Webhook-triggered Lambda that listens for ClickUp tag events and triggers the engineer agent.

## Architecture

```
ClickUp Webhook → ALB (ai.goodparty.org) → Lambda → (Future: Fargate engineer_agent)
```

## Webhook Configuration

| Field | Value |
|-------|-------|
| Endpoint | `https://ai.goodparty.org/clickup/webhook` |
| Events | `taskTagUpdated` |
| Scope | Whole workspace (no space_id filter) |
| Webhook ID | `f32d86c4-29c4-4cd9-b260-797389eda10c` |

### Supported Tags

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

## Deployment

### Quick Deploy (Local)

```bash
AWS_PROFILE=work ./infrastructure/modules/clickup-bot/scripts/deploy.sh
```

### CI/CD

Automatically deploys via GitHub Actions when changes are pushed to:
- `infrastructure/modules/clickup-bot/lambda/**`

Workflow: `.github/workflows/deploy-clickup-bot.yml`

### Terraform (Full Infrastructure)

```bash
cd infrastructure/environments/prod/clickup-bot
AWS_PROFILE=work terraform apply
```

## Logs

```bash
AWS_PROFILE=work aws logs tail /aws/lambda/clickup-bot-prod --follow
```

## Webhook Management

### List Webhooks

```bash
curl -s "https://api.clickup.com/api/v2/team/90132012119/webhook" \
  -H "Authorization: $CLICKUP_API_KEY" | jq
```

### Delete Webhook

```bash
curl -X DELETE "https://api.clickup.com/api/v2/webhook/{webhook_id}" \
  -H "Authorization: $CLICKUP_API_KEY"
```

### Create Webhook (Workspace-Wide)

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

- Webhook requests are verified using HMAC-SHA256 signature
- Signature is sent in `x-signature` header
- Invalid signatures return 401 Unauthorized

## Flow

1. User adds `gpbot-analyze` or `gpbot-work` tag to a task
2. ClickUp sends webhook to `https://ai.goodparty.org/clickup/webhook`
3. Lambda verifies signature
4. Lambda checks if task already has `[GP-Bot]` comment (skip if yes)
5. Lambda logs handoff data (future: triggers Fargate)
