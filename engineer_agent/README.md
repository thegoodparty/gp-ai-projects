# Engineer Agent

Claude-powered agent that processes ClickUp tasks tagged with `production-bug` or similar tags, analyzes the bug, and posts results back to ClickUp.

## Architecture

```
ClickUp Webhook → Lambda (clickup-bot) → Fargate (engineer-agent) → ClickUp Comment
```

## Docker Build

### Local Build

```bash
./engineer_agent/scripts/build.sh
```

### Push to ECR (Manual)

```bash
AWS_PROFILE=work ./engineer_agent/scripts/push-ecr.sh engineer-agent-dev
```

### CI/CD

Automatically builds and pushes via GitHub Actions when changes are pushed to:
- `engineer_agent/**`
- `shared/**`
- `pyproject.toml` / `uv.lock`

Workflow: `.github/workflows/build-engineer-agent.yml`

**Tags pushed:**
| Branch | Tag |
|--------|-----|
| develop | `engineer-agent-dev` |
| qa | `engineer-agent-qa` |
| prod | `engineer-agent-prod` |
| all | `engineer-agent-latest` |

## Local Development

### Prerequisites

- Docker
- AWS credentials configured
- ClickUp API key
- Anthropic API key
- GitHub token (for repo access)

### Environment Variables

Create `.env` in `engineer_agent/`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
CLICKUP_API_KEY=pk_...
GITHUB_TOKEN=ghp_...
CLICKUP_TASK_ID=<task-id-to-process>
TAG_TYPE=production-bug
ENVIRONMENT=development
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-west-2
```

### Run with Docker Compose

```bash
cd engineer_agent
docker compose up --build
```

### Run Specific Task

```bash
CLICKUP_TASK_ID=86aefd82p docker compose up --build
```

## ECR Repository

```
333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:engineer-agent-{env}
```

## Fargate Deployment

(Future) The Lambda clickup-bot will trigger Fargate tasks with environment variables:
- `CLICKUP_TASK_ID` - Task to process
- `TAG_TYPE` - Type of tag (production-bug, simple-task)
- `ENVIRONMENT` - Runtime environment

## Workflow

1. User adds `production-bug` tag to ClickUp task
2. ClickUp sends webhook to Lambda (clickup-bot)
3. Lambda verifies signature and checks for duplicate processing
4. Lambda triggers Fargate task with task ID
5. Engineer agent:
   - Fetches task details from ClickUp
   - Analyzes bug (logs, code, etc.)
   - Posts `[GP-Bot]` comment with analysis
6. Fargate task completes and shuts down

## Session Tracking

Every agent run posts a completion comment to ClickUp with a `session_id`:

```
[GP-Bot] Completed in 54 turns. Cost: $1.57

`session_id: abc123-def456-789`
```

The session_id can be used to **resume** an agent session via the Claude Agent SDK:

```python
options = ClaudeAgentOptions(
    session_id="abc123-def456-789",  # Resume from previous session
    ...
)
```

### Future: Resume Capability

To implement resume:
1. Add `SESSION_ID` env var to AgentConfig
2. Pass to `ClaudeAgentOptions(session_id=...)`
3. Parse session_id from ClickUp comments or add `gpbot-resume` tag
4. Agent continues with full context from previous run
