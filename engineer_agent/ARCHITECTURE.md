# Engineer Agent - Architecture

## Overview

The Engineer Agent is a Claude Agent SDK powered service that analyzes bugs and implements fixes from ClickUp tasks, triggered by webhook when specific tags are added.

## System Flow

```
ClickUp Task (user adds tag: gpbot-analyze or gpbot-work)
    │
    ▼ webhook POST (taskTagUpdated)
Lambda: clickup-bot-{env}
    │ - Validates webhook signature (HMAC-SHA256)
    │ - Checks tag → looks up in TAG_CONFIG
    │ - Checks for existing [GP-Bot] comment (skip if exists)
    │ - Posts "[GP-Bot] Processing started..." comment
    │
    ▼ ecs:RunTask with environment overrides
Fargate: engineer-agent (private subnet)
    │ - Receives task_id and instruction via environment
    │ - Runs Claude Agent SDK with system prompt
    │ - Agent clones repos, analyzes code, queries logs
    │
    ▼
ClickUp Comment (analysis) OR GitHub PR (implementation)
```

## Tag Configuration

| Tag | Action | Model | Result |
|-----|--------|-------|--------|
| `gpbot-analyze` | Analyze and report | opus | Posts bug analysis as [GP-Bot] comment |
| `gpbot-work` | Implement and create PR | opus | Creates PR and posts link to ClickUp |

## Available Repositories

The agent can clone repos based on context:

| Repo | Description | When to Clone |
|------|-------------|---------------|
| gp-webapp | Next.js frontend | UI bugs, frontend errors |
| gp-api | NestJS backend API | API errors, backend logic bugs |
| gp-ai-projects | AI services (this repo) | AI/ML bugs, campaign planning issues |
| gp-people-api | People/voter data API | Voter data issues, P2V bugs |
| gp-data-platform | Data platform | Data pipeline issues |

## Agent Capabilities

### Built-in Tools (Claude Agent SDK)

- **Bash** - Run commands (git, gh, aws cli, python, node)
- **Read** - Read files from cloned repos
- **Write** - Write/create files
- **Edit** - Edit existing files
- **Glob** - Find files by pattern
- **Grep** - Search file contents

### Helper Scripts

| Script | Description |
|--------|-------------|
| `python -m engineer_agent.scripts.query_db` | Query Databricks (read-only) |
| `python -m engineer_agent.scripts.post_to_clickup` | Post [GP-Bot] comment to ClickUp |

## Environment Variables (Fargate)

Passed via container overrides from Lambda:

| Variable | Description |
|----------|-------------|
| `CLICKUP_TASK_ID` | ClickUp task ID |
| `INSTRUCTION` | Task instruction (analyze or implement) |
| `AGENT_MODEL` | Model to use (opus) |
| `WORKSPACE_DIR` | Working directory (/workspace) |

Injected from Secrets Manager:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `CLICKUP_API_KEY` | ClickUp API key |
| `GITHUB_TOKEN` | For cloning private repos |
| `DATABRICKS_API_KEY` | Databricks access |
| `SLACK_BOT_TOKEN` | Slack notifications |

## Directory Structure

```
engineer_agent/
├── pyproject.toml              # Workspace package config
├── Dockerfile                  # Fargate container
├── docker-compose.yml          # Local development
├── entrypoint.sh               # Container startup
├── ARCHITECTURE.md             # This file
├── FUTURE_DESIGN_NOTES.md      # Future enhancement notes
├── README.md                   # Quick start guide
├── agent/
│   ├── __init__.py
│   ├── main.py                 # Main agent entry point (Claude SDK)
│   └── config.py               # Agent config, capability prompt
└── scripts/
    ├── build.sh                # Docker build script
    ├── run_agent.py            # Local testing CLI
    ├── post_to_clickup.py      # Post comments to ClickUp
    └── query_db.py             # Query Databricks (read-only)
```

## Infrastructure (Terraform)

**clickup-bot** (`infrastructure/modules/clickup-bot/`):
- Lambda function for webhook handling
- Secrets Manager access
- ECS RunTask permissions

**engineer-agent-fargate** (`infrastructure/modules/engineer-agent-fargate/`):
- ECS cluster and Fargate task definition
- IAM roles (task execution, task role)
- Security groups (HTTPS, SSH, DNS egress)
- CloudWatch log groups
- SNS topic for failure notifications → Slack

## Future Enhancements

1. **Session resume** - Use Claude SDK sessions to resume across container restarts
2. **Human-in-the-loop** - Pause for approval before creating PRs
3. **Parallel processing** - Multiple Fargate tasks for batch operations
