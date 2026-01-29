# ClickUp Bot

Webhook handler that triggers engineer_agent based on ClickUp task tags.

## Architecture

```
ClickUp (tag added)
    ↓ webhook POST
ALB → Lambda (validate, check comments, trigger)
    ↓ ecs:runTask with OUTPUT_ACTION
Fargate (engineer_agent)
    ↓ based on action
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
   - Does task already have a `[GP-Bot]` comment? → Skip
4. Lambda posts `[GP-Bot] Processing started (post_analysis)...` comment
5. Lambda triggers Fargate with `CLICKUP_TASK_ID` and `OUTPUT_ACTION`
6. engineer_agent executes based on action type

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CLICKUP_API_KEY` | ClickUp API token |
| `CLUSTER_NAME` | ECS cluster for engineer_agent |
| `TASK_DEFINITION` | Fargate task definition ARN |
| `SUBNET_IDS` | Comma-separated subnet IDs |
| `SECURITY_GROUP_ID` | Security group for Fargate tasks |

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

```bash
cd clickup_bot/lambda
zip handler.zip handler.py
aws lambda update-function-code --function-name clickup-bot --zip-file fileb://handler.zip
```

## ClickUp Webhook Setup

1. Go to ClickUp Settings → Integrations → Webhooks
2. Create webhook with:
   - Endpoint: `https://ai.goodparty.org/clickup/webhook`
   - Events: `taskTagUpdated`
   - Space: Your engineering space
