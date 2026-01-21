# Engineer Agent - Architecture

## Overview

The Engineer Agent is a Claude Agent SDK powered service that analyzes production bugs from ClickUp, investigates code and logs, and posts analysis results back to ClickUp.

## System Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  EventBridge (cron: every 6 hours)                                  │
│           │                                                         │
│           ▼                                                         │
│  Lambda: clickup-scanner                                            │
│  - Scans ClickUp lists for tasks with `production-bug` tag          │
│  - Checks for [GP-Bot] comments to skip already-processed tasks     │
│  - For each unprocessed task:                                       │
│           │                                                         │
│           ▼  POST to ai.goodparty.org/engineer/analyze              │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │ ALB (ai.goodparty.org)                                         │ │
│  │      │                                                         │ │
│  │      ▼                                                         │ │
│  │ Lambda: engineer-agent-trigger                                 │ │
│  │ - Validates x-api-key header                                   │ │
│  │ - Parses bug details from POST body                            │ │
│  │ - Triggers Fargate task via ecs:runTask                        │ │
│  │      │                                                         │ │
│  │      ▼                                                         │ │
│  │ Fargate: engineer-agent (private subnet)                       │ │
│  │ - Receives bug details via container environment overrides     │ │
│  │ - Agent decides which repos are relevant                       │ │
│  │ - Clones repos to /workspace                                   │ │
│  │ - Analyzes code with Claude Agent SDK                          │ │
│  │ - Queries CloudWatch logs if needed                            │ │
│  │ - Posts [GP-Bot] analysis result to ClickUp                    │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## API Contract

### Endpoint

```
POST https://ai.goodparty.org/engineer/analyze
Headers:
  x-api-key: <API_KEY>
  Content-Type: application/json
```

### Request Body

```json
{
  "task_id": "86ae1mj9b",
  "task_name": "Change to Pro Status Failed After Payment",
  "task_url": "https://app.clickup.com/t/86ae1mj9b",
  "description": "User paid but Pro status didn't update. Stripe webhook received but...",
  "tag": "production-bug"
}
```

### Response

```json
{
  "status": "triggered",
  "task_id": "86ae1mj9b",
  "ecs_task_arn": "arn:aws:ecs:us-west-2:123456789:task/engineer-agent-prod/abc123"
}
```

## Available Repositories

The agent can choose which repos to clone based on the bug context:

| Repo | Description | When to Clone |
|------|-------------|---------------|
| gp-webapp | Next.js frontend | UI bugs, frontend errors |
| gp-api | NestJS backend API | API errors, backend logic bugs |
| gp-ai-projects | AI services (this repo) | AI/ML bugs, campaign planning issues |
| gp-people-api | People/voter data API | Voter data issues, P2V bugs |

## Agent Capabilities

### Built-in Tools (Claude Agent SDK)

- **Read** - Read files from cloned repos
- **Glob** - Find files by pattern
- **Grep** - Search file contents
- **Bash** - Run commands (git, aws cli, etc.)

### Custom Tools

| Tool | Description |
|------|-------------|
| `clone_repo` | Clone a specific repo to /workspace |
| `get_cloudwatch_logs` | Query CloudWatch logs for a service/time range |
| `update_clickup` | Post [GP-Bot] comment to ClickUp task |

## Environment Variables (Fargate)

Passed via container overrides from Lambda trigger:

| Variable | Description |
|----------|-------------|
| `CLICKUP_TASK_ID` | ClickUp task ID to analyze |
| `CLICKUP_TASK_NAME` | Task name/title |
| `CLICKUP_TASK_URL` | Direct URL to task |
| `TASK_DESCRIPTION` | Bug description |
| `TAG_TYPE` | Tag type (production-bug, simple-task) |
| `ENVIRONMENT` | Environment (dev/qa/prod) |

Injected from Secrets Manager:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `CLICKUP_API_KEY` | ClickUp API key |
| `GITHUB_TOKEN` | For cloning private repos |

## Output Format

The agent posts a comment to ClickUp with the `[GP-Bot]` prefix:

```
[GP-Bot] Analysis Complete

## Summary
Payment webhook is received but the updateProStatus function fails silently 
when the user's subscription record doesn't exist.

## Root Cause
In gp-api/src/subscriptions/subscription.service.ts:142, the updateProStatus 
function doesn't handle the case where findOne returns null.

## Suggested Fix
Add null check before updating:
- File: gp-api/src/subscriptions/subscription.service.ts
- Line: 142-145

## Files Investigated
- gp-api/src/subscriptions/subscription.service.ts
- gp-api/src/webhooks/stripe.controller.ts
- CloudWatch logs: /aws/ecs/gp-api-prod (last 2 hours)

## Severity
Medium - Affects new Pro signups

---
*Analyzed by GP Engineer Agent*
```

## Directory Structure

```
engineer_agent/
├── pyproject.toml              # Workspace package config
├── ARCHITECTURE.md             # This file
├── agent/
│   ├── __init__.py
│   ├── main.py                 # Main agent entry point
│   ├── tools.py                # Custom MCP tools
│   └── config.py               # Repos, AWS settings
├── lambda/
│   └── handler.py              # Lambda trigger for ALB
├── scripts/
│   └── run_agent.py            # Local testing CLI
├── Dockerfile                  # Fargate container
└── entrypoint.sh               # Container startup
```

## Infrastructure (Terraform)

Located in `infrastructure/modules/engineer-agent-fargate/`:

- EventBridge rule (for clickup-scanner cron)
- Lambda functions (scanner + trigger)
- ALB listener rule for /engineer/analyze
- ECS Fargate task definition
- IAM roles (Lambda execution, ECS task)
- Security groups
- CloudWatch log groups
- Secrets Manager references

## Future Enhancements

1. **simple-task tag** - Trigger coding agent for simple fixes
2. **PR creation** - Auto-create PRs for suggested fixes
3. **Slack notifications** - Alert team when analysis is complete
4. **Multiple bug processing** - Parallel Fargate tasks for multiple bugs
